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
            safe_nonce=11, safe_tx_hash=bytes.fromhex(POSTED_HASH[2:])
        )
        self.superseded_nonces = []

        def superseded(_to, _calldata, safe_nonce, _safe_global):
            self.superseded_nonces.append(safe_nonce)
            return []

        propose_tx._superseded_hashes = superseded
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

    def test_a_failed_post_whose_proposal_is_queued_is_an_ordinary_success(self):
        """The queue entry proves it landed and describes it fully.

        Reporting it as merely "posted" would throw away the link, the
        confirmation count and the mentions of the signers still to sign -- in
        the one case where the bot has proof the proposal is waiting for them.
        """
        self.set_post(lambda: Exception("hash mismatch in gateway response"))
        self.set_queue([None, queued()])

        transaction, is_new, _superseded = self.propose()

        self.assertTrue(is_new)
        self.assertEqual(transaction.id, queued().id)
        self.assertEqual(self.superseded_nonces, [11], "and under the signed nonce")

    def test_a_failed_post_with_a_different_entry_queued_reports_posted(self):
        """Something is at this calldata but it is not what we signed."""
        self.set_post(lambda: Exception("gateway rejected its own response"))
        self.set_queue([None, queued(tx_hash="0x" + "ee" * 32)])

        with self.assertRaises(ProposalPosted):
            self.propose()

    def test_a_failed_post_that_left_nothing_behind_stays_a_plain_failure(self):
        self.set_post(lambda: Exception("connection refused"))
        self.set_queue([None, None])

        with self.assertRaises(Exception) as caught:
            self.propose()

        self.assertNotIsInstance(caught.exception, ProposalPosted)
        self.assertIn("connection refused", str(caught.exception))

    def test_an_unreadable_queue_after_a_failed_post_is_a_plain_failure(self):
        """No evidence either way, so do not claim a proposal is waiting.

        Calling it "posted" would clear the scheduler's failure counter and wait
        a whole interval. A Safe API outage takes down the POST and the read-back
        together, and two quiet runs consume the entire expiry margin. A retry
        that turns out to be a duplicate lands on the same nonce and the
        supersedes notice names the one to sign -- visible and recoverable.
        """
        self.set_post(lambda: Exception("connection reset"))
        self.set_queue([None, Exception("service unavailable")])

        with self.assertRaises(Exception) as caught:
            self.propose()

        self.assertNotIsInstance(caught.exception, ProposalPosted)
        self.assertIn("connection reset", str(caught.exception))

    def test_the_lookup_is_retried_before_concluding_nothing_landed(self):
        """The success path assumes a row is not readable the instant it lands.

        A single immediate read would answer "not queued" for exactly the case
        this exists to detect.
        """
        self.set_post(lambda: Exception("read timeout"))
        self.set_queue([None, None, queued()])

        transaction, is_new, _superseded = self.propose()

        self.assertTrue(is_new)


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


class TestSupersededLookupNonce(ProposeTestCase):
    """The competing entries are the ones bound to this proposal's nonce.

    Looking under any other nonce reports the wrong set, or none, and the notice
    then tells signers nothing about the entry it displaced.
    """

    def test_the_signed_nonce_is_what_is_looked_up(self):
        self.set_queue([None, queued()])

        self.propose()

        self.assertEqual(self.superseded_nonces, [11])


if __name__ == "__main__":
    unittest.main()
