"""The oracle is brought back in line when rebalancing finds it out of line.

Until this existed the oracle was written only on its eight-hour heartbeat, and
nothing in between reconciled it with the value it tracks. Two things moved
that value in between and left rebalancing refusing until the next write:

- a deposit priced against the stale oracle in the seconds after a reward
  distribution, before the write that prices it. It takes shares at the old
  price and dilutes the share price below the value just written;
- W0G sent straight to the SourceCore, which raises the value without minting.
"""

import asyncio
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from config.read_config import (
    Config,
    Deployment,
    MIN_POST_ASCEND_GAP_SECONDS,
    DEFAULT_POST_ASCEND_GAP_SECONDS,
    OracleUpdateConfig,
    SchedulerConfig,
    SourceConfig,
    TxConfig,
    _create_scheduler_config,
)
from web3_scripts import oracle_update
from web3_scripts.base import ORACLE_VALUE_TOLERANCE, SECURE_INTERVAL
from web3_scripts.operator_bot import ORACLE_VALUE_INCORRECT
from web3_scripts.oracle_script import OracleValidationResult
from web3_scripts.oracle_update import (
    RESYNC_NOOP,
    RESYNC_REFUSE,
    RESYNC_WAIT,
    RESYNC_WRITE,
    OracleUpdateResult,
    resync_action,
    resync_floor,
    resync_oracle,
    value_before,
)
import main
import scheduler as scheduler_module

ORACLE = "0x" + "8f" * 20
KEY = "0x" + "11" * 32
DAY = 86400

# 2026-09-28, the incident this was written for. The heartbeat wrote WRITTEN
# over BEFORE; a deposit priced at BEFORE had already landed in the seconds
# between the oracle's read and its write, leaving the vault worth DILUTED.
BEFORE = 1119071245324487369
WRITTEN = 1119188080110290876
DILUTED = 1119188056500925596


def validation(oracle_value, actual_value, **overrides) -> OracleValidationResult:
    fields = dict(
        oracle_address=ORACLE,
        chain_id=16661,
        oracle_value=oracle_value,
        actual_value=actual_value,
        remaining_time=10**6,
        recently_updated=False,
        source_nonces=(1, 1),
        target_nonces=(1, 1),
        transfer_in_progress=False,
        almost_expired=False,
        incorrect_value=abs(oracle_value - actual_value) > ORACLE_VALUE_TOLERANCE,
    )
    fields.update(overrides)
    return OracleValidationResult(**fields)


def source(**overrides) -> SourceConfig:
    fields = dict(
        name="OG",
        rpc="https://rpc.invalid",
        source_core_helper="0x" + "11" * 20,
        deployments=(DEPLOYMENT,),
        tx=TxConfig(),
        oracle_update=OracleUpdateConfig(updater_private_key=KEY),
    )
    fields.update(overrides)
    return SourceConfig(**fields)


DEPLOYMENT = Deployment(
    name="OG", source_core="0x" + "22" * 20, target_core="0x" + "33" * 20
)


def config(*sources, telegram=False, **scheduler_kwargs) -> Config:
    intervals = scheduler_kwargs.pop(
        "task_intervals",
        {"ascend": DAY, "rebalance": 7200, "oracle_update": DAY, "handle_epoch": DAY},
    )
    return Config(
        telegram_bot_api_key="key" if telegram else "",
        telegram_group_chat_id="chat" if telegram else "",
        telegram_owner_nicknames={},
        telegram_proposal_message_prefix="",
        oracle_expiry_threshold_seconds=3600,
        oracle_recent_update_threshold_seconds=0,
        target_rpc="",
        target_core_helper="",
        sources=list(sources),
        scheduler=SchedulerConfig(task_intervals=intervals, **scheduler_kwargs),
    )


class Harness:
    """Stands in for every chain call resync_oracle makes."""

    def __init__(self, test, reading, previous=None):
        self.sends = []
        self.history_reads = 0
        self._previous = previous
        for name, replacement in (
            ("run_oracle_validation", lambda **_kwargs: reading),
            ("get_w3", lambda _rpc: SimpleNamespace()),
            ("get_contract", lambda *_a, **_k: self._oracle()),
            ("send_and_confirm", self._send),
            ("previous_oracle_value", self._history),
        ):
            original = getattr(oracle_update, name)
            setattr(oracle_update, name, replacement)
            test.addCleanup(setattr, oracle_update, name, original)

    def _oracle(self):
        return SimpleNamespace(
            functions=SimpleNamespace(setValue=lambda value: ("setValue", value))
        )

    def _history(self, _w3, _oracle, _current):
        self.history_reads += 1
        return self._previous

    def _send(self, contract_function, value, key, **kwargs):
        self.sends.append(contract_function[1])
        return SimpleNamespace(tx_hash="0xabc")


def resync(test, reading, previous=None):
    harness = Harness(test, reading, previous)
    result = resync_oracle(
        source=source(),
        deployment=DEPLOYMENT,
        target_rpc="",
        target_core_helper="",
        oracle_expiry_threshold_seconds=3600,
    )
    return result, harness


class TestSettlingGap(unittest.TestCase):
    """The gap is the part of the window between a distribution and the write
    that prices it which this bot controls, so it is kept short -- but the
    oracle is read SECURE_INTERVAL behind the chain, and a gap inside that reads
    a block from before the distribution and writes the old price back."""

    def test_the_minimum_clears_the_read_lag(self):
        self.assertGreater(MIN_POST_ASCEND_GAP_SECONDS, SECURE_INTERVAL)

    def test_the_default_leaves_margin_over_it(self):
        self.assertGreaterEqual(DEFAULT_POST_ASCEND_GAP_SECONDS, SECURE_INTERVAL + 5)

    def test_a_gap_inside_the_lag_is_rejected(self):
        with self.assertRaises(ValueError):
            _create_scheduler_config({"post_ascend_gap_seconds": SECURE_INTERVAL})

    def test_the_minimum_is_accepted(self):
        parsed = _create_scheduler_config(
            {"post_ascend_gap_seconds": MIN_POST_ASCEND_GAP_SECONDS}
        )
        self.assertEqual(parsed.post_ascend_gap_seconds, MIN_POST_ASCEND_GAP_SECONDS)


class TestFloor(unittest.TestCase):
    def test_it_is_the_value_the_last_write_replaced(self):
        self.assertEqual(resync_floor(WRITTEN, BEFORE), BEFORE)

    def test_after_a_fall_nothing_lower_is_explained(self):
        """If the last write lowered the value, no stale price below the
        current one ever existed for a deposit to have paid."""
        self.assertEqual(resync_floor(BEFORE, WRITTEN), BEFORE)

    def test_without_history_no_fall_is_allowed(self):
        self.assertEqual(resync_floor(WRITTEN, None), WRITTEN)


class TestValueBefore(unittest.TestCase):
    def test_the_write_before_the_current_one(self):
        self.assertEqual(value_before([WRITTEN, BEFORE, 1], WRITTEN), BEFORE)

    def test_writes_newer_than_the_read_are_skipped(self):
        """The oracle is read a few seconds behind the chain."""
        self.assertEqual(value_before([7, WRITTEN, BEFORE], WRITTEN), BEFORE)

    def test_an_unchanged_heartbeat_is_not_the_replacing_write(self):
        self.assertEqual(value_before([WRITTEN, WRITTEN, BEFORE], WRITTEN), BEFORE)

    def test_no_history_far_enough_back(self):
        self.assertIsNone(value_before([WRITTEN], WRITTEN))
        self.assertIsNone(value_before([BEFORE], WRITTEN))


class TestAction(unittest.TestCase):
    def test_in_line_is_left_alone(self):
        self.assertEqual(
            resync_action(WRITTEN, WRITTEN + ORACLE_VALUE_TOLERANCE, BEFORE, 100)[0],
            RESYNC_NOOP,
        )

    def test_a_rise_is_written(self):
        self.assertEqual(
            resync_action(WRITTEN, WRITTEN + 10**12, WRITTEN, 100)[0], RESYNC_WRITE
        )

    def test_a_fall_down_to_the_floor_is_written(self):
        self.assertEqual(resync_action(WRITTEN, BEFORE, BEFORE, 100)[0], RESYNC_WRITE)

    def test_a_fall_past_the_floor_is_refused(self):
        action, reason = resync_action(WRITTEN, BEFORE - 1, BEFORE, 100)
        self.assertEqual(action, RESYNC_REFUSE)
        self.assertIn("below", reason)

    def test_the_deviation_guard_still_applies(self):
        action, reason = resync_action(WRITTEN, WRITTEN * 102 // 100, WRITTEN, 100)
        self.assertEqual(action, RESYNC_REFUSE)
        self.assertIn("bps", reason)


class TestResyncOracle(unittest.TestCase):
    def test_the_incident_is_written_not_refused(self):
        """The heartbeat's decrease guard would refuse this 23.6 gwei fall; it
        is the fall a stale-priced deposit causes, so a resync writes it."""
        result, harness = resync(self, validation(WRITTEN, DILUTED), previous=BEFORE)

        self.assertEqual(result.action, RESYNC_WRITE)
        self.assertEqual(harness.sends, [DILUTED])
        self.assertEqual(result.floor, BEFORE)

    def test_a_donation_is_written(self):
        """W0G sent straight to the SourceCore raises the value without minting;
        a rise within the deviation guard is written back."""
        result, harness = resync(self, validation(WRITTEN, WRITTEN + 10**12))

        self.assertEqual(result.action, RESYNC_WRITE)
        self.assertEqual(harness.sends, [WRITTEN + 10**12])
        self.assertEqual(harness.history_reads, 0, "only a fall needs the history")

    def test_in_line_writes_nothing(self):
        """What a rebalance refusal right after a heartbeat looks like: the
        refusal read a block from before the write, the resync reads after."""
        result, harness = resync(self, validation(WRITTEN, WRITTEN))

        self.assertEqual(result.action, RESYNC_NOOP)
        self.assertEqual(harness.sends, [])

    def test_a_transfer_in_flight_waits(self):
        result, harness = resync(
            self, validation(WRITTEN, DILUTED, transfer_in_progress=True), BEFORE
        )

        self.assertEqual(result.action, RESYNC_WAIT)
        self.assertEqual(harness.sends, [])

    def test_a_loss_is_refused(self):
        result, harness = resync(self, validation(WRITTEN, BEFORE - 10**12), BEFORE)

        self.assertEqual(result.action, RESYNC_REFUSE)
        self.assertEqual(harness.sends, [])

    def test_a_fall_with_no_history_is_refused(self):
        result, harness = resync(self, validation(WRITTEN, DILUTED), previous=None)

        self.assertEqual(result.action, RESYNC_REFUSE)
        self.assertEqual(harness.sends, [])


class TestRunOracleResync(unittest.TestCase):
    def setUp(self):
        self._resync = main.resync_oracle
        self.addCleanup(setattr, main, "resync_oracle", self._resync)

    def _returns(self, *outcomes):
        queue = list(outcomes)

        def fake(**_kwargs):
            outcome = queue.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return oracle_update.ResyncResult(
                name="OG", action=outcome[0], reason=outcome[1]
            )

        main.resync_oracle = fake

    def test_a_write_closes_the_request(self):
        self._returns((RESYNC_WRITE, ""))
        summary = main.run_oracle_resync(config(source()))
        self.assertEqual(
            (summary.written, summary.pending, summary.refusals), (1, False, [])
        )

    def test_a_transfer_keeps_it_open(self):
        self._returns((RESYNC_WAIT, "OFT transfer in flight"))
        self.assertTrue(main.run_oracle_resync(config(source())).pending)

    def test_a_failure_keeps_it_open_and_does_not_raise(self):
        self._returns(RuntimeError("rpc down"))
        self.assertTrue(main.run_oracle_resync(config(source())).pending)

    def test_a_refusal_is_reported_by_name(self):
        self._returns((RESYNC_REFUSE, "would lower the value"))
        summary = main.run_oracle_resync(config(source()))
        self.assertFalse(summary.pending)
        self.assertEqual(summary.refusals, ["OG/OG: would lower the value"])


class FakeClock:
    def __init__(self, start):
        self.now = float(start)

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def advance(self, seconds):
        self.now += seconds


class SchedulerCase(unittest.TestCase):
    def make(self, cfg, start):
        self.clock = FakeClock(start)
        state = os.path.join(tempfile.mkdtemp(), "state.json")
        s = scheduler_module.Scheduler(
            cfg, now=self.clock.time, sleep=self.clock.sleep, state_path=state
        )
        self.alerts = []
        s.notify = self.alerts.append
        return s


class TestRebalanceAsksForAResync(SchedulerCase):
    def setUp(self):
        self._rebalance = scheduler_module.run_rebalance
        self.addCleanup(setattr, scheduler_module, "run_rebalance", self._rebalance)
        self.scheduler = self.make(config(), 100 * DAY)

    def _reasons(self, *reasons):
        scheduler_module.run_rebalance = lambda *_a, **_k: [
            ("OG:{}".format(i), reason) for i, reason in enumerate(reasons)
        ]

    def test_an_out_of_line_oracle_is_declined_and_resynced(self):
        self._reasons(ORACLE_VALUE_INCORRECT)

        self.assertIs(self.scheduler.task_rebalance(), False)
        self.assertTrue(self.scheduler.resync_requested)

    def test_other_refusals_keep_their_interval(self):
        self._reasons("OFT transfers in progress")

        self.assertIsNone(self.scheduler.task_rebalance())
        self.assertFalse(self.scheduler.resync_requested)

    def test_a_refused_resync_is_named_in_the_rebalance_alert(self):
        self._reasons(ORACLE_VALUE_INCORRECT)
        self.scheduler.resync_refusals = ["OG/OG: would lower the value below X"]

        for _ in range(80):  # well past the six-hour threshold, five minutes apart
            self.scheduler.task_rebalance()
            self.clock.advance(300)

        self.assertTrue(self.alerts)
        self.assertIn(
            "resync refused: OG/OG: would lower the value below X", self.alerts[0]
        )


class TestResyncRunsNextCycle(SchedulerCase):
    """The whole loop, with the chain replaced by what each task reports."""

    def setUp(self):
        self._rebalance = scheduler_module.run_rebalance
        self._resync = main.run_oracle_resync
        self.addCleanup(setattr, scheduler_module, "run_rebalance", self._rebalance)
        self.addCleanup(setattr, main, "run_oracle_resync", self._resync)
        self.order = []
        self.rebalance_reasons = []
        self.resync_summary = main.ResyncSummary()

        def rebalance(*_a, **_k):
            self.order.append("rebalance")
            return [("OG:OG", self.rebalance_reasons.pop(0))]

        def resync(_config, **_k):
            self.order.append("resync")
            return self.resync_summary

        scheduler_module.run_rebalance = rebalance
        main.run_oracle_resync = resync

        # Just past a rebalance boundary; oracle and ascend a day apart, so
        # neither heartbeat is due during the test.
        start = 100 * DAY + 60
        self.scheduler = self.make(config(), start)
        self.scheduler.last_run["rebalance"] = start - 7200
        for task in ("ascend", "handle_epoch"):
            setattr(self.scheduler, "task_" + task, lambda: None)

    def test_the_gap_is_closed_before_rebalancing_retries(self):
        self.rebalance_reasons = [ORACLE_VALUE_INCORRECT, None]

        self.scheduler.run_cycle()
        declined_at = self.scheduler.last_run["rebalance"]
        self.clock.advance(300)
        self.scheduler.run_cycle()

        self.assertEqual(self.order, ["rebalance", "resync", "rebalance"])
        self.assertLess(declined_at, self.scheduler.last_run["rebalance"])
        self.assertFalse(self.scheduler.resync_requested)

    def test_a_pending_resync_asks_again(self):
        self.rebalance_reasons = [ORACLE_VALUE_INCORRECT, ORACLE_VALUE_INCORRECT]
        self.resync_summary = main.ResyncSummary(pending=True)

        self.scheduler.run_cycle()
        self.clock.advance(300)
        self.scheduler.run_cycle()

        self.assertTrue(self.scheduler.resync_requested)

    def test_the_heartbeat_supersedes_a_request(self):
        self.scheduler.resync_requested = True
        self.scheduler.last_run["oracle_update"] = self.clock.now - DAY
        self.scheduler.task_oracle_update = lambda: self.order.append("heartbeat")
        self.rebalance_reasons = [None]

        self.scheduler.run_cycle()

        self.assertEqual(self.order, ["heartbeat", "rebalance"])
        self.assertFalse(self.scheduler.resync_requested)


class TestRefusalsArePaced(SchedulerCase):
    """A refused write is retried every cycle; announcing each retry sent the
    same alert every five minutes, and the skip alert added another every half
    hour. It is now said once, then once per oracle interval."""

    def setUp(self):
        self._run = main.run_oracle_update
        self.addCleanup(setattr, main, "run_oracle_update", self._run)
        self.scheduler = self.make(config(), 100 * DAY)
        self.announce_flags = []
        self.next = main.OracleRunSummary(refused=1)

        async def run(_config, **kwargs):
            self.announce_flags.append(kwargs["announce"])
            summary = self.next
            summary.announced = kwargs["announce"] and not summary.written
            return summary

        main.run_oracle_update = run

    def _cycle(self, seconds=300):
        self.scheduler.task_oracle_update()
        self.clock.advance(seconds)

    def test_said_once_then_held_until_the_interval_passes(self):
        for _ in range(12):  # an hour of retries
            self._cycle()

        self.assertEqual(self.announce_flags, [True] + [False] * 11)
        self.assertEqual(self.alerts, [], "the skip alert must not repeat it")

    def test_repeated_once_per_interval(self):
        self._cycle(DAY)
        self._cycle()

        self.assertEqual(self.announce_flags, [True, True])

    def test_a_write_resets_it(self):
        self._cycle()
        self.next = main.OracleRunSummary(written=1)
        self._cycle()
        self.next = main.OracleRunSummary(refused=1)
        self._cycle()

        self.assertEqual(self.announce_flags, [True, False, True])

    def test_a_skip_still_alerts_on_its_own(self):
        """A transfer in flight has no alert of its own; the skip alert is it."""
        self.next = main.OracleRunSummary(skip_reasons=["OG/OG: in flight"])

        for _ in range(8):
            self._cycle()

        self.assertEqual(len(self.alerts), 1)


class TestHeldBackAnnouncement(unittest.TestCase):
    """run_oracle_update's side of the pacing."""

    def setUp(self):
        self.sent = []
        for name in ("update_oracles", "send_message", "print_telegram_info"):
            self.addCleanup(setattr, main, name, getattr(main, name))

        async def send(_key, _chat, message):
            self.sent.append(message)
            return True

        async def info(*_a):
            return None

        main.send_message = send
        main.print_telegram_info = info

    def _results(self, written, alerts):
        result = OracleUpdateResult(name="OG", written=written, alerts=list(alerts))
        main.update_oracles = lambda _c, **_k: ([(source(), result)], [])

    def test_a_refusal_is_held_back_when_asked(self):
        self._results(False, ["refused: would lower the value"])

        summary = asyncio.run(
            main.run_oracle_update(config(source(), telegram=True), announce=False)
        )

        self.assertEqual(self.sent, [])
        self.assertFalse(summary.announced)
        self.assertEqual(summary.refused, 1)

    def test_a_refusal_is_sent_otherwise(self):
        self._results(False, ["refused: would lower the value"])

        summary = asyncio.run(main.run_oracle_update(config(source(), telegram=True)))

        self.assertEqual(len(self.sent), 1)
        self.assertTrue(summary.announced)

    def test_what_accompanies_a_write_is_never_held_back(self):
        """A low-gas warning rides on a successful write."""
        self._results(True, ["oracle updater is low on gas"])

        asyncio.run(
            main.run_oracle_update(config(source(), telegram=True), announce=False)
        )

        self.assertEqual(len(self.sent), 1)


if __name__ == "__main__":
    unittest.main()
