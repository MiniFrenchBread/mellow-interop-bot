"""Guards against settings that exist in config but never reach a transaction.

Per-chain timeouts were added because a single budget cannot serve both chains,
then not passed to the rebalancing path -- so the value that mattered most, the
long target-chain budget, was configured and ignored. These tests fail if that
happens again.
"""

import dataclasses
import os
import sys
import unittest

# Add the src directory to the Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from eth_account import Account

from config.read_config import Config, Deployment, SourceConfig, TxConfig
from web3_scripts import operator_bot

SOURCE_KEY = "0x" + "11" * 32
GLOBAL_KEY = "0x" + "22" * 32


class FakeEth:
    chain_id = 16661


class FakeW3:
    eth = FakeEth()


def make_config(executor_key=SOURCE_KEY) -> Config:
    source = SourceConfig(
        name="OG",
        rpc="https://source.invalid",
        source_core_helper="0x" + "11" * 20,
        deployments=(
            Deployment(
                name="OG",
                source_core="0x" + "22" * 20,
                target_core="0x" + "33" * 20,
            ),
        ),
        executor_private_key=executor_key,
        tx=TxConfig(receipt_timeout_seconds=60, max_attempts=3, fee_cap_gwei=4),
    )
    return Config(
        telegram_bot_api_key="",
        telegram_group_chat_id="",
        telegram_owner_nicknames={},
        telegram_proposal_message_prefix="",
        oracle_expiry_threshold_seconds=3600,
        oracle_recent_update_threshold_seconds=0,
        target_rpc="https://target.invalid",
        target_core_helper="0x" + "44" * 20,
        sources=[source],
        target_tx=TxConfig(receipt_timeout_seconds=600, max_attempts=4, fee_cap_gwei=9),
    )


class TestTxConfigAsKwargs(unittest.TestCase):

    def test_every_field_is_exported(self):
        """A new setting must not be able to hide from the callers."""
        exported = TxConfig().as_kwargs()
        fields = {f.name for f in dataclasses.fields(TxConfig)}
        self.assertEqual(len(exported), len(fields))

    def test_keys_match_execute_parameters(self):
        import inspect

        from web3_scripts.base import execute

        parameters = set(inspect.signature(execute).parameters)
        for key in TxConfig().as_kwargs():
            self.assertIn(key, parameters, "execute() cannot accept '%s'" % key)


class TestRebalanceWiring(unittest.TestCase):

    def setUp(self):
        self.captured = {}

        def fake_run(**kwargs):
            self.captured = kwargs
            return None

        self._original_run = operator_bot.run
        self._original_get_w3 = operator_bot.get_w3
        operator_bot.run = fake_run
        operator_bot.get_w3 = lambda _rpc: FakeW3()
        os.environ["DEPLOYMENTS"] = "OG:OG"
        os.environ["NON_INTERACTIVE"] = "true"

    def tearDown(self):
        operator_bot.run = self._original_run
        operator_bot.get_w3 = self._original_get_w3
        os.environ.pop("DEPLOYMENTS", None)
        os.environ.pop("NON_INTERACTIVE", None)

    def test_target_chain_settings_reach_the_rebalance(self):
        operator_bot.run_all(make_config(), operator_pk=GLOBAL_KEY, interactive=False)

        self.assertEqual(self.captured["target_tx"]["receipt_timeout"], 600)
        self.assertEqual(self.captured["target_tx"]["max_attempts"], 4)
        self.assertEqual(self.captured["target_tx"]["fee_cap_gwei"], 9)

    def test_source_chain_settings_reach_the_rebalance(self):
        operator_bot.run_all(make_config(), operator_pk=GLOBAL_KEY, interactive=False)

        self.assertEqual(self.captured["source_tx"]["receipt_timeout"], 60)
        self.assertEqual(self.captured["source_tx"]["fee_cap_gwei"], 4)

    def test_the_two_chains_do_not_share_one_budget(self):
        operator_bot.run_all(make_config(), operator_pk=GLOBAL_KEY, interactive=False)

        self.assertNotEqual(
            self.captured["source_tx"]["receipt_timeout"],
            self.captured["target_tx"]["receipt_timeout"],
        )

    def test_the_source_executor_key_signs(self):
        operator_bot.run_all(make_config(), operator_pk=GLOBAL_KEY, interactive=False)

        self.assertEqual(self.captured["operator_pk"], SOURCE_KEY)

    def test_falls_back_to_the_global_key_when_none_is_configured(self):
        operator_bot.run_all(
            make_config(executor_key=None), operator_pk=GLOBAL_KEY, interactive=False
        )

        self.assertEqual(self.captured["operator_pk"], GLOBAL_KEY)

    def test_a_configured_key_is_a_real_account(self):
        operator_bot.run_all(make_config(), operator_pk=GLOBAL_KEY, interactive=False)

        self.assertEqual(
            Account.from_key(self.captured["operator_pk"]).address,
            Account.from_key(SOURCE_KEY).address,
        )


if __name__ == "__main__":
    unittest.main()
