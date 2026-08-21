import os
import sys
import unittest
from types import SimpleNamespace

# Add the src directory to the Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import cli
from config.read_config import Config, SafeGlobal, SourceConfig


def config() -> Config:
    source = SourceConfig(
        name="OG",
        rpc="https://rpc.invalid/apikey-SECRET",
        source_core_helper="0x" + "11" * 20,
        deployments=(),
        safe_global=SafeGlobal(
            safe_address="0x" + "fc" * 20,
            proposer_private_key="0x" + "ab" * 32,
            api_url="https://api.safe.global/tx-service/0g",
            web_client_url="https://app.safe.global",
        ),
    )
    return Config(
        telegram_bot_api_key="",
        telegram_group_chat_id="",
        telegram_owner_nicknames={},
        telegram_proposal_message_prefix="",
        oracle_expiry_threshold_seconds=172800,
        oracle_recent_update_threshold_seconds=0,
        target_rpc="",
        target_core_helper="",
        sources=[source],
    )


class TestOracleExitStatus(unittest.TestCase):
    """A person is watching the CLI, so it has to say when nobody was told.

    The scheduler deliberately does not treat this as a failure -- retrying
    would propose again against the same Safe nonce -- so the CLI is the only
    place that surfaces it.
    """

    def setUp(self):
        import main

        self._run = main.run_oracle_report
        self.main = main

    def tearDown(self):
        self.main.run_oracle_report = self._run

    def run_oracle(self, delivered: bool):
        async def report(_config):
            return delivered

        self.main.run_oracle_report = report
        return cli.cmd_oracle(config(), SimpleNamespace())

    def test_full_delivery_is_not_an_error(self):
        self.run_oracle(delivered=True)

    def test_a_failed_notification_raises(self):
        with self.assertRaises(Exception) as caught:
            self.run_oracle(delivered=False)

        self.assertIn("could not notify anyone", str(caught.exception))


class TestErrorMasking(unittest.TestCase):
    """RPC URLs carry API keys and reach exception text verbatim."""

    def setUp(self):
        self._config = cli._loaded_config
        cli._loaded_config = config()

    def tearDown(self):
        cli._loaded_config = self._config

    def test_an_rpc_credential_is_masked(self):
        error = Exception(
            "HTTPSConnectionPool(host='rpc.invalid', port=443): Max retries "
            "exceeded with url: https://rpc.invalid/apikey-SECRET"
        )

        self.assertNotIn("apikey-SECRET", cli._sanitise(error))

    def test_a_private_key_is_masked(self):
        error = Exception("signing failed with 0x" + "ab" * 32)

        self.assertNotIn("ab" * 32, cli._sanitise(error))

    def test_masking_survives_no_config(self):
        cli._loaded_config = None

        self.assertIn("boom", cli._sanitise(Exception("boom")))

    def test_the_lock_held_path_masks_what_it_prints(self):
        """Testing the helper alone leaves the call site free to bypass it."""
        import process_lock
        from web3_scripts import base

        printed = []
        original_print = base.print_colored
        original_load = cli.load
        original_parse = cli.parse_args
        original_acquire = process_lock.ProcessLock.acquire
        base.print_colored = lambda text, color="yellow": printed.append(text)
        cli.print_colored = base.print_colored
        cli.load = lambda: config()
        cli.parse_args = lambda argv=None: SimpleNamespace(
            command="handle-epoch", source=None, no_lock=False
        )

        def refuse(self):
            # The path carries the credential so the assertion has teeth.
            raise cli.LockHeld("https://rpc.invalid/apikey-SECRET", 1)

        process_lock.ProcessLock.acquire = refuse
        try:
            self.assertEqual(cli.main(), 1)
        finally:
            base.print_colored = original_print
            cli.print_colored = original_print
            cli.load = original_load
            cli.parse_args = original_parse
            process_lock.ProcessLock.acquire = original_acquire

        self.assertTrue(printed)
        self.assertNotIn("apikey-SECRET", " ".join(printed))


if __name__ == "__main__":
    unittest.main()
