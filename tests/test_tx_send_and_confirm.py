import os
import sys
import unittest
from types import SimpleNamespace

# Add the src directory to the Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from eth_account import Account
from web3 import Web3
from web3.exceptions import TransactionNotFound

from web3_scripts.tx import (
    NonceAlreadyUsed,
    TxNotConfirmed,
    TxReverted,
    is_already_known,
    is_receipt_pending,
    is_revert,
    send_and_confirm,
)

TEST_KEY = "0x" + "11" * 32
TEST_ADDRESS = Account.from_key(TEST_KEY).address
CHAIN_ID = 16661
TARGET = "0x000000000000000000000000000000000000dEaD"

# The exact error a 0G reth node returns while a block's receipts are still
# being indexed, and the one a node returns for a payload it already holds.
RECEIPTS_NOT_INDEXED = (
    "server returned an error response: error code -32000: "
    "no matching receipts found: this may indicate potential data corruption"
)
ALREADY_KNOWN = "server returned an error response: error code -32000: already known"
NONCE_TOO_LOW = (
    "server returned an error response: error code -32000: "
    "nonce too low: next nonce 2129, tx nonce 2128"
)
REVERTED = "execution reverted: SourceCore: zero shares"
UNDERPRICED = (
    "server returned an error response: error code -32000: "
    "replacement transaction underpriced"
)


def receipt(status=1, block_number=100, tx_hash=None):
    return {
        "status": status,
        "blockNumber": block_number,
        "transactionHash": tx_hash or ("0x" + "ab" * 32),
    }


class FakeEth:
    """Minimal eth namespace driven by scripted responses.

    receipt_script and send_script are consumed one entry per call; an entry that
    is an Exception instance is raised, anything else is returned. A resolver
    callable takes precedence over receipt_script and decides from the fake's own
    state, which keeps tests about replacement behaviour independent of timing.
    Signing uses the real offline signer so hashes behave like production hashes.
    """

    def __init__(
        self, receipt_script=None, send_script=None, resolver=None, balance=10**18
    ):
        self.account = Web3().eth.account
        self.chain_id = CHAIN_ID
        self.max_priority_fee = 10**9
        self._balance = balance
        self.receipt_script = list(receipt_script or [])
        self.send_script = list(send_script or [])
        self.resolver = resolver
        self.sent = []
        self.receipt_queries = []
        self.nonce_calls = []

    def get_balance(self, _address):
        return self._balance

    def set_balance(self, value):
        self._balance = value

    def get_block(self, _identifier):
        self.block_reads = getattr(self, "block_reads", 0) + 1
        return SimpleNamespace(baseFeePerGas=7)

    def get_transaction_count(self, _address, block="latest"):
        self.nonce_calls.append(block)
        return 42

    def send_raw_transaction(self, raw):
        self.sent.append(raw)
        if self.send_script:
            entry = self.send_script.pop(0)
            if isinstance(entry, Exception):
                raise entry
        return b"\x00"

    def get_transaction_receipt(self, tx_hash):
        self.receipt_queries.append(tx_hash)
        if self.resolver is not None:
            entry = self.resolver(self, tx_hash)
        elif self.receipt_script:
            entry = self.receipt_script.pop(0)
        else:
            entry = None
        if isinstance(entry, Exception):
            raise entry
        if entry is None:
            raise TransactionNotFound("not found")
        return entry


class FakeW3:
    def __init__(self, eth):
        self.eth = eth
        self.provider = None

    def to_wei(self, amount, unit):
        return Web3.to_wei(amount, unit)


class FakeContractFunction:
    def __init__(self, w3, gas=100000, estimate_error=None):
        self.w3 = w3
        self._gas = gas
        self._estimate_error = estimate_error
        self.built = []

    def estimate_gas(self, _params):
        if self._estimate_error is not None:
            raise self._estimate_error
        return self._gas

    def build_transaction(self, params):
        transaction = dict(params)
        transaction.update({"to": TARGET, "data": "0x", "chainId": CHAIN_ID})
        self.built.append(transaction)
        return transaction


def make(receipt_script=None, send_script=None, estimate_error=None, resolver=None):
    eth = FakeEth(
        receipt_script=receipt_script, send_script=send_script, resolver=resolver
    )
    w3 = FakeW3(eth)
    return w3, FakeContractFunction(w3, estimate_error=estimate_error)


def only_after_replacement(eth, tx_hash):
    """No receipt exists until a second transaction has been broadcast."""
    if len(eth.sent) >= 2:
        return receipt(tx_hash=tx_hash)
    return None


def only_the_replacement_lands(eth, tx_hash):
    """Only the second broadcast ever gets a receipt; the original never does."""
    if len(eth.sent) >= 2 and tx_hash != eth.receipt_queries[0]:
        return receipt(tx_hash=tx_hash)
    return None


class TestErrorClassification(unittest.TestCase):

    def test_missing_receipts_is_treated_as_pending(self):
        self.assertTrue(is_receipt_pending(Exception(RECEIPTS_NOT_INDEXED)))

    def test_transaction_not_found_is_pending(self):
        self.assertTrue(is_receipt_pending(TransactionNotFound("not found")))

    def test_already_known_is_recognised(self):
        self.assertTrue(is_already_known(Exception(ALREADY_KNOWN)))

    def test_revert_is_recognised(self):
        self.assertTrue(is_revert(Exception(REVERTED)))

    def test_missing_receipts_is_not_a_revert(self):
        self.assertFalse(is_revert(Exception(RECEIPTS_NOT_INDEXED)))


class TestFeeArithmetic(unittest.TestCase):
    """The bump is what makes every attempt a distinct payload.

    That distinctness is the whole premise for dropping a refused hash from the
    poll set, so the floor that guarantees it is load-bearing on its own.
    """

    def test_a_bump_always_moves_the_number(self):
        from web3_scripts.tx import _bump

        # A percentage alone rounds back to the input for small values, and the
        # config would allow a percentage that never moves it at all.
        for value in (0, 1, 2, 7, 100):
            self.assertGreater(_bump(value, 101), value)

    def test_the_config_refuses_a_percentage_that_cannot_replace(self):
        from config.read_config import _create_tx_config

        with self.assertRaises(ValueError):
            _create_tx_config({"fee_bump_percent": 100})

    def test_the_config_refuses_zero_attempts(self):
        from config.read_config import _create_tx_config

        with self.assertRaises(ValueError):
            _create_tx_config({"max_attempts": 0})


class TestPreflightGuards(unittest.TestCase):
    """Refuse before signing rather than after broadcasting."""

    def test_a_balance_below_the_call_value_is_refused(self):
        w3, fn = make(receipt_script=[receipt()])
        w3.eth.set_balance(5)

        with self.assertRaises(Exception) as caught:
            send_and_confirm(fn, 10, TEST_KEY, w3=w3, poll_latency=0.001)

        # Named specifically: the affordability guard below rejects this case
        # too, and asserting the shared prefix would let this one be deleted.
        self.assertIn("LayerZero payment", str(caught.exception))
        self.assertEqual(w3.eth.sent, [])

    def test_a_balance_that_cannot_cover_gas_is_refused(self):
        w3, fn = make(receipt_script=[receipt()])
        w3.eth.set_balance(1)

        with self.assertRaises(Exception) as caught:
            send_and_confirm(fn, 0, TEST_KEY, w3=w3, poll_latency=0.001)

        self.assertIn("transaction execution", str(caught.exception))
        self.assertEqual(w3.eth.sent, [])

    def test_the_gas_estimate_carries_a_buffer(self):
        w3, fn = make(receipt_script=[receipt()])

        send_and_confirm(fn, 0, TEST_KEY, w3=w3, receipt_timeout=5, poll_latency=0.001)

        self.assertGreater(fn.built[0]["gas"], fn._gas)


class TestSendAndConfirm(unittest.TestCase):

    def test_survives_receipt_indexing_lag(self):
        """A null result and a missing-receipts error must not end the wait."""
        w3, fn = make(
            receipt_script=[
                None,
                Exception(RECEIPTS_NOT_INDEXED),
                None,
                receipt(),
            ]
        )

        outcome = send_and_confirm(
            fn, 0, TEST_KEY, w3=w3, receipt_timeout=5, poll_latency=0.001
        )

        self.assertEqual(outcome.attempts, 1)
        self.assertEqual(outcome.nonce, 42)
        self.assertEqual(len(w3.eth.sent), 1, "must not rebroadcast while polling")

    def test_already_known_counts_as_broadcast(self):
        w3, fn = make(
            receipt_script=[receipt()], send_script=[Exception(ALREADY_KNOWN)]
        )

        outcome = send_and_confirm(
            fn, 0, TEST_KEY, w3=w3, receipt_timeout=5, poll_latency=0.001
        )

        self.assertEqual(outcome.attempts, 1)
        self.assertEqual(len(outcome.tx_hashes), 1)

    def test_nonce_comes_from_latest_so_a_retry_replaces(self):
        """A retry of a timed-out operation must reuse the nonce, not skip it.

        Counting a stuck transaction would sign the retry one slot further
        along, and if the stuck one later mined the retry would mine behind it
        and the operation would happen twice.
        """
        w3, fn = make(receipt_script=[receipt()])

        send_and_confirm(fn, 0, TEST_KEY, w3=w3, receipt_timeout=5, poll_latency=0.001)

        self.assertEqual(w3.eth.nonce_calls, ["latest"])

    def test_an_rpc_outage_while_polling_keeps_the_hashes(self):
        """A polling failure says nothing about the transaction.

        Losing the hashes would leave the caller unable to tell a broadcast
        transaction from one that was never sent.
        """
        w3, fn = make(receipt_script=[ConnectionError("All 2 RPC endpoint(s) failed")])

        with self.assertRaises(TxNotConfirmed) as caught:
            send_and_confirm(
                fn, 0, TEST_KEY, w3=w3, receipt_timeout=5, poll_latency=0.001
            )

        self.assertEqual(len(caught.exception.tx_hashes), 1)
        self.assertIsInstance(caught.exception.__cause__, ConnectionError)

    def test_an_unexpected_send_error_keeps_the_hashes(self):
        w3, fn = make(
            receipt_script=[], send_script=[Exception("insufficient funds for gas")]
        )

        with self.assertRaises(TxNotConfirmed) as caught:
            send_and_confirm(
                fn, 0, TEST_KEY, w3=w3, receipt_timeout=1, poll_latency=0.001
            )

        self.assertEqual(len(caught.exception.tx_hashes), 1)

    def test_the_opening_bid_leaves_room_to_be_replaced(self):
        """A cap on the opening bid disables the replacement it is meant to bound.

        The suggested tip on a busy chain is routinely a large fraction of the
        cap, so a first send priced straight at the cap can never be replaced --
        which is the mechanism that rescues a transaction the network outran.
        """
        w3, fn = make(resolver=only_after_replacement)
        w3.eth.max_priority_fee = Web3.to_wei(2, "gwei")  # x3 exceeds the cap

        outcome = send_and_confirm(
            fn,
            0,
            TEST_KEY,
            w3=w3,
            receipt_timeout=0.01,
            max_attempts=4,
            fee_cap_gwei=4,
            poll_latency=0.001,
        )

        self.assertEqual(outcome.attempts, 2, "a replacement must be possible")
        first, second = fn.built[0], fn.built[1]
        self.assertGreater(
            second["maxPriorityFeePerGas"], first["maxPriorityFeePerGas"]
        )
        self.assertLessEqual(
            second["maxPriorityFeePerGas"], Web3.to_wei(4, "gwei"), "still capped"
        )

    def test_every_planned_bump_fits_under_the_cap(self):
        """The headroom exists so the planned replacements can all be signed."""
        w3, fn = make(receipt_script=[])
        w3.eth.max_priority_fee = Web3.to_wei(1, "gwei") // 2

        with self.assertRaises(TxNotConfirmed):
            send_and_confirm(
                fn,
                0,
                TEST_KEY,
                w3=w3,
                receipt_timeout=0.01,
                max_attempts=4,
                fee_cap_gwei=4,
                poll_latency=0.001,
            )

        self.assertEqual(len(fn.built), 4, "all four attempts signed something new")
        cap = Web3.to_wei(4, "gwei")
        for transaction in fn.built:
            self.assertLessEqual(transaction["maxPriorityFeePerGas"], cap)

    def test_bumping_stops_once_the_cap_is_reached(self):
        """With the tip already above the cap there is nowhere left to climb."""
        w3, fn = make(receipt_script=[])
        w3.eth.max_priority_fee = Web3.to_wei(5, "gwei")

        with self.assertRaises(TxNotConfirmed):
            send_and_confirm(
                fn,
                0,
                TEST_KEY,
                w3=w3,
                receipt_timeout=0.01,
                max_attempts=4,
                fee_cap_gwei=4,
                poll_latency=0.001,
            )

        self.assertLess(len(fn.built), 4, "stops instead of repeating a payload")
        self.assertLessEqual(
            fn.built[-1]["maxPriorityFeePerGas"], Web3.to_wei(4, "gwei")
        )

    def test_no_attempt_repeats_a_payload(self):
        """Every attempt must offer strictly more than the last.

        Re-signing identical fields produces the same hash, which is noise, and
        makes it ambiguous whether a later refusal refers to a payload the node
        had already accepted -- the premise the refusal handling relies on.
        """
        w3, fn = make(receipt_script=[])
        w3.eth.max_priority_fee = Web3.to_wei(5, "gwei")

        with self.assertRaises(TxNotConfirmed):
            send_and_confirm(
                fn,
                0,
                TEST_KEY,
                w3=w3,
                receipt_timeout=0.01,
                max_attempts=4,
                fee_cap_gwei=4,
                poll_latency=0.001,
            )

        fees = [t["maxPriorityFeePerGas"] for t in fn.built]
        self.assertEqual(len(fees), len(set(fees)), "a payload was signed twice")
        self.assertEqual(fees, sorted(fees))

    def test_the_fee_cap_still_caps_after_a_bump(self):
        """A cap is a limit on what we will pay, replacements included."""
        w3, fn = make(receipt_script=[])

        with self.assertRaises(TxNotConfirmed):
            send_and_confirm(
                fn,
                0,
                TEST_KEY,
                w3=w3,
                receipt_timeout=0.01,
                max_attempts=4,
                fee_cap_gwei=1,
                poll_latency=0.001,
            )

        cap = Web3.to_wei(1, "gwei")
        for transaction in fn.built:
            self.assertLessEqual(transaction["maxPriorityFeePerGas"], cap)

    def test_the_whole_call_shares_one_time_budget(self):
        """Attempts that fail instantly must not each buy another full timeout."""
        import time as _time

        w3, fn = make(
            receipt_script=[],
            send_script=[None, Exception(UNDERPRICED), Exception(UNDERPRICED)],
        )

        started = _time.monotonic()
        with self.assertRaises(TxNotConfirmed):
            send_and_confirm(
                fn,
                0,
                TEST_KEY,
                w3=w3,
                receipt_timeout=0.2,
                max_attempts=3,
                poll_latency=0.01,
            )
        elapsed = _time.monotonic() - started

        self.assertLess(elapsed, 0.2 * 3 + 0.2)

    def test_timeout_replaces_same_nonce_with_a_higher_fee(self):
        w3, fn = make(resolver=only_after_replacement)

        outcome = send_and_confirm(
            fn,
            0,
            TEST_KEY,
            w3=w3,
            receipt_timeout=0.01,
            max_attempts=2,
            poll_latency=0.001,
        )

        self.assertEqual(outcome.attempts, 2)
        first, second = fn.built[0], fn.built[1]
        self.assertEqual(first["nonce"], second["nonce"], "replacement, not a new tx")
        self.assertGreaterEqual(
            second["maxFeePerGas"] * 100,
            first["maxFeePerGas"] * 110,
            "replacement must clear the node's 10% premium",
        )
        self.assertGreaterEqual(
            second["maxPriorityFeePerGas"] * 100,
            first["maxPriorityFeePerGas"] * 110,
        )
        self.assertEqual(len(outcome.tx_hashes), 2)

    def test_original_hash_is_still_polled_after_a_replacement(self):
        """A replaced transaction can still be the one that lands."""
        w3, fn = make(resolver=only_after_replacement)

        outcome = send_and_confirm(
            fn,
            0,
            TEST_KEY,
            w3=w3,
            receipt_timeout=0.01,
            max_attempts=2,
            poll_latency=0.001,
        )

        self.assertEqual(outcome.tx_hash, outcome.tx_hashes[0])

    def test_replacement_hash_is_polled_too(self):
        w3, fn = make(resolver=only_the_replacement_lands)

        outcome = send_and_confirm(
            fn,
            0,
            TEST_KEY,
            w3=w3,
            receipt_timeout=0.01,
            max_attempts=2,
            poll_latency=0.001,
        )

        self.assertEqual(outcome.tx_hash, outcome.tx_hashes[1])
        self.assertNotEqual(outcome.tx_hashes[0], outcome.tx_hashes[1])

    def test_refused_payloads_are_not_polled_for(self):
        """A refusal is definitive: that payload is in no pool and cannot appear.

        Polling for it spends the whole budget on nothing and names hashes in
        the failure that an operator will never find on chain.
        """
        import time as _time

        w3, fn = make(receipt_script=[], send_script=[Exception(UNDERPRICED)] * 3)

        started = _time.monotonic()
        with self.assertRaises(TxNotConfirmed) as caught:
            send_and_confirm(
                fn,
                0,
                TEST_KEY,
                w3=w3,
                receipt_timeout=1,
                max_attempts=3,
                fee_cap_gwei=4,
                poll_latency=0.01,
            )
        elapsed = _time.monotonic() - started

        self.assertEqual(caught.exception.tx_hashes, [])
        self.assertIn("refused", str(caught.exception))
        self.assertIn("nothing is live", str(caught.exception).lower())
        self.assertLess(elapsed, 1, "must not wait out a budget for nothing")

    def test_gives_up_carrying_every_hash(self):
        w3, fn = make(receipt_script=[])

        with self.assertRaises(TxNotConfirmed) as caught:
            send_and_confirm(
                fn,
                0,
                TEST_KEY,
                w3=w3,
                receipt_timeout=0.01,
                max_attempts=2,
                poll_latency=0.001,
            )

        self.assertEqual(caught.exception.nonce, 42)
        self.assertEqual(len(caught.exception.tx_hashes), 2)

    def test_revert_during_estimation_is_not_retried(self):
        w3, fn = make(estimate_error=Exception(REVERTED))

        with self.assertRaises(TxReverted):
            send_and_confirm(fn, 0, TEST_KEY, w3=w3, poll_latency=0.001)

        self.assertEqual(w3.eth.sent, [], "a reverting call must never be broadcast")

    def test_on_chain_failure_status_raises(self):
        w3, fn = make(receipt_script=[receipt(status=0)])

        with self.assertRaises(TxReverted):
            send_and_confirm(
                fn, 0, TEST_KEY, w3=w3, receipt_timeout=5, poll_latency=0.001
            )

    def test_nonce_too_low_without_our_receipt_raises(self):
        w3, fn = make(receipt_script=[], send_script=[Exception(NONCE_TOO_LOW)])

        with self.assertRaises(NonceAlreadyUsed):
            send_and_confirm(
                fn, 0, TEST_KEY, w3=w3, receipt_timeout=0.01, poll_latency=0.001
            )

    def test_nonce_too_low_waits_out_the_indexing_lag(self):
        """The rejection usually means our own attempt was mined moments ago.

        Its receipt is not queryable for a few seconds after inclusion, so a
        single immediate lookup would report a mined transaction as lost.
        """
        w3, fn = make(
            receipt_script=[
                Exception(RECEIPTS_NOT_INDEXED),
                None,
                receipt(),
            ],
            send_script=[Exception(NONCE_TOO_LOW)],
        )

        outcome = send_and_confirm(
            fn, 0, TEST_KEY, w3=w3, receipt_timeout=5, poll_latency=0.001
        )

        self.assertEqual(outcome.receipt["status"], 1)

    def test_the_final_sweep_finds_a_receipt_the_attempts_missed(self):
        """A rejected replacement returns instantly and sends nothing new.

        The attempts can therefore run out in milliseconds while the first
        broadcast is still live, so the sweep after the loop is what actually
        finds it. The receipt is withheld until every attempt has been made, so
        this cannot pass on a receipt found inside the loop.
        """

        # The first send is accepted and stays live; the second is refused at
        # the cap, which ends the loop instantly with most of the budget unspent.
        # The receipt appears only after both sends, so nothing inside the loop
        # can find it -- only the sweep afterwards.
        def only_after_the_loop_has_ended(eth, tx_hash):
            if len(eth.sent) < 2:
                return None
            return receipt(tx_hash=tx_hash)

        w3, fn = make(
            resolver=only_after_the_loop_has_ended,
            send_script=[None, Exception(UNDERPRICED)],
        )
        w3.eth.max_priority_fee = Web3.to_wei(5, "gwei")

        outcome = send_and_confirm(
            fn,
            0,
            TEST_KEY,
            w3=w3,
            receipt_timeout=0.05,
            max_attempts=2,
            fee_cap_gwei=4,
            poll_latency=0.001,
        )

        self.assertEqual(outcome.receipt["status"], 1)
        self.assertEqual(len(w3.eth.sent), 2, "both sends must have been attempted")
        self.assertEqual(
            outcome.tx_hashes,
            [outcome.tx_hash],
            "the refused hash is dropped; the live one is what the sweep found",
        )

    def test_the_underpriced_path_makes_no_network_call(self):
        """The node just refused a payload; do not go back to it for a fee.

        The slow path consults the network deliberately and is guarded for it.
        This one relies on a keyword default to stay offline.
        """
        w3, fn = make(receipt_script=[], send_script=[Exception(UNDERPRICED)] * 4)
        w3.eth.block_reads = 0

        with self.assertRaises(TxNotConfirmed):
            send_and_confirm(
                fn,
                0,
                TEST_KEY,
                w3=w3,
                receipt_timeout=0.01,
                max_attempts=4,
                fee_cap_gwei=4,
                poll_latency=0.001,
            )

        self.assertEqual(w3.eth.block_reads, 1, "only the opening fee reads a block")

    def test_the_underpriced_path_never_repeats_a_payload(self):
        """Every refusal must be answered with a strictly better offer.

        This is what lets a refusal be read as proof the payload is not live,
        which is what justifies dropping its hash from the poll set. The count
        of attempts is deliberately not asserted: `priority_ceiling` derives the
        opening bid from `max_attempts`, so the ladder has exactly that many
        rungs and the loop ends on the last one either way. The property that
        carries weight is that no two of them are the same.
        """
        attempts = 8
        w3, fn = make(
            receipt_script=[], send_script=[Exception(UNDERPRICED)] * attempts
        )
        w3.eth.max_priority_fee = Web3.to_wei(5, "gwei")

        with self.assertRaises(TxNotConfirmed) as caught:
            send_and_confirm(
                fn,
                0,
                TEST_KEY,
                w3=w3,
                receipt_timeout=0.01,
                max_attempts=attempts,
                fee_cap_gwei=4,
                poll_latency=0.001,
            )

        fees = [t["maxPriorityFeePerGas"] for t in fn.built]
        self.assertEqual(len(fees), len(set(fees)), "a payload was signed twice")
        self.assertEqual(fees, sorted(fees))
        self.assertLessEqual(fees[-1], Web3.to_wei(4, "gwei"))
        self.assertEqual(caught.exception.tx_hashes, [], "all refused, none live")

    def test_nonce_too_low_after_our_tx_landed_is_success(self):
        w3, fn = make(
            receipt_script=[receipt()], send_script=[Exception(NONCE_TOO_LOW)]
        )

        outcome = send_and_confirm(
            fn, 0, TEST_KEY, w3=w3, receipt_timeout=0.01, poll_latency=0.001
        )

        self.assertEqual(outcome.receipt["status"], 1)


if __name__ == "__main__":
    unittest.main()
