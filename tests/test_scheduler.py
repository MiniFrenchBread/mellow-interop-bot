import os
import sys
import unittest

# Add the src directory to the Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from config.read_config import Config, SchedulerConfig
from scheduler import Scheduler, format_duration, is_due, next_due

DAY = 86400
HOUR = 3600


def make_config(**scheduler_kwargs) -> Config:
    intervals = scheduler_kwargs.pop(
        "task_intervals",
        {
            "ascend": 1209600,
            "rebalance": 7200,
            "oracle_report": DAY,
            "handle_epoch": 300,
        },
    )
    return Config(
        telegram_bot_api_key="",
        telegram_group_chat_id="",
        telegram_owner_nicknames={},
        telegram_proposal_message_prefix="",
        oracle_expiry_threshold_seconds=3600,
        oracle_recent_update_threshold_seconds=0,
        target_rpc="",
        target_core_helper="",
        sources=[],
        scheduler=SchedulerConfig(task_intervals=intervals, **scheduler_kwargs),
    )


class FakeClock:
    def __init__(self, start):
        self.now = start
        self.slept = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds

    def advance(self, seconds):
        self.now += seconds


class TestIntervalBuckets(unittest.TestCase):

    def test_not_due_inside_the_same_bucket(self):
        self.assertFalse(is_due(1000, 1100, HOUR))

    def test_due_after_crossing_a_boundary(self):
        self.assertTrue(is_due(HOUR - 1, HOUR + 1, HOUR))

    def test_boundary_is_anchored_to_the_epoch_not_to_the_last_run(self):
        # Two runs an hour apart that stay inside one bucket must not fire; the
        # schedule follows wall-clock buckets, not elapsed time.
        self.assertFalse(is_due(2 * DAY + 100, 2 * DAY + HOUR, DAY))
        self.assertTrue(is_due(2 * DAY + 100, 3 * DAY + 1, DAY))

    def test_next_due_lands_on_the_next_boundary(self):
        self.assertEqual(next_due(2 * DAY + 100, DAY), 3 * DAY)


class TestRestartBehaviour(unittest.TestCase):
    """A restart must neither move a task's schedule nor replay it."""

    def test_restart_keeps_the_same_next_fire_time(self):
        started = 10 * DAY + 5 * HOUR
        first = Scheduler(make_config(), now=lambda: started)
        restarted = Scheduler(make_config(), now=lambda: started + 2 * HOUR)

        self.assertEqual(
            next_due(first.last_run["oracle_report"], DAY),
            next_due(restarted.last_run["oracle_report"], DAY),
        )

    def test_restart_does_not_replay_a_bucket_already_served(self):
        started = 10 * DAY + 5 * HOUR
        scheduler = Scheduler(make_config(), now=lambda: started)
        self.assertFalse(is_due(scheduler.last_run["oracle_report"], started, DAY))

    def test_a_crossed_boundary_still_fires_after_a_restart(self):
        started = 10 * DAY + 5 * HOUR
        scheduler = Scheduler(make_config(), now=lambda: started)
        self.assertTrue(is_due(scheduler.last_run["oracle_report"], 11 * DAY + 1, DAY))


class TestCycleIsolation(unittest.TestCase):

    def setUp(self):
        self.clock = FakeClock(10 * DAY)
        # Every task due immediately.
        self.config = make_config(
            task_intervals={
                "ascend": 1,
                "rebalance": 1,
                "oracle_report": 1,
                "handle_epoch": 1,
            },
            post_ascend_gap_seconds=0,
        )
        self.scheduler = Scheduler(
            self.config, now=self.clock.time, sleep=self.clock.sleep
        )
        self.ran = []
        for task in ("ascend", "rebalance", "oracle_report", "handle_epoch"):
            self._stub(task)

    def _stub(self, task, error=None):
        def handler():
            self.ran.append(task)
            if error is not None:
                raise error

        setattr(self.scheduler, "task_" + task, handler)

    def test_one_failing_task_does_not_stop_the_others(self):
        self._stub("rebalance", RuntimeError("RPC exploded"))
        self.clock.advance(10)

        self.scheduler.run_cycle()

        self.assertEqual(
            self.ran, ["ascend", "rebalance", "oracle_report", "handle_epoch"]
        )
        self.assertEqual(self.scheduler.failures["rebalance"], 1)
        self.assertEqual(self.scheduler.failures["ascend"], 0)

    def test_failures_alert_once_at_the_threshold(self):
        self._stub("rebalance", RuntimeError("RPC exploded"))
        sent = []
        self.scheduler.notify = sent.append

        for _ in range(5):
            self.clock.advance(10)
            self.scheduler.run_cycle()

        self.assertEqual(self.scheduler.failures["rebalance"], 5)
        self.assertEqual(len(sent), 1, "alert once at the threshold, not every cycle")

    def test_recovery_clears_the_counter(self):
        self._stub("rebalance", RuntimeError("RPC exploded"))
        self.clock.advance(10)
        self.scheduler.run_cycle()
        self._stub("rebalance")
        self.clock.advance(10)
        self.scheduler.run_cycle()

        self.assertEqual(self.scheduler.failures["rebalance"], 0)

    def test_repeated_skips_alert(self):
        sent = []
        self.scheduler.notify = sent.append

        for _ in range(3):
            self.scheduler.record_skip("rebalance", "oracle value is incorrect")

        self.assertEqual(len(sent), 1)
        self.assertIn("oracle value is incorrect", sent[0])

    def test_a_failure_does_not_consume_the_task_interval(self):
        """Marking a failed run as done costs a fortnightly task a fortnight."""
        self._stub("ascend", RuntimeError("RPC exploded"))
        before = self.scheduler.last_run["ascend"]
        self.clock.advance(10)

        self.scheduler.run_cycle()

        self.assertEqual(self.scheduler.last_run["ascend"], before)
        self.assertGreater(self.scheduler.retry_after["ascend"], self.clock.now)

    def test_a_successful_run_clears_the_retry(self):
        self._stub("ascend", RuntimeError("RPC exploded"))
        self.clock.advance(10)
        self.scheduler.run_cycle()
        self._stub("ascend")
        self.clock.advance(10_000)

        self.scheduler.run_cycle()

        self.assertEqual(self.scheduler.retry_after["ascend"], 0.0)
        self.assertEqual(self.scheduler.failures["ascend"], 0)

    def test_the_retry_waits_before_running_again(self):
        self._stub("ascend", RuntimeError("RPC exploded"))
        self.clock.advance(10)
        self.scheduler.run_cycle()
        self.ran.clear()

        self.scheduler.run_cycle()

        self.assertNotIn("ascend", self.ran)

    def test_backoff_never_exceeds_the_task_interval(self):
        self.scheduler.failures["ascend"] = 20
        interval = self.config.scheduler.interval("ascend")

        self.assertLessEqual(self.scheduler.retry_delay("ascend"), interval)

    def test_a_stop_request_abandons_the_rest_of_the_cycle(self):
        def stop_then_run():
            self.ran.append("ascend")
            self.scheduler.request_stop()

        self.scheduler.task_ascend = stop_then_run
        self.clock.advance(10)

        self.scheduler.run_cycle()

        self.assertEqual(self.ran, ["ascend"])

    def test_ascend_is_followed_by_a_settling_gap(self):
        self.config.scheduler = SchedulerConfig(
            task_intervals=self.config.scheduler.task_intervals,
            post_ascend_gap_seconds=60,
        )
        self.clock.advance(10)

        self.scheduler.run_cycle()

        self.assertIn(60, self.clock.slept)


class TestFormatDuration(unittest.TestCase):

    def test_examples(self):
        self.assertEqual(format_duration(0), "<1m")
        self.assertEqual(format_duration(90), "1m")
        self.assertEqual(format_duration(3 * HOUR + 4 * 60), "3h 4m")
        self.assertEqual(format_duration(2 * DAY + HOUR), "2d 1h")


if __name__ == "__main__":
    unittest.main()
