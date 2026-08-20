import asyncio
import os
import sys
import unittest

# Add the src directory to the Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import main
from config.read_config import Config
from main import OracleData, needs_attention
from web3_scripts import OracleValidationResult


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
            object(),
            OracleData(name="OG", deployment=None, validation=validation(**overrides)),
        )
    ]


def config_without_telegram() -> Config:
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
        results = [(object(), OracleData(name="OG", deployment=None, validation=None))]
        self.assertTrue(needs_attention(results))

    def test_a_healthy_oracle_does_not(self):
        self.assertFalse(needs_attention(result()))

    def test_no_deployments_does_not(self):
        self.assertFalse(needs_attention([]))


class TestProposalIsNotGatedOnTelegram(unittest.TestCase):

    def setUp(self):
        self.proposed = []
        self._validate = main.validate_oracles
        self._propose = main.propose_tx_to_update_oracle
        self._info = main.print_telegram_info
        main.validate_oracles = lambda _config: result(incorrect_value=True)
        main.propose_tx_to_update_oracle = (
            lambda results: self.proposed.append(results) or []
        )

        async def no_info(*_args):
            return None

        main.print_telegram_info = no_info

    def tearDown(self):
        main.validate_oracles = self._validate
        main.propose_tx_to_update_oracle = self._propose
        main.print_telegram_info = self._info

    def test_the_safe_proposal_still_happens_with_telegram_unconfigured(self):
        asyncio.run(main.run_oracle_report(config_without_telegram()))

        self.assertEqual(len(self.proposed), 1, "the proposal is the load-bearing act")


if __name__ == "__main__":
    unittest.main()
