import os
import sys
import unittest
from types import SimpleNamespace

# Add the src directory to the Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from config.read_config import SafeGlobal, SourceConfig
from safe_global import propose_tx
from safe_global.propose_tx import ProposalPosted, propose_tx_if_needed

SAFE = SafeGlobal(
    safe_address="0xfc7350b0d7a358Db58875148faF3bDEAaFC82911",
    proposer_private_key="0x" + "11" * 32,
    api_url="https://api.safe.global/tx-service/0g",
    web_client_url="https://app.safe.global",
    eip_3770="og",
)
SOURCE = SourceConfig(
    name="OG",
    rpc="https://rpc.invalid",
    source_core_helper="0x" + "11" * 20,
    deployments=(),
    safe_global=SAFE,
)
ORACLE = "0x8f7b85432F7BB3534ca34E42c215146Db47a4Eab"
CALLS = [(ORACLE, [1107091510212295064])]
POSTED_HASH = "0x25bcdb57102960e752ea85b2e0c89ff7a2200a9e84507e4d81841a43f3a5a3c7"


def queued(tx_hash=POSTED_HASH):
    return SimpleNamespace(id="multisig_{}_{}".format(SAFE.safe_address, tx_hash))


class ProposeTestCase(unittest.TestCase):
    """Drives propose_tx_if_needed itself; every other test mocks it away."""

    def setUp(self):
        self.originals = {
            name: getattr(propose_tx, name)
            for name in (
                "_resolve_call",
                "_create_signed_safe_tx_for_safe",
                "_propose_tx_for_safe",
                "_get_queued_transaction_for_safe",
                "_superseded_hashes",
            )
        }
        self.queue_lookups = []
        self.posts = 0
        # The read-back loop sleeps 1+2+...+8 seconds; the schedule is not what
        # these tests are about.
        self._sleep = propose_tx.time.sleep
        propose_tx.time.sleep = lambda _seconds: None

        propose_tx._resolve_call = lambda *_a, **_k: (ORACLE, "0xCALLDATA", 0)
        propose_tx._create_signed_safe_tx_for_safe = lambda *_a: SimpleNamespace(
            safe_nonce=11
        )
        propose_tx._superseded_hashes = lambda *_a: []
        self.set_post(lambda: POSTED_HASH)
        self.set_queue([None])

    def tearDown(self):
        propose_tx.time.sleep = self._sleep
        for name, value in self.originals.items():
            setattr(propose_tx, name, value)

    def set_post(self, behaviour):
        def post(_safe_tx, _safe_global):
            self.posts += 1
            result = behaviour()
            if isinstance(result, Exception):
                raise result
            return result

        propose_tx._propose_tx_for_safe = post

    def set_queue(self, script):
        """Each lookup consumes one entry; the last repeats."""

        def lookup(_to, _calldata, _rpc, _safe_global):
            self.queue_lookups.append(1)
            entry = script[min(len(self.queue_lookups) - 1, len(script) - 1)]
            if isinstance(entry, Exception):
                raise entry
            return entry

        propose_tx._get_queued_transaction_for_safe = lookup

    def propose(self):
        return propose_tx_if_needed("Oracle", "setValue", CALLS, SOURCE, SAFE)


class TestPostFailure(ProposeTestCase):
    """A POST can fail after the service has already queued the proposal.

    The gateway validates its own success response and can reject it, and a read
    timeout on a committed row is retried as a duplicate. Calling either "not
    queued" makes the caller propose again, against a share price that has moved,
    so the new calldata matches nothing and competes for the same Safe nonce.
    """

    def test_a_failed_post_whose_proposal_is_queued_reports_posted(self):
        self.set_post(lambda: Exception("hash mismatch in gateway response"))
        self.set_queue([None, queued()])

        with self.assertRaises(ProposalPosted):
            self.propose()

    def test_a_failed_post_that_left_nothing_behind_stays_a_plain_failure(self):
        self.set_post(lambda: Exception("connection refused"))
        self.set_queue([None, None])

        with self.assertRaises(Exception) as caught:
            self.propose()

        self.assertNotIsInstance(caught.exception, ProposalPosted)
        self.assertIn("connection refused", str(caught.exception))

    def test_an_unreadable_queue_after_a_failed_post_assumes_posted(self):
        """Proposing a duplicate is the worse of the two mistakes."""
        self.set_post(lambda: Exception("connection reset"))
        self.set_queue([None, Exception("service unavailable")])

        with self.assertRaises(ProposalPosted):
            self.propose()


class TestReadBack(ProposeTestCase):

    def test_never_becoming_visible_reports_posted(self):
        self.set_queue([None])

        with self.assertRaises(ProposalPosted) as caught:
            self.propose()

        self.assertEqual(caught.exception.safe_tx_hash, POSTED_HASH)

    def test_a_transient_read_error_does_not_end_the_attempts(self):
        """One blip is not an answer; the indexer is often just behind."""
        self.set_queue([None, Exception("502"), queued()])

        transaction, is_new, _superseded = self.propose()

        self.assertTrue(is_new)
        self.assertEqual(self.posts, 1, "and it must not post twice")

    def test_a_different_id_coming_back_reports_posted(self):
        self.set_queue([None, queued(tx_hash="0x" + "ee" * 32)])

        with self.assertRaises(ProposalPosted):
            self.propose()


class TestDeduplication(ProposeTestCase):

    def test_an_identical_queued_proposal_is_reused(self):
        self.set_queue([queued()])

        transaction, is_new, superseded = self.propose()

        self.assertFalse(is_new)
        self.assertEqual(self.posts, 0, "must not propose a duplicate")
        self.assertEqual(superseded, [])


if __name__ == "__main__":
    unittest.main()
