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

GAS_BUFFER_PERCENT = 105
BASE_FEE_BUFFER_PERCENT = 105
PRIORITY_FEE_MULTIPLIER = 3
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
    """Return (max_fee_per_gas, max_priority_fee_per_gas) for the next attempt."""
    base_fee = w3.eth.get_block("latest").baseFeePerGas * BASE_FEE_BUFFER_PERCENT // 100
    if ceiling is None:
        ceiling = w3.to_wei(fee_cap_gwei, "gwei")
    try:
        max_priority_fee = min(
            w3.eth.max_priority_fee * PRIORITY_FEE_MULTIPLIER, ceiling
        )
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
    """The fees for a replacement, or None when the cap forbids one.

    A cap bounds replacements as well as opening bids, and once it is reached
    there is nothing further to offer -- signing again would only repeat the
    payload under the same hash. `consider_network` also takes the current
    suggestion into account, for the case where the transaction is merely slow
    rather than refused.
    """
    bumped_priority = _bump(max_priority_fee, fee_bump_percent)
    bumped_max = _bump(max_fee, fee_bump_percent)
    if consider_network:
        fresh_max, fresh_priority = compute_fees(w3, fee_cap_gwei)
        bumped_priority = max(bumped_priority, fresh_priority)
        bumped_max = max(bumped_max, fresh_max)
    if bumped_priority > w3.to_wei(fee_cap_gwei, "gwei"):
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
) -> TxOutcome:
    """Broadcast one call and return only once the transaction is settled.

    Settled means the chain has decided: a receipt (mined, or mined and
    reverted), or the nonce consumed by something else. Short of that this does
    not return. It re-signs the same nonce at a rising fee for as long as it
    takes, and treats an unreachable node as a reason to wait rather than a
    reason to stop.

    That is the whole design, and everything else follows from it. A caller can
    only reach its next transaction once this one is settled, so the account's
    next nonce is always free and two operations can never contend for one --
    which is why there is no bookkeeping here about transactions left in flight.
    An earlier version gave up after a fixed number of attempts, and needed a
    guard band to stop the next operation signing onto the nonce the abandoned
    one still held. Not abandoning it removes the problem rather than policing
    it.

    `receipt_timeout` is how long to wait before raising the fee and trying
    again -- not a budget for the call. There is no attempt limit: the fee cap
    is the only ceiling. Once it is reached nothing higher can be offered -- the
    cap is our own limit, not the network's, so no change in conditions makes a
    better bid possible -- and this stops signing and simply waits for the
    transaction already out there.

    `should_stop` lets a caller interrupt for shutdown. It is the one way out
    that leaves a transaction in flight, and it raises TxNotConfirmed carrying
    every hash broadcast, so the process that comes back can find them.
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

    # Opened at what the network is asking, clamped to the cap. No room has to
    # be held back for a planned number of bumps, because the number is not
    # planned: the fee climbs until it reaches the cap and then stops.
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
    at_cap = False
    # Set when the node itself refused the last payload as underpriced. It has
    # just told us what it thinks; going back to it for a fee suggestion is a
    # round trip that can only repeat the answer.
    refused = False

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

        if at_cap:
            # Nothing higher can be offered, so re-signing would rebroadcast a
            # payload the node already has. Just wait for it.
            receipt = _poll(w3, sent_hashes, receipt_timeout, poll_latency)
            if receipt is not None:
                return _confirmed(w3, receipt, nonce, attempt, sent_hashes, prefix)
            continue

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
            if is_already_known(e):
                _log(
                    "{}Transaction {} already in the node's pool, waiting for it".format(
                        prefix, tx_hash
                    )
                )
            elif is_nonce_too_low(e):
                # The nonce is spent. Either by one of ours -- an unchanged
                # operation re-signs to the same hash, so an earlier broadcast
                # may already have mined -- or by something else entirely.
                # Either way the chain has decided, so this is settled.
                receipt = _poll(w3, sent_hashes, receipt_timeout, poll_latency)
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
            elif is_underpriced(e):
                # The node refused this exact payload, so it is not live and
                # must not be polled for.
                if tx_hash in sent_hashes:
                    sent_hashes.remove(tx_hash)
                refused = True
                _log(
                    "{}Replacement underpriced at attempt {}, raising the fee".format(
                        prefix, attempt
                    )
                )
            elif is_revert(e):
                # The call cannot succeed against current state. Retrying buys
                # nothing and this is as settled as it gets.
                raise TxReverted("{}Transaction cannot succeed: {}".format(prefix, e))
            else:
                # Anything else -- an unreachable node, a refused connection --
                # says nothing about the transaction. Waiting is the only answer
                # that cannot lose it.
                _log(
                    "{}Could not broadcast at attempt {}: {}".format(prefix, attempt, e)
                )

        if not refused:
            # A refused payload is in no pool and cannot appear, so there is
            # nothing to wait for -- and waiting would spend the timeout on it.
            receipt = _poll(w3, sent_hashes, receipt_timeout, poll_latency)
            if receipt is not None:
                return _confirmed(w3, receipt, nonce, attempt, sent_hashes, prefix)

        raised = _next_fees_or_wait(
            w3,
            max_fee,
            max_priority_fee,
            fee_bump_percent,
            fee_cap_gwei,
            consider_network=not refused,
        )
        refused = False
        if raised is None:
            _log(
                "{}Fee cap of {} gwei reached; nothing further can outbid the "
                "incumbent, so waiting for it".format(prefix, fee_cap_gwei)
            )
            at_cap = True
            continue
        max_fee, max_priority_fee = raised
        _log(
            "{}No receipt after {}s, replacing nonce {} at {} gwei".format(
                prefix, receipt_timeout, nonce, max_fee / 1e9
            )
        )


def _poll(w3: Web3, sent_hashes, timeout: float, poll_latency: float):
    """Wait for any of these hashes, treating an unreachable node as "not yet".

    An RPC outage during polling says nothing about the transaction, and this
    never gives up, so there is nothing to be gained by turning it into an
    error.
    """
    if not sent_hashes:
        return None
    try:
        return wait_any_receipt(w3, sent_hashes, timeout, poll_latency)
    except Exception as e:
        _log("Could not check for a receipt: {}".format(e))
        return None


def _next_fees_or_wait(
    w3: Web3,
    max_fee: int,
    max_priority_fee: int,
    bump_percent: int,
    cap_gwei: int,
    consider_network: bool = True,
):
    """The next fee to offer, or None when the cap leaves nothing higher."""
    try:
        return next_fees(
            w3,
            max_fee,
            max_priority_fee,
            bump_percent,
            cap_gwei,
            consider_network=consider_network,
        )
    except Exception as e:
        # Reads the latest block over a connection that may be the reason there
        # was no receipt. Keep the current fee and try again next round.
        _log("Could not work out a replacement fee: {}".format(e))
        return None


def _with_hashes(error: Exception, sent_hashes: List[str], nonce: int) -> Exception:
    """Re-shape an error raised after a broadcast so it carries the hashes.

    Whatever went wrong, the transactions named here may still be on chain, and
    losing track of them is the one outcome this module must never produce.
    """
    if not sent_hashes:
        return error
    wrapped = TxNotConfirmed(
        "Interrupted while confirming nonce {}: {}. Broadcast hashes: {}".format(
            nonce, error, ", ".join(sent_hashes)
        ),
        sent_hashes,
        nonce,
    )
    wrapped.__cause__ = error
    return wrapped


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
