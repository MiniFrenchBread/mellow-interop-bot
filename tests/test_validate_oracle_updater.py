import os
import sys
import unittest
from types import SimpleNamespace

# Add the src directory to the Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import importlib

from config.read_config import Deployment, OracleUpdateConfig, SourceConfig

# Not `from config import validate_config`: the package's __init__ re-exports the
# *function* of that name, which shadows the submodule, and patching attributes
# on a function object fails in a way that looks like the test is wrong.
vc = importlib.import_module("config.validate_config")

KEY = "0x" + "11" * 32
KEY_ADDRESS = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
SOURCE_CORE = "0x" + "22" * 20
UPDATER = "0x" + "44" * 20
SET_VALUE_ROLE_HEX = "a9146fff3103c7a809874c345ee6b6c99f1d8c7b9043121571b2a4f2bb2557a0"


def source(**overrides) -> SourceConfig:
    fields = dict(
        name="OG",
        rpc="https://rpc.invalid",
        source_core_helper="0x" + "11" * 20,
        deployments=(
            Deployment(
                name="OG",
                source_core=SOURCE_CORE,
                target_core="0x" + "33" * 20,
                oracle_updater=UPDATER,
            ),
        ),
        oracle_update=OracleUpdateConfig(updater_private_key=KEY),
    )
    fields.update(overrides)
    return SourceConfig(**fields)


class FakeAccessControl:
    """A contract answering `hasRole` from a set of (role hex, account) grants."""

    def __init__(self, grants, source_core=SOURCE_CORE):
        self.grants = set(grants)
        self.asked = []
        self._source_core = source_core

    @property
    def functions(self):
        return self

    def hasRole(self, role, account):
        self.asked.append((role.hex(), account))
        return SimpleNamespace(call=lambda: (role.hex(), account) in self.grants)

    def sourceCore(self):
        return SimpleNamespace(call=lambda: self._source_core)


UPDATER_ROLE_HEX = vc.UPDATER_ROLE.hex()
ALL_GRANTED = {(SET_VALUE_ROLE_HEX, UPDATER)}
KEY_GRANTED = {(UPDATER_ROLE_HEX, KEY_ADDRESS)}


class TestTheRoleCheck(unittest.TestCase):
    """The grants are manual multisig steps done outside this repo.

    Forgetting one produces a bot that starts cleanly and then fails every eight
    hours, so it is worth one line at deploy time.
    """

    def setUp(self):
        self._get_contract = vc.get_contract

    def tearDown(self):
        vc.get_contract = self._get_contract

    def _chain(self, core_grants=ALL_GRANTED, updater_grants=KEY_GRANTED, **kwargs):
        core = FakeAccessControl(core_grants)
        updater = FakeAccessControl(updater_grants, **kwargs)
        vc.get_contract = lambda _w3, _address, name: (
            updater if name == "OracleUpdater" else core
        )
        return core, updater

    def test_everything_granted_passes(self):
        self._chain()
        vc.validate_oracle_updater(None, source())

    def test_a_missing_oracle_grant_raises_with_the_remedy(self):
        self._chain(core_grants=set())

        with self.assertRaises(Exception) as caught:
            vc.validate_oracle_updater(None, source())

        message = str(caught.exception)
        self.assertIn("does not hold SET_VALUE_ROLE", message)
        # The remedy has to be in the message. Whoever hits this is mid-deploy
        # and needs the call to hand, not a pointer to go and derive it.
        self.assertIn("grantRole", message)
        self.assertIn(UPDATER, message)

    def test_a_key_without_the_updater_role_raises(self):
        self._chain(updater_grants=set())

        with self.assertRaisesRegex(Exception, "does not hold UPDATER_ROLE"):
            vc.validate_oracle_updater(None, source())

    def test_an_updater_for_another_vault_raises(self):
        self._chain(source_core="0x" + "99" * 20)

        with self.assertRaisesRegex(Exception, "writes the oracle of"):
            vc.validate_oracle_updater(None, source())

    def test_no_updater_address_raises(self):
        self._chain()
        bare = source(
            deployments=(
                Deployment(
                    name="OG", source_core=SOURCE_CORE, target_core="0x" + "33" * 20
                ),
            )
        )

        with self.assertRaisesRegex(Exception, "no oracle-updater address"):
            vc.validate_oracle_updater(None, bare)

    def test_the_roles_asked_about_are_the_ones_the_contracts_check(self):
        """A typo in either hash would pass a check against a role nothing
        enforces, which is worse than no check."""
        core, updater = self._chain()

        vc.validate_oracle_updater(None, source())

        self.assertIn((SET_VALUE_ROLE_HEX, UPDATER), core.asked)
        self.assertEqual(updater.asked, [(UPDATER_ROLE_HEX, KEY_ADDRESS)])
        self.assertEqual(
            UPDATER_ROLE_HEX,
            "73e573f9566d61418a34d5de3ff49360f9c51fec37f7486551670290f6285dab",
        )

    def test_a_key_still_holding_the_direct_role_passes_with_a_note(self):
        """Between the Safe's grant and its revoke both hold the role."""
        self._chain(core_grants=ALL_GRANTED | {(SET_VALUE_ROLE_HEX, KEY_ADDRESS)})
        vc.validate_oracle_updater(None, source())

    def test_no_key_configured_is_skipped_not_failed(self):
        """Most sources have no oracle-update section; failing them would make
        validate-config unusable for everything else."""
        self._chain(core_grants=set(), updater_grants=set())

        vc.validate_oracle_updater(None, source(oracle_update=None))


class TestItRunsEarlyEnoughToBeUseful(unittest.TestCase):
    """The role check must not sit behind the deployment cross-checks.

    It answers "was the grant done?" for someone who is mid-deploy. Behind
    validate_deployments, any unrelated failure in that pass aborts the run
    before the question is asked -- and there is currently one: the symbol
    assertion fails on this config because the deployment is named with a letter
    O while the vault's symbol uses a zero.
    """

    def setUp(self):
        self.order = []
        self._patched = {}
        for name in (
            "get_w3",
            "validate_rpc_url",
            "validate_source_helper",
            "validate_oracle_updater",
            "validate_deployments",
            "validate_all_safe_globals",
        ):
            self._patched[name] = getattr(vc, name)

        vc.get_w3 = lambda _rpc: None
        for name in (
            "validate_rpc_url",
            "validate_source_helper",
            "validate_oracle_updater",
            "validate_all_safe_globals",
        ):
            setattr(vc, name, self._record(name))

    def tearDown(self):
        for name, original in self._patched.items():
            setattr(vc, name, original)

    def _record(self, name, error=None):
        def handler(*_args, **_kwargs):
            self.order.append(name)
            if error is not None:
                raise error

        return handler

    def test_the_role_check_precedes_deployment_validation(self):
        vc.validate_deployments = self._record("validate_deployments")

        vc.validate_source(None, source())

        self.assertLess(
            self.order.index("validate_oracle_updater"),
            self.order.index("validate_deployments"),
        )

    def test_a_deployment_failure_does_not_hide_the_role_answer(self):
        vc.validate_deployments = self._record(
            "validate_deployments", Exception("symbol mismatch")
        )

        with self.assertRaises(Exception):
            vc.validate_source(None, source())

        self.assertIn("validate_oracle_updater", self.order)


if __name__ == "__main__":
    unittest.main()
