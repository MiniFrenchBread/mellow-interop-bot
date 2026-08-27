import importlib
import os
import sys
import unittest
from types import SimpleNamespace

# Add the src directory to the Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from web3 import Web3

from config.read_config import (
    Config,
    Deployment,
    OracleUpdateConfig,
    SourceConfig,
)

# Not `from config import validate_config`: the package's __init__ re-exports
# the *function* of that name, which shadows the submodule.
vc = importlib.import_module("config.validate_config")

OPERATOR_KEY = "0x" + "11" * 32
UPDATER_KEY = "0x" + "22" * 32
SOURCE_CORE = "0x" + "33" * 20
TARGET_CORE = "0x" + "44" * 20

SOURCE_ROLES = ("PUSH_ROLE",)
TARGET_ROLES = ("PUSH_ROLE", "REDEEM_ROLE", "DEPOSIT_ROLE", "CLAIM_ROLE")


def role_id(name: str) -> bytes:
    """What the contract's `NAME_ROLE()` view returns. The check reads these off
    the contract rather than guessing a preimage, so the fake has to hand back
    something bytes-shaped for the error message to format."""
    return bytes(Web3.keccak(text=name))


def make_config(**source_overrides) -> Config:
    fields = dict(
        name="OG",
        rpc="https://source.invalid",
        source_core_helper="0x" + "55" * 20,
        deployments=(
            Deployment(name="OG", source_core=SOURCE_CORE, target_core=TARGET_CORE),
        ),
        executor_private_key=OPERATOR_KEY,
        oracle_update=OracleUpdateConfig(updater_private_key=UPDATER_KEY),
    )
    fields.update(source_overrides)
    return Config(
        telegram_bot_api_key="",
        telegram_group_chat_id="",
        telegram_owner_nicknames={},
        telegram_proposal_message_prefix="",
        oracle_expiry_threshold_seconds=1,
        oracle_recent_update_threshold_seconds=0,
        target_rpc="https://target.invalid",
        target_core_helper="0x" + "66" * 20,
        sources=[SourceConfig(**fields)],
    )


class FakeCore:
    """A core contract that grants whatever it is told to."""

    def __init__(self, name, granted, claimer=None):
        self.name = name
        self.granted = set(granted)
        self._claimer = claimer or ("0x" + "99" * 20)

    @property
    def functions(self):
        return self

    def hasRole(self, role, account):
        return SimpleNamespace(call=lambda: bytes(role) in self.granted)

    def claimer(self):
        return SimpleNamespace(call=lambda: self._claimer)

    def __getattr__(self, item):
        if item.endswith("_ROLE"):
            return lambda: SimpleNamespace(call=lambda: role_id(item))
        raise AttributeError(item)


class FakeW3:
    def __init__(self, balances):
        self.balances = balances
        self.eth = SimpleNamespace(
            block_number=1,
            get_balance=lambda a: self.balances.get(a, 0),
        )


class Harness(unittest.TestCase):
    """check_operator_requirements is a report, not a gate.

    Six roles across two chains have to be granted by a multisig before this bot
    can act, and today only one of them is checked anywhere. Whoever is running
    that ceremony needs the whole list at once, so this must keep going after
    each failure rather than raising on the first.
    """

    def setUp(self):
        self.source_granted = {role_id(r) for r in SOURCE_ROLES} | {
            bytes(vc.SET_VALUE_ROLE)
        }
        self.target_granted = {role_id(r) for r in TARGET_ROLES}
        self.claimer = "0x" + "99" * 20
        self.balances = {}

        from eth_account import Account

        self.operator = Account.from_key(OPERATOR_KEY).address
        self.updater = Account.from_key(UPDATER_KEY).address
        self.balances[self.operator] = 10**18
        self.balances[self.updater] = 10**18

        self._get_w3 = vc.get_w3
        self._get_contract = vc.get_contract
        vc.get_w3 = lambda rpc: FakeW3(self.balances)
        vc.get_contract = self.fake_contract

    def tearDown(self):
        vc.get_w3 = self._get_w3
        vc.get_contract = self._get_contract

    def fake_contract(self, w3, address, name):
        if name == "SourceCore":
            return FakeCore(name, self.source_granted)
        return FakeCore(name, self.target_granted, claimer=self.claimer)

    def check(self):
        return vc.check_operator_requirements(make_config())


class TestNothingMissing(Harness):
    def test_fully_granted_and_funded_reports_nothing(self):
        self.assertEqual(self.check(), [])


class TestMissingRoles(Harness):
    def test_every_missing_role_is_reported_not_just_the_first(self):
        self.source_granted = set()
        self.target_granted = set()
        missing = self.check()
        for role in ("SET_VALUE_ROLE", "PUSH_ROLE", "REDEEM_ROLE", "DEPOSIT_ROLE"):
            self.assertTrue(
                any(role in m for m in missing),
                "{} not reported: {}".format(role, missing),
            )

    def test_a_missing_role_names_the_grant_to_make(self):
        self.target_granted -= {role_id("REDEEM_ROLE")}
        (line,) = [m for m in self.check() if "REDEEM_ROLE" in m]
        self.assertIn("grantRole", line)
        self.assertIn(self.operator, line)
        self.assertIn(TARGET_CORE.lower(), line.lower())

    def test_set_value_role_is_checked_against_the_source_core(self):
        # The role is defined by the Oracle but enforced by the SourceCore's
        # access control, so that is where the grant has to land.
        self.source_granted = {role_id(r) for r in SOURCE_ROLES}
        (line,) = [m for m in self.check() if "SET_VALUE_ROLE" in m]
        self.assertIn("SourceCore", line)
        self.assertIn(self.updater, line)


class TestClaimIsGatedTwice(Harness):
    def test_the_role_alone_is_enough(self):
        self.claimer = "0x" + "88" * 20
        self.assertEqual([m for m in self.check() if "claim" in m.lower()], [])

    def test_being_the_named_claimer_alone_is_enough(self):
        # Checking only the role would report a working deployment as broken.
        self.target_granted -= {role_id("CLAIM_ROLE")}
        self.claimer = self.operator
        self.assertEqual([m for m in self.check() if "claim" in m.lower()], [])

    def test_neither_is_reported(self):
        self.target_granted -= {role_id("CLAIM_ROLE")}
        self.claimer = "0x" + "88" * 20
        (line,) = [m for m in self.check() if "cannot claim" in m]
        self.assertIn(TARGET_CORE, line)


class TestBalances(Harness):
    def test_an_unfunded_operator_is_reported_on_both_chains(self):
        self.balances[self.operator] = 0
        missing = [m for m in self.check() if "send it gas" in m]
        self.assertEqual(len(missing), 2, missing)

    def test_the_floors_are_overridable(self):
        self.balances[self.operator] = 10**18
        os.environ["OPERATOR_MIN_BALANCE_WEI"] = str(10**19)
        try:
            self.assertTrue(any("send it gas" in m for m in self.check()))
        finally:
            del os.environ["OPERATOR_MIN_BALANCE_WEI"]


class TestUnreachableIsNotGranted(Harness):
    """ "Could not check" and "granted" must never look the same."""

    def test_an_unreachable_target_rpc_is_reported(self):
        def boom(rpc):
            if "target" in rpc:
                raise ConnectionError("no route to host")
            return FakeW3(self.balances)

        vc.get_w3 = boom
        (line,) = self.check()
        self.assertIn("target chain RPC unreachable", line)

    def test_a_reverting_role_read_is_reported_rather_than_passing(self):
        def boom(w3, address, name):
            raise ValueError("execution reverted")

        vc.get_contract = boom
        missing = self.check()
        self.assertTrue(missing)
        self.assertTrue(all("could not" in m for m in missing if "gas" not in m))


class TestNoKey(Harness):
    def test_a_source_without_an_executor_key_says_so(self):
        config = make_config(executor_private_key=None)
        (line,) = vc.check_operator_requirements(config)
        self.assertIn("no executor key", line)
        self.assertIn("TAPP_APP_ID", line)


if __name__ == "__main__":
    unittest.main()
