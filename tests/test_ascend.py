import os
import sys
import unittest

# Add the src directory to the Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from eth_account import Account
from web3 import Web3

from web3_scripts import ascend
from web3_scripts.ascend import DISTRIBUTION_DUST_WEI, run_ascend

KEY = "0x" + "11" * 32
SENDER = Account.from_key(KEY).address
ROUTER = Web3.to_checksum_address("0x" + "4a" * 20)
REWARDERS = [Web3.to_checksum_address("0x" + byte * 20) for byte in ("81", "b3", "73")]


class FakeCall:
    def __init__(self, contract, name, args):
        self.contract = contract
        self.name = name
        self.args = args

    def call(self, _params=None):
        return self.contract.call_returns.get(self.name, 0)


class FakeFunctions:
    def __init__(self, contract):
        self._contract = contract

    def __getattr__(self, name):
        def factory(*args):
            return FakeCall(self._contract, name, args)

        return factory


class FakeEvent:
    def __init__(self, entries):
        self._entries = entries

    def process_receipt(self, _receipt, errors=None):
        return self._entries


class FakeEvents:
    def __init__(self, contract):
        self._contract = contract

    def __getattr__(self, name):
        return lambda: FakeEvent(self._contract.events_for.get(name, []))


class FakeContract:
    def __init__(self, address, name, world):
        self.address = address
        self.name = name
        self.call_returns = {}
        self.events_for = {}
        self.functions = FakeFunctions(self)
        self.events = FakeEvents(self)
        world.contracts[address] = self


class FakeEth:
    def __init__(self, world):
        self._world = world
        self.chain_id = 16661

    def get_balance(self, address):
        return self._world.balances.get(address, 0)

    def get_transaction_count(self, _address, block="latest"):
        self._world.nonce_reads.append(block)
        return self._world.starting_nonce


class FakeW3:
    def __init__(self, world):
        self.eth = FakeEth(world)


class World:
    """Just enough chain to drive run_ascend without touching a node."""

    def __init__(self):
        self.contracts = {}
        self.balances = {}
        self.nonce_reads = []
        self.starting_nonce = 42
        self.sent = []
        self.claimed = {}
        self.distributed = []

    def w3(self):
        return FakeW3(self)


def claimed_event(account, reward):
    return {"args": {"account": account, "reward": reward}}


def distributed_event(amount):
    return {"args": {"amount": amount}}


class AscendTestCase(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self._get_contract = ascend.get_contract
        self._send = ascend.send_and_confirm

        def get_contract(_w3, address, name):
            address = Web3.to_checksum_address(address)
            existing = self.world.contracts.get(address)
            if existing:
                return existing
            return FakeContract(address, name, self.world)

        def send_and_confirm(function, value, private_key, **kwargs):
            self.world.sent.append(
                {"name": function.name, "nonce": kwargs.get("nonce"), **kwargs}
            )
            return type(
                "Outcome",
                (),
                {"receipt": {"status": 1}, "tx_hash": "0x%02x" % len(self.world.sent)},
            )()

        ascend.get_contract = get_contract
        ascend.send_and_confirm = send_and_confirm

    def tearDown(self):
        ascend.get_contract = self._get_contract
        ascend.send_and_confirm = self._send

    def prepare(self, rewards, router_balance):
        for address, reward in zip(REWARDERS, rewards):
            contract = FakeContract(
                Web3.to_checksum_address(address), "Rewarder", self.world
            )
            contract.call_returns["claim"] = reward
            contract.events_for["Claimed"] = [claimed_event(ROUTER, reward)]
        FakeContract(ROUTER, "AscendRouter", self.world)
        self.world.balances[ROUTER] = router_balance

    def run_ascend(self, **kwargs):
        return run_ascend(
            self.world.w3(),
            router=ROUTER,
            rewarders=list(REWARDERS),
            private_key=KEY,
            **kwargs,
        )


class TestNonceSequencing(AscendTestCase):
    """A stuck claim from an earlier run must be replaced, not queued behind."""

    def test_the_starting_nonce_comes_from_latest(self):
        self.prepare([1, 2, 3], router_balance=0)

        self.run_ascend()

        self.assertEqual(self.world.nonce_reads, ["latest"])

    def test_the_nonce_advances_once_per_send(self):
        self.prepare([1, 2, 3], router_balance=6)
        self.world.contracts[ROUTER].events_for["Distributed"] = [distributed_event(6)]

        self.run_ascend()

        nonces = [sent["nonce"] for sent in self.world.sent]
        self.assertEqual(nonces, [42, 43, 44, 45])

    def test_the_nonce_is_read_only_once(self):
        self.prepare([1, 2, 3], router_balance=6)
        self.world.contracts[ROUTER].events_for["Distributed"] = [distributed_event(6)]

        self.run_ascend()

        self.assertEqual(len(self.world.nonce_reads), 1)


class TestClaimAccounting(AscendTestCase):
    """Amounts come from the receipt, never from a simulation.

    Reporting a simulated number as a result is what made two earlier failures
    read as successes in the logs.
    """

    def test_rewards_come_from_the_claimed_event(self):
        self.prepare([10, 20, 30], router_balance=60)
        self.world.contracts[ROUTER].events_for["Distributed"] = [distributed_event(60)]
        # A simulation would return these; the events say otherwise.
        for address in REWARDERS:
            self.world.contracts[Web3.to_checksum_address(address)].call_returns[
                "claim"
            ] = 999

        result = self.run_ascend()

        self.assertEqual(result.total_claimed, 60)

    def test_an_event_for_another_account_is_ignored(self):
        self.prepare([10, 20, 30], router_balance=60)
        self.world.contracts[ROUTER].events_for["Distributed"] = [distributed_event(60)]
        other = Web3.to_checksum_address("0x" + "99" * 20)
        self.world.contracts[Web3.to_checksum_address(REWARDERS[0])].events_for[
            "Claimed"
        ] = [claimed_event(other, 10)]

        result = self.run_ascend()

        self.assertEqual(result.claims[0][1], 0)


class TestDistribute(AscendTestCase):

    def test_an_empty_router_is_not_distributed(self):
        self.prepare([1, 2, 3], router_balance=0)

        result = self.run_ascend()

        self.assertEqual([s["name"] for s in self.world.sent], ["claim"] * 3)
        self.assertEqual(result.distributed, 0)

    def test_a_funded_router_is_distributed(self):
        self.prepare([1, 2, 3], router_balance=6)
        self.world.contracts[ROUTER].events_for["Distributed"] = [
            distributed_event(5),
            distributed_event(1),
        ]

        result = self.run_ascend()

        self.assertEqual(self.world.sent[-1]["name"], "distribute")
        self.assertEqual(result.distributed, 6)

    def test_rounding_dust_is_not_reported_as_a_shortfall(self):
        self.prepare([1, 2, 3], router_balance=1000)
        self.world.contracts[ROUTER].events_for["Distributed"] = [
            distributed_event(1000 - DISTRIBUTION_DUST_WEI + 1)
        ]

        result = self.run_ascend()

        self.assertLess(
            result.router_balance - result.distributed, DISTRIBUTION_DUST_WEI
        )


class TestDryRun(AscendTestCase):

    def test_a_dry_run_broadcasts_nothing(self):
        self.prepare([10, 20, 30], router_balance=60)

        result = self.run_ascend(dry_run=True)

        self.assertEqual(self.world.sent, [])
        self.assertEqual(self.world.nonce_reads, [])
        self.assertEqual(result.total_claimed, 60, "simulated amounts are fine here")


class TestTxSettings(AscendTestCase):

    def test_settings_reach_every_send(self):
        self.prepare([1, 2, 3], router_balance=6)
        self.world.contracts[ROUTER].events_for["Distributed"] = [distributed_event(6)]

        self.run_ascend(tx={"receipt_timeout": 60, "max_attempts": 3})

        for sent in self.world.sent:
            self.assertEqual(sent["receipt_timeout"], 60)
            self.assertEqual(sent["max_attempts"], 3)


if __name__ == "__main__":
    unittest.main()
