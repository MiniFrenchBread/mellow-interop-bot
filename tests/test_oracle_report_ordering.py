import asyncio
import os
import sys
import unittest
from types import SimpleNamespace

# Add the src directory to the Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import main
from config.read_config import Config, SafeGlobal
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

    def test_a_working_telegram_reports_delivery(self):
        async def ok_send(*_args, **_kwargs):
            self.sends.append(1)
            return SimpleNamespace(message_id=1)

        main.send_message = ok_send

        self.assertTrue(asyncio.run(main.run_oracle_report(with_telegram())))


if __name__ == "__main__":
    unittest.main()
