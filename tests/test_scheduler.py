import os
import sys
import tempfile
import unittest

# Add the src directory to the Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from config.read_config import Config, SchedulerConfig
from scheduler import Scheduler, format_duration, is_due, next_due
from web3_scripts.tx import NonceBlocked
import main as oracle_main_module

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


def make_scheduler(config, now=None, sleep=None, state_path=None):
    """Always give the scheduler its own state file.

    Sharing the real one would let tests read each other's persisted schedule.
    """
    kwargs = {
        "state_path": state_path or os.path.join(tempfile.mkdtemp(), "state.json")
    }
    if now is not None:
        kwargs["now"] = now
    if sleep is not None:
        kwargs["sleep"] = sleep
    return Scheduler(config, **kwargs)


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

    def test_a_non_positive_interval_does_not_mean_run_constantly(self):
        """The value an operator reaches for to stop a task must not start it.

        Config rejects it; if one ever gets through, not running is the
        direction that cannot cause harm.
        """
        self.assertFalse(is_due(1000, 10**9, 0))
        self.assertFalse(is_due(1000, 10**9, -1))

    def test_the_config_refuses_a_non_positive_interval(self):
        from config.read_config import _create_scheduler_config

        with self.assertRaises(ValueError):
            _create_scheduler_config({"tasks": {"ascend": {"interval_seconds": 0}}})

    def test_next_due_lands_on_the_next_boundary(self):
        self.assertEqual(next_due(2 * DAY + 100, DAY), 3 * DAY)


class TestRestartBehaviour(unittest.TestCase):
    """A restart must neither move a task's schedule nor replay it."""

    def test_restart_keeps_the_same_next_fire_time(self):
        started = 10 * DAY + 5 * HOUR
        first = make_scheduler(make_config(), now=lambda: started)
        restarted = make_scheduler(make_config(), now=lambda: started + 2 * HOUR)

        self.assertEqual(
            next_due(first.last_run["oracle_report"], DAY),
            next_due(restarted.last_run["oracle_report"], DAY),
        )

    def test_restart_does_not_replay_a_bucket_already_served(self):
        started = 10 * DAY + 5 * HOUR
        scheduler = make_scheduler(make_config(), now=lambda: started)
        self.assertFalse(is_due(scheduler.last_run["oracle_report"], started, DAY))

    def test_a_crossed_boundary_still_fires_after_a_restart(self):
        started = 10 * DAY + 5 * HOUR
        scheduler = make_scheduler(make_config(), now=lambda: started)
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
        self.scheduler = make_scheduler(
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

    def test_a_task_that_stays_broken_keeps_alerting(self):
        """One alert then silence is the failure mode the alerting exists to close."""
        self._stub("rebalance", RuntimeError("RPC exploded"))
        sent = []
        self.scheduler.notify = sent.append

        for _ in range(6):
            self.scheduler.retry_after["rebalance"] = 0.0
            self.clock.advance(10)
            self.scheduler.run_cycle()

        self.assertEqual(self.scheduler.failures["rebalance"], 6)
        self.assertEqual(len(sent), 2, "at the threshold and every threshold after")

    def test_failures_do_not_alert_on_every_cycle(self):
        self._stub("rebalance", RuntimeError("RPC exploded"))
        sent = []
        self.scheduler.notify = sent.append

        for _ in range(5):
            self.scheduler.retry_after["rebalance"] = 0.0
            self.clock.advance(10)
            self.scheduler.run_cycle()

        self.assertEqual(self.scheduler.failures["rebalance"], 5)
        self.assertEqual(len(sent), 1, "at the threshold, not on every failure")

    def test_recovery_clears_the_counter(self):
        self._stub("rebalance", RuntimeError("RPC exploded"))
        self.clock.advance(10)
        self.scheduler.run_cycle()
        self._stub("rebalance")
        self.clock.advance(10)
        self.scheduler.run_cycle()

        self.assertEqual(self.scheduler.failures["rebalance"], 0)

    def test_a_blocked_nonce_is_not_counted_against_the_task(self):
        """Another task left a live transaction on the nonce.

        That is neither this task's fault nor an outage, and the task that owns
        the stuck transaction is the one that will alert about it. Counting it
        here would raise an alarm naming the wrong task and, with a long-enough
        block, drown the real one.
        """
        sent = []
        self.scheduler.notify = sent.append

        for _ in range(5):
            self.scheduler.record_failure(
                "oracle_report", NonceBlocked(42, "handleEpoch 7", ["0xabc"])
            )

        self.assertEqual(self.scheduler.failures["oracle_report"], 0)
        self.assertEqual(sent, [])

    def test_a_failure_breaks_a_run_of_skips(self):
        """ "Skipped three times in a row" has to mean in a row."""
        self.scheduler.record_skip("rebalance", "oracle value is incorrect")
        self.scheduler.record_failure("rebalance", RuntimeError("boom"))

        self.assertEqual(self.scheduler.skips["rebalance"], 0)

    def test_repeated_skips_alert(self):
        sent = []
        self.scheduler.notify = sent.append

        for _ in range(3):
            self.scheduler.record_skip("rebalance", "oracle value is incorrect")

        self.assertEqual(len(sent), 1)
        self.assertIn("oracle value is incorrect", sent[0])

    def test_a_task_stuck_skipping_keeps_alerting(self):
        """Like failures: one alert then silence is what this exists to prevent."""
        sent = []
        self.scheduler.notify = sent.append

        for _ in range(6):
            self.scheduler.record_skip("rebalance", "oracle value is incorrect")

        self.assertEqual(len(sent), 2)

    def test_backticks_in_an_error_cannot_break_the_alert(self):
        sent = []
        self.scheduler.notify = sent.append
        self.scheduler.failures["ascend"] = 2

        self.scheduler.record_failure("ascend", RuntimeError("bad `code` here"))

        self.assertEqual(len(sent), 1)
        self.assertNotIn("`code`", sent[0])

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

    def test_the_cycle_persists_the_schedule(self):
        """Serialising is covered elsewhere; this pins that the cycle calls it.

        Without the call a restart cannot know a slot was already served, which
        is the property the persisted schedule exists for.
        """
        saves = []
        self.scheduler._save_state = lambda: saves.append(1)
        self.clock.advance(10)

        self.scheduler.run_cycle()

        self.assertGreater(len(saves), 0)

    def test_rebalance_pins_force_withdrawal_off(self):
        """A stray FORCE_WITHDRAWAL must not pull the whole position back."""
        captured = {}

        def fake_rebalance(config, **kwargs):
            captured.update(kwargs)
            return []

        import scheduler as scheduler_module

        original = scheduler_module.run_rebalance
        scheduler_module.run_rebalance = fake_rebalance
        try:
            # The real method, not setUp's stub of the same name.
            Scheduler.task_rebalance(self.scheduler)
        finally:
            scheduler_module.run_rebalance = original

        self.assertIs(captured.get("force_withdrawal"), False)

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


class TestPersistedSchedule(unittest.TestCase):
    """A restart must not be able to skip a slot that was already due."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, "state.json")

    def tearDown(self):
        self.directory.cleanup()

    def test_a_restart_after_a_boundary_still_owes_the_run(self):
        boundary = 100 * DAY
        interval = DAY

        before = make_scheduler(
            make_config(task_intervals={"oracle_report": interval}),
            now=lambda: boundary - HOUR,
            state_path=self.path,
        )
        before._save_state()

        after = make_scheduler(
            make_config(task_intervals={"oracle_report": interval}),
            now=lambda: boundary + 60,
            state_path=self.path,
        )

        self.assertTrue(
            is_due(after.last_run["oracle_report"], boundary + 60, interval),
            "the slot was owed before the restart and is still owed after it",
        )

    def test_a_first_start_does_not_replay_the_current_bucket(self):
        fresh = make_scheduler(
            make_config(), now=lambda: 100 * DAY + HOUR, state_path=self.path
        )

        self.assertFalse(is_due(fresh.last_run["oracle_report"], 100 * DAY + HOUR, DAY))

    def test_a_corrupt_state_file_falls_back_to_now(self):
        with open(self.path, "w") as handle:
            handle.write("{ not json")

        scheduler = make_scheduler(
            make_config(), now=lambda: 100 * DAY, state_path=self.path
        )

        self.assertEqual(scheduler.last_run["ascend"], 100 * DAY)

    def test_unknown_tasks_in_the_file_are_ignored(self):
        import json

        with open(self.path, "w") as handle:
            json.dump({"ascend": 5.0, "retired_task": 1.0}, handle)

        scheduler = make_scheduler(
            make_config(), now=lambda: 100 * DAY, state_path=self.path
        )

        self.assertEqual(scheduler.last_run["ascend"], 5.0)
        self.assertNotIn("retired_task", scheduler.last_run)


class TestMissingExecutorKey(unittest.TestCase):
    """A task with no key must fail, not report success while doing nothing."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.directory.cleanup()

    def scheduler_without_a_key(self):
        from config.read_config import AscendConfig, SourceConfig, WithdrawalQueueConfig

        config = make_config()
        config.sources = [
            SourceConfig(
                name="OG",
                rpc="https://rpc.invalid",
                source_core_helper="0x" + "11" * 20,
                deployments=(),
                executor_private_key=None,
                ascend=AscendConfig(
                    router="0x" + "22" * 20, rewarders=("0x" + "33" * 20,)
                ),
                withdrawal_queue=WithdrawalQueueConfig(address="0x" + "44" * 20),
            )
        ]
        return make_scheduler(
            config, state_path=os.path.join(self.directory.name, "state.json")
        )

    def test_ascend_refuses_to_run_without_a_key(self):
        with self.assertRaises(Exception) as caught:
            self.scheduler_without_a_key().task_ascend()
        self.assertIn("OPERATOR_PK", str(caught.exception))

    def test_handle_epoch_refuses_to_run_without_a_key(self):
        with self.assertRaises(Exception):
            self.scheduler_without_a_key().task_handle_epoch()


class TestOracleTaskFailuresAreVisible(unittest.TestCase):
    """The oracle report is the 21-day heartbeat; its silence must be loud."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.scheduler = make_scheduler(
            make_config(),
            state_path=os.path.join(self.directory.name, "state.json"),
        )
        self._original = oracle_main_module.run_oracle_report

    def tearDown(self):
        oracle_main_module.run_oracle_report = self._original
        self.directory.cleanup()

    def test_a_failing_oracle_report_reaches_the_scheduler(self):
        async def boom(_config):
            raise RuntimeError("RPC down")

        oracle_main_module.run_oracle_report = boom

        with self.assertRaises(RuntimeError):
            self.scheduler.task_oracle_report()

    def test_the_standalone_entry_point_still_swallows(self):
        """main() is the standalone path and should exit cleanly, not traceback."""
        import asyncio

        async def boom(_config):
            raise RuntimeError("RPC down")

        oracle_main_module.run_oracle_report = boom

        asyncio.run(oracle_main_module.main())


class TestRetryBackoff(unittest.TestCase):
    """A pending retry replaces the schedule; the backoff is what paces it.

    Without the backoff a failing task runs every cycle -- for ascend that is
    real claim transactions every five minutes.
    """

    def setUp(self):
        self.clock = FakeClock(100 * DAY)
        self.directory = tempfile.TemporaryDirectory()
        self.config = make_config(
            task_intervals={"ascend": 14 * DAY}, post_ascend_gap_seconds=0
        )
        self.scheduler = make_scheduler(
            self.config,
            now=self.clock.time,
            sleep=self.clock.sleep,
            state_path=os.path.join(self.directory.name, "state.json"),
        )
        self.ran = []

        def failing():
            self.ran.append(1)
            raise RuntimeError("RPC exploded")

        self.scheduler.task_ascend = failing
        for task in ("rebalance", "oracle_report", "handle_epoch"):
            setattr(self.scheduler, "task_" + task, lambda: None)
        self.scheduler.last_run["ascend"] = 0

    def tearDown(self):
        self.directory.cleanup()

    def test_a_failure_does_not_run_again_on_the_next_cycle(self):
        self.scheduler.run_cycle()
        self.assertEqual(len(self.ran), 1)

        self.clock.advance(1)
        self.scheduler.run_cycle()

        self.assertEqual(len(self.ran), 1, "the backoff has not elapsed")

    def test_it_runs_once_the_backoff_elapses(self):
        self.scheduler.run_cycle()

        self.clock.advance(self.scheduler.retry_after["ascend"] - self.clock.now + 1)
        self.scheduler.run_cycle()

        self.assertEqual(len(self.ran), 2)

    def test_a_slow_failure_still_gets_its_backoff(self):
        """Measured from when the task finished, not when the cycle began.

        A task that fails because its sends timed out runs for longer than its
        own backoff, so a start-of-cycle timestamp puts the retry in the past
        and it runs again every cycle -- no pacing, for the one failure the
        backoff exists to pace.
        """
        clock = self.clock

        def slow_failure():
            self.ran.append(1)
            clock.advance(2400)
            raise RuntimeError("every send timed out")

        self.scheduler.task_ascend = slow_failure

        self.scheduler.run_cycle()

        self.assertGreater(self.scheduler.retry_after["ascend"], clock.now)

    def test_the_backoff_grows_with_consecutive_failures(self):
        delays = []
        for _ in range(3):
            self.scheduler.run_cycle()
            delays.append(self.scheduler.retry_after["ascend"] - self.clock.now)
            self.clock.advance(delays[-1] + 1)

        self.assertEqual(delays, sorted(delays))
        self.assertLess(delays[0], delays[-1])


if __name__ == "__main__":
    unittest.main()
