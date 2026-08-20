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

        def fake_fetch(api_url, api_key, safe_address, safe_nonce, to):
            self.calls.append(safe_nonce)
            return self.queue

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

    def test_identical_calldata_is_reuse_not_supersession(self):
        self.queue = [queued(11, "0xNEW", OLD_HASH)]
        self.assertEqual(self.find(), [])

    def test_a_later_nonce_is_queued_behind_not_competing(self):
        self.queue = [queued(12, "0xOLD", OLD_HASH)]
        self.assertEqual(self.find(), [])

    def test_a_different_target_is_ignored(self):
        self.queue = [queued(11, "0xOLD", OLD_HASH, to=SAFE_ADDRESS)]
        self.assertEqual(self.find(), [])

    def test_target_casing_does_not_matter(self):
        self.queue = [queued(11, "0xOLD", OLD_HASH, to=ORACLE.lower())]
        self.assertEqual([t["safeTxHash"] for t in self.find()], [OLD_HASH])

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
