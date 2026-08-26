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
SOURCE_CORE = "0x" + "22" * 20


def source(**overrides) -> SourceConfig:
    fields = dict(
        name="OG",
        rpc="https://rpc.invalid",
        source_core_helper="0x" + "11" * 20,
        deployments=(
            Deployment(
                name="OG", source_core=SOURCE_CORE, target_core="0x" + "33" * 20
            ),
        ),
        oracle_update=OracleUpdateConfig(updater_private_key=KEY),
    )
    fields.update(overrides)
    return SourceConfig(**fields)


class FakeCore:
    def __init__(self, granted):
        self.granted = granted
        self.asked = []

    @property
    def functions(self):
        return self

    def hasRole(self, role, account):
        self.asked.append((role, account))
        return SimpleNamespace(call=lambda: self.granted)


class TestTheRoleCheck(unittest.TestCase):
    """Granting the role is a manual multisig step done outside this repo.

    Forgetting it produces a bot that starts cleanly and then fails every eight
    hours with "Oracle: forbidden", so it is worth one line at deploy time.
    """

    def setUp(self):
        self._get_contract = vc.get_contract

    def tearDown(self):
        vc.get_contract = self._get_contract

    def _with_role(self, granted):
        core = FakeCore(granted)
        vc.get_contract = lambda *_a, **_k: core
        return core

    def test_a_missing_grant_raises(self):
        self._with_role(False)

        with self.assertRaises(Exception) as caught:
            vc.validate_oracle_updater(None, source())

        message = str(caught.exception)
        self.assertIn("does not hold SET_VALUE_ROLE", message)
        # The remedy has to be in the message. Whoever hits this is mid-deploy
        # and needs the call to hand, not a pointer to go and derive it.
        self.assertIn("grantRole", message)
        self.assertIn(SOURCE_CORE, message)

    def test_a_granted_role_passes(self):
        self._with_role(True)

        vc.validate_oracle_updater(None, source())

    def test_the_role_asked_about_is_the_one_the_oracle_checks(self):
        """keccak256("ORACLE:SET_VALUE_ROLE") -- a typo here would pass a check
        against a role nothing enforces, which is worse than no check."""
        core = self._with_role(True)

        vc.validate_oracle_updater(None, source())

        role, _account = core.asked[0]
        self.assertEqual(
            role.hex(),
            "a9146fff3103c7a809874c345ee6b6c99f1d8c7b9043121571b2a4f2bb2557a0",
        )

    def test_it_checks_the_address_derived_from_the_key(self):
        core = self._with_role(True)

        vc.validate_oracle_updater(None, source())

        _role, account = core.asked[0]
        self.assertEqual(account, "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A")

    def test_no_key_configured_is_skipped_not_failed(self):
        """Most sources have no oracle-update section; failing them would make
        validate-config unusable for everything else."""
        self._with_role(False)

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
