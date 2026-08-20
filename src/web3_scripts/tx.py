"""Transaction sending with confirmation driven by on-chain state.

The rule this module exists to enforce: a transaction's success or failure is
decided by whether a receipt appears on chain, never by whether the broadcast
call raised. Those two things diverge constantly in practice -- a broadcast can
raise after the node accepted the transaction, and a broadcast can succeed for a
transaction that is later dropped from the mempool -- and treating the broadcast
result as the answer loses transactions in both directions.
"""

import time
from dataclasses import dataclass, field
from typing import List, Optional

from eth_account import Account
from web3 import Web3
from web3.exceptions import TransactionIndexingInProgress, TransactionNotFound

DEFAULT_RECEIPT_TIMEOUT = 180
DEFAULT_MAX_ATTEMPTS = 3
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
    """The nonce was consumed by a transaction that is not one of ours."""

    def __init__(self, message: str, nonce: int):
        super().__init__(message)
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


def compute_fees(w3: Web3, fee_cap_gwei: int = DEFAULT_FEE_CAP_GWEI):
    """Return (max_fee_per_gas, max_priority_fee_per_gas) for the next attempt."""
    base_fee = w3.eth.get_block("latest").baseFeePerGas * BASE_FEE_BUFFER_PERCENT // 100
    try:
        max_priority_fee = min(
            w3.eth.max_priority_fee * PRIORITY_FEE_MULTIPLIER,
            w3.to_wei(fee_cap_gwei, "gwei"),
        )
    except Exception:
        max_priority_fee = w3.to_wei(FALLBACK_PRIORITY_FEE_GWEI, "gwei")
    return base_fee + max_priority_fee, max_priority_fee


def _bump(value: int, percent: int) -> int:
    # Strictly increasing even for tiny values, so a replacement always clears
    # the node's minimum premium.
    return max(value + 1, value * percent // 100)


def estimate_gas(contract_function, sender: str, value: int) -> int:
    try:
        estimated = contract_function.estimate_gas({"from": sender, "value": value})
    except Exception as e:
        if is_revert(e):
            raise TxReverted("Call reverted during gas estimation: {}".format(e))
        raise Exception("Gas estimation failed: {}".format(e))
    return estimated * GAS_BUFFER_PERCENT // 100


def send_and_confirm(
    contract_function,
    value: int,
    private_key: str,
    w3: Optional[Web3] = None,
    nonce: Optional[int] = None,
    gas: Optional[int] = None,
    receipt_timeout: float = DEFAULT_RECEIPT_TIMEOUT,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    fee_bump_percent: int = DEFAULT_FEE_BUMP_PERCENT,
    fee_cap_gwei: int = DEFAULT_FEE_CAP_GWEI,
    poll_latency: float = DEFAULT_POLL_LATENCY,
    label: str = "",
) -> TxOutcome:
    """Broadcast one call and return only once a receipt exists on chain.

    Each attempt re-signs the same nonce with a higher fee, so a transaction that
    is merely slow gets replaced rather than abandoned. Every hash stays in the
    poll set because any of them may be the one that lands.
    """
    if w3 is None:
        w3 = contract_function.w3
    disable_send_retry(w3)

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
        # "pending" so a transaction still sitting in the mempool is counted;
        # "latest" would hand back a nonce that is already spoken for.
        nonce = w3.eth.get_transaction_count(sender, "pending")

    sent_hashes: List[str] = []

    for attempt in range(1, max_attempts + 1):
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
        signed = w3.eth.account.sign_transaction(transaction, private_key=private_key)
        tx_hash = _to_hex(signed.hash)

        # Recorded before the broadcast: if the call raises after the node
        # accepted the payload, this hash is the only way to find it again.
        if tx_hash not in sent_hashes:
            sent_hashes.append(tx_hash)

        try:
            w3.eth.send_raw_transaction(signed.raw_transaction)
            print(
                "{}Transaction sent: {} (nonce {}, attempt {}/{})".format(
                    prefix, tx_hash, nonce, attempt, max_attempts
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
                receipt = wait_any_receipt(w3, sent_hashes, 0, poll_latency)
                if receipt is not None:
                    return _confirmed(w3, receipt, nonce, attempt, sent_hashes, prefix)
                raise NonceAlreadyUsed(
                    "Nonce {} was consumed by another transaction: {}".format(nonce, e),
                    nonce,
                )
            elif is_underpriced(e):
                _log(
                    "{}Replacement underpriced at attempt {}, raising the fee".format(
                        prefix, attempt
                    )
                )
                max_fee = _bump(max_fee, fee_bump_percent)
                max_priority_fee = _bump(max_priority_fee, fee_bump_percent)
                continue
            else:
                raise

        receipt = wait_any_receipt(w3, sent_hashes, receipt_timeout, poll_latency)
        if receipt is not None:
            return _confirmed(w3, receipt, nonce, attempt, sent_hashes, prefix)

        if attempt < max_attempts:
            fresh_max_fee, fresh_priority = compute_fees(w3, fee_cap_gwei)
            max_fee = max(_bump(max_fee, fee_bump_percent), fresh_max_fee)
            max_priority_fee = max(
                _bump(max_priority_fee, fee_bump_percent), fresh_priority
            )
            _log(
                "{}No receipt after {}s, replacing nonce {} at {} gwei".format(
                    prefix, receipt_timeout, nonce, max_fee / 1e9
                )
            )

    raise TxNotConfirmed(
        "No receipt for nonce {} after {} attempt(s). Broadcast hashes: {}".format(
            nonce, max_attempts, ", ".join(sent_hashes)
        ),
        sent_hashes,
        nonce,
    )


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
