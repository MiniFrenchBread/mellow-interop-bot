import os
import sys
import unittest

# Add the src directory to the Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import scheduler as scheduler_module
from scheduler import Scheduler

sys.path.insert(0, os.path.dirname(__file__))
from test_scheduler import make_config  # noqa: E402

ADDRESS = "0x" + "ab" * 20


class GateHarness(unittest.TestCase):
    """The gate exists because a tapp's first boot is the one case where the bot
    is guaranteed to be unable to act: it derives an address nobody has seen, so
    nobody has funded it or granted it anything. Left to the ordinary failure
    path that surfaces as a silent retry loop -- the oracle would report it
    after three failures at eight-hour intervals."""

    def setUp(self):
        self.scheduler = Scheduler(make_config())
        self.alerts = []
        self.scheduler.notify = self.alerts.append
        self.slept = []
        self.scheduler.interruptible_sleep = self.slept.append

        self.answers = []
        self._real_check = scheduler_module.check_operator_requirements
        scheduler_module.check_operator_requirements = self.next_answer

    def tearDown(self):
        scheduler_module.check_operator_requirements = self._real_check

    def next_answer(self, config):
        if not self.answers:
            return []
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class TestReady(GateHarness):
    def test_an_already_ready_signer_starts_immediately(self):
        self.scheduler.wait_until_ready(ADDRESS)
        self.assertEqual(self.slept, [])
        # The address is still announced -- it is the one thing an operator
        # needs and cannot get from outside the TEE.
        self.assertEqual(len(self.alerts), 1)
        self.assertIn(ADDRESS, self.alerts[0])

    def test_no_ready_alert_when_it_never_had_to_wait(self):
        self.scheduler.wait_until_ready(ADDRESS)
        self.assertFalse(any("ready" in a.lower() for a in self.alerts))


class TestWaiting(GateHarness):
    def test_it_waits_until_the_requirements_are_met(self):
        self.answers = [["needs PUSH_ROLE"], ["needs PUSH_ROLE"], []]
        self.scheduler.wait_until_ready(ADDRESS)
        self.assertEqual(
            self.slept, [scheduler_module.READY_CHECK_INTERVAL_SECONDS] * 2
        )

    def test_the_alert_lists_what_is_missing(self):
        self.answers = [["needs PUSH_ROLE on 0xdead", "send it gas"], []]
        self.scheduler.wait_until_ready(ADDRESS)
        waiting = [a for a in self.alerts if "waiting" in a.lower()]
        self.assertEqual(len(waiting), 1)
        self.assertIn("PUSH_ROLE", waiting[0])
        self.assertIn("send it gas", waiting[0])

    def test_it_says_so_once_it_starts(self):
        self.answers = [["needs PUSH_ROLE"], []]
        self.scheduler.wait_until_ready(ADDRESS)
        self.assertTrue(any("ready" in a.lower() for a in self.alerts))

    def test_backticks_are_stripped_from_alerts(self):
        # The message wraps the list in a code fence; a backtick inside would
        # close it and Telegram would reject the whole alert -- losing it
        # exactly when the error is unusual.
        self.answers = [["reverted: `oracle` forbidden"], []]
        self.scheduler.wait_until_ready(ADDRESS)
        waiting = [a for a in self.alerts if "waiting" in a.lower()][0]
        self.assertNotIn("`oracle`", waiting)

    def test_a_broken_check_keeps_waiting_rather_than_starting(self):
        # An RPC that will not answer must not read as "everything is granted".
        self.answers = [ConnectionError("no route"), []]
        self.scheduler.wait_until_ready(ADDRESS)
        self.assertEqual(len(self.slept), 1)
        self.assertTrue(any("no route" in a for a in self.alerts))


class TestStopping(GateHarness):
    def test_sigterm_during_the_wait_returns_instead_of_looping(self):
        def stop_then_answer(config):
            self.scheduler.stopping = True
            return ["needs PUSH_ROLE"]

        scheduler_module.check_operator_requirements = stop_then_answer
        self.scheduler.wait_until_ready(ADDRESS)
        self.assertEqual(self.slept, [scheduler_module.READY_CHECK_INTERVAL_SECONDS])

    def test_a_scheduler_already_stopping_does_not_check_at_all(self):
        self.scheduler.stopping = True
        self.answers = [["needs PUSH_ROLE"]]
        self.scheduler.wait_until_ready(None)
        self.assertEqual(self.slept, [])
        self.assertEqual(self.alerts, [])


if __name__ == "__main__":
    unittest.main()
