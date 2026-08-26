import asyncio
import os
import sys
import unittest
from types import SimpleNamespace

# Add the src directory to the Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import cli
import main
from config.read_config import Config, Deployment, SafeGlobal, SourceConfig
from main import OracleData
from safe_global.common import PendingTransactionInfo, ThresholdWithOwners
from web3_scripts import OracleValidationResult

ORACLE = "0x" + "8f" * 20
ONE = 10**18

SAFE = SafeGlobal(
    safe_address="0x" + "fc" * 20,
    proposer_private_key="0x" + "11" * 32,
    api_url="https://api.safe.global/tx-service/0g",
    web_client_url="https://app.safe.global",
    eip_3770="og",
)

SOURCE = SourceConfig(
    name="OG",
    rpc="https://rpc.invalid",
    source_core_helper="0x" + "11" * 20,
    deployments=(),
    safe_global=SAFE,
)

DEPLOYMENT = Deployment(
    name="OG",
    source_core="0x" + "22" * 20,
    target_core="0x" + "33" * 20,
    safe_global=SAFE,
)


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
        incorrect_value=False,
    )
    fields.update(overrides)
    return OracleValidationResult(**fields)


def queued() -> PendingTransactionInfo:
    """What propose_tx_if_needed returns once the Safe service has the proposal.

    A None transaction would trip the "nothing reached the service" check and
    make every test here fail for a reason none of them is about.
    """
    return PendingTransactionInfo(
        id="multisig_{}_0xabc".format(SAFE.safe_address),
        number_of_required_confirmations=2,
        threshold_with_owners=ThresholdWithOwners(threshold=2, owners=[]),
        confirmations=[],
        missing_confirmations=[],
    )


def config() -> Config:
    return Config(
        telegram_bot_api_key="",
        telegram_group_chat_id="",
        telegram_owner_nicknames={},
        telegram_proposal_message_prefix="",
        oracle_expiry_threshold_seconds=172800,
        oracle_recent_update_threshold_seconds=0,
        target_rpc="",
        target_core_helper="",
        sources=[SOURCE],
    )


class TestTheRecoveryPathNeedsNoLock(unittest.TestCase):
    """oracle-propose must be usable while the scheduler is running.

    It signs the Safe transaction off chain and posts it to the Safe service, so
    it broadcasts nothing and takes no nonce -- there is nothing for the lock to
    protect against. That matters beyond tidiness: this is what someone reaches
    for when the heartbeat has refused to write and the oracle is heading for
    expiry, and requiring the lock would mean stopping the rest of the bot to
    use it.
    """

    def test_oracle_propose_is_lock_free(self):
        self.assertIn("oracle-propose", cli.LOCK_FREE)

    def test_the_heartbeat_command_still_takes_the_lock(self):
        """It broadcasts, so it can collide with a scheduler mid-cycle."""
        self.assertNotIn("oracle", cli.LOCK_FREE)

    def test_the_parser_registers_the_command(self):
        args = cli.parse_args(["oracle-propose"])

        self.assertEqual(args.command, "oracle-propose")
        self.assertIsNone(args.value)
        self.assertFalse(args.dry_run)

    def test_a_lock_free_command_does_not_reach_the_lock(self):
        """Binds the wiring, not just the constant: `main` consults LOCK_FREE to
        decide, and reading it wrongly would still demand the lock."""
        taken = []

        class Boom:
            def __enter__(self_inner):
                taken.append(1)
                raise AssertionError("oracle-propose must not take the lock")

            def __exit__(self_inner, *_args):
                return False

        original_lock = cli.ProcessLock
        original_load = cli.load
        original_commands = dict(cli.COMMANDS)
        cli.ProcessLock = lambda _path: Boom()
        cli.load = config
        cli.COMMANDS["oracle-propose"] = lambda _config, _args: None
        sys.argv = ["cli", "oracle-propose"]
        try:
            self.assertEqual(cli.main(), 0)
        finally:
            cli.ProcessLock = original_lock
            cli.load = original_load
            cli.COMMANDS.clear()
            cli.COMMANDS.update(original_commands)

        self.assertEqual(taken, [])


class TestValueOverride(unittest.TestCase):
    """`--value` exists for the case the guard fired on.

    When a guard refuses because the computed value cannot be trusted,
    proposing that same computed value to the signers would just launder the bad
    reading through the multisig.
    """

    def setUp(self):
        self._validate = main.validate_oracles
        self._propose = main.propose_tx_if_needed
        self.proposed = []

        main.validate_oracles = lambda _config: [
            (
                SOURCE,
                OracleData(name="OG", deployment=DEPLOYMENT, validation=validation()),
            )
        ]

        def propose(contract, method, calls, source, safe):
            self.proposed.append(calls)
            return (queued(), True, [])

        main.propose_tx_if_needed = propose

    def tearDown(self):
        main.validate_oracles = self._validate
        main.propose_tx_if_needed = self._propose

    def test_an_explicit_value_replaces_the_computed_one(self):
        asyncio.run(main.run_oracle_propose(config(), value_override=12345))

        self.assertEqual(self.proposed, [[(ORACLE, [12345])]])

    def test_without_an_override_the_computed_value_is_used(self):
        asyncio.run(main.run_oracle_propose(config()))

        self.assertEqual(self.proposed, [[(ORACLE, [ONE + 10**15])]])

    def test_a_healthy_oracle_is_still_proposed_when_asked(self):
        """On a schedule this was filtered out as "nothing to do". Invoked by
        hand the request itself is the reason to act -- and the value the
        operator wants to restore may well look healthy to the heuristics."""
        main.validate_oracles = lambda _config: [
            (
                SOURCE,
                OracleData(
                    name="OG",
                    deployment=DEPLOYMENT,
                    validation=validation(actual_value=ONE),
                ),
            )
        ]

        asyncio.run(main.run_oracle_propose(config()))

        self.assertEqual(self.proposed, [[(ORACLE, [ONE])]])

    def test_an_explicit_value_is_proposed_even_mid_transfer(self):
        """The reason to wait is that the computed sum is wrong while a message
        is in flight. A hand-supplied number was not derived from it."""
        main.validate_oracles = lambda _config: [
            (
                SOURCE,
                OracleData(
                    name="OG",
                    deployment=DEPLOYMENT,
                    validation=validation(transfer_in_progress=True),
                ),
            )
        ]

        asyncio.run(main.run_oracle_propose(config(), value_override=999))

        self.assertEqual(self.proposed, [[(ORACLE, [999])]])

    def test_a_computed_value_is_not_proposed_mid_transfer(self):
        main.validate_oracles = lambda _config: [
            (
                SOURCE,
                OracleData(
                    name="OG",
                    deployment=DEPLOYMENT,
                    validation=validation(transfer_in_progress=True),
                ),
            )
        ]

        with self.assertRaises(Exception):
            asyncio.run(main.run_oracle_propose(config()))

        self.assertEqual(self.proposed, [])


class TestProposeDryRun(unittest.TestCase):
    def setUp(self):
        self._validate = main.validate_oracles
        self._propose = main.propose_tx_if_needed
        self.proposed = []
        main.validate_oracles = lambda _config: [
            (
                SOURCE,
                OracleData(name="OG", deployment=DEPLOYMENT, validation=validation()),
            )
        ]

        def propose(*_args):
            self.proposed.append(1)
            return (queued(), True, [])

        main.propose_tx_if_needed = propose

    def tearDown(self):
        main.validate_oracles = self._validate
        main.propose_tx_if_needed = self._propose

    def test_nothing_is_posted_to_the_safe(self):
        self.assertTrue(asyncio.run(main.run_oracle_propose(config(), dry_run=True)))

        self.assertEqual(self.proposed, [])


if __name__ == "__main__":
    unittest.main()


class TestDryRunMatchesTheRealRun(unittest.TestCase):
    """A preview that only tells the truth when nothing is wrong is worse than none.

    The dry run used to print a line for every deployment it had validated --
    including ones with no Safe, no proposer key, or a transfer in flight -- and
    exit zero, while the same command without --dry-run skipped all of them and
    failed. Both now go through the same planning.
    """

    def setUp(self):
        self._validate = main.validate_oracles
        self._propose = main.propose_tx_if_needed
        self.posted = []

        def propose(*_args):
            self.posted.append(1)
            return (queued(), True, [])

        main.propose_tx_if_needed = propose

    def tearDown(self):
        main.validate_oracles = self._validate
        main.propose_tx_if_needed = self._propose

    def _results(self, source, deployment, **overrides):
        main.validate_oracles = lambda _config: [
            (
                source,
                OracleData(
                    name="OG", deployment=deployment, validation=validation(**overrides)
                ),
            )
        ]

    def _plan_and_real_agree(self, expect_proposable):
        """Whether the dry run succeeds must match whether the real run does."""
        dry_ok = True
        try:
            asyncio.run(main.run_oracle_propose(config(), dry_run=True))
        except Exception:
            dry_ok = False

        real_ok = True
        try:
            asyncio.run(main.run_oracle_propose(config()))
        except Exception:
            real_ok = False

        self.assertEqual(dry_ok, real_ok, "the preview disagreed with the real run")
        self.assertEqual(dry_ok, expect_proposable)

    def test_no_safe_configured_fails_both(self):
        no_safe = Deployment(
            name="OG",
            source_core="0x" + "22" * 20,
            target_core="0x" + "33" * 20,
            safe_global=None,
        )
        self._results(SOURCE, no_safe)

        self._plan_and_real_agree(expect_proposable=False)
        self.assertEqual(self.posted, [], "nothing may be posted either way")

    def test_no_proposer_key_fails_both(self):
        keyless = SafeGlobal(
            safe_address=SAFE.safe_address,
            proposer_private_key="",
            api_url=SAFE.api_url,
            web_client_url=SAFE.web_client_url,
            eip_3770=SAFE.eip_3770,
        )
        deployment = Deployment(
            name="OG",
            source_core="0x" + "22" * 20,
            target_core="0x" + "33" * 20,
            safe_global=keyless,
        )
        self._results(SOURCE, deployment)

        self._plan_and_real_agree(expect_proposable=False)
        self.assertEqual(self.posted, [])

    def test_a_transfer_in_flight_fails_both(self):
        self._results(SOURCE, DEPLOYMENT, transfer_in_progress=True)

        self._plan_and_real_agree(expect_proposable=False)
        self.assertEqual(self.posted, [])

    def test_a_proposable_deployment_succeeds_in_both(self):
        self._results(SOURCE, DEPLOYMENT)

        self._plan_and_real_agree(expect_proposable=True)
        self.assertEqual(self.posted, [1], "only the real run posts")

    def test_the_dry_run_previews_the_value_the_real_run_would_send(self):
        self._results(SOURCE, DEPLOYMENT)

        plans = main.plan_oracle_proposals(
            main.validate_oracles(config()), force=True, value_override=4242
        )

        self.assertEqual([p.calls for p in plans], [[(ORACLE, [4242])]])
