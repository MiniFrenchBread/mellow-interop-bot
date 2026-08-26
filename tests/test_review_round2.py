"""Regressions for the second review round.

Each of these binds a specific way the code told an operator something untrue,
or dropped information they needed to act on.
"""

import asyncio
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import cli
import main
from config.read_config import (
    Config,
    Deployment,
    OracleUpdateConfig,
    SafeGlobal,
    SourceConfig,
    TxConfig,
)
from web3_scripts import deviation_bps, signed_deviation_bps
from web3_scripts.oracle_script import OracleValidationResult

ONE = 10**18
ORACLE = "0x" + "8f" * 20


def validation(**overrides) -> OracleValidationResult:
    fields = dict(
        oracle_address=ORACLE,
        chain_id=16661,
        oracle_value=ONE,
        actual_value=ONE + 10**15,
        remaining_time=10**6,
        recently_updated=False,
        source_nonces=(1, 1),
        target_nonces=(1, 1),
        transfer_in_progress=False,
        almost_expired=False,
        incorrect_value=True,
    )
    fields.update(overrides)
    return OracleValidationResult(**fields)


SAFE = SafeGlobal(
    safe_address="0x" + "fc" * 20,
    proposer_private_key="0x" + "11" * 32,
    api_url="https://api.safe.global/tx-service/0g",
    web_client_url="https://app.safe.global",
    eip_3770="og",
)


def source(name="OG", **overrides) -> SourceConfig:
    fields = dict(
        name=name,
        rpc="https://rpc.invalid",
        source_core_helper="0x" + "11" * 20,
        deployments=(
            Deployment(
                name=name,
                source_core="0x" + "22" * 20,
                target_core="0x" + "33" * 20,
                safe_global=SAFE,
            ),
        ),
        safe_global=SAFE,
        tx=TxConfig(),
        oracle_update=OracleUpdateConfig(updater_private_key="0x" + "11" * 32),
    )
    fields.update(overrides)
    return SourceConfig(**fields)


def config(*sources) -> Config:
    return Config(
        telegram_bot_api_key="",
        telegram_group_chat_id="",
        telegram_owner_nicknames={},
        telegram_proposal_message_prefix="",
        oracle_expiry_threshold_seconds=172800,
        oracle_recent_update_threshold_seconds=0,
        target_rpc="",
        target_core_helper="",
        sources=list(sources) or [source()],
    )


class TestTheFailureAlertKeepsItsCause(unittest.TestCase):
    """A generic wrapper threw away both the type and the text.

    The alert is the only description of a failure that reaches a phone, and
    stdout is exactly what an operator does not have. "RPC unreachable",
    "Oracle: forbidden" and "reverted on chain" call for different responses.
    """

    def setUp(self):
        self._update = main.update_oracles

    def tearDown(self):
        main.update_oracles = self._update

    def _fails_with(self, *errors):
        results = [(source(), None) for _ in errors]
        main.update_oracles = lambda _c, **_k: (results, list(errors))

    def test_a_lone_failure_is_re_raised_unchanged(self):
        """The same object, not a copy: a caller may branch on its type."""
        original = ValueError("execution reverted: Oracle: forbidden")
        self._fails_with(original)

        with self.assertRaises(ValueError) as caught:
            asyncio.run(main.run_oracle_update(config()))

        self.assertIs(caught.exception, original)

    def test_the_cause_survives_into_the_message(self):
        self._fails_with(Exception("execution reverted: Oracle: forbidden"))

        with self.assertRaises(Exception) as caught:
            asyncio.run(main.run_oracle_update(config()))

        self.assertIn("Oracle: forbidden", str(caught.exception))

    def test_several_failures_are_all_named(self):
        self._fails_with(Exception("rpc down"), Exception("nonce too low"))

        with self.assertRaises(Exception) as caught:
            asyncio.run(main.run_oracle_update(config()))

        self.assertIn("rpc down", str(caught.exception))
        self.assertIn("nonce too low", str(caught.exception))


class TestSourceScoping(unittest.TestCase):
    """`--source` was parsed and discarded on exactly the two commands where a
    silently widened scope is dangerous: --force writes past both guards, and
    --value puts one hand-computed figure into every deployment."""

    def setUp(self):
        self._validate = main.validate_oracles
        self._update = main.update_oracle
        self.touched = []

        def update(source, deployment, **_kwargs):
            self.touched.append(source.name)
            return SimpleNamespace(
                name=deployment.name,
                written=True,
                alerts=[],
                skip_reason="",
                new_value=ONE,
            )

        main.update_oracle = update

    def tearDown(self):
        main.validate_oracles = self._validate
        main.update_oracle = self._update

    def test_the_heartbeat_honours_it(self):
        asyncio.run(
            main.run_oracle_update(
                config(source("OG"), source("OTHER")), source_name="OG"
            )
        )

        self.assertEqual(self.touched, ["OG"])

    def test_without_it_every_source_is_touched(self):
        asyncio.run(main.run_oracle_update(config(source("OG"), source("OTHER"))))

        self.assertEqual(self.touched, ["OG", "OTHER"])

    def test_an_unknown_name_is_refused_rather_than_ignored(self):
        with self.assertRaises(Exception) as caught:
            asyncio.run(main.run_oracle_update(config(), source_name="NOPE"))

        self.assertIn("Unknown source", str(caught.exception))

    def test_propose_honours_it_too(self):
        seen = []
        main.validate_oracles = self._validate
        planned = main.plan_oracle_proposals

        cfg = config(source("OG"), source("OTHER"))
        main.validate_oracles = lambda c, source_name=None: [
            (
                s,
                main.OracleData(
                    name=s.name, deployment=s.deployments[0], validation=validation()
                ),
            )
            for s in main.selected_sources(c, source_name)
        ]

        def plan(results, **kwargs):
            seen.extend(src.name for src, _ in results)
            return planned(results, **kwargs)

        main.plan_oracle_proposals = plan
        try:
            asyncio.run(main.run_oracle_propose(cfg, dry_run=True, source_name="OTHER"))
        finally:
            main.plan_oracle_proposals = planned

        self.assertEqual(seen, ["OTHER"])


class TestEmptyPlanNamesTheRealReason(unittest.TestCase):
    """The message always blamed Safe configuration. On the recovery path the
    likeliest reason is a transfer in flight, whose remedy is --value."""

    def setUp(self):
        self._validate = main.validate_oracles

    def tearDown(self):
        main.validate_oracles = self._validate

    def _validated(self, **overrides):
        src = source()
        main.validate_oracles = lambda _c, **_k: [
            (
                src,
                main.OracleData(
                    name="OG",
                    deployment=src.deployments[0],
                    validation=validation(**overrides),
                ),
            )
        ]

    def test_a_transfer_in_flight_says_so_and_points_at_the_remedy(self):
        self._validated(transfer_in_progress=True)

        with self.assertRaises(Exception) as caught:
            asyncio.run(main.run_oracle_propose(config()))

        message = str(caught.exception)
        self.assertIn("transfer is in flight", message)
        self.assertIn("--value", message)
        self.assertNotIn("proposer key", message)

    def test_a_missing_proposer_key_still_says_that(self):
        keyless = SafeGlobal(
            safe_address=SAFE.safe_address,
            proposer_private_key="",
            api_url=SAFE.api_url,
            web_client_url=SAFE.web_client_url,
            eip_3770=SAFE.eip_3770,
        )
        src = source(
            deployments=(
                Deployment(
                    name="OG",
                    source_core="0x" + "22" * 20,
                    target_core="0x" + "33" * 20,
                    safe_global=keyless,
                ),
            )
        )
        main.validate_oracles = lambda _c, **_k: [
            (
                src,
                main.OracleData(
                    name="OG", deployment=src.deployments[0], validation=validation()
                ),
            )
        ]

        with self.assertRaises(Exception) as caught:
            asyncio.run(main.run_oracle_propose(config()))

        self.assertIn("no proposer key", str(caught.exception))


class TestForcedWritesAreVisible(unittest.TestCase):
    def test_a_drop_is_reported_as_a_drop(self):
        """The one line an operator reads back after overriding the decrease
        guard used to render the fall as a rise."""
        self.assertEqual(deviation_bps(ONE, ONE - 5 * 10**14), 5.0)
        self.assertEqual(signed_deviation_bps(ONE, ONE - 5 * 10**14), -5.0)

    def test_a_rise_keeps_its_sign(self):
        self.assertEqual(signed_deviation_bps(ONE, ONE + 5 * 10**14), 5.0)

    def test_the_guards_still_use_the_magnitude(self):
        """Signing the value used by exceeds_deviation would let any fall
        through, since a negative can never exceed a positive threshold."""
        from web3_scripts import exceeds_deviation

        self.assertTrue(exceeds_deviation(ONE, ONE // 2, 100))

    def test_a_forced_result_is_marked(self):
        from web3_scripts import OracleUpdateResult

        forced = OracleUpdateResult(name="OG", forced=True)
        routine = OracleUpdateResult(name="OG", forced=False)

        self.assertIn("forced", forced.forced_note)
        self.assertEqual(routine.forced_note, "")


class TestRefusalIsNotSilentWithoutTelegram(unittest.TestCase):
    """Rendering nothing turned a run where every deployment refused into
    "refreshed: none written", which the scheduler recorded as a success."""

    def setUp(self):
        self._update = main.update_oracles

    def tearDown(self):
        main.update_oracles = self._update

    def test_a_refusal_reports_that_nobody_was_told(self):
        refused = SimpleNamespace(
            name="OG",
            written=False,
            alerts=["refused: would move the value by 900.00 bps"],
            skip_reason="",
            new_value=ONE,
            alert="refused: would move the value by 900.00 bps",
        )
        main.update_oracles = lambda _c, **_k: ([(source(), refused)], [])

        summary = asyncio.run(main.run_oracle_update(config()))

        self.assertFalse(
            summary.notified, "a scheduler must not record this as a clean run"
        )

    def test_an_ordinary_run_is_still_quiet(self):
        written = SimpleNamespace(
            name="OG",
            written=True,
            alerts=[],
            skip_reason="",
            new_value=ONE,
            alert="",
        )
        main.update_oracles = lambda _c, **_k: ([(source(), written)], [])

        summary = asyncio.run(main.run_oracle_update(config()))

        self.assertTrue(summary.notified)


class TestDryRunBroadcastsNothing(unittest.TestCase):
    """Including to Telegram.

    A guard refusal returns before the dry-run check inside update_oracle, so
    the alert was composed and really sent -- naming the refusal, @ing the
    signers, and telling the reader to run the very command that re-sends it.
    README promises this broadcasts nothing.
    """

    def setUp(self):
        self._update = main.update_oracles
        self._send = main.send_message
        self.sent = []

        async def send(_key, _chat, text, **_kwargs):
            self.sent.append(text)
            return SimpleNamespace(message_id=1)

        main.send_message = send

        refused = SimpleNamespace(
            name="OG",
            written=False,
            alerts=["refused: would move the value by 10000.00 bps"],
            alert="refused: would move the value by 10000.00 bps",
            skip_reason="",
            new_value=ONE,
        )
        main.update_oracles = lambda _c, **_k: ([(source(), refused)], [])

    def tearDown(self):
        main.update_oracles = self._update
        main.send_message = self._send

    def _config_with_telegram(self):
        cfg = config()
        cfg.telegram_bot_api_key = "token"
        cfg.telegram_group_chat_id = "-100"
        cfg.telegram_owner_nicknames = {"alice": "0x" + "aa" * 20}
        return cfg

    def test_a_dry_run_sends_nothing(self):
        asyncio.run(main.run_oracle_update(self._config_with_telegram(), dry_run=True))

        self.assertEqual(self.sent, [])

    def test_a_real_run_still_sends(self):
        asyncio.run(main.run_oracle_update(self._config_with_telegram()))

        self.assertEqual(len(self.sent), 1)


class TestAFailedDeploymentIsNotACleanRun(unittest.TestCase):
    """With Telegram unconfigured, a failure used to report as success.

    compose_oracle_update_message renders a line for a failed deployment, but
    the check that decides whether anyone needs telling only looked at alerts.
    """

    def setUp(self):
        self._update = main.update_oracles

    def tearDown(self):
        main.update_oracles = self._update

    def test_a_failure_alongside_a_success_is_not_clean(self):
        written = SimpleNamespace(
            name="OG", written=True, alerts=[], alert="", skip_reason="", new_value=ONE
        )
        main.update_oracles = lambda _c, **_k: (
            [(source(), None), (source(), written)],
            [Exception("rpc down")],
        )

        summary = asyncio.run(main.run_oracle_update(config()))

        self.assertFalse(summary.notified)


if __name__ == "__main__":
    unittest.main()
