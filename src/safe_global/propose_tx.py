import time
from web3 import Web3, constants
from safe_eth.safe import SafeTx
from safe_eth.eth import EthereumClient

from . import client_gateway_api, transaction_api
from .multi_send_call import encode_multi, resolve_multi_send_contract
from .common import PendingTransactionInfo

from web3_scripts import get_contract, print_colored, get_w3
from config import SourceConfig, SafeGlobal


class ProposalPosted(Exception):
    """The proposal reached the Safe service but could not be confirmed.

    Distinct from a plain failure because the transaction is queued and
    signable. Treating this as "nothing happened" and proposing again produces a
    second entry competing for the same Safe nonce, since the share price will
    have moved and the calldata no longer matches.
    """

    def __init__(self, safe_tx_hash: str, reason: str):
        super().__init__(
            "Proposal {} was posted but not confirmed: {}".format(safe_tx_hash, reason)
        )
        self.safe_tx_hash = safe_tx_hash


def _create_calldata(contract_name: str, method: str, args: list) -> str:
    contract = get_contract(Web3(), address=constants.ADDRESS_ZERO, name=contract_name)
    calldata = contract.encode_abi(method, args)
    return calldata


def _create_signed_safe_tx(
    safe_address: str,
    private_key: str,
    to: str,
    calldata: str,
    operation: int,
    chain_id: int,
    safe_version: str,
    safe_nonce: int,
) -> SafeTx:
    # chain_id, safe_version and safe_nonce are supplied explicitly so signing is
    # fully offline: SafeTx never has to reach a node (which would bypass the
    # fallback RPC handling in get_w3 and break on comma-separated RPC strings).
    safe_tx = SafeTx(
        ethereum_client=EthereumClient(),
        safe_address=safe_address,
        to=to,
        value=0,
        data=calldata,
        operation=operation,
        safe_tx_gas=0,
        base_gas=0,
        gas_price=0,
        gas_token=None,
        refund_receiver=None,
        chain_id=chain_id,
        safe_nonce=safe_nonce,
        safe_version=safe_version,
    )
    safe_tx.sign(private_key)
    return safe_tx


def _create_signed_safe_tx_for_safe(
    rpc: str, safe_global: SafeGlobal, to: str, calldata: str, operation: int
) -> SafeTx:
    w3 = get_w3(rpc)
    safe_contract = get_contract(w3, address=safe_global.safe_address, name="Safe")
    chain_id = w3.eth.chain_id
    safe_version = safe_contract.functions.VERSION().call()
    safe_nonce = safe_contract.functions.nonce().call()
    safe_tx = _create_signed_safe_tx(
        safe_global.safe_address,
        safe_global.proposer_private_key,
        to,
        calldata,
        operation,
        chain_id,
        safe_version,
        safe_nonce,
    )
    return safe_tx


def _is_transaction_api(safe: SafeGlobal) -> bool:
    try:
        transaction_api.get_version(safe.api_url, safe.api_key)
        return True
    except Exception:
        try:
            client_gateway_api.get_version(safe.api_url)
        except Exception:
            raise Exception(
                "Unable to resolve API type, please check the API URL and API key"
            )
        return False


def _propose_tx_for_safe(safe_tx: SafeTx, safe_global: SafeGlobal):
    if _is_transaction_api(safe_global):
        print_colored(
            f"Proposing transaction using Transaction API: {safe_tx}", "yellow"
        )
        return transaction_api.propose_safe_tx(
            safe_global.api_url, safe_global.api_key, safe_tx
        )
    else:
        print_colored(
            f"Proposing transaction using Client Gateway API: {safe_tx}", "yellow"
        )
        return client_gateway_api.propose_safe_tx(safe_global.api_url, safe_tx)


def _get_queued_transaction_for_safe(
    to: str, calldata: str, rpc: str, safe_global: SafeGlobal
) -> PendingTransactionInfo:
    w3 = get_w3(rpc)
    chain_id = w3.eth.chain_id
    safe_contract = get_contract(w3, address=safe_global.safe_address, name="Safe")
    api_url = safe_global.api_url
    api_key = safe_global.api_key
    safe_address = safe_global.safe_address
    if _is_transaction_api(safe_global):
        nonce = safe_contract.functions.nonce().call()
        return transaction_api.get_queued_transaction(
            api_url,
            api_key,
            safe_address,
            nonce,
            to,
            calldata,
        )
    else:
        version = safe_contract.functions.VERSION().call()
        return client_gateway_api.get_queued_transaction(
            api_url,
            chain_id,
            safe_address,
            version,
            to,
            calldata,
        )


def _resolve_call(
    contract_name: str,
    method: str,
    calls: list,
    source: SourceConfig,
    safe_global: SafeGlobal,
    verbose: bool = False,
):
    """Resolve one or more calls into the (to, calldata, operation) a Safe tx carries."""
    if len(calls) > 1:
        to = resolve_multi_send_contract(source.rpc, safe_global.safe_address)
        calls_with_calldata = [
            (to, _create_calldata(contract_name, method, args)) for to, args in calls
        ]
        calldata = encode_multi(calls_with_calldata)
        operation = 1  # delegatecall
        if verbose:
            print(
                f"Going to propose multi-send transaction to multi-send contract {to} with calldata: {calldata}..."
            )
    else:
        to, args = calls[0]
        calldata = _create_calldata(contract_name, method, args)
        operation = 0  # call
        if verbose:
            print(
                f"Going to propose single transaction to {to} with args: {args} (calldata: {calldata})..."
            )
    return to, calldata, operation


def _classify_propose_failure(
    error: Exception,
    to: str,
    calldata: str,
    source: SourceConfig,
    safe_global: SafeGlobal,
) -> Exception:
    """Decide whether a failed POST left a proposal behind.

    Returns ProposalPosted when the queue already holds this exact calldata, so
    the caller knows not to propose it again; otherwise the original error, so a
    retry is free to try once more. A lookup that itself fails is treated as
    "posted", because proposing a duplicate is the worse of the two mistakes.
    """
    try:
        queued = _get_queued_transaction_for_safe(to, calldata, source.rpc, safe_global)
    except Exception as lookup_error:
        return ProposalPosted(
            "unknown",
            "the proposal failed with '{}' and the queue could not be read "
            "back either ({}), so it is unclear whether it landed".format(
                error, lookup_error
            ),
        )
    if queued:
        return ProposalPosted(
            queued.id, "the proposal failed with '{}' but it is queued".format(error)
        )
    return error


def _superseded_hashes(
    to: str, calldata: str, safe_nonce: int, safe_global: SafeGlobal
) -> list:
    """safeTxHashes of queued proposals now competing for this proposal's nonce.

    Takes the nonce the proposal was actually signed with rather than re-reading
    it, so an execution landing in between cannot make this describe a different
    position in the queue than the one just proposed.

    Only meaningful for the Transaction API; the Client Gateway path returns
    nothing rather than guessing. Never fatal: failing to describe the queue is
    not a reason to report a proposal that already succeeded as failed.
    """
    try:
        if not _is_transaction_api(safe_global):
            return []
        superseded = transaction_api.get_superseded_transactions(
            safe_global.api_url,
            safe_global.api_key,
            safe_global.safe_address,
            safe_nonce,
            to,
            calldata,
        )
        return [tx.get("safeTxHash") for tx in superseded if tx.get("safeTxHash")]
    except Exception as e:
        print_colored(f"Could not check for superseded proposals: {e}", "yellow")
        return []


def propose_tx_if_needed(
    contract_name: str,
    method: str,
    calls: list[tuple[str, list]],
    source: SourceConfig,
    safe_global: SafeGlobal,
) -> tuple[PendingTransactionInfo, bool, list]:
    print(
        f"Starting proposing transaction... source: '{source.name}', safe: '{safe_global.safe_address}', contract: '{contract_name}', method: '{method}', calls: {calls}..."
    )

    to, calldata, operation = _resolve_call(
        contract_name, method, calls, source, safe_global, verbose=True
    )

    safe_tx = _create_signed_safe_tx_for_safe(
        source.rpc, safe_global, to, calldata, operation
    )

    print(f"Trying to find existing transaction...")
    queued_transaction = _get_queued_transaction_for_safe(
        to, calldata, source.rpc, safe_global
    )
    if queued_transaction:
        print_colored(
            f"Transaction '{queued_transaction.id}' is already queued", "yellow"
        )
        return queued_transaction, False, []

    print(f"Proposing transaction: {safe_tx}...")
    try:
        tx_hash = _propose_tx_for_safe(safe_tx, safe_global)
    except Exception as e:
        # The POST itself can fail after the service accepted the proposal: the
        # gateway validates its own 200 response and can reject it, and a read
        # timeout on a committed row is retried as a duplicate POST. Ask the
        # queue which happened rather than assuming, because assuming "not
        # queued" makes the caller propose again and compete for the nonce.
        raise _classify_propose_failure(e, to, calldata, source, safe_global)
    print_colored(f"Transaction proposed: {tx_hash}", "green")

    # Past this point the proposal exists and is signable. Everything below only
    # confirms that, so its failures are reported as ProposalPosted: a caller
    # that retries after a plain failure would propose again, and against a share
    # price that has moved by then the calldata differs, so the new proposal is
    # not deduplicated and competes with the one already queued.
    attempts = 8
    for attempt in range(attempts):
        time.sleep(attempt + 1)

        print(
            f"Trying to get transaction: {tx_hash}... (attempt {attempt + 1} of {attempts})"
        )
        try:
            transaction = _get_queued_transaction_for_safe(
                to, calldata, source.rpc, safe_global
            )
        except Exception as e:
            # One blip is not an answer; the indexer is often just behind. Only
            # give up once the attempts are spent.
            print_colored("Could not read the queue back: {}".format(e), "yellow")
            transaction = None
        if transaction:
            tx_id = f"multisig_{safe_global.safe_address}_{tx_hash}"
            if transaction.id != tx_id:
                raise ProposalPosted(
                    tx_hash,
                    "read back a different id: expected {}, got {}".format(
                        tx_id, transaction.id
                    ),
                )
            # The queue now holds the new entry, so what comes back here is
            # whatever it displaced at the same nonce.
            return (
                transaction,
                True,
                _superseded_hashes(to, calldata, safe_tx.safe_nonce, safe_global),
            )

    raise ProposalPosted(
        tx_hash, "not visible in the queue after {} attempts".format(attempts)
    )
