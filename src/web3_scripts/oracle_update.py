"""Write the vault's share price to the oracle on a heartbeat.

Replaces proposing a Safe transaction and waiting for signers. Two things about
that old path are worth stating, because they are what this exists to fix: it
only acted when the value had already drifted or was about to expire, so the
oracle sat near its `maxAge` for weeks at a time; and every refresh needed a
quorum of humans, so the share price moved in fortnightly steps regardless of
how often anything was checked.

The write is unconditional. Rewriting an unchanged value is not waste -- it is
the point: `getValue()` reverts once `lastUpdated + maxAge` passes, and a revert
there stops deposits, withdrawals and rebalancing. A heartbeat that only fires
when the number changes is not a heartbeat.

What replaces the signers' judgement is two guards. Nothing reviews these writes
any more, so a helper that returns zero, reads half the position, or gets the
decimals wrong would otherwise be committed to the contract that prices every
deposit and withdrawal. The guards refuse instead of writing, and refusing is
survivable: `maxAge` is weeks, which is ample time for a person to look. The
recovery path is deliberately a different authority -- see `cli.py
oracle-propose`.
"""

try:
    from .base import *
    from .oracle_script import format_remaining_time, run_oracle_validation
    from .tx import send_and_confirm
except ImportError:
    from base import *
    from oracle_script import format_remaining_time, run_oracle_validation
    from tx import send_and_confirm

from dataclasses import dataclass, field

from eth_account import Account

BPS_DENOMINATOR = 10000

# Names the operation in logs and in the fee-bump messages. It carries no
# value, so a replacement of a slow send reads as the same operation rather
# than a new one -- useful when reading a log, and nothing more than that: a
# send now runs until the chain settles it, so no two operations can be
# in flight on one nonce for a label to have to tell apart.
SET_VALUE_LABEL = "oracle setValue"


@dataclass
class OracleUpdateResult:
    """What one deployment's heartbeat did, and whether anyone needs to know.

    `skip_reason` and `alert` are separate because they escalate differently. A
    skip is routine and only matters if it repeats; an alert means the oracle is
    not being written and will not start again on its own.
    """

    name: str
    oracle_address: str = ""
    old_value: int = 0
    new_value: int = 0
    remaining_time: int = 0
    written: bool = False
    forced: bool = False
    tx_hash: str = ""
    skip_reason: str = ""
    alerts: list = field(default_factory=list)

    @property
    def alert(self) -> str:
        return "; ".join(self.alerts)

    @property
    def deviation_bps(self) -> float:
        return deviation_bps(self.old_value, self.new_value)

    @property
    def signed_deviation_bps(self) -> float:
        return signed_deviation_bps(self.old_value, self.new_value)

    @property
    def forced_note(self) -> str:
        """Marks a write that went past the guards, wherever it is reported.

        A forced write is the one an operator most needs to be able to find
        afterwards, and it was previously indistinguishable from a routine one
        in every log line and message.
        """
        return " [forced past the guards]" if self.forced else ""


def signed_deviation_bps(old_value: int, new_value: int) -> float:
    """Movement in basis points, keeping its direction.

    Separate from deviation_bps because the guards want a magnitude and the
    messages want a direction. Rendering the magnitude with a sign format made
    every forced write read as a rise -- including the decrease that had to be
    forced past the guard in the first place, on the one line an operator reads
    back after overriding it.
    """
    if old_value == 0:
        return float("inf")
    return (new_value - old_value) * BPS_DENOMINATOR / old_value


def deviation_bps(old_value: int, new_value: int) -> float:
    """How far the new value sits from the old one, in basis points.

    An old value of zero has no scale to measure against, so it reports as
    infinite rather than dividing by zero. That routes an uninitialised oracle
    into the refusing branch, which is the right direction: the first value
    written to a live oracle is exactly the one worth a human glance.
    """
    if old_value == 0:
        return float("inf")
    return abs(new_value - old_value) * BPS_DENOMINATOR / old_value


def exceeds_deviation(old_value: int, new_value: int, max_bps: int) -> bool:
    # Integer comparison rather than the float above, so the boundary is exact
    # and a value sitting precisely on the threshold is allowed through.
    if old_value == 0:
        return True
    return abs(new_value - old_value) * BPS_DENOMINATOR > old_value * max_bps


def is_decrease(old_value: int, new_value: int, tolerance_wei: int) -> bool:
    """Whether the value fell by more than rounding can account for.

    The tolerance matters. A share price computed from three separate reads
    wobbles in the last few digits, and treating a one-wei dip as a loss would
    freeze the oracle on noise. The tolerance is orders of magnitude below a
    normal increase, so it cannot hide a real fall either.
    """
    return old_value - new_value > tolerance_wei


def update_oracle(
    source,
    deployment,
    target_rpc: str,
    target_core_helper: str,
    oracle_expiry_threshold_seconds: int,
    force: bool = False,
    dry_run: bool = False,
    tx: dict = None,
) -> OracleUpdateResult:
    """Refresh one deployment's oracle, or say why it did not.

    Raises only when the run could not be attempted at all -- no configuration,
    or the send failed. A guard refusing is a returned result, not an exception:
    the scheduler must keep the other deployments and the other tasks going, and
    a refusal is not something a retry can fix.
    """
    config = getattr(source, "oracle_update", None)
    if config is None or not config.updater_private_key:
        # Raised rather than skipped. With no key this task would report success
        # forever while never writing anything, and an oracle that is quietly
        # never written looks identical to one that never needs writing.
        raise Exception(
            "Source {} has no oracle-update key; set ORACLE_UPDATER_PK".format(
                source.name
            )
        )

    validation = run_oracle_validation(
        source_core_address=deployment.source_core,
        target_core_address=deployment.target_core,
        source_rpc=source.rpc,
        target_rpc=target_rpc,
        source_core_helper=source.source_core_helper,
        target_core_helper=target_core_helper,
        oracle_expiry_threshold_seconds=oracle_expiry_threshold_seconds,
        oracle_recent_update_threshold_seconds=0,
    )

    old_value = validation.oracle_value
    new_value = validation.actual_value
    result = OracleUpdateResult(
        name=deployment.name,
        oracle_address=validation.oracle_address,
        old_value=old_value,
        new_value=new_value,
        remaining_time=validation.remaining_time,
        forced=force,
    )

    if validation.transfer_in_progress:
        # The two sides are counted at different points of a cross-chain
        # transfer, so their sum is wrong in one direction or the other while a
        # message is in flight. Writing that sum would put a knowingly wrong
        # price on chain; waiting costs one tick and the transfer settles in
        # minutes. Not overridable by `force` for that reason -- there is no
        # correct value to force.
        result.skip_reason = "OFT transfer in flight"
        print_colored(
            "{}: OFT transfer in flight, leaving the oracle alone".format(
                deployment.name
            ),
            "yellow",
        )
        return result

    if not force:
        if exceeds_deviation(old_value, new_value, config.max_deviation_bps):
            result.alerts.append(
                "refused: {} would move the value by {:.2f} bps (limit {} bps), "
                "{} -> {}".format(
                    deployment.name,
                    result.deviation_bps,
                    config.max_deviation_bps,
                    old_value,
                    new_value,
                )
            )
        elif is_decrease(old_value, new_value, config.decrease_tolerance_wei):
            result.alerts.append(
                "refused: {} would lower the value by {} wei, {} -> {}".format(
                    deployment.name, old_value - new_value, old_value, new_value
                )
            )

        if result.alerts:
            # Said here as well as in Telegram: this run left the oracle
            # unwritten, and nothing will change that without a person.
            print_colored(result.alert, "red")
            _note_expiry(result, oracle_expiry_threshold_seconds)
            return result

    tx_options = tx or source.tx.as_kwargs()
    source_w3 = get_w3(source.rpc)
    oracle = get_contract(source_w3, validation.oracle_address, "Oracle")
    sender = Account.from_key(config.updater_private_key).address
    balance = source_w3.eth.get_balance(sender)
    if balance < config.min_balance_wei:
        # Reported, not fatal. The send below may well succeed; the point is to
        # be told while there is still time to top the account up, rather than
        # discovering it from a heartbeat that has already stopped.
        result.alerts.append(
            "oracle updater {} is low on gas: {} wei left (warn below {})".format(
                sender, balance, config.min_balance_wei
            )
        )
        print_colored(result.alert, "yellow")

    if dry_run:
        print(
            "{}: would call setValue({}) on {} (was {}, {:+.2f} bps){}".format(
                deployment.name,
                new_value,
                validation.oracle_address,
                old_value,
                result.signed_deviation_bps if old_value else 0.0,
                result.forced_note,
            )
        )
        return result

    outcome = send_and_confirm(
        oracle.functions.setValue(new_value),
        0,
        config.updater_private_key,
        w3=source_w3,
        label=SET_VALUE_LABEL,
        **tx_options,
    )
    result.written = True
    result.tx_hash = outcome.tx_hash
    print_colored(
        "{}: setValue({}) confirmed in {} (was {}, {:+.2f} bps){}".format(
            deployment.name,
            new_value,
            outcome.tx_hash,
            old_value,
            result.signed_deviation_bps if old_value else 0.0,
            result.forced_note,
        ),
        "green",
    )
    return result


def _note_expiry(result: OracleUpdateResult, threshold_seconds: int) -> None:
    """Add how long the refusal can be left alone before it stops the vault.

    A refusal is only mildly urgent on its own and becomes an outage at expiry,
    so the deadline belongs in the same message. Without it the third identical
    alert reads exactly like the first.
    """
    if result.remaining_time <= 0:
        result.alerts.append(
            "the oracle is ALREADY EXPIRED ({} overdue); deposits, withdrawals "
            "and rebalancing are blocked".format(
                format_remaining_time(-result.remaining_time)
            )
        )
    elif result.remaining_time <= threshold_seconds:
        result.alerts.append(
            "the oracle expires in {} and this refusal is what is stopping it "
            "being refreshed".format(format_remaining_time(result.remaining_time))
        )
    else:
        result.alerts.append(
            "the oracle expires in {}".format(
                format_remaining_time(result.remaining_time)
            )
        )
