import json
import os
import sys
import tempfile
import unittest

# Add the src directory to the Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from config.read_config import Config, SchedulerConfig
from scheduler import (
    Scheduler,
    TASK_ORDER,
    format_duration,
    is_due,
    next_due,
)
from web3_scripts.tx import NonceBlocked
import main as oracle_main_module

DAY = 86400
HOUR = 3600


def make_config(**scheduler_kwargs) -> Config:
    # Deliberately not the production numbers: these exercise the bucket
    # arithmetic, and spreading them across four magnitudes is what makes a
    # due/not-due bug visible. The real intervals are asserted against
    # config.json in test_oracle_expiry_window.
    intervals = scheduler_kwargs.pop(
        "task_intervals",
        {
            "ascend": DAY,
            "rebalance": 7200,
            "oracle_update": DAY,
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


class _NullLock:
    """Stands in for ProcessLock where the test is not about locking."""

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


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

    def test_omitting_a_task_leaves_it_on_its_default(self):
        """Not a way to stop it -- the comment beside this once claimed it was."""
        from config.read_config import DEFAULT_TASK_INTERVALS, _create_scheduler_config

        config = _create_scheduler_config(
            {"tasks": {"ascend": {"interval_seconds": 60}}}
        )

        self.assertEqual(config.interval("ascend"), 60)
        self.assertEqual(
            config.interval("handle_epoch"), DEFAULT_TASK_INTERVALS["handle_epoch"]
        )

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
            next_due(first.last_run["oracle_update"], DAY),
            next_due(restarted.last_run["oracle_update"], DAY),
        )

    def test_restart_does_not_replay_a_bucket_already_served(self):
        started = 10 * DAY + 5 * HOUR
        scheduler = make_scheduler(make_config(), now=lambda: started)
        self.assertFalse(is_due(scheduler.last_run["oracle_update"], started, DAY))

    def test_a_crossed_boundary_still_fires_after_a_restart(self):
        started = 10 * DAY + 5 * HOUR
        scheduler = make_scheduler(make_config(), now=lambda: started)
        self.assertTrue(is_due(scheduler.last_run["oracle_update"], 11 * DAY + 1, DAY))


class TestCycleIsolation(unittest.TestCase):

    def setUp(self):
        self.clock = FakeClock(10 * DAY)
        # Every task due immediately.
        self.config = make_config(
            task_intervals={
                "ascend": 1,
                "rebalance": 1,
                "oracle_update": 1,
                "handle_epoch": 1,
            },
            post_ascend_gap_seconds=0,
        )
        self.scheduler = make_scheduler(
            self.config, now=self.clock.time, sleep=self.clock.sleep
        )
        self.ran = []
        for task in ("ascend", "rebalance", "oracle_update", "handle_epoch"):
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
            self.ran, ["ascend", "oracle_update", "rebalance", "handle_epoch"]
        )
        self.assertEqual(self.scheduler.failures["rebalance"], 1)
        self.assertEqual(self.scheduler.failures["ascend"], 0)

    def test_the_oracle_is_refreshed_before_rebalancing(self):
        """Rebalancing refuses while the oracle disagrees with the computed
        value, and ascend is what makes them disagree. Running rebalance first
        therefore guaranteed a refusal on every pass that followed a reward
        distribution -- the ordering is the fix, so it is worth binding.
        """
        self.clock.advance(10)

        self.scheduler.run_cycle()

        self.assertLess(
            self.ran.index("oracle_update"),
            self.ran.index("rebalance"),
            "the oracle must be written before rebalancing reads it",
        )
        self.assertLess(
            self.ran.index("ascend"),
            self.ran.index("oracle_update"),
            "ascend moves the value the oracle is about to record",
        )

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
                "oracle_update", NonceBlocked(42, "handleEpoch 7", ["0xabc"])
            )

        self.assertEqual(self.scheduler.failures["oracle_update"], 0)
        self.assertEqual(sent, [])

    def test_a_failure_breaks_a_run_of_skips(self):
        """ "Skipped three times in a row" has to mean in a row."""
        self.scheduler.record_skip("rebalance", "oracle value is incorrect")
        self.scheduler.record_failure("rebalance", RuntimeError("boom"))

        self.assertEqual(self.scheduler.skips["rebalance"], 0)

    def test_repeated_skips_alert(self):
        """Judged by how long the run has lasted, not how many attempts it took.

        Rebalance's patience is unchanged: `alert_after_failures` of its own
        scheduled runs, which at a two-hour interval is six hours.
        """
        sent = []
        self.scheduler.notify = sent.append
        # Expressed in the task's own threshold rather than in hours: this
        # fixture gives every task a one-second interval so that everything is
        # due at once, which would make any wall-clock figure meaningless.
        threshold = self.scheduler.skip_alert_after("rebalance")

        for _ in range(2):
            self.scheduler.record_skip("rebalance", "oracle value is incorrect")
            self.clock.advance(threshold)

        self.assertEqual(len(sent), 1)
        self.assertIn("oracle value is incorrect", sent[0])

    def test_a_task_stuck_skipping_keeps_alerting(self):
        """Like failures: one alert then silence is what this exists to prevent."""
        sent = []
        self.scheduler.notify = sent.append
        threshold = self.scheduler.skip_alert_after("rebalance")

        for _ in range(3):
            self.scheduler.record_skip("rebalance", "oracle value is incorrect")
            self.clock.advance(threshold)

        self.assertEqual(len(sent), 2, "one per threshold, not one and then silence")

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
            make_config(task_intervals={"oracle_update": interval}),
            now=lambda: boundary - HOUR,
            state_path=self.path,
        )
        before._save_state()

        after = make_scheduler(
            make_config(task_intervals={"oracle_update": interval}),
            now=lambda: boundary + 60,
            state_path=self.path,
        )

        self.assertTrue(
            is_due(after.last_run["oracle_update"], boundary + 60, interval),
            "the slot was owed before the restart and is still owed after it",
        )

    def test_a_first_start_does_not_replay_the_current_bucket(self):
        fresh = make_scheduler(
            make_config(), now=lambda: 100 * DAY + HOUR, state_path=self.path
        )

        self.assertFalse(is_due(fresh.last_run["oracle_update"], 100 * DAY + HOUR, DAY))

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


class TestOracleSkipsAreTracked(unittest.TestCase):
    """A run of skips means the oracle quietly stopped being written.

    Skipping is the right call while a cross-chain transfer is in flight, and a
    single skip is an ordinary few minutes of the day -- so it is not alerted
    on. What must not happen is skipping forever while the task reports success
    every time, which is indistinguishable from a healthy bot until the vault
    freezes.
    """

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.clock = FakeClock(100 * DAY)
        self.scheduler = make_scheduler(
            make_config(),
            now=self.clock.time,
            sleep=self.clock.sleep,
            state_path=os.path.join(self.directory.name, "state.json"),
        )
        self.alerts = []
        self.scheduler.notify = self.alerts.append
        self._original = oracle_main_module.run_oracle_update

    def tearDown(self):
        oracle_main_module.run_oracle_update = self._original
        self.directory.cleanup()

    def _returns(self, summary):
        async def run(_config):
            return summary

        oracle_main_module.run_oracle_update = run

    def test_a_single_skip_does_not_alert(self):
        self._returns(
            oracle_main_module.OracleRunSummary(skip_reasons=["OG/OG: in flight"])
        )

        self.scheduler.task_oracle_update()

        self.assertEqual(self.scheduler.skips["oracle_update"], 1)
        self.assertEqual(self.alerts, [])

    def test_a_run_of_skips_alerts(self):
        self._returns(
            oracle_main_module.OracleRunSummary(skip_reasons=["OG/OG: in flight"])
        )

        for _ in range(8):  # forty minutes of five-minute retries
            self.scheduler.task_oracle_update()
            self.clock.advance(300)

        self.assertEqual(len(self.alerts), 1)
        self.assertIn("in flight", self.alerts[0])
        self.assertIn("in flight", self.alerts[0])

    def test_a_write_clears_the_run(self):
        """Otherwise an occasional transfer accumulates into a false alarm."""
        self._returns(
            oracle_main_module.OracleRunSummary(skip_reasons=["OG/OG: in flight"])
        )
        self.scheduler.task_oracle_update()

        self._returns(oracle_main_module.OracleRunSummary(written=1))
        self.scheduler.task_oracle_update()

        self.assertEqual(self.scheduler.skips["oracle_update"], 0)

    def test_a_partial_run_is_not_a_skip(self):
        """One deployment skipped while another was written is not the oracle
        going unwritten, and counting it would alert on a healthy bot."""
        self._returns(
            oracle_main_module.OracleRunSummary(
                skip_reasons=["OG/OG: in flight"], written=1
            )
        )

        self.scheduler.task_oracle_update()

        self.assertEqual(self.scheduler.skips["oracle_update"], 0)


class TestOracleTaskFailuresAreVisible(unittest.TestCase):
    """The oracle write is what keeps the vault open; its silence must be loud."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.scheduler = make_scheduler(
            make_config(),
            state_path=os.path.join(self.directory.name, "state.json"),
        )
        self._original = oracle_main_module.run_oracle_update

    def tearDown(self):
        oracle_main_module.run_oracle_update = self._original
        self.directory.cleanup()

    def test_a_failing_oracle_update_reaches_the_scheduler(self):
        async def boom(_config):
            raise RuntimeError("RPC down")

        oracle_main_module.run_oracle_update = boom

        with self.assertRaises(RuntimeError):
            self.scheduler.task_oracle_update()

    def test_the_standalone_entry_point_still_swallows(self):
        """main() is the standalone path and should exit cleanly, not traceback.

        The lock is stubbed out: main() takes the real configured path, and a
        test that grabs it would contend with a scheduler running on the same
        machine. Whether it takes the lock at all is covered separately.
        """
        import asyncio

        original_lock = oracle_main_module.ProcessLock
        oracle_main_module.ProcessLock = lambda _path: _NullLock()
        self.addCleanup(setattr, oracle_main_module, "ProcessLock", original_lock)

        async def boom(_config):
            raise RuntimeError("RPC down")

        oracle_main_module.run_oracle_update = boom

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
        for task in ("rebalance", "oracle_update", "handle_epoch"):
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


class TestOracleFollowsAscend(unittest.TestCase):
    """The oracle owes a write whenever ascend has run since it last did.

    Distributing rewards is the only thing that moves the share price, so
    between ascend and the write that follows it the oracle is knowingly wrong:
    rebalance refuses against it, and deposits and withdrawals price against the
    stale figure. Sharing an interval is not enough, because the two drift apart
    exactly when it matters.
    """

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, "state.json")
        self.clock = FakeClock(100 * DAY)

    def tearDown(self):
        self.directory.cleanup()

    def _scheduler(self, saved=None):
        if saved is not None:
            with open(self.path, "w") as handle:
                json.dump(saved, handle)
        # sleep is the fake one: run_cycle waits out the post-ascend settling
        # gap, and a real sleep here makes the suite take a minute per test.
        return make_scheduler(
            make_config(),
            now=self.clock.time,
            sleep=self.clock.sleep,
            state_path=self.path,
        )

    def test_a_renamed_task_still_catches_up(self):
        """The deployment case. The saved schedule knows `oracle_report`, which
        no longer exists, so `oracle_update` is seeded to now and looks fresher
        than an ascend that last ran days ago -- the task that most needs to
        catch up is the one that looks newest."""
        scheduler = self._scheduler(
            {
                "ascend": 100 * DAY - 3 * DAY,
                "oracle_report": 100 * DAY - 2 * DAY,
                "rebalance": 100 * DAY,
                "handle_epoch": 100 * DAY,
            }
        )

        self.assertFalse(
            is_due(scheduler.last_run["oracle_update"], self.clock.time(), DAY),
            "seeded to now, so the interval alone would not run it",
        )
        self.assertTrue(scheduler.owes_dependency("oracle_update"))

    def test_a_stale_oracle_after_ascend_runs_is_owed(self):
        scheduler = self._scheduler(
            {"ascend": 100 * DAY - DAY, "oracle_update": 100 * DAY - DAY}
        )
        self.assertFalse(scheduler.owes_dependency("oracle_update"))

        scheduler.last_run["ascend"] = self.clock.time()

        self.assertTrue(scheduler.owes_dependency("oracle_update"))

    def test_an_oracle_newer_than_ascend_owes_nothing(self):
        """Steady state. The rule must cost nothing when they are in step, or it
        would force a write every cycle."""
        scheduler = self._scheduler(
            {"ascend": 100 * DAY - DAY, "oracle_update": 100 * DAY - DAY + 60}
        )

        self.assertFalse(scheduler.owes_dependency("oracle_update"))

    def test_a_first_ever_start_owes_nothing(self):
        """No saved file at all: neither has run, so neither is behind."""
        scheduler = self._scheduler()

        self.assertFalse(scheduler.owes_dependency("oracle_update"))

    def test_a_task_with_no_dependency_is_unaffected(self):
        scheduler = self._scheduler({"ascend": 100 * DAY - DAY})

        for task in ("ascend", "rebalance", "handle_epoch"):
            self.assertFalse(scheduler.owes_dependency(task))

    def test_the_rule_runs_the_task_in_the_same_cycle(self):
        """End to end through run_cycle, and before rebalance reads the value."""
        scheduler = self._scheduler(
            {
                "ascend": 100 * DAY - 3 * DAY,
                "oracle_report": 100 * DAY - 2 * DAY,
                "rebalance": 100 * DAY - DAY,
                "handle_epoch": 100 * DAY - DAY,
            }
        )
        ran = []
        for task in TASK_ORDER:
            setattr(scheduler, "task_" + task, (lambda t: lambda: ran.append(t))(task))

        scheduler.run_cycle()

        self.assertIn("oracle_update", ran)
        self.assertLess(ran.index("oracle_update"), ran.index("rebalance"))

    def test_a_pending_retry_still_wins(self):
        """A failing oracle must not be hammered once per cycle by the rule."""
        scheduler = self._scheduler(
            {"ascend": 100 * DAY - 3 * DAY, "oracle_report": 100 * DAY - 2 * DAY}
        )
        self.assertTrue(scheduler.owes_dependency("oracle_update"))
        scheduler.retry_after["oracle_update"] = self.clock.time() + HOUR

        ran = []
        for task in TASK_ORDER:
            setattr(scheduler, "task_" + task, (lambda t: lambda: ran.append(t))(task))

        scheduler.run_cycle()

        self.assertNotIn("oracle_update", ran)


class TestTheHeartbeatKeepsItsInterval(unittest.TestCase):
    """Regression for a write every loop instead of every eight hours.

    The set of tasks we have a run record for was seeded from the saved
    schedule and never added to, so a task missing from that file was
    permanently "owed" by the dependency rule and the interval never got a say.
    Every one of these drives more than one cycle: the whole class of bug is
    invisible to a test that runs a single cycle, and until this file none did.
    """

    #: What the pre-PR scheduler wrote. `oracle_report` no longer exists, so
    #: loading it leaves oracle_update with no record while ascend has one --
    #: the shape every real upgrade of this bot starts from.
    PRE_RENAME = {
        "ascend": 100 * DAY - 3 * DAY,
        "rebalance": 100 * DAY - HOUR,
        "oracle_report": 100 * DAY - 2 * DAY,
        "handle_epoch": 100 * DAY - 300,
    }

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, "state.json")
        self.clock = FakeClock(100 * DAY)
        self.calls = []

    def tearDown(self):
        self.directory.cleanup()

    def _scheduler(self, saved, acted=True):
        with open(self.path, "w") as handle:
            json.dump(saved, handle)
        scheduler = make_scheduler(
            make_config(
                task_intervals={
                    "ascend": DAY,
                    "rebalance": 7200,
                    "oracle_update": DAY,
                    "handle_epoch": 300,
                },
                post_ascend_gap_seconds=0,
                loop_sleep_seconds=300,
            ),
            now=self.clock.time,
            sleep=self.clock.sleep,
            state_path=self.path,
        )
        scheduler.notify = lambda _text: None
        for task in ("ascend", "rebalance", "handle_epoch"):
            setattr(scheduler, "task_" + task, lambda: None)

        def oracle():
            self.calls.append(self.clock.time())
            return acted

        scheduler.task_oracle_update = oracle
        return scheduler

    def _drive(self, scheduler, cycles):
        for _ in range(cycles):
            scheduler.run_cycle()
            self.clock.advance(300)

    def test_it_catches_up_once_and_then_holds_its_interval(self):
        scheduler = self._scheduler(self.PRE_RENAME)

        self._drive(scheduler, 12)  # an hour of loops

        self.assertEqual(
            len(self.calls),
            1,
            "caught up once, then every loop -- the interval never applied",
        )

    def test_a_completed_run_is_recorded_so_the_catch_up_is_one_shot(self):
        scheduler = self._scheduler(self.PRE_RENAME)

        self._drive(scheduler, 2)

        self.assertIn("oracle_update", scheduler._has_record)
        self.assertFalse(scheduler.owes_dependency("oracle_update"))

    def test_it_still_runs_again_when_the_interval_comes_round(self):
        """The fix must not turn the catch-up into a task that never fires."""
        scheduler = self._scheduler(self.PRE_RENAME)

        self._drive(scheduler, 2)
        self.clock.advance(DAY)
        self._drive(scheduler, 1)

        self.assertEqual(len(self.calls), 2)

    def test_a_declined_run_is_not_recorded_and_retries(self):
        """A skip must not be credited as a run.

        Recording it moves last_run past ascend's, marks the dependency paid,
        and leaves the distribution ascend just made unpriced until the next
        boundary -- with rebalancing refusing throughout.
        """
        scheduler = self._scheduler(self.PRE_RENAME, acted=False)

        self._drive(scheduler, 6)

        self.assertEqual(len(self.calls), 6, "it must keep trying")
        self.assertNotIn("oracle_update", scheduler._has_record)
        self.assertTrue(scheduler.owes_dependency("oracle_update"))

    def test_it_writes_as_soon_as_the_blocker_clears(self):
        scheduler = self._scheduler(self.PRE_RENAME, acted=False)
        self._drive(scheduler, 3)

        def oracle_now_acts():
            self.calls.append(self.clock.time())
            return True

        scheduler.task_oracle_update = oracle_now_acts
        self._drive(scheduler, 1)
        before = len(self.calls)
        self._drive(scheduler, 6)

        self.assertEqual(len(self.calls), before, "and then goes quiet again")
        self.assertIn("oracle_update", scheduler._has_record)


class TestSkipAlertsMeasureTime(unittest.TestCase):
    """Counting attempts made the threshold mean whatever loop_sleep was.

    With a retry every loop, three attempts is fifteen minutes where the
    threshold was meant to express a much longer patience.
    """

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.clock = FakeClock(100 * DAY)
        self.alerts = []
        self.scheduler = make_scheduler(
            make_config(loop_sleep_seconds=300),
            now=self.clock.time,
            sleep=self.clock.sleep,
            state_path=os.path.join(self.directory.name, "state.json"),
        )
        self.scheduler.notify = self.alerts.append

    def tearDown(self):
        self.directory.cleanup()

    def test_frequent_retries_do_not_bring_the_alert_forward(self):
        for _ in range(5):  # 25 minutes of five-minute retries
            self.scheduler.record_skip("oracle_update", "transfer in flight")
            self.clock.advance(300)

        self.assertEqual(self.alerts, [], "under the half-hour fuse")

    def test_it_alerts_once_the_run_has_lasted_long_enough(self):
        for _ in range(8):  # 40 minutes
            self.scheduler.record_skip("oracle_update", "transfer in flight")
            self.clock.advance(300)

        self.assertEqual(len(self.alerts), 1)
        self.assertIn("unable to act", self.alerts[0])

    def test_it_repeats_rather_than_alerting_once(self):
        for _ in range(24):  # two hours
            self.scheduler.record_skip("oracle_update", "transfer in flight")
            self.clock.advance(300)

        self.assertEqual(
            len(self.alerts), 3, "one per half hour, the fourth not yet due"
        )

    def test_acting_clears_the_run(self):
        for _ in range(8):
            self.scheduler.record_skip("oracle_update", "transfer in flight")
            self.clock.advance(300)
        self.scheduler.clear_skips("oracle_update")
        self.alerts.clear()

        for _ in range(5):
            self.scheduler.record_skip("oracle_update", "transfer in flight")
            self.clock.advance(300)

        self.assertEqual(self.alerts, [], "the window restarts from zero")

    def test_rebalance_keeps_its_original_patience(self):
        """Its threshold was three scheduled runs; changing the oracle's fuse
        must not shorten a task that legitimately declines for hours."""
        self.assertEqual(
            self.scheduler.skip_alert_after("rebalance"),
            3 * 7200,
        )


if __name__ == "__main__":
    unittest.main()
