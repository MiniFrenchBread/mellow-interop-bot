import os
import sys
import unittest

# Add the src directory to the Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from config.read_config import SafeGlobal
from main import SafeProposal, compose_safe_proposal_message
from safe_global.common import PendingTransactionInfo, ThresholdWithOwners
from safe_global.transaction_api import get_superseded_transactions

SAFE_ADDRESS = "0xfc7350b0d7a358Db58875148faF3bDEAaFC82911"
ORACLE = "0x8f7b85432F7BB3534ca34E42c215146Db47a4Eab"
OLD_HASH = "0x25bcdb57102960e752ea85b2e0c89ff7a2200a9e84507e4d81841a43f3a5a3c7"

SAFE_GLOBAL = SafeGlobal(
    safe_address=SAFE_ADDRESS,
    proposer_private_key="",
    api_url="https://api.safe.global/tx-service/0g",
    web_client_url="https://app.safe.global",
    eip_3770="og",
)


def proposal(superseded=None) -> SafeProposal:
    return SafeProposal(
        method="setValue",
        deployment_names=["OG"],
        calls=[(ORACLE, [1107091510212295064])],
        transaction=PendingTransactionInfo(
            id="multisig_{}_0xabc".format(SAFE_ADDRESS),
            number_of_required_confirmations=2,
            threshold_with_owners=ThresholdWithOwners(threshold=2, owners=[]),
            confirmations=[],
            missing_confirmations=[],
        ),
        is_newly_created=True,
        superseded=superseded or [],
    )


def queued(nonce, data, tx_hash, to=ORACLE):
    # The Safe service reports nonce as a string and may checksum addresses
    # differently from the caller; both are reproduced here deliberately.
    return {"nonce": str(nonce), "data": data, "to": to, "safeTxHash": tx_hash}


class TestSupersededLookup(unittest.TestCase):
    """A newer oracle value lands on the same Safe nonce as the pending one."""

    def setUp(self):
        self.calls = []
        self.filters = []

        def fake_fetch(api_url, api_key, safe_address, safe_nonce, to=None):
            self.calls.append(safe_nonce)
            self.filters.append(to)
            if to is None:
                return self.queue
            # The service filters server-side, so a `to` narrows the result set
            # before any local filtering sees it.
            return [
                entry
                for entry in self.queue
                if (entry.get("to") or "").lower() == to.lower()
            ]

        import safe_global.transaction_api as module

        self._original = module._get_queued_transactions
        module._get_queued_transactions = fake_fetch
        self._module = module

    def tearDown(self):
        self._module._get_queued_transactions = self._original

    def find(self, calldata="0xNEW", nonce=11):
        return get_superseded_transactions(
            "url", "key", SAFE_ADDRESS, nonce, ORACLE, calldata
        )

    def test_same_nonce_different_calldata_is_superseded(self):
        self.queue = [queued(11, "0xOLD", OLD_HASH)]
        self.assertEqual([t["safeTxHash"] for t in self.find()], [OLD_HASH])

    def test_identical_calldata_to_the_same_target_is_reuse(self):
        self.queue = [queued(11, "0xNEW", OLD_HASH)]
        self.assertEqual(self.find(), [])

    def test_identical_calldata_to_a_different_target_still_competes(self):
        self.queue = [queued(11, "0xNEW", OLD_HASH, to=SAFE_ADDRESS)]
        self.assertEqual([t["safeTxHash"] for t in self.find()], [OLD_HASH])

    def test_a_later_nonce_is_queued_behind_not_competing(self):
        self.queue = [queued(12, "0xOLD", OLD_HASH)]
        self.assertEqual(self.find(), [])

    def test_a_competitor_with_a_different_target_still_counts(self):
        """One stale oracle is proposed direct, several go via MultiSend.

        Both bind the same Safe nonce and void each other, so the target is not
        what decides whether a queued proposal competes.
        """
        multi_send = "0x38869bf66a61cF6bDB996A6aE40D5853Fd43B526"
        self.queue = [queued(11, "0xOLD", OLD_HASH, to=multi_send)]

        self.assertEqual([t["safeTxHash"] for t in self.find()], [OLD_HASH])

    def test_target_casing_does_not_matter(self):
        """Reuse is decided by target and calldata together.

        With casing ignored, a proposal would fail to recognise the one it just
        made and tell signers to leave their own current entry unsigned.
        """
        self.queue = [queued(11, "0xNEW", OLD_HASH, to=ORACLE.lower())]

        self.assertEqual(self.find(), [], "same target, same calldata: reuse")

    def test_the_fetch_is_not_narrowed_to_one_target(self):
        """One stale oracle goes direct, several via MultiSend.

        Both bind the same nonce, so a server-side target filter would hide the
        competitor in the case where the shape changed between proposals.
        """
        self.queue = [queued(11, "0xOLD", OLD_HASH)]

        self.find()

        self.assertEqual(self.filters, [None])

    def test_an_unparseable_nonce_is_ignored(self):
        self.queue = [queued("", "0xOLD", OLD_HASH)]
        self.assertEqual(self.find(), [])


class TestSupersedesNotice(unittest.TestCase):

    def test_notice_names_the_displaced_proposal(self):
        message = compose_safe_proposal_message(
            {}, "OG", SAFE_GLOBAL, proposal([OLD_HASH])
        )

        self.assertIn("Supersedes", message)
        self.assertIn(OLD_HASH[:10], message)
        self.assertIn("Only one of them can execute", message)

    def test_no_notice_when_nothing_was_displaced(self):
        message = compose_safe_proposal_message({}, "OG", SAFE_GLOBAL, proposal())

        self.assertNotIn("Supersedes", message)


if __name__ == "__main__":
    unittest.main()
