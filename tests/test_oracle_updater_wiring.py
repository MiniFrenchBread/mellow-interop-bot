"""How the bot reaches the OracleUpdater: its address from config, and what a
revert from it looks like to whoever reads the alert."""

import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from web3 import Web3
from web3.exceptions import ContractCustomError

from config import read_config
from web3_scripts import oracle_update
from web3_scripts.oracle_update import send_within_band
from web3_scripts.tx import TxReverted, decode_custom_error, estimate_gas

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.json")
ABI_PATH = os.path.join(os.path.dirname(__file__), "..", "abi", "OracleUpdater.json")
MAINNET_UPDATER = "0xa57e348Afb83153cE2aB27210FFB35c9860B3A42"
ONE = 10**18


def _abi():
    with open(ABI_PATH) as f:
        return json.load(f)


def _revert_data(signature: str, *words: int) -> str:
    selector = Web3.keccak(text=signature).hex().removeprefix("0x")[:8]
    return "0x" + selector + "".join(w.to_bytes(32, "big").hex() for w in words)


ABOVE_NAV = "OracleUpdaterValueAboveNav(uint256,uint256)"
DECREASE = "OracleUpdaterValueDecrease(uint256,uint256)"


class TestConfig(unittest.TestCase):
    def test_the_mainnet_deployment_names_its_updater(self):
        config = read_config(CONFIG_PATH)
        deployments = [d for s in config.sources for d in s.deployments]
        self.assertEqual(
            [d.oracle_updater for d in deployments if d.name == "OG"],
            [MAINNET_UPDATER],
        )

    def test_a_lowercase_address_is_checksummed(self):
        with open(CONFIG_PATH) as f:
            raw = f.read().replace(MAINNET_UPDATER, MAINNET_UPDATER.lower())
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(raw)
        self.addCleanup(os.unlink, f.name)

        config = read_config(f.name)
        deployments = [d for s in config.sources for d in s.deployments]
        self.assertIn(MAINNET_UPDATER, [d.oracle_updater for d in deployments])


class TestCustomErrors(unittest.TestCase):
    def test_decoded_by_name_and_argument(self):
        self.assertEqual(
            decode_custom_error(_abi(), _revert_data(ABOVE_NAV, 5, 7)),
            ("OracleUpdaterValueAboveNav", {"maxValue": 5, "value": 7}),
        )

    def test_unknown_data_is_left_alone(self):
        self.assertIsNone(decode_custom_error(_abi(), "0xdeadbeef"))
        self.assertIsNone(decode_custom_error(_abi(), None))

    def test_estimate_gas_names_the_error(self):
        data = _revert_data(DECREASE, ONE, 1)

        def raise_custom(_tx):
            raise ContractCustomError(data, data=data)

        function = SimpleNamespace(contract_abi=_abi(), estimate_gas=raise_custom)

        with self.assertRaises(TxReverted) as caught:
            estimate_gas(function, "0x" + "00" * 20, 0)

        self.assertIn("OracleUpdaterValueDecrease(current=", str(caught.exception))
        self.assertEqual(caught.exception.custom_error[0], "OracleUpdaterValueDecrease")


class TestSendWithinBand(unittest.TestCase):
    """The NAV can fall between reading `maxValue()` and the send landing."""

    def setUp(self):
        self.sent = []
        self.failures = []
        original = oracle_update.send_and_confirm
        self.addCleanup(setattr, oracle_update, "send_and_confirm", original)
        oracle_update.send_and_confirm = self._send
        self.updater = SimpleNamespace(
            functions=SimpleNamespace(setValue=lambda value: ("setValue", value))
        )

    def _send(self, call, _value, _key, **_kwargs):
        self.sent.append(call[1])
        if self.failures:
            raise self.failures.pop(0)
        return SimpleNamespace(tx_hash="0xabc")

    def _run(self, value, old=ONE, new=None):
        return send_within_band(
            self.updater, value, old, new or value, 10**9, "0x" + "11" * 32, None, {}
        )

    def test_retried_once_at_the_reported_nav(self):
        self.failures = [
            TxReverted(
                "above",
                custom_error=(
                    "OracleUpdaterValueAboveNav",
                    {"maxValue": ONE + 5, "value": ONE + 9},
                ),
            )
        ]
        value, outcome = self._run(ONE + 9)
        self.assertEqual(self.sent, [ONE + 9, ONE + 5])
        self.assertEqual(value, ONE + 5)

    def test_a_nav_below_the_oracle_is_not_retried(self):
        self.failures = [
            TxReverted(
                "above",
                custom_error=(
                    "OracleUpdaterValueAboveNav",
                    {"maxValue": ONE - 1, "value": ONE + 9},
                ),
            )
        ]
        with self.assertRaises(TxReverted):
            self._run(ONE + 9)
        self.assertEqual(self.sent, [ONE + 9])

    def test_other_reverts_are_raised(self):
        self.failures = [TxReverted("Oracle: forbidden")]
        with self.assertRaises(TxReverted):
            self._run(ONE)
        self.assertEqual(self.sent, [ONE])


if __name__ == "__main__":
    unittest.main()
