import dotenv
import asyncio
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
    OracleValidationResult,
    run_oracle_validation,
    format_remaining_time,
)
from safe_global import PendingTransactionInfo, propose_tx_if_needed
from dataclasses import dataclass, field

from web3_scripts.base import print_colored

# Resolved from this file rather than the working directory: the scheduler calls
# main() in-process, so a scheduler started from anywhere but the repo root would
# otherwise fail this task alone while every other task kept working.
CONFIG_PATH = Path(__file__).parent.parent / "config.json"


@dataclass
class OracleData:
    name: str
    deployment: Deployment
    validation: Optional[OracleValidationResult]


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


async def main():
    """Standalone entry point: report a failure and exit rather than traceback."""
    config = None
    try:
        dotenv.load_dotenv()
        config = read_config(str(CONFIG_PATH))
        await run_oracle_report(config)
    except FileNotFoundError:
        print(f"Error: config.json not found")
    except Exception as e:
        error_message = mask_all_sensitive_config_data(str(e), config)
        print(f"Unexpected error: {error_message}")


async def run_oracle_report(config: Config) -> bool:
    """Validate every oracle, alert, and propose updates.

    Returns whether every announcement it tried to send got through, so a caller
    that a human is watching can say otherwise. Never raises for a delivery
    failure -- see below.

    Kept separate from main() because the scheduler needs to see failures: with
    the catch-all wrapped around this work, a broken oracle report always looked
    like a successful one, and the whole retry-and-alert path was dead for the
    one task whose silence started this.

    Raises on anything that stops the report being produced, and on every oracle
    failing validation, which is what a total RPC outage looks like. A single
    deployment or Safe failing is still caught and reported inline, so one broken
    chain does not suppress the others.

    Telegram is best-effort throughout. Reaching the signers matters, but the
    proposal is what keeps the oracle from expiring, and it must not be
    cancelled because the channel that announces it is down. Nor may a failed
    send fail the task: the scheduler would retry, and a retry after the share
    price has moved proposes different calldata, which lands as a second
    transaction competing for the same Safe nonce instead of being deduplicated.
    """
    await _best_effort(
        "check the Telegram bot and group",
        print_telegram_info(config.telegram_bot_api_key, config.telegram_group_chat_id),
    )

    # Validate and get oracles data
    oracle_validation_results = validate_oracles(config)

    if oracle_validation_results and all(
        oracle_data.validation is None for _, oracle_data in oracle_validation_results
    ):
        # validate_oracles catches per deployment so one broken chain cannot
        # suppress the others, but every one failing is an outage rather than a
        # report. Returning normally here would reset the scheduler's failure
        # counter and keep the alert from ever arming.
        raise Exception(
            "Could not validate any of {} oracle deployment(s)".format(
                len(oracle_validation_results)
            )
        )

    if not needs_attention(oracle_validation_results):
        print("No invalid oracle statuses to report")
        return True

    # Proposed before anything is sent. The proposal is the action that keeps
    # the oracle alive; the messages only tell people about it, and an outage in
    # the telling must not cancel the doing.
    safe_proposals = propose_tx_to_update_oracle(oracle_validation_results)

    message = compose_oracle_data_message(config, oracle_validation_results)
    status_message = (
        await _best_effort(
            "send the oracle status message",
            send_message(
                config.telegram_bot_api_key, config.telegram_group_chat_id, message
            ),
        )
        if message
        else None
    )

    # Compose message with safe data
    attempted = 0
    delivered = 0
    for source, safe_global, safe_proposal in safe_proposals:
        message = compose_safe_proposal_message(
            config.telegram_owner_nicknames,
            source.name,
            safe_global,
            safe_proposal,
        )

        # Send message with safe proposal for each source
        if message:
            # Only add prefix for newly created transactions
            if (
                config.telegram_proposal_message_prefix
                and safe_proposal.is_newly_created
            ):
                message = (
                    config.telegram_proposal_message_prefix.replace("_", "\\_")
                    + "\n"
                    + message
                )

            # Wrapped per proposal: this is the message carrying the Safe link,
            # the missing confirmations and the supersedes notice, so one source
            # failing must not take the others' announcements with it -- and it
            # must not fail the run, or the retry proposes again against a moved
            # share price and competes for the same Safe nonce.
            sent = await _best_effort(
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
            delivered += sent is not None
            attempted += 1

    if attempted and not delivered:
        # Everything else succeeded, so nothing else will report this. Say it
        # loudly here, because the channel that would normally carry the alarm
        # is the one that is down.
        print_colored(
            "Proposed {} Safe transaction(s) but could not announce any of them; "
            "signers have not been told".format(len(safe_proposals)),
            "red",
        )
    else:
        print(f"Sent {delivered} message(s) with safe proposal")
    return delivered == attempted


async def _best_effort(what: str, awaitable):
    """Await something whose failure must not stop the run. Returns None if it did."""
    try:
        return await awaitable
    except Exception as e:
        print_colored("Could not {}: {}".format(what, e), "yellow")
        return None


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


def propose_tx_to_update_oracle(
    oracle_validation_results: List[Tuple[SourceConfig, OracleData]],
) -> List[Tuple[SourceConfig, SafeGlobal, SafeProposal]]:
    result: List[Tuple[SourceConfig, SafeGlobal, SafeProposal]] = []

    # Group validation results by effective Safe identity (chain prefix + address)
    # Key: (eip_3770, safe_address) -> List of oracle data that share the same Safe
    grouped_data: defaultdict[Tuple[str, str], List[OracleData]] = defaultdict(list)
    safe_global_map: Dict[Tuple[str, str], SafeGlobal] = {}
    source_map: Dict[Tuple[str, str], SourceConfig] = {}

    for source, oracle_data in oracle_validation_results:
        # Get the effective safe_global for this deployment (deployment override or chain-level)
        effective_safe = oracle_data.deployment.safe_global
        if effective_safe is None:
            continue

        key = (effective_safe.eip_3770, effective_safe.safe_address)
        grouped_data[key].append(oracle_data)
        safe_global_map[key] = effective_safe
        source_map.setdefault(key, source)

    # Process each Safe group
    for (safe_eip_3770, safe_address), oracle_data_list in grouped_data.items():
        safe_global = safe_global_map[(safe_eip_3770, safe_address)]
        source = source_map[(safe_eip_3770, safe_address)]

        if not safe_global.proposer_private_key:
            print(
                f"Skipping proposal for {source.name} (safe: {safe_address}) because proposer pk is not set"
            )
            continue

        contract_abi = "Oracle"
        method = "setValue"
        deployment_names = []
        calls: list[tuple[str, list[int]]] = []

        # Process each oracle data
        for oracle_data in oracle_data_list:
            # Skip if oracle validation failed
            validation = oracle_data.validation
            if validation is None:
                continue

            # Skip if transfer is in progress
            if validation.transfer_in_progress:
                continue

            # Skip if oracle is not expired or incorrect
            if not validation.almost_expired and not validation.incorrect_value:
                continue

            # Update is required, add oracle data to calls
            to = validation.oracle_address
            args = [validation.actual_value]
            deployment_names.append(oracle_data.name)
            calls.append((to, args))

        if len(calls) == 0:
            print(
                f"No oracle updates required for source {source.name} (safe: {safe_address})"
            )
            continue

        transaction = None
        is_newly_created = False
        superseded = []
        try:
            transaction, is_newly_created, superseded = propose_tx_if_needed(
                contract_abi, method, calls, source, safe_global
            )
        except Exception as e:
            error_message = str(e)
            # Mask all source-related sensitive data (RPC URL, private key, API key)
            masked_error = mask_source_sensitive_data(error_message, source)
            print_colored(
                f"Error proposing tx for source {source.name} (safe: {safe_address}): {masked_error}",
                "red",
            )

        proposal = SafeProposal(
            method=method,
            deployment_names=deployment_names,
            calls=calls,
            transaction=transaction,
            is_newly_created=is_newly_created,
            superseded=superseded,
        )
        result.append((source, safe_global, proposal))

    return result


def validate_oracles(
    config: Config,
) -> List[Tuple[SourceConfig, OracleData]]:
    result: List[Tuple[SourceConfig, OracleData]] = []
    for source in config.sources:
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
    asyncio.run(main())
