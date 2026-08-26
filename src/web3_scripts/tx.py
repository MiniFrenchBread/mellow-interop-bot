"""Transaction sending with confirmation driven by on-chain state.

The rule this module exists to enforce: a transaction's success or failure is
decided by whether a receipt appears on chain, never by whether the broadcast
call raised. Those two things diverge constantly in practice -- a broadcast can
raise after the node accepted the transaction, and a broadcast can succeed for a
transaction that is later dropped from the mempool -- and treating the broadcast
result as the answer loses transactions in both directions.

From that follows the second rule: a send does not return until the chain has
decided. There is no attempt limit and no time budget -- only the fee cap
bounds what can be offered. A caller therefore always finds the next nonce
free, so two operations cannot contend for one, and none of the bookkeeping
that used to police abandoned nonces needs to exist.
"""

import time
from dataclasses import dataclass, field
from typing import List, Optional

from eth_account import Account
from web3 import Web3
from web3.exceptions import TransactionIndexingInProgress, TransactionNotFound

DEFAULT_RECEIPT_TIMEOUT = 180
DEFAULT_FEE_BUMP_PERCENT = 115
DEFAULT_FEE_CAP_GWEI = 4
DEFAULT_POLL_LATENCY = 0.5
# How long a single transaction may go unsettled before a person is asked to
# look. Past this the useful question is not which unusual thing happened -- a
# fee cap that cannot be beaten, a node that will not answer, a nonce someone
# else is using -- but whether anyone knows. The send keeps trying either way.
STUCK_AFTER_SECONDS = 1800

GAS_BUFFER_PERCENT = 105
# Headroom over the current base fee in maxFeePerGas. It is a ceiling, not a
# price -- the cost is base fee plus tip either way -- so it should absorb the
# base fee moving while the transaction waits. EIP-1559 lets that rise 12.5% a
# block, so 5% was one block of headroom: enough for an ordinary rise to strand
# a transaction we could no longer replace, because replacing needs a higher
# tip and the tip may already be at its cap.
BASE_FEE_BUFFER_PERCENT = 200
# What to fall back to when the node will not suggest a tip. Offered as-is:
# the opening bid is deliberately not multiplied up, because the cap is a wall
# rather than a speed bump -- see next_fees.
FALLBACK_PRIORITY_FEE_GWEI = 2

# A receipt query failing does not mean the transaction failed. Nodes serve a
# block's header and body before that block's receipts become queryable, so a
# receipt lookup for a just-included transaction comes back empty for a few
# seconds. 0G's reth nodes answer that window either with a null result -- which
# web3 raises as TransactionNotFound -- or with an explicit error naming missing
# receipts. Both mean "not indexed yet"; both must keep the poll alive.
RECEIPT_PENDING_HINTS = (
    "no matching receipts found",
    "transaction indexing is in progress",
)

# The node already holds this exact signed payload, which means an earlier
# broadcast of it succeeded. This is a success, not an error: stop rebroadcasting
# and start waiting for the receipt.
ALREADY_KNOWN_HINTS = (
    "already known",
    "known transaction",
    "alreadyknown",
    "transaction already exists",
)

NONCE_TOO_LOW_HINTS = ("nonce too low",)

# The replacement offered less than the node's required premium over the
# transaction already sitting at this nonce. Bump harder and try again.
UNDERPRICED_HINTS = (
    "replacement transaction underpriced",
    "transaction underpriced",
)

# The call cannot succeed against current state. Retrying wastes time and gas.
REVERT_HINTS = ("execution reverted", "reverted")


class TxReverted(Exception):
    """Simulation showed the call cannot succeed. Not retryable."""


class TxNotConfirmed(Exception):
    """No receipt appeared for any broadcast attempt within the budget.

    The transaction may still land later, so every hash broadcast for this nonce
    is carried on the exception -- without them there is no way to reconcile
    against the chain afterwards.
    """

    def __init__(self, message: str, tx_hashes: List[str], nonce: int):
        super().__init__(message)
        self.tx_hashes = list(tx_hashes)
        self.nonce = nonce


class NonceAlreadyUsed(Exception):
    """The nonce is taken and no receipt for our own attempts turned up.

    Carries the hashes anyway. On a replacement the likeliest occupant is an
    earlier attempt of ours that mined while we were waiting, so reporting this
    without them would describe a mined transaction as a lost one.
    """

    def __init__(self, message: str, tx_hashes: List[str], nonce: int):
        super().__init__(message)
        self.tx_hashes = list(tx_hashes)
        self.nonce = nonce


@dataclass
class TxOutcome:
    receipt: object
    tx_hash: str
    nonce: int
    attempts: int
    tx_hashes: List[str] = field(default_factory=list)


def _log(text: str, color: str = "yellow") -> None:
    # Imported lazily: base imports this module, so a module-level import here
    # would close the loop.
    try:
        from .base import print_colored
    except ImportError:
        from base import print_colored

    print_colored(text, color)


def _to_hex(value) -> str:
    return value if isinstance(value, str) else Web3.to_hex(value)


def _matches(exc: Exception, hints) -> bool:
    text = str(exc).lower()
    return any(hint in text for hint in hints)


def is_receipt_pending(exc: Exception) -> bool:
    if isinstance(exc, (TransactionNotFound, TransactionIndexingInProgress)):
        return True
    return _matches(exc, RECEIPT_PENDING_HINTS)


def is_already_known(exc: Exception) -> bool:
    return _matches(exc, ALREADY_KNOWN_HINTS)


def is_nonce_too_low(exc: Exception) -> bool:
    return _matches(exc, NONCE_TOO_LOW_HINTS)


def is_underpriced(exc: Exception) -> bool:
    return _matches(exc, UNDERPRICED_HINTS)


def is_revert(exc: Exception) -> bool:
    return _matches(exc, REVERT_HINTS)


def disable_send_retry(w3: Web3) -> None:
    """Stop web3 from retrying eth_sendRawTransaction at the transport layer.

    Rebroadcasting an identical signed payload is harmless on chain, but the node
    answers the second attempt with "already known", which web3 raises as an
    error -- hiding the fact that the first attempt worked. Broadcast retries
    belong in send_and_confirm, which knows that answer means success.

    No-ops for the multi-endpoint provider, which does its own failover and
    carries no retry configuration to edit. That is fine, and desirable for a
    broadcast: if one endpoint is unreachable the transaction should still reach
    another. Whichever path a resend takes, the "already known" answer it may come
    back with is handled below rather than treated as a failure.
    """
    provider = getattr(w3, "provider", None)
    config = getattr(provider, "exception_retry_configuration", None)
    allowlist = getattr(config, "method_allowlist", None)
    if not allowlist:
        return
    config.method_allowlist = [
        method for method in allowlist if method != "eth_sendRawTransaction"
    ]


def wait_any_receipt(
    w3: Web3,
    tx_hashes,
    timeout: float,
    poll_latency: float = DEFAULT_POLL_LATENCY,
):
    """Poll every hash broadcast for one nonce; return the first receipt found.

    A fee bump re-signs the same nonce under a new hash, so several hashes can be
    live at once and any one of them may be the one that lands. Returns None if
    the budget runs out.
    """
    hashes = list(tx_hashes)
    if not hashes:
        return None

    deadline = time.monotonic() + timeout
    while True:
        for tx_hash in hashes:
            try:
                receipt = w3.eth.get_transaction_receipt(tx_hash)
            except Exception as e:
                if not is_receipt_pending(e):
                    raise
                receipt = None
            if receipt is not None:
                return receipt

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(poll_latency, remaining))


def compute_fees(
    w3: Web3,
    fee_cap_gwei: int = DEFAULT_FEE_CAP_GWEI,
    ceiling: int = None,
):
    """Return (max_fee_per_gas, max_priority_fee_per_gas) for the next attempt.

    The tip opens at what the node suggests, not a multiple of it. Bidding
    several times the suggestion spends the whole distance to the cap on the
    first attempt -- on the target chain, where the suggestion is routinely a
    good fraction of the cap, it landed the opening bid exactly on it -- and
    reaching the cap is a wall, not a speed bump (see next_fees). The ladder of
    replacements exists to close that distance gradually; opening at the top of
    it just throws the ladder away.
    """
    base_fee = w3.eth.get_block("latest").baseFeePerGas * BASE_FEE_BUFFER_PERCENT // 100
    if ceiling is None:
        ceiling = w3.to_wei(fee_cap_gwei, "gwei")
    try:
        max_priority_fee = min(w3.eth.max_priority_fee, ceiling)
    except Exception:
        max_priority_fee = min(w3.to_wei(FALLBACK_PRIORITY_FEE_GWEI, "gwei"), ceiling)
    return base_fee + max_priority_fee, max_priority_fee


def _bump(value: int, percent: int) -> int:
    # Strictly increasing even for tiny values, so a replacement always clears
    # the node's minimum premium.
    return max(value + 1, value * percent // 100)


def next_fees(
    w3: Web3,
    max_fee: int,
    max_priority_fee: int,
    fee_bump_percent: int,
    fee_cap_gwei: int,
    consider_network: bool = False,
):
    """The fees for a replacement, or None when nothing better is on offer.

    The tip is the competitive part, and it is clamped to the cap rather than
    abandoned on reaching it.

    maxFeePerGas is a ceiling on what will be paid, not a bid, so it is bounded
    by the current base fee plus that cap rather than compounded off the
    previous one. Compounding it independently was unbounded: on a chain
    reporting a zero tip, `_bump` moves the tip a single wei at a time, so it
    took well over a hundred rounds to climb to a 20 gwei cap -- and
    maxFeePerGas grew 15% on every one of them, reaching multiples of the cap
    that no balance could cover. The node then refuses every send for
    insufficient funds, which matches no hint here and is retried for ever.

    Reading the base fee each round is also what lets a send recover: the
    ceiling rises with the network, so a transaction stranded by a base-fee
    rise can be replaced once there is room, instead of waiting on a payload
    that can no longer be mined.
    """
    cap = w3.to_wei(fee_cap_gwei, "gwei")
    base_fee = w3.eth.get_block("latest").baseFeePerGas * BASE_FEE_BUFFER_PERCENT // 100

    bumped_priority = min(_bump(max_priority_fee, fee_bump_percent), cap)
    if consider_network:
        _fresh_max, fresh_priority = compute_fees(w3, fee_cap_gwei)
        bumped_priority = min(max(bumped_priority, fresh_priority), cap)

    ceiling = base_fee + cap
    bumped_max = min(
        max(base_fee + bumped_priority, _bump(max_fee, fee_bump_percent)), ceiling
    )

    if bumped_priority <= max_priority_fee:
        # No valid replacement exists. A node will only accept one if BOTH
        # maxFeePerGas and maxPriorityFeePerGas are raised by its price bump,
        # so once the tip is pinned at the cap, raising maxFeePerGas alone
        # buys nothing -- the payload would be signed, broadcast, and refused
        # as underpriced, over and over.
        #
        # That makes the cap a wall rather than a ceiling to climb to: past it
        # the transaction can only mine, be evicted, or wait for someone to
        # raise the cap. Worth remembering when choosing one.
        return None
    return bumped_max, bumped_priority


def estimate_gas(contract_function, sender: str, value: int) -> int:
    try:
        estimated = contract_function.estimate_gas({"from": sender, "value": value})
    except Exception as e:
        if is_revert(e):
            raise TxReverted("Call reverted during gas estimation: {}".format(e))
        raise Exception("Gas estimation failed: {}".format(e))
    return estimated * GAS_BUFFER_PERCENT // 100


def _sign(
    contract_function,
    w3: Web3,
    private_key: str,
    sender: str,
    nonce: int,
    gas: int,
    value: int,
    max_fee: int,
    max_priority_fee: int,
):
    transaction = contract_function.build_transaction(
        {
            "gas": gas,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": max_priority_fee,
            "value": value,
            "from": sender,
            "nonce": nonce,
        }
    )
    return w3.eth.account.sign_transaction(transaction, private_key=private_key)


def send_and_confirm(
    contract_function,
    value: int,
    private_key: str,
    w3: Optional[Web3] = None,
    nonce: Optional[int] = None,
    gas: Optional[int] = None,
    receipt_timeout: float = DEFAULT_RECEIPT_TIMEOUT,
    fee_bump_percent: int = DEFAULT_FEE_BUMP_PERCENT,
    fee_cap_gwei: int = DEFAULT_FEE_CAP_GWEI,
    poll_latency: float = DEFAULT_POLL_LATENCY,
    label: str = "",
    should_stop=None,
    on_stuck=None,
    stuck_after: float = STUCK_AFTER_SECONDS,
) -> TxOutcome:
    """Broadcast one call and return only once the chain has settled it.

    Two rules, and nothing else:

    1. Keep trying. Every hash broadcast for this nonce stays in the poll set,
       and the wait ends only when the chain answers -- a receipt, mined or
       reverted, or the nonce spent by something else.
    2. Each replacement offers a higher fee than the last, up to a cap.

    Once the cap is reached the same payload is simply re-signed and re-sent
    each round. It hashes to the same transaction, so a node that still holds
    it says "already known" and one that dropped it takes it again -- which is
    all that eviction needs, without a branch of its own.

    Anything that is neither of those two rules is not decided here. A send
    that has not settled within `stuck_after` calls `on_stuck` so a person can
    look, and keeps going. Enumerating the ways a transaction can be unusual --
    and acting on each -- is how this function grew a guard for every scenario
    anyone could imagine; asking for a human is both simpler and more likely to
    be right.

    `should_stop` is the one exit that leaves a transaction in flight. It is
    what keeps SIGTERM working, and it is consulted inside the wait as well as
    around it, so a stop does not take a whole `receipt_timeout` to be noticed.
    """
    if w3 is None:
        w3 = contract_function.w3
    disable_send_retry(w3)

    stopping = should_stop or (lambda: False)
    sender = Web3.to_checksum_address(Account.from_key(private_key).address)
    prefix = "{}: ".format(label) if label else ""

    balance = w3.eth.get_balance(sender)
    if balance < value:
        raise Exception(
            "Operator balance is too low: {}. Required for LayerZero payment: {}".format(
                balance / 1e18, value / 1e18
            )
        )

    if gas is None:
        gas = estimate_gas(contract_function, sender, value)

    max_fee, max_priority_fee = compute_fees(w3, fee_cap_gwei)

    required = gas * max_fee + value
    if balance < required:
        raise Exception(
            "Operator balance is too low: {}. Required for transaction execution: {}".format(
                balance / 1e18, required / 1e18
            )
        )

    if nonce is None:
        # "latest", so a replacement reuses the nonce of the transaction it is
        # replacing rather than queueing behind it. Counting a pending one would
        # mean that if the pending transaction later mined, the replacement
        # mined behind it and the operation happened twice.
        nonce = w3.eth.get_transaction_count(sender, "latest")

    sent_hashes: List[str] = []
    attempt = 0
    started = time.monotonic()
    alerts_sent = 0

    while True:
        if stopping():
            raise TxNotConfirmed(
                "Stopped while waiting for nonce {} after {} attempt(s); "
                "{} transaction(s) may still be in flight".format(
                    nonce, attempt, len(sent_hashes)
                ),
                sent_hashes,
                nonce,
            )

        waiting = time.monotonic() - started
        if (
            on_stuck is not None
            and stuck_after
            and waiting >= stuck_after * (alerts_sent + 1)
        ):
            alerts_sent += 1
            _call(
                on_stuck,
                "{}nonce {} has not settled in {:.0f} minutes over {} attempt(s) "
                "at up to {} gwei".format(
                    prefix, nonce, waiting / 60, attempt, max_priority_fee / 1e9
                ),
            )

        if attempt > 0:
            raised = next_fees(
                w3, max_fee, max_priority_fee, fee_bump_percent, fee_cap_gwei
            )
            if raised is not None:
                max_fee, max_priority_fee = raised
                _log(
                    "{}Replacing nonce {} at {} gwei".format(
                        prefix, nonce, max_priority_fee / 1e9
                    )
                )

        attempt += 1
        try:
            signed = _sign(
                contract_function,
                w3,
                private_key,
                sender,
                nonce,
                gas,
                value,
                max_fee,
                max_priority_fee,
            )
            tx_hash = _to_hex(signed.hash)
            # Recorded before the broadcast: if the call raises after the node
            # accepted the payload, this hash is the only way to find it again.
            if tx_hash not in sent_hashes:
                sent_hashes.append(tx_hash)
            w3.eth.send_raw_transaction(signed.raw_transaction)
            print(
                "{}Transaction sent: {} (nonce {}, attempt {})".format(
                    prefix, tx_hash, nonce, attempt
                )
            )
        except Exception as e:
            if is_nonce_too_low(e):
                # The nonce is spent, so the chain has decided. It may have been
                # spent by one of ours: an unchanged operation re-signs to the
                # same hash, and an earlier broadcast may already have mined.
                receipt = _poll(
                    w3, sent_hashes, receipt_timeout, poll_latency, stopping
                )
                if receipt is not None:
                    return _confirmed(w3, receipt, nonce, attempt, sent_hashes, prefix)
                raise NonceAlreadyUsed(
                    "Nonce {} is already taken and none of this run's {} "
                    "transaction(s) has a receipt: {}".format(
                        nonce, len(sent_hashes), e
                    ),
                    sent_hashes,
                    nonce,
                )
            if is_underpriced(e) or is_revert(e):
                # A refused payload is in no pool and cannot appear, so it comes
                # back out of the poll set. The hash goes in before the
                # broadcast precisely because a call can raise after the node
                # accepted it, which a refusal is not.
                if tx_hash in sent_hashes:
                    sent_hashes.remove(tx_hash)
                if is_revert(e) and not sent_hashes:
                    # Nothing is out there and the call cannot succeed.
                    raise TxReverted(
                        "{}Transaction cannot succeed: {}".format(prefix, e)
                    )
            # Everything else -- already known, an unreachable node, a refused
            # connection -- says nothing that ends the wait.
            _log("{}Attempt {} did not land: {}".format(prefix, attempt, e))

        receipt = _poll(w3, sent_hashes, receipt_timeout, poll_latency, stopping)
        if receipt is not None:
            return _confirmed(w3, receipt, nonce, attempt, sent_hashes, prefix)


def _poll(w3: Web3, sent_hashes, timeout: float, poll_latency: float, stopping=None):
    """Ask the node whether any hash we sent for this nonce has a receipt yet.

    Waits up to `timeout`, then gives up for this round and lets the caller
    replace the transaction.

    It always consumes the time it was given, including when there is nothing
    to ask about and when the node will not answer. This is the only pause in
    the send loop, so returning early does not save time -- it turns the retry
    into a spin that broadcasts nothing and pins a core.

    An RPC error is retried inside the window rather than ending it: the node
    being unreachable says nothing about the transaction.
    """
    stopping = stopping or (lambda: False)
    deadline = time.monotonic() + timeout
    while True:
        if stopping():
            return None
        if sent_hashes:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                try:
                    # One slice at a time so a stop is noticed promptly;
                    # wait_any_receipt has no way to ask.
                    receipt = wait_any_receipt(
                        w3, sent_hashes, min(poll_latency, remaining), poll_latency
                    )
                    if receipt is not None:
                        return receipt
                except Exception as e:
                    _log("Could not check for a receipt: {}".format(e))
        left = deadline - time.monotonic()
        if left <= 0:
            return None
        time.sleep(min(poll_latency, left))


def _call(hook, message: str) -> None:
    """Run a caller's notification hook without letting it end the send."""
    try:
        hook(message)
    except Exception as e:
        _log("Could not report a stuck transaction: {}".format(e))


def _confirmed(w3, receipt, nonce, attempt, sent_hashes, prefix) -> TxOutcome:
    tx_hash = _to_hex(receipt["transactionHash"])
    if receipt["status"] != 1:
        raise TxReverted(
            "Transaction {} reverted on chain in block {}".format(
                tx_hash, receipt["blockNumber"]
            )
        )
    print(
        "{}Transaction mined in block: {}. Chain id: {}".format(
            prefix, receipt["blockNumber"], w3.eth.chain_id
        )
    )
    return TxOutcome(
        receipt=receipt,
        tx_hash=tx_hash,
        nonce=nonce,
        attempts=attempt,
        tx_hashes=sent_hashes,
    )
