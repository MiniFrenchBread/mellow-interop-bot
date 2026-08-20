import dataclasses
import os
import sys
import unittest

# Add the src directory to the Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from web3_scripts import withdrawal_queue
from web3_scripts.withdrawal_queue import QueueParams, handle_epochs, read_params

DAY = 86400
WEEK = 7 * DAY
QUEUE = "0x10A98a5344742308744Bd59829786584A12C1146"
KEY = "0x" + "11" * 32


class Call:
    def __init__(self, value):
        self._value = value

    def call(self):
        return self._value() if callable(self._value) else self._value


class FakeFunctions:
    def __init__(self, values, log):
        self._values = values
        self._log = log

    def __getattr__(self, name):
        def factory():
            self._log.append(name)
            return Call(self._values[name])

        return factory


class FakeContract:
    def __init__(self, values, log):
        self.functions = FakeFunctions(values, log)


class FakeBlock:
    def __init__(self, timestamp):
        self.timestamp = timestamp


class FakeEth:
    chain_id = 16661

    def __init__(self, timestamp):
        self._timestamp = timestamp

    def get_block(self, _identifier):
        return FakeBlock(self._timestamp)


class FakeW3:
    def __init__(self, timestamp):
        self.eth = FakeEth(timestamp)


class TestQueueParams(unittest.TestCase):

    def test_only_the_immutable_values_are_cacheable(self):
        """withdrawalDelay is ordinary storage behind a role-gated setter.

        Keeping it here would let it be cached for the life of the process.
        """
        fields = {f.name for f in dataclasses.fields(QueueParams)}
        self.assertEqual(fields, {"init_timestamp", "epoch_duration"})


class TestWithdrawalDelayIsReadFresh(unittest.TestCase):

    def setUp(self):
        self.calls = []
        self.delay = WEEK
        self.epoch_iterator = 1
        # Epoch 1 matures at 2 days + the delay, so it is short of maturity under
        # a one-week delay and past it once the delay is lifted.
        self.values = {
            "initTimestamp": 0,
            "epochDuration": DAY,
            "epochIterator": lambda: self.epoch_iterator,
            "currentEpoch": 5,
            "withdrawalDelay": lambda: self.delay,
            "handleEpoch": None,
        }
        withdrawal_queue._params_cache.clear()
        self._original_contract = withdrawal_queue.get_contract
        self._original_send = withdrawal_queue.send_and_confirm
        withdrawal_queue.get_contract = lambda w3, address, name: FakeContract(
            self.values, self.calls
        )

        def fake_send(*_args, **_kwargs):
            self.epoch_iterator += 1

        withdrawal_queue.send_and_confirm = fake_send

    def tearDown(self):
        withdrawal_queue.get_contract = self._original_contract
        withdrawal_queue.send_and_confirm = self._original_send
        withdrawal_queue._params_cache.clear()

    def run_once(self, now):
        return handle_epochs(FakeW3(now), QUEUE, KEY, max_iterations=1)

    def test_the_delay_is_read_on_every_run(self):
        self.run_once(now=8 * DAY)
        self.calls.clear()

        self.run_once(now=8 * DAY)

        self.assertIn("withdrawalDelay", self.calls)

    def test_the_immutable_values_are_read_only_once(self):
        self.run_once(now=8 * DAY)
        self.calls.clear()

        self.run_once(now=8 * DAY)

        self.assertNotIn("initTimestamp", self.calls)
        self.assertNotIn("epochDuration", self.calls)

    def test_a_shortened_delay_takes_effect_without_a_restart(self):
        """Governance can shorten the delay; a long-lived scheduler must notice."""
        now = 8 * DAY
        self.assertEqual(self.run_once(now), 0, "not mature under a one-week delay")

        self.delay = 0
        self.assertEqual(
            self.run_once(now), 1, "mature once the delay is lifted, no restart"
        )


class TestNoResendWhenNothingMoved(unittest.TestCase):
    """handleEpoch returns without advancing when liquidity is short.

    Resending on that is how the shell loop answered "already known" 472 times.
    """

    def setUp(self):
        self.calls = []
        self.values = {
            "initTimestamp": 0,
            "epochDuration": DAY,
            "epochIterator": 1,
            "currentEpoch": 5,
            "withdrawalDelay": 0,
            "handleEpoch": None,
        }
        withdrawal_queue._params_cache.clear()
        self._contract = withdrawal_queue.get_contract
        self._send = withdrawal_queue.send_and_confirm
        withdrawal_queue.get_contract = lambda w3, address, name: FakeContract(
            self.values, self.calls
        )
        self.sends = []
        withdrawal_queue.send_and_confirm = lambda *a, **k: self.sends.append(1)

    def tearDown(self):
        withdrawal_queue.get_contract = self._contract
        withdrawal_queue.send_and_confirm = self._send
        withdrawal_queue._params_cache.clear()

    def test_a_stalled_epoch_is_sent_once_and_left_alone(self):
        processed = handle_epochs(FakeW3(8 * DAY), QUEUE, KEY, max_iterations=5)

        self.assertEqual(processed, 0)
        self.assertEqual(len(self.sends), 1, "sent once, then left for next time")


if __name__ == "__main__":
    unittest.main()
