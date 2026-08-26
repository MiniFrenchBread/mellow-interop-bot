import dotenv
import asyncio
import sys
from pathlib import Path
from collections import defaultdict
from typing import List, Optional, Tuple, Dict

from telegram_bot import send_message, print_telegram_info
from config import (
    read_config,
    Config,
    SourceConfig,
    SafeGlobal,
    Deployment,
    mask_source_sensitive_data,
    mask_url_credentials,
    mask_all_sensitive_config_data,
)
from web3_scripts import (
    OracleUpdateResult,
    OracleValidationResult,
    run_oracle_validation,
    format_remaining_time,
    update_oracle,
)
from safe_global import PendingTransactionInfo, ProposalPosted, propose_tx_if_needed
from process_lock import LockHeld, ProcessLock
from dataclasses import dataclass, field

from web3_scripts.base import print_colored

# Resolved from this file rather than the working directory: the scheduler calls
# main() in-process, so a scheduler started from anywhere but the repo root would
# otherwise fail this task alone while every other task kept working.
CONFIG_PATH = Path(__file__).parent.parent / "config.json"

# What a Safe proposal calls. Named once so the planning and the proposing
# cannot describe different calls.
ORACLE_CONTRACT = "Oracle"
SET_VALUE_METHOD = "setValue"


@dataclass
class OracleData:
    name: str
    deployment: Deployment
    validation: Optional[OracleValidationResult]


@dataclass
class OracleRunSummary:
    """What the caller needs after a heartbeat run.

    `notified` and `skip_reasons` are both here because they escalate through
    different channels: a failed announcement is shouted locally and dropped,
    while a run of skips is the scheduler's to notice -- one skip is routine,
    three in a row means the oracle has quietly stopped being written.
    """

    notified: bool = True
    skip_reasons: list = field(default_factory=list)
    written: int = 0


@dataclass
class ProposalPlan:
    """One Safe's worth of calls, decided but not yet sent.

    Exists so that previewing a proposal and making one are the same decision.
    """

    source: SourceConfig
    safe_global: SafeGlobal
    deployment_names: List[str]
    calls: List[Tuple[str, List[int]]]


@dataclass
class SafeProposal:
    method: str  # e.g. "setValue"
    deployment_names: list[str]  # e.g. ["BSC", "FRAXTAL", ...]
    calls: list[
        tuple[str, list[int]]
    ]  # List of tuples(<oracle_address>, <args>), e.g. [("0x123", [1e18]), ...]
    transaction: Optional[PendingTransactionInfo]
    is_newly_created: bool  # True if TX was newly created, False if already existed
    # safeTxHashes of queued proposals sharing this one's Safe nonce. Only one of
    # them can ever execute, so signers need to be told which is the current one.
    superseded: list[str] = field(default_factory=list)
    # Set when the proposal reached the Safe service but could not be read back.
    # It is queued and signable, so this run did put an update in front of the
    # signers -- they have to be told that, and told what to look for, because
    # there is no transaction record to render a link from.
    posted: bool = False
    posted_reference: str = ""


async def main() -> int:
    """Standalone entry point: report a failure and exit rather than traceback.

    Returns a process exit status. Printing the error and exiting zero was
    survivable while this only queued Safe proposals for people to look at; now
    that it broadcasts, a run that wrote nothing at all would be reported to
    systemd, cron or any other supervisor as a success.

    Takes the same lock as the scheduler and the CLI, for the same reason: this
    signs and sends. Without it a stray manual run alongside the scheduler has
    two processes reading the same account's nonce independently, and
    tx._unreconciled is per-process, so nothing stops them replacing each
    other's transactions or writing values computed from different snapshots.
    """
    config = None
    try:
        dotenv.load_dotenv()
        config = read_config(str(CONFIG_PATH))
        with ProcessLock(config.scheduler.lock_file):
            await run_oracle_update(config)
        return 0
    except FileNotFoundError:
        print(f"Error: config.json not found")
    except LockHeld as e:
        print_colored(str(e), "red")
    except Exception as e:
        error_message = mask_all_sensitive_config_data(str(e), config)
        print(f"Unexpected error: {error_message}")
    return 1


async def run_oracle_update(
    config: Config,
    force: bool = False,
    dry_run: bool = False,
    source_name: Optional[str] = None,
) -> OracleRunSummary:
    """Refresh every oracle, and tell someone only when a person is needed.

    Returns whether every announcement it tried to send got through, so a caller
    that a human is watching can say otherwise. Never raises for a delivery
    failure -- see below.

    Kept separate from main() because the scheduler needs to see failures: with
    the catch-all wrapped around this work, a broken oracle run always looked
    like a successful one, and the whole retry-and-alert path was dead for the
    one task whose silence started this.

    Silence on success is deliberate. This runs three times a day, and a
    "refreshed the oracle" message on each would train everyone to ignore the
    channel that the refusals and the failures also arrive on.

    Raises when nothing could be written at all, which is what a total RPC
    outage looks like. A single deployment failing is caught and reported
    inline, so one broken chain does not suppress the others.

    Telegram is best-effort throughout: the write is what keeps the oracle
    alive, and it must not be cancelled or retried because the channel that
    announces it is down.
    """
    if config.telegram_bot_api_key and config.telegram_group_chat_id:
        await _best_effort(
            "check the Telegram bot and group",
            print_telegram_info(
                config.telegram_bot_api_key, config.telegram_group_chat_id
            ),
        )

    results, errors = update_oracles(
        config, force=force, dry_run=dry_run, source_name=source_name
    )

    if not results:
        print("No deployments configured; nothing to update")
        return OracleRunSummary()

    if all(result is None for _, result in results):
        # update_oracles catches per deployment so one broken chain cannot
        # suppress the others, but every one failing is an outage rather than a
        # run. Returning normally here would reset the scheduler's failure
        # counter and keep the alert from ever arming.
        #
        # The original exception is re-raised when there is only one, rather
        # than a summary of it. Its type is load-bearing: the scheduler declines
        # to count NonceBlocked against the task and declines to alert on it,
        # because another operation holding the nonce is neither this task's
        # fault nor an outage -- and a generic wrapper makes that branch
        # unreachable. Its text is load-bearing too: "nonce held", "RPC
        # unreachable" and "Oracle: forbidden" need different responses, and the
        # alert is the only place an operator sees which one happened.
        if len(errors) == 1:
            raise errors[0]
        raise Exception(
            "Could not update any of {} oracle deployment(s): {}".format(
                len(results),
                "; ".join(
                    mask_all_sensitive_config_data(str(error), config)
                    for error in errors
                )
                or "no exception was recorded",
            )
        )

    summary = OracleRunSummary(
        skip_reasons=[
            "{}/{}: {}".format(source.name, result.name, result.skip_reason)
            for source, result in results
            if result is not None and result.skip_reason
        ],
        written=sum(
            1 for _, result in results if result is not None and result.written
        ),
    )

    needs_attention_from = [
        result for _, result in results if result is not None and result.alerts
    ]

    message = compose_oracle_update_message(config, results)
    if not message:
        if needs_attention_from:
            # compose_oracle_update_message renders nothing when Telegram is
            # unconfigured, which would otherwise turn a run where every
            # deployment refused into "refreshed: none written" -- and the
            # scheduler would record it as a success and reset the failure
            # counter. The alerts still have to be said somewhere.
            print_colored(
                "The oracle needs attention and Telegram is not configured:\n"
                + "\n".join(
                    "- {}: {}".format(result.name, result.alert)
                    for result in needs_attention_from
                ),
                "red",
            )
            summary.notified = False
            return summary
        print(
            "Oracle(s) refreshed: {}".format(
                ", ".join(
                    "{}={}".format(result.name, result.new_value)
                    for _, result in results
                    if result is not None and result.written
                )
                or "none written"
            )
        )
        return summary

    ok, _sent = await _best_effort(
        "send the oracle alert",
        send_message(
            config.telegram_bot_api_key, config.telegram_group_chat_id, message
        ),
    )
    if not ok:
        # Everything else already happened, so nothing else will report this.
        # Say it loudly here, because the channel that would normally carry the
        # alarm is the one that is down.
        print_colored(
            "The oracle needs attention and nobody could be told:\n" + message,
            "red",
        )
    summary.notified = ok
    return summary


def update_oracles(
    config: Config,
    force: bool = False,
    dry_run: bool = False,
    source_name: Optional[str] = None,
) -> Tuple[List[Tuple[SourceConfig, Optional[OracleUpdateResult]]], List[Exception]]:
    """Run the heartbeat for every deployment, isolating per-deployment failures.

    A None result means this deployment could not be reached or its send failed.
    It is kept in the list rather than dropped so the caller can tell "every one
    failed" -- an outage -- from "some succeeded".

    The exceptions come back alongside, because their type and their text both
    carry information the caller needs and a fresh generic exception destroys:
    the scheduler treats NonceBlocked differently from an outage, and the alert
    that reaches a phone is the only description of the failure an operator gets
    -- stdout is exactly what they do not have.
    """
    results: List[Tuple[SourceConfig, Optional[OracleUpdateResult]]] = []
    errors: List[Exception] = []
    for source in selected_sources(config, source_name):
        for deployment in source.deployments:
            result: Optional[OracleUpdateResult] = None
            try:
                result = update_oracle(
                    source=source,
                    deployment=deployment,
                    target_rpc=config.target_rpc,
                    target_core_helper=config.target_core_helper,
                    oracle_expiry_threshold_seconds=config.oracle_expiry_threshold_seconds,
                    force=force,
                    dry_run=dry_run,
                )
            except Exception as e:
                errors.append(e)
                masked_error = mask_source_sensitive_data(str(e), source)
                masked_error = mask_url_credentials(masked_error, config.target_rpc)
                print_colored(
                    "Could not update the oracle for {}/{}: {}".format(
                        source.name, deployment.name, masked_error
                    ),
                    "red",
                )
            results.append((source, result))
    return results, errors


def compose_oracle_update_message(
    config: Config,
    results: List[Tuple[SourceConfig, Optional[OracleUpdateResult]]],
) -> str:
    """Render only what needs a person, or "" when nothing does.

    Built from what the run actually did, never from the pre-write validation.
    Before a write the value on chain is nearly always stale by a basis point or
    so -- that is the condition this task exists to correct -- so a message
    driven by the pre-write state would fire every eight hours and say nothing.
    """
    if not config.telegram_bot_api_key or not config.telegram_group_chat_id:
        return ""

    lines = []
    needs_mention = False
    for source, result in results:
        if result is None:
            lines.append(
                "- `{}`: ❌ could not be updated (see the logs)".format(source.name)
            )
            needs_mention = True
            continue
        if result.alerts:
            lines.append(
                "- `{}/{}`: ⚠️ {}".format(source.name, result.name, result.alert)
            )
            if not result.written:
                needs_mention = True
        # A skip is deliberately not reported here. A cross-chain transfer in
        # flight is an ordinary few minutes of the day, and announcing each one
        # would put a message in the group most days for something nobody acts
        # on. Repeated skips do matter, and the scheduler is what notices them.

    if not lines:
        return ""

    message = "Oracle update needs attention:\n" + "\n".join(lines)
    if needs_mention:
        mentions = format_mentions(list(config.telegram_owner_nicknames))
        if mentions:
            message += "\n\ncc {}".format(mentions)
        message += (
            "\n\nThe oracle is not being refreshed and will not resume on its own. "
            "Check with `cli.py oracle --dry-run`, then either propose the value "
            "through the Safe (`cli.py oracle-propose`) or fix the source of the "
            "bad reading."
        )
    return message


async def run_oracle_propose(
    config: Config,
    value_override: Optional[int] = None,
    dry_run: bool = False,
    source_name: Optional[str] = None,
) -> bool:
    """Queue an oracle update through the Safe and tell the signers.

    The recovery path, invoked by hand. It signs off chain and posts to the Safe
    service, so it broadcasts nothing and consumes no nonce -- which is why it
    can run while the scheduler is up, and why it stays usable once the heartbeat
    key lives somewhere a person cannot reach.
    """
    oracle_validation_results = validate_oracles(config, source_name=source_name)

    if not oracle_validation_results or all(
        oracle_data.validation is None for _, oracle_data in oracle_validation_results
    ):
        raise Exception(
            "Could not validate any of {} oracle deployment(s); nothing to "
            "propose".format(len(oracle_validation_results))
        )

    if dry_run:
        # The same planning the real run does, stopping short of the one step
        # that has an effect. Anything the real run would skip is skipped here
        # too, and an empty plan fails here exactly as it would there.
        skipped: List[str] = []
        plans = plan_oracle_proposals(
            oracle_validation_results,
            force=True,
            value_override=value_override,
            skipped=skipped,
        )
        if not plans:
            raise Exception(_nothing_proposed("Nothing would be proposed", skipped))
        for plan in plans:
            for name, (oracle_address, args) in zip(plan.deployment_names, plan.calls):
                print(
                    "{}/{}: would propose {}({}) on {} via Safe {}".format(
                        plan.source.name,
                        name,
                        SET_VALUE_METHOD,
                        args[0],
                        oracle_address,
                        plan.safe_global.safe_address,
                    )
                )
        return True

    skipped: List[str] = []
    safe_proposals = propose_tx_to_update_oracle(
        oracle_validation_results,
        force=True,
        value_override=value_override,
        skipped=skipped,
    )

    if not safe_proposals:
        raise Exception(_nothing_proposed("Nothing was proposed", skipped))

    attempted = 0
    delivered = 0
    status_message = None

    # Sent first, and the proposals reply to it. Signers are being asked to
    # approve a number; the oracle's current value, the computed one and how
    # long is left before expiry are what make that number checkable rather
    # than something to rubber-stamp.
    status = compose_oracle_data_message(config, oracle_validation_results)
    if status:
        attempted += 1
        ok, status_message = await _best_effort(
            "send the oracle status message",
            send_message(
                config.telegram_bot_api_key, config.telegram_group_chat_id, status
            ),
        )
        delivered += ok

    for source, safe_global, safe_proposal in safe_proposals:
        message = compose_safe_proposal_message(
            config.telegram_owner_nicknames,
            source.name,
            safe_global,
            safe_proposal,
        )
        if not message:
            continue
        if config.telegram_proposal_message_prefix and safe_proposal.is_newly_created:
            message = (
                config.telegram_proposal_message_prefix.replace("_", "\\_")
                + "\n"
                + message
            )
        attempted += 1
        ok, _sent = await _best_effort(
            "send the proposal message for {}".format(source.name),
            send_message(
                config.telegram_bot_api_key,
                config.telegram_group_chat_id,
                message,
                reply_to_message_id=(
                    status_message.message_id if status_message else None
                ),
            ),
        )
        delivered += ok

    if all(
        proposal.transaction is None and not proposal.posted
        for _, _, proposal in safe_proposals
    ):
        raise Exception(
            "Every one of {} Safe proposal(s) failed before reaching the service; "
            "nothing is queued".format(len(safe_proposals))
        )

    if attempted and not delivered:
        print_colored(
            "The proposal is queued but nobody could be told; send the Safe link "
            "to the signers by hand",
            "red",
        )
    return delivered == attempted


async def _best_effort(what: str, awaitable):
    """Await something whose failure must not stop the run.

    Returns (succeeded, result). Success is reported separately from the result
    because a successful send can legitimately return nothing -- dry-run mode
    does exactly that -- and inferring failure from a missing result turns every
    dry run into a false alarm about the notification channel.
    """
    try:
        return True, await awaitable
    except Exception as e:
        print_colored("Could not {}: {}".format(what, e), "yellow")
        return False, None


def needs_attention(
    oracle_validation_results: List[Tuple[SourceConfig, OracleData]],
) -> bool:
    """Whether this run has anything to act on or report.

    Deliberately independent of the Telegram settings. Whether an oracle needs
    updating is a fact about the chain; whether anyone can be told about it is a
    separate question, and letting the second decide the first is what made the
    Safe proposal -- the part that actually keeps the oracle alive -- conditional
    on a notification succeeding.
    """
    if len(oracle_validation_results) == 0:
        return False

    has_any_problem = any(
        oracle_data.validation is None  # Error during validation on-chain data
        or oracle_data.validation.almost_expired
        or oracle_data.validation.transfer_in_progress
        or oracle_data.validation.incorrect_value
        for _, oracle_data in oracle_validation_results
    )
    has_recent_update = any(
        oracle_data.validation is not None and oracle_data.validation.recently_updated
        for _, oracle_data in oracle_validation_results
    )
    return has_any_problem or has_recent_update


def compose_oracle_data_message(
    config: Config,
    oracle_validation_results: List[Tuple[SourceConfig, OracleData]],
) -> str:
    # Skip if telegram variables are not set
    if not config.telegram_bot_api_key or not config.telegram_group_chat_id:
        return ""

    if not needs_attention(oracle_validation_results):
        return ""

    # Group validation results by source.name
    grouped_data: defaultdict[str, List[OracleData]] = defaultdict(list)
    for source, oracle_data in oracle_validation_results:
        grouped_data[source.name].append(oracle_data)

    message = ""

    # Process each group
    for source_name, oracle_data_list in grouped_data.items():
        message += f"\n{source_name}:\n"
        message += "```solidity\n"
        for oracle_data in oracle_data_list:
            message += f"- {oracle_data.name}: "
            if oracle_data.validation is not None:
                validation = oracle_data.validation
                if validation.transfer_in_progress:
                    message += f"ℹ️ OFT transfers in progress (remaining time: {format_remaining_time(validation.remaining_time)}, address: {validation.oracle_address})"
                elif validation.almost_expired:
                    if validation.remaining_time < 0:
                        message += f"⚠️ Already expired, needs update (overdue: {format_remaining_time(-validation.remaining_time)}, oracle value: {validation.oracle_value}, actual value: {validation.actual_value}, address: {validation.oracle_address})"
                    else:
                        message += f"⚠️ Almost expired, needs update (remaining time: {format_remaining_time(validation.remaining_time)}, oracle value: {validation.oracle_value}, actual value: {validation.actual_value}, address: {validation.oracle_address})"
                elif validation.incorrect_value:
                    message += f"⚠️ Incorrect value, needs update (remaining time: {format_remaining_time(validation.remaining_time)}, oracle value: {validation.oracle_value}, actual value: {validation.actual_value}, address: {validation.oracle_address})"
                else:
                    message += f"✅ Up to date (remaining time: {format_remaining_time(validation.remaining_time)})"
            else:
                message += f"❌ Error during validation (RPC problem)"
            message += "\n"
        message += "```"

    return message


def compose_safe_proposal_message(
    nickname_address_map: Dict[str, str],
    source_name: str,
    safe_global: SafeGlobal,
    proposal: SafeProposal,
) -> str:
    message = f"Approve tx for `{source_name}` to update {len(proposal.deployment_names)} oracle(s):\n"

    if proposal.transaction is None:
        if proposal.posted:
            # Queued and signable, just not readable back yet. Saying it failed
            # would leave the signers ignoring a transaction that is waiting for
            # them, and a later run will not propose it again.
            message += (
                "⚠️ Submitted but not yet visible in the queue"
                + (
                    " (`{}`)".format(proposal.posted_reference)
                    if proposal.posted_reference
                    and proposal.posted_reference != "unknown"
                    else ""
                )
                + ". It is signable — open the Safe and refresh: "
                + "{}/transactions/queue?safe={}:{}".format(
                    safe_global.web_client_url,
                    safe_global.eip_3770,
                    safe_global.safe_address,
                )
            )
        else:
            message += "❌ Error occurred during proposal"
        return message

    message += "```solidity\n"
    for index, call in enumerate(proposal.calls):
        name = proposal.deployment_names[index]
        oracle_address = call[0]
        args = call[1]
        args_str = ", ".join(str(arg) for arg in args)
        message += (
            f"- {name}: {proposal.method}({args_str}), address: {oracle_address}\n"
        )
    message += "```"

    link = compose_safe_tx_link(safe_global, proposal)
    message += f"\nLink: [{link}]({link})\n"

    if proposal.superseded:
        short = ", ".join(f"`{h[:10]}…{h[-6:]}`" for h in proposal.superseded)
        message += (
            f"\n⚠️ Supersedes {short}, which share this Safe nonce. "
            "Only one of them can execute and it voids the others, "
            "so sign this one and leave the earlier proposal(s) unsigned.\n"
        )

    confirmations_message, is_confirmed = compose_safe_tx_confirmations(proposal)
    message += f"\n{confirmations_message}"

    if not is_confirmed:
        mentions = compose_owner_mentions(nickname_address_map, proposal)
        if mentions:
            message += f", cc {mentions}"
    else:
        message += " ✅, ready to be executed"

    return message


def compose_safe_tx_link(
    safe_global_config: SafeGlobal,
    proposal: SafeProposal,
) -> str:
    url = safe_global_config.web_client_url
    eip_3770 = safe_global_config.eip_3770
    safe_address = safe_global_config.safe_address
    return f"{url}/transactions/tx?safe={eip_3770}:{safe_address}&id={proposal.transaction.id}"


def compose_owner_mentions(
    nickname_address_map: Dict[str, str],
    proposal: SafeProposal,
) -> str:
    owners = []
    for nickname, address in nickname_address_map.items():
        if address in proposal.transaction.missing_confirmations:
            owners.append(nickname)
    return format_mentions(owners)


def format_mentions(mentions: List[str]) -> str:
    return ", ".join("@" + mention.replace("_", "\\_") for mention in mentions)


def compose_safe_tx_confirmations(proposal: SafeProposal) -> tuple[str, bool]:
    confirmations = len(proposal.transaction.confirmations)
    required_confirmations = proposal.transaction.number_of_required_confirmations
    return (
        f"Confirmations: {confirmations} / {required_confirmations}",
        confirmations >= required_confirmations,
    )


def _note_skip(collected: Optional[List[str]], reason: str) -> None:
    print("Skipping {}".format(reason))
    if collected is not None:
        collected.append(reason)


def plan_oracle_proposals(
    oracle_validation_results: List[Tuple[SourceConfig, OracleData]],
    force: bool = False,
    value_override: Optional[int] = None,
    skipped: Optional[List[str]] = None,
) -> List[ProposalPlan]:
    """Work out what would be proposed, without proposing anything.

    `skipped` collects why each deployment was left out, so a caller that ends
    up with nothing can say which reason applied. The likeliest one on the
    recovery path is a transfer in flight, whose remedy is `--value` -- not the
    Safe configuration the fixed message used to blame.

    Split out from the proposing so a dry run and a real run cannot disagree
    about what is proposable. They did: the dry run printed a line for every
    deployment it had validated -- including ones with no Safe configured, no
    proposer key, or a transfer in flight -- and exited zero, while the same
    command without --dry-run skipped all of them and failed. A preview that
    only tells the truth when nothing is wrong is worse than none.

    Returns one plan per Safe, each with at least one call. A Safe with nothing
    to propose is left out entirely rather than returned empty.
    """
    # Group by effective Safe identity (chain prefix + address), so several
    # deployments sharing one Safe become a single multi-send.
    grouped_data: defaultdict[Tuple[str, str], List[OracleData]] = defaultdict(list)
    safe_global_map: Dict[Tuple[str, str], SafeGlobal] = {}
    source_map: Dict[Tuple[str, str], SourceConfig] = {}

    for source, oracle_data in oracle_validation_results:
        # The deployment override if there is one, else the chain-level config.
        effective_safe = oracle_data.deployment.safe_global
        if effective_safe is None:
            _note_skip(
                skipped,
                "{}/{}: no Safe is configured for it".format(
                    source.name, oracle_data.name
                ),
            )
            continue

        key = (effective_safe.eip_3770, effective_safe.safe_address)
        grouped_data[key].append(oracle_data)
        safe_global_map[key] = effective_safe
        source_map.setdefault(key, source)

    plans: List[ProposalPlan] = []
    for (safe_eip_3770, safe_address), oracle_data_list in grouped_data.items():
        safe_global = safe_global_map[(safe_eip_3770, safe_address)]
        source = source_map[(safe_eip_3770, safe_address)]

        if not safe_global.proposer_private_key:
            _note_skip(
                skipped,
                "{} (safe {}): no proposer key is configured".format(
                    source.name, safe_address
                ),
            )
            continue

        deployment_names: List[str] = []
        calls: List[Tuple[str, List[int]]] = []

        for oracle_data in oracle_data_list:
            validation = oracle_data.validation
            if validation is None:
                continue

            # Skip if transfer is in progress. An explicit value is exempt: it
            # was not derived from the two sides that a transfer in flight puts
            # out of step, so the reason to wait does not apply to it.
            if validation.transfer_in_progress and value_override is None:
                _note_skip(
                    skipped,
                    "{}/{}: an OFT transfer is in flight (pass --value to "
                    "propose a hand-computed figure anyway)".format(
                        source.name, oracle_data.name
                    ),
                )
                continue

            # Skip if oracle is not expired or incorrect
            if (
                not force
                and not validation.almost_expired
                and not validation.incorrect_value
            ):
                continue

            deployment_names.append(oracle_data.name)
            calls.append(
                (
                    validation.oracle_address,
                    [
                        (
                            value_override
                            if value_override is not None
                            else validation.actual_value
                        )
                    ],
                )
            )

        if not calls:
            _note_skip(
                skipped,
                "{} (safe {}): nothing to update".format(source.name, safe_address),
            )
            continue

        plans.append(
            ProposalPlan(
                source=source,
                safe_global=safe_global,
                deployment_names=deployment_names,
                calls=calls,
            )
        )

    return plans


def _nothing_proposed(prefix: str, skipped: List[str]) -> str:
    """Say which reason applied, rather than guessing at the commonest one."""
    if not skipped:
        return "{}; there were no deployments to consider".format(prefix)
    return "{}: {}".format(prefix, "; ".join(skipped))


def propose_tx_to_update_oracle(
    oracle_validation_results: List[Tuple[SourceConfig, OracleData]],
    force: bool = False,
    value_override: Optional[int] = None,
    skipped: Optional[List[str]] = None,
) -> List[Tuple[SourceConfig, SafeGlobal, SafeProposal]]:
    """Queue a Safe transaction setting the oracle, for the operator to sign.

    No longer on any schedule. The heartbeat writes the oracle directly, and
    this is what a person reaches for when a guard refused that write: it moves
    the decision to a different authority -- a quorum of signers rather than the
    key the bot holds -- which is the right shape for a value the bot has just
    declined to write unreviewed.

    `force` skips the freshness heuristics, because being asked to run this at
    all is the reason to act; the automatic caller that needed them is gone.
    `value_override` supplies a number worked out by hand, for the case where
    the guard fired precisely because the computed one cannot be trusted.
    """
    result: List[Tuple[SourceConfig, SafeGlobal, SafeProposal]] = []

    for plan in plan_oracle_proposals(
        oracle_validation_results,
        force=force,
        value_override=value_override,
        skipped=skipped,
    ):
        source = plan.source
        safe_global = plan.safe_global
        safe_address = safe_global.safe_address

        transaction = None
        is_newly_created = False
        superseded = []
        posted = False
        posted_reference = ""
        try:
            transaction, is_newly_created, superseded = propose_tx_if_needed(
                ORACLE_CONTRACT, SET_VALUE_METHOD, plan.calls, source, safe_global
            )
        except ProposalPosted as e:
            # Queued and signable; only the read-back failed. Retrying would add
            # a second entry competing for the same nonce.
            posted = True
            posted_reference = e.safe_tx_hash
            print_colored(
                f"Proposal for source {source.name} (safe: {safe_address}): {e}",
                "yellow",
            )
        except Exception as e:
            error_message = str(e)
            # Mask all source-related sensitive data (RPC URL, private key, API key)
            masked_error = mask_source_sensitive_data(error_message, source)
            print_colored(
                f"Error proposing tx for source {source.name} (safe: {safe_address}): {masked_error}",
                "red",
            )

        result.append(
            (
                source,
                safe_global,
                SafeProposal(
                    method=SET_VALUE_METHOD,
                    deployment_names=plan.deployment_names,
                    calls=plan.calls,
                    transaction=transaction,
                    is_newly_created=is_newly_created,
                    superseded=superseded,
                    posted=posted,
                    posted_reference=posted_reference,
                ),
            )
        )

    return result


def selected_sources(config: Config, source_name: Optional[str]) -> List[SourceConfig]:
    """The sources a command should act on.

    `--source` is accepted by every subcommand through the shared parent parser,
    so a command that iterates every source regardless silently widens what the
    operator asked for. That is tolerable for a read; it is not for `--force`,
    which writes past both guards, or for `--value`, which would put one
    hand-computed figure into every deployment's setValue.
    """
    if not source_name:
        return list(config.sources)
    chosen = [s for s in config.sources if s.name.lower() == source_name.lower()]
    if not chosen:
        raise Exception(
            "Unknown source '{}'. Available: {}".format(
                source_name, ", ".join(s.name for s in config.sources)
            )
        )
    return chosen


def validate_oracles(
    config: Config, source_name: Optional[str] = None
) -> List[Tuple[SourceConfig, OracleData]]:
    result: List[Tuple[SourceConfig, OracleData]] = []
    for source in selected_sources(config, source_name):
        for deployment in source.deployments:
            validation_result: Optional[OracleValidationResult] = None
            try:
                oracle_validation_result = run_oracle_validation(
                    source_core_address=deployment.source_core,
                    target_core_address=deployment.target_core,
                    source_rpc=source.rpc,
                    target_rpc=config.target_rpc,
                    source_core_helper=source.source_core_helper,
                    target_core_helper=config.target_core_helper,
                    oracle_expiry_threshold_seconds=config.oracle_expiry_threshold_seconds,
                    oracle_recent_update_threshold_seconds=config.oracle_recent_update_threshold_seconds,
                )
                validation_result = oracle_validation_result
            except Exception as e:
                error_message = str(e)
                # Mask source RPC and target RPC URLs that might be in the error
                masked_error = mask_source_sensitive_data(error_message, source)
                masked_error = mask_url_credentials(masked_error, config.target_rpc)
                print(
                    f"Error validating oracle for source {source.name}: {masked_error}"
                )
            oracle_data = OracleData(
                name=deployment.name,
                deployment=deployment,
                validation=validation_result,
            )
            result.append((source, oracle_data))
    return result


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
