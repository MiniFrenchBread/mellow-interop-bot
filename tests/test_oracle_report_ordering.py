import asyncio
import os
import sys
import unittest
from types import SimpleNamespace

# Add the src directory to the Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import main
from config.read_config import Config, Deployment, SafeGlobal, SourceConfig
from main import OracleData, SafeProposal, needs_attention
from safe_global.common import PendingTransactionInfo, ThresholdWithOwners
from web3_scripts import OracleValidationResult

SOURCE = SimpleNamespace(name="OG")
SAFE = SafeGlobal(
    safe_address="0x" + "fc" * 20,
    proposer_private_key="",
    api_url="https://api.safe.global/tx-service/0g",
    web_client_url="https://app.safe.global",
    eip_3770="og",
)


def validation(**overrides) -> OracleValidationResult:
    fields = dict(
        oracle_address="0x" + "11" * 20,
        chain_id=16661,
        oracle_value=1,
        actual_value=1,
        remaining_time=10**6,
        recently_updated=False,
        source_nonces=(1, 1),
        target_nonces=(1, 1),
        transfer_in_progress=False,
        almost_expired=False,
        incorrect_value=False,
    )
    fields.update(overrides)
    return OracleValidationResult(**fields)


def result(**overrides):
    return [
        (
            SOURCE,
            OracleData(name="OG", deployment=None, validation=validation(**overrides)),
        )
    ]


def one_proposal():
    """A proposal that will actually be announced, so the send path runs.

    Returning an empty list here instead would let the announcement loop be
    skipped entirely, which is how a send left outside the best-effort wrapper
    went unnoticed by a test that claimed to cover it.
    """
    proposal = SafeProposal(
        method="setValue",
        deployment_names=["OG"],
        calls=[("0x" + "8f" * 20, [1])],
        transaction=PendingTransactionInfo(
            id="multisig_{}_0xabc".format(SAFE.safe_address),
            number_of_required_confirmations=2,
            threshold_with_owners=ThresholdWithOwners(threshold=2, owners=[]),
            confirmations=[],
            missing_confirmations=[],
        ),
        is_newly_created=True,
    )
    return [(SOURCE, SAFE, proposal)]


def base_config() -> Config:
    return Config(
        telegram_bot_api_key="",
        telegram_group_chat_id="",
        telegram_owner_nicknames={},
        telegram_proposal_message_prefix="",
        oracle_expiry_threshold_seconds=172800,
        oracle_recent_update_threshold_seconds=0,
        target_rpc="",
        target_core_helper="",
        sources=[],
    )


def with_telegram() -> Config:
    config = base_config()
    config.telegram_bot_api_key = "token"
    config.telegram_group_chat_id = "-100"
    return config


class TestNeedsAttention(unittest.TestCase):
    """Whether an oracle needs updating is a fact about the chain.

    It must not depend on whether anyone can be told, or the notification ends
    up deciding whether the action happens.
    """

    def test_a_stale_oracle_needs_attention_without_telegram(self):
        self.assertTrue(needs_attention(result(incorrect_value=True)))

    def test_an_expiring_oracle_needs_attention(self):
        self.assertTrue(needs_attention(result(almost_expired=True)))

    def test_a_failed_validation_needs_attention(self):
        results = [(SOURCE, OracleData(name="OG", deployment=None, validation=None))]
        self.assertTrue(needs_attention(results))

    def test_a_healthy_oracle_does_not(self):
        self.assertFalse(needs_attention(result()))

    def test_no_deployments_does_not(self):
        self.assertFalse(needs_attention([]))


class TestTotalValidationOutage(unittest.TestCase):
    """Every oracle failing validation is an outage, not a report.

    Returning normally would reset the scheduler's failure counter and keep the
    alert from ever arming -- for the task whose silence started this.
    """

    def setUp(self):
        self._validate = main.validate_oracles
        self._info = main.print_telegram_info

        async def no_info(*_args):
            return None

        main.print_telegram_info = no_info

    def tearDown(self):
        main.validate_oracles = self._validate
        main.print_telegram_info = self._info

    def test_all_validations_failing_raises(self):
        main.validate_oracles = lambda _config: [
            (SOURCE, OracleData(name="OG", deployment=None, validation=None)),
            (SOURCE, OracleData(name="OG2", deployment=None, validation=None)),
        ]

        with self.assertRaises(Exception) as caught:
            asyncio.run(main.run_oracle_report(base_config()))

        self.assertIn("Could not validate any", str(caught.exception))

    def test_one_validation_failing_does_not_raise(self):
        """One broken chain must not suppress the others."""
        main.validate_oracles = lambda _config: [
            (SOURCE, OracleData(name="OG", deployment=None, validation=None)),
            (
                SOURCE,
                OracleData(
                    name="OG2",
                    deployment=None,
                    validation=validation(incorrect_value=True),
                ),
            ),
        ]
        propose = main.propose_tx_to_update_oracle
        main.propose_tx_to_update_oracle = lambda _results: []
        try:
            asyncio.run(main.run_oracle_report(base_config()))
        finally:
            main.propose_tx_to_update_oracle = propose


class TestTelegramCannotCancelTheProposal(unittest.TestCase):

    def setUp(self):
        self.proposed = []
        self.sends = []
        self._validate = main.validate_oracles
        self._propose = main.propose_tx_to_update_oracle
        self._info = main.print_telegram_info
        self._send = main.send_message

        main.validate_oracles = lambda _config: result(incorrect_value=True)

        def propose(results):
            self.proposed.append(results)
            return one_proposal()

        main.propose_tx_to_update_oracle = propose

        async def failing_info(*_args):
            # The shape print_telegram_info produces when the API is unreachable.
            raise Exception("Unable to get telegram info: httpx.ConnectError")

        async def failing_send(*_args, **_kwargs):
            self.sends.append(1)
            raise Exception("httpx.ConnectError: telegram unreachable")

        main.print_telegram_info = failing_info
        main.send_message = failing_send

    def tearDown(self):
        main.validate_oracles = self._validate
        main.propose_tx_to_update_oracle = self._propose
        main.print_telegram_info = self._info
        main.send_message = self._send

    def test_the_proposal_survives_telegram_being_unreachable(self):
        """The first Telegram call happens before anything else in the report.

        Left fatal, it aborts the run before an oracle is even validated, so the
        proposal that keeps the oracle from expiring never happens.
        """
        asyncio.run(main.run_oracle_report(with_telegram()))

        self.assertEqual(len(self.proposed), 1, "the proposal is the load-bearing act")

    def test_a_failed_announcement_does_not_fail_the_task(self):
        """Failing would make the scheduler retry.

        A retry after the share price moved proposes different calldata, which
        competes for the same Safe nonce instead of being deduplicated.
        """
        delivered = asyncio.run(main.run_oracle_report(with_telegram()))

        self.assertGreater(len(self.sends), 1, "the announcement path must run")
        self.assertFalse(delivered, "and it must report that nobody was told")

    def test_a_status_message_with_no_proposal_is_still_counted(self):
        """When nothing is proposed the status message is the only announcement.

        A transfer in flight, a recent update, or a missing proposer key all
        produce this shape; leaving it out of the tally reported a clean run for
        exactly the outage the tally exists to expose.
        """
        main.propose_tx_to_update_oracle = lambda _results: []

        delivered = asyncio.run(main.run_oracle_report(with_telegram()))

        self.assertGreater(len(self.sends), 0)
        self.assertFalse(delivered)

    def test_a_dry_run_send_is_not_a_delivery_failure(self):
        """Dry-run send_message returns None on success.

        Inferring failure from a missing result turned every dry run into a
        false alarm telling the operator to go and check a healthy token.
        """

        async def dry_run_send(*_args, **_kwargs):
            self.sends.append(1)
            return None

        main.send_message = dry_run_send

        self.assertTrue(asyncio.run(main.run_oracle_report(with_telegram())))

    def test_every_proposal_failing_fails_the_run(self):
        """Nothing reached the service, so the scheduler must see it and retry."""
        proposals = one_proposal()
        proposals[0][2].transaction = None
        main.propose_tx_to_update_oracle = lambda _results: proposals

        with self.assertRaises(Exception) as caught:
            asyncio.run(main.run_oracle_report(with_telegram()))

        self.assertIn("queued no update", str(caught.exception))

    def test_one_failure_among_several_does_not_fail_the_run(self):
        """Retrying would re-propose against the Safe whose proposal succeeded.

        This is what separates `all` from `any`, and a single-proposal fixture
        cannot tell them apart.
        """
        proposals = one_proposal() + one_proposal()
        proposals[0][2].transaction = None

        main.propose_tx_to_update_oracle = lambda _results: proposals

        asyncio.run(main.run_oracle_report(with_telegram()))

    def test_a_posted_but_unconfirmed_proposal_does_not_fail_the_run(self):
        """It is queued and signable; a retry would compete for its nonce."""
        proposals = one_proposal()
        proposals[0][2].transaction = None
        proposals[0][2].posted = True
        main.propose_tx_to_update_oracle = lambda _results: proposals

        asyncio.run(main.run_oracle_report(with_telegram()))

    def test_a_partial_delivery_is_not_reported_as_complete(self):
        """The status message got through and the proposal message did not.

        Counting attempts rather than successes would call that a clean run.
        """
        calls = {"n": 0}

        async def first_ok_then_fail(*_args, **_kwargs):
            calls["n"] += 1
            self.sends.append(1)
            if calls["n"] == 1:
                return SimpleNamespace(message_id=1)
            raise Exception("httpx.ConnectError")

        main.send_message = first_ok_then_fail

        self.assertFalse(asyncio.run(main.run_oracle_report(with_telegram())))

    def test_a_working_telegram_reports_delivery(self):
        async def ok_send(*_args, **_kwargs):
            self.sends.append(1)
            return SimpleNamespace(message_id=1)

        main.send_message = ok_send

        self.assertTrue(asyncio.run(main.run_oracle_report(with_telegram())))


class TestPostedProposalWiring(unittest.TestCase):
    """From the exception through to what the signers are shown.

    Setting `posted` on a fixture skips the two links that matter: the except
    clause that records it, and the message that tells the signers the proposal
    is waiting for them rather than that it failed.
    """

    def setUp(self):
        from safe_global import ProposalPosted

        self.ProposalPosted = ProposalPosted
        self._propose = main.propose_tx_if_needed
        # A proposer key is required or propose_tx_to_update_oracle skips the
        # Safe entirely before it can reach the code under test.
        self.safe = SafeGlobal(
            safe_address=SAFE.safe_address,
            proposer_private_key="0x" + "11" * 32,
            api_url=SAFE.api_url,
            web_client_url=SAFE.web_client_url,
            eip_3770=SAFE.eip_3770,
        )
        # A real SourceConfig: the failure path masks against source.rpc, which
        # the lightweight stand-in used elsewhere does not have.
        self.source = SourceConfig(
            name="OG",
            rpc="https://rpc.invalid",
            source_core_helper="0x" + "11" * 20,
            deployments=(),
        )
        self.deployment = Deployment(
            name="OG",
            source_core="0x" + "22" * 20,
            target_core="0x" + "33" * 20,
            safe_global=self.safe,
        )

    def tearDown(self):
        main.propose_tx_if_needed = self._propose

    def results(self):
        return [
            (
                self.source,
                OracleData(
                    name="OG",
                    deployment=self.deployment,
                    validation=validation(incorrect_value=True),
                ),
            )
        ]

    def test_a_posted_proposal_is_recorded_as_such(self):
        def raise_posted(*_args):
            raise self.ProposalPosted("0xabc", "not visible yet")

        main.propose_tx_if_needed = raise_posted

        proposals = main.propose_tx_to_update_oracle(self.results())

        self.assertEqual(len(proposals), 1)
        self.assertTrue(proposals[0][2].posted)
        self.assertEqual(proposals[0][2].posted_reference, "0xabc")

    def test_an_ordinary_failure_is_not_recorded_as_posted(self):
        def raise_plain(*_args):
            raise Exception("connection refused")

        main.propose_tx_if_needed = raise_plain

        proposals = main.propose_tx_to_update_oracle(self.results())

        self.assertFalse(proposals[0][2].posted)

    def test_the_signers_are_told_it_is_waiting_not_that_it_failed(self):
        proposal = one_proposal()[0][2]
        proposal.transaction = None
        proposal.posted = True
        proposal.posted_reference = "0xabc"

        message = main.compose_safe_proposal_message({}, "OG", SAFE, proposal)

        self.assertNotIn("❌", message)
        self.assertIn("0xabc", message)
        self.assertIn(SAFE.safe_address, message)

    def test_a_real_failure_still_says_so(self):
        proposal = one_proposal()[0][2]
        proposal.transaction = None

        message = main.compose_safe_proposal_message({}, "OG", SAFE, proposal)

        self.assertIn("❌", message)


if __name__ == "__main__":
    unittest.main()
