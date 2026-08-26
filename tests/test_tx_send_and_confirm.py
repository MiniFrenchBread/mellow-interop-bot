import os
import sys
import unittest
from types import SimpleNamespace

# Add the src directory to the Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from eth_account import Account
from web3 import Web3
from web3.exceptions import TransactionNotFound

from web3_scripts import tx as tx_module
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
        return getattr(self, "nonce", 42)

    def get_transaction(self, _tx_hash):
        error = getattr(self, "lookup_error", None)
        if error is not None:
            raise error
        # Present unless a test says the node has forgotten it.
        if getattr(self, "known", True):
            return {"blockNumber": None}
        raise TransactionNotFound("not found")

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


def stop_after(attempts: int):
    """Bound an otherwise unbounded send so a test can inspect N attempts.

    send_and_confirm runs until the chain settles the transaction, so a test
    whose fake never produces a receipt has to say when to stop -- which is the
    same hook the scheduler uses for shutdown.
    """
    seen = {"n": 0}

    def should_stop() -> bool:
        seen["n"] += 1
        return seen["n"] > attempts

    return should_stop


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
                should_stop=stop_after(4),
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
                should_stop=stop_after(4),
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
                should_stop=stop_after(4),
                fee_cap_gwei=1,
                poll_latency=0.001,
            )

        cap = Web3.to_wei(1, "gwei")
        for transaction in fn.built:
            self.assertLessEqual(transaction["maxPriorityFeePerGas"], cap)

    def test_timeout_replaces_same_nonce_with_a_higher_fee(self):
        w3, fn = make(resolver=only_after_replacement)

        outcome = send_and_confirm(
            fn,
            0,
            TEST_KEY,
            w3=w3,
            receipt_timeout=0.01,
            should_stop=stop_after(2),
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
            should_stop=stop_after(2),
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
            should_stop=stop_after(2),
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
                should_stop=stop_after(3),
                fee_cap_gwei=4,
                poll_latency=0.01,
            )
        elapsed = _time.monotonic() - started

        self.assertEqual(caught.exception.tx_hashes, [])
        self.assertIn("0 transaction(s) may still be in flight", str(caught.exception))
        self.assertLess(elapsed, 1, "must not wait out a timeout for nothing")

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
                should_stop=stop_after(4),
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
                should_stop=stop_after(attempts),
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


class TestItRunsUntilTheChainDecides(unittest.TestCase):
    """The rule the rest of the design rests on.

    A send returns only when the chain has settled the transaction, so a caller
    always finds the next nonce free and two operations can never contend for
    one. An earlier version gave up after a fixed number of attempts and needed
    a guard band to police the nonce it abandoned; these bind the property that
    removed the need for it.
    """

    def test_it_keeps_replacing_far_past_any_old_attempt_limit(self):
        """Twenty timeouts, then a receipt. The old bound was three."""
        w3, fn = make(receipt_script=[None] * 20 + [receipt()])

        outcome = send_and_confirm(
            fn,
            0,
            TEST_KEY,
            w3=w3,
            receipt_timeout=0.001,
            # High enough that the cap is not what ends the bumping; this is
            # about the absence of an attempt limit, not about the ceiling.
            fee_cap_gwei=10_000,
            poll_latency=0.001,
        )

        self.assertEqual(outcome.receipt["status"], 1)
        self.assertGreater(
            len(fn.built), 3, "gave up at what used to be the attempt limit"
        )

    def test_every_replacement_reuses_the_one_nonce(self):
        """Queueing behind the stuck transaction would make the operation
        happen twice if the stuck one later mined."""
        w3, fn = make(receipt_script=[None] * 8 + [receipt()])

        send_and_confirm(
            fn, 0, TEST_KEY, w3=w3, receipt_timeout=0.001, poll_latency=0.001
        )

        self.assertEqual({built["nonce"] for built in fn.built}, {42})

    def test_an_unreachable_node_is_waited_out_rather_than_raised(self):
        """An RPC outage says nothing about the transaction, and this never
        gives up, so turning it into an error would only lose track of it."""
        w3, fn = make(
            receipt_script=[Exception("connection refused")] * 6 + [receipt()],
        )

        outcome = send_and_confirm(
            fn, 0, TEST_KEY, w3=w3, receipt_timeout=0.001, poll_latency=0.001
        )

        self.assertEqual(outcome.receipt["status"], 1)

    def test_a_broadcast_failure_is_waited_out_too(self):
        w3, fn = make(
            send_script=[Exception("connection refused")] * 4,
            receipt_script=[None] * 4 + [receipt()],
        )

        outcome = send_and_confirm(
            fn, 0, TEST_KEY, w3=w3, receipt_timeout=0.001, poll_latency=0.001
        )

        self.assertEqual(outcome.receipt["status"], 1)

    def test_a_revert_is_settled_and_stops_immediately(self):
        """The chain decided. Retrying buys nothing and would never end."""
        w3, fn = make(receipt_script=[receipt(status=0)])

        with self.assertRaises(TxReverted):
            send_and_confirm(
                fn, 0, TEST_KEY, w3=w3, receipt_timeout=0.001, poll_latency=0.001
            )

    def test_at_the_fee_cap_it_waits_without_resigning(self):
        """Nothing higher can be offered -- the cap is ours, not the network's
        -- so re-signing would only rebroadcast a payload the node has."""
        w3, fn = make(receipt_script=[None] * 30 + [receipt()])
        w3.eth.max_priority_fee = Web3.to_wei(4, "gwei")

        send_and_confirm(
            fn,
            0,
            TEST_KEY,
            w3=w3,
            receipt_timeout=0.001,
            fee_cap_gwei=4,
            poll_latency=0.001,
        )

        fees = [built["maxPriorityFeePerGas"] for built in fn.built]
        self.assertEqual(len(fees), len(set(fees)), "a payload was signed twice")
        self.assertLessEqual(max(fees), Web3.to_wei(4, "gwei"))

    def test_stopping_is_the_only_way_out_with_a_transaction_in_flight(self):
        """And it carries every hash, so the next process can find them."""
        w3, fn = make(receipt_script=[None] * 50)

        with self.assertRaises(TxNotConfirmed) as caught:
            send_and_confirm(
                fn,
                0,
                TEST_KEY,
                w3=w3,
                receipt_timeout=0.001,
                poll_latency=0.001,
                should_stop=stop_after(3),
            )

        self.assertTrue(caught.exception.tx_hashes)
        self.assertEqual(caught.exception.nonce, 42)

    def test_it_stops_promptly_rather_than_finishing_the_wait(self):
        stopped = {"now": False}
        w3, fn = make(receipt_script=[None] * 50)

        def should_stop():
            if fn.built:
                stopped["now"] = True
            return stopped["now"]

        with self.assertRaises(TxNotConfirmed):
            send_and_confirm(
                fn,
                0,
                TEST_KEY,
                w3=w3,
                receipt_timeout=0.001,
                poll_latency=0.001,
                should_stop=should_stop,
            )

        self.assertEqual(len(fn.built), 1, "stopped at the first check after a send")


if __name__ == "__main__":
    unittest.main()
