"""Advance the withdrawal queue past epochs that have matured.

`handleEpoch()` carries no access control and returns early whenever its
preconditions do not hold, so calling it is always safe and never needs to be
undone. It also runs as a modifier on SourceCore deposits and withdrawals, which
means user activity advances the queue on its own; this exists to keep the queue
moving during quiet periods.
"""

try:
    from .base import *
    from .tx import send_and_confirm
except ImportError:
    from base import *
    from tx import send_and_confirm

from dataclasses import dataclass
from typing import Optional

from eth_account import Account
from web3 import Web3

DEFAULT_MAX_ITERATIONS = 8


@dataclass(frozen=True)
class QueueParams:
    init_timestamp: int
    epoch_duration: int
    withdrawal_delay: int


_params_cache = {}


def read_params(w3, address: str) -> QueueParams:
    """Read the queue's immutable timing parameters, once per chain and address."""
    address = Web3.to_checksum_address(address)
    key = (w3.eth.chain_id, address)
    if key not in _params_cache:
        queue = get_contract(w3, address, "WithdrawalQueue")
        _params_cache[key] = QueueParams(
            init_timestamp=queue.functions.initTimestamp().call(),
            epoch_duration=queue.functions.epochDuration().call(),
            withdrawal_delay=queue.functions.withdrawalDelay().call(),
        )
    return _params_cache[key]


def handle_epochs(
    w3,
    address: str,
    private_key: str,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    receipt_timeout: float = None,
    max_attempts: int = None,
    fee_cap_gwei: int = None,
) -> int:
    """Process every matured epoch, returning how many were advanced.

    Progress is judged by whether `epochIterator` actually moved on chain, not by
    whether the broadcast returned cleanly. Those differ: `handleEpoch()` returns
    without advancing when the SourceCore lacks the liquidity to satisfy the
    epoch, and a loop that retries on anything short of real progress will resend
    the same transaction indefinitely.
    """
    address = Web3.to_checksum_address(address)
    sender = Web3.to_checksum_address(Account.from_key(private_key).address)
    params = read_params(w3, address)
    queue = get_contract(w3, address, "WithdrawalQueue")

    tx_options = {}
    if receipt_timeout is not None:
        tx_options["receipt_timeout"] = receipt_timeout
    if max_attempts is not None:
        tx_options["max_attempts"] = max_attempts
    if fee_cap_gwei is not None:
        tx_options["fee_cap_gwei"] = fee_cap_gwei

    processed = 0
    for _ in range(max_iterations):
        epoch_iterator = queue.functions.epochIterator().call()
        current_epoch = queue.functions.currentEpoch().call()

        if epoch_iterator >= current_epoch:
            print(
                "Withdrawal queue caught up (epochIterator={}, currentEpoch={})".format(
                    epoch_iterator, current_epoch
                )
            )
            break

        block_timestamp = w3.eth.get_block("latest").timestamp
        maturity = (
            params.init_timestamp
            + (epoch_iterator + 1) * params.epoch_duration
            + params.withdrawal_delay
        )
        if maturity > block_timestamp:
            print(
                "Epoch {} not yet mature (maturity={}, block={})".format(
                    epoch_iterator, maturity, block_timestamp
                )
            )
            break

        print("Processing epoch {}...".format(epoch_iterator))
        send_and_confirm(
            queue.functions.handleEpoch(),
            0,
            private_key,
            w3=w3,
            label="handleEpoch {}".format(epoch_iterator),
            **tx_options,
        )

        if queue.functions.epochIterator().call() == epoch_iterator:
            print_colored(
                "handleEpoch() confirmed but epoch {} did not advance; the "
                "SourceCore is short of liquidity. Leaving it for the next "
                "run rather than resending.".format(epoch_iterator),
                "yellow",
            )
            break

        processed += 1
        print_colored("Epoch {} processed".format(epoch_iterator), "green")
    else:
        print_colored(
            "Stopped after {} epochs in one run; more may remain".format(
                max_iterations
            ),
            "yellow",
        )

    return processed
