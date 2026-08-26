import os
import sys
import unittest
from types import SimpleNamespace

# Add the src directory to the Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from config.read_config import Deployment, OracleUpdateConfig, SourceConfig, TxConfig
from web3_scripts import oracle_update
from web3_scripts.oracle_update import (
    SET_VALUE_LABEL,
    exceeds_deviation,
    is_decrease,
    update_oracle,
)
from web3_scripts.oracle_script import OracleValidationResult

ORACLE = "0x" + "8f" * 20
ONE = 10**18
KEY = "0x" + "11" * 32


def validation(**overrides) -> OracleValidationResult:
    fields = dict(
        oracle_address=ORACLE,
        chain_id=16661,
        oracle_value=ONE,
        actual_value=ONE,
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


def source(**overrides) -> SourceConfig:
    fields = dict(
        name="OG",
        rpc="https://rpc.invalid",
        source_core_helper="0x" + "11" * 20,
        deployments=(),
        tx=TxConfig(),
        oracle_update=OracleUpdateConfig(updater_private_key=KEY),
    )
    fields.update(overrides)
    return SourceConfig(**fields)


DEPLOYMENT = Deployment(
    name="OG", source_core="0x" + "22" * 20, target_core="0x" + "33" * 20
)


class Harness:
    """Stands in for every chain call update_oracle makes.

    Balance defaults comfortably above the warning threshold so that a test
    about a guard is not quietly also a test about gas.
    """

    def __init__(self, test, result, balance=10**19):
        self.sends = []
        self.balance = balance
        self._patches = []
        self._patch(test, "run_oracle_validation", lambda **_kwargs: result)
        self._patch(test, "get_w3", lambda _rpc: self._w3())
        self._patch(test, "get_contract", lambda *_a, **_k: self._oracle())
        self._patch(test, "send_and_confirm", self._send)

    def _patch(self, test, name, replacement):
        original = getattr(oracle_update, name)
        setattr(oracle_update, name, replacement)
        test.addCleanup(setattr, oracle_update, name, original)

    def _w3(self):
        return SimpleNamespace(eth=SimpleNamespace(get_balance=lambda _a: self.balance))

    def _oracle(self):
        return SimpleNamespace(
            functions=SimpleNamespace(setValue=lambda value: ("setValue", value))
        )

    def _send(self, contract_function, value, key, **kwargs):
        self.sends.append(
            {
                "call": contract_function,
                "key": key,
                "label": kwargs.get("label"),
                "kwargs": kwargs,
            }
        )
        return SimpleNamespace(tx_hash="0xdeadbeef", receipt=None)

    @property
    def written_values(self):
        return [send["call"][1] for send in self.sends]


class TestGuardArithmetic(unittest.TestCase):
    """The two comparisons, away from any chain access."""

    def test_a_value_on_the_threshold_is_allowed(self):
        # Exactly 1% of 1e18. Allowing the boundary matters because the limit is
        # chosen as "this much movement is plausible", not "this much is wrong".
        self.assertFalse(exceeds_deviation(ONE, ONE + ONE // 100, 100))

    def test_a_value_past_the_threshold_is_refused(self):
        self.assertTrue(exceeds_deviation(ONE, ONE + ONE // 100 + 1, 100))

    def test_a_large_drop_is_refused_by_deviation_too(self):
        self.assertTrue(exceeds_deviation(ONE, ONE // 2, 100))

    def test_an_uninitialised_oracle_is_refused(self):
        """Nothing to measure against, and the first value on a live oracle is
        exactly the one worth a human glance."""
        self.assertTrue(exceeds_deviation(0, ONE, 100))

    def test_a_dip_within_tolerance_is_not_a_decrease(self):
        self.assertFalse(is_decrease(ONE, ONE - 10**9, 10**9))

    def test_a_dip_past_tolerance_is_a_decrease(self):
        self.assertTrue(is_decrease(ONE, ONE - 10**9 - 1, 10**9))

    def test_an_increase_is_never_a_decrease(self):
        self.assertFalse(is_decrease(ONE, ONE + 10**15, 10**9))


class TestHeartbeatWrites(unittest.TestCase):
    def test_an_unchanged_value_is_still_written(self):
        """The write refreshes lastUpdated, which is the whole point.

        Skipping it because the number matches would let the oracle drift to
        expiry during any quiet period, and an expired oracle makes getValue()
        revert -- stopping deposits, withdrawals and rebalancing.
        """
        harness = Harness(self, validation(oracle_value=ONE, actual_value=ONE))

        result = self._run(harness)

        self.assertTrue(result.written)
        self.assertEqual(harness.written_values, [ONE])

    def test_a_normal_increase_is_written(self):
        harness = Harness(
            self, validation(oracle_value=ONE, actual_value=ONE + 6 * 10**13)
        )

        result = self._run(harness)

        self.assertTrue(result.written)
        self.assertEqual(result.tx_hash, "0xdeadbeef")

    def test_the_label_does_not_change_with_the_value(self):
        """A label carrying the value would break the heartbeat after one slow send.

        tx.blocking_transaction treats a differing label as a different
        operation and refuses to reuse the nonce, so a setValue still stuck in
        the mempool would make every later tick raise NonceBlocked instead of
        replacing it. A constant label makes each tick a replacement carrying
        the newer value.
        """
        first = Harness(self, validation(actual_value=ONE + 10**13))
        self._run(first)
        second = Harness(self, validation(actual_value=ONE + 9 * 10**13))
        self._run(second)

        self.assertEqual(first.sends[0]["label"], second.sends[0]["label"])
        self.assertEqual(first.sends[0]["label"], SET_VALUE_LABEL)

    def test_the_chain_transaction_settings_are_passed_through(self):
        harness = Harness(self, validation())

        self._run(harness, src=source(tx=TxConfig(receipt_timeout_seconds=42)))

        self.assertEqual(harness.sends[0]["kwargs"]["receipt_timeout"], 42)

    def test_the_updater_key_signs_not_the_executor_key(self):
        harness = Harness(self, validation())

        self._run(
            harness,
            src=source(executor_private_key="0x" + "22" * 32),
        )

        self.assertEqual(harness.sends[0]["key"], KEY)

    def _run(self, harness, src=None, **kwargs):
        return update_oracle(
            source=src or source(),
            deployment=DEPLOYMENT,
            target_rpc="https://target.invalid",
            target_core_helper="0x" + "44" * 20,
            oracle_expiry_threshold_seconds=172800,
            **kwargs,
        )


class TestGuardsRefuseToWrite(unittest.TestCase):
    """A refusal must leave the chain untouched and say so loudly.

    Nothing reviews these writes any more, so these are what stands between a
    bad reading and the number every deposit and withdrawal is priced against.
    """

    def _run(self, harness, **kwargs):
        return update_oracle(
            source=source(),
            deployment=DEPLOYMENT,
            target_rpc="https://target.invalid",
            target_core_helper="0x" + "44" * 20,
            oracle_expiry_threshold_seconds=172800,
            **kwargs,
        )

    def test_a_big_jump_is_refused(self):
        harness = Harness(self, validation(oracle_value=ONE, actual_value=2 * ONE))

        result = self._run(harness)

        self.assertFalse(result.written)
        self.assertEqual(harness.sends, [], "nothing may reach the chain")
        self.assertIn("refused", result.alert)

    def test_a_helper_returning_zero_is_refused(self):
        harness = Harness(self, validation(oracle_value=ONE, actual_value=0))

        result = self._run(harness)

        self.assertFalse(result.written)
        self.assertEqual(harness.sends, [])

    def test_a_decrease_past_tolerance_is_refused(self):
        harness = Harness(self, validation(oracle_value=ONE, actual_value=ONE - 10**15))

        result = self._run(harness)

        self.assertFalse(result.written)
        self.assertEqual(harness.sends, [])
        self.assertIn("lower the value", result.alert)

    def test_a_decrease_within_tolerance_is_written(self):
        """Three separate reads wobble in the last digits; freezing the oracle
        on rounding noise would be a self-inflicted outage."""
        harness = Harness(self, validation(oracle_value=ONE, actual_value=ONE - 10**8))

        result = self._run(harness)

        self.assertTrue(result.written)

    def test_a_refusal_says_how_long_is_left(self):
        """The third identical alert has to read differently from the first."""
        harness = Harness(self, validation(actual_value=2 * ONE, remaining_time=3600))

        result = self._run(harness)

        self.assertIn("expires in", result.alert)

    def test_an_already_expired_oracle_says_so(self):
        harness = Harness(self, validation(actual_value=2 * ONE, remaining_time=-3600))

        result = self._run(harness)

        self.assertIn("ALREADY EXPIRED", result.alert)

    def test_force_writes_past_both_guards(self):
        harness = Harness(self, validation(oracle_value=ONE, actual_value=2 * ONE))

        result = self._run(harness, force=True)

        self.assertTrue(result.written)
        self.assertEqual(harness.written_values, [2 * ONE])


class TestTransferInFlight(unittest.TestCase):
    """Neither side's value is complete while a message is in flight."""

    def _run(self, harness, **kwargs):
        return update_oracle(
            source=source(),
            deployment=DEPLOYMENT,
            target_rpc="https://target.invalid",
            target_core_helper="0x" + "44" * 20,
            oracle_expiry_threshold_seconds=172800,
            **kwargs,
        )

    def test_nothing_is_written(self):
        harness = Harness(self, validation(transfer_in_progress=True))

        result = self._run(harness)

        self.assertFalse(result.written)
        self.assertEqual(harness.sends, [])
        self.assertTrue(result.skip_reason)

    def test_it_is_a_skip_not_an_alert(self):
        """One transfer is an ordinary few minutes of the day. Announcing each
        would put a message in the group most days for something nobody acts
        on; the scheduler is what notices a run of them."""
        harness = Harness(self, validation(transfer_in_progress=True))

        result = self._run(harness)

        self.assertEqual(result.alerts, [])

    def test_force_does_not_override_it(self):
        """There is no correct value to force: the sum is wrong in one
        direction or the other until the transfer settles."""
        harness = Harness(self, validation(transfer_in_progress=True))

        result = self._run(harness, force=True)

        self.assertFalse(result.written)
        self.assertEqual(harness.sends, [])


class TestGasWarning(unittest.TestCase):
    def _run(self, harness):
        return update_oracle(
            source=source(),
            deployment=DEPLOYMENT,
            target_rpc="https://target.invalid",
            target_core_helper="0x" + "44" * 20,
            oracle_expiry_threshold_seconds=172800,
        )

    def test_a_low_balance_warns_but_still_writes(self):
        """The point is to be told while there is still runway, not to stop the
        heartbeat and create the outage the warning is about."""
        harness = Harness(self, validation(), balance=1)

        result = self._run(harness)

        self.assertTrue(result.written)
        self.assertIn("low on gas", result.alert)

    def test_a_healthy_balance_is_silent(self):
        harness = Harness(self, validation(), balance=10**19)

        result = self._run(harness)

        self.assertEqual(result.alerts, [])


class TestDryRun(unittest.TestCase):
    def test_nothing_is_broadcast(self):
        harness = Harness(self, validation(actual_value=ONE + 10**13))

        result = update_oracle(
            source=source(),
            deployment=DEPLOYMENT,
            target_rpc="https://target.invalid",
            target_core_helper="0x" + "44" * 20,
            oracle_expiry_threshold_seconds=172800,
            dry_run=True,
        )

        self.assertFalse(result.written)
        self.assertEqual(harness.sends, [])
        self.assertEqual(result.new_value, ONE + 10**13)


class TestMissingKey(unittest.TestCase):
    def test_no_key_raises_rather_than_skipping(self):
        """Skipping would report success forever while writing nothing, and an
        oracle that is quietly never written looks exactly like one that does
        not need writing."""
        Harness(self, validation())

        with self.assertRaises(Exception) as caught:
            update_oracle(
                source=source(oracle_update=None),
                deployment=DEPLOYMENT,
                target_rpc="https://target.invalid",
                target_core_helper="0x" + "44" * 20,
                oracle_expiry_threshold_seconds=172800,
            )

        self.assertIn("ORACLE_UPDATER_PK", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
