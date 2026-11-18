import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import daily_report as dr
from daily_report import (
    RED,
    YELLOW,
    BotStats,
    ChainFacts,
    Process,
    ServiceLogStats,
    Stake,
)

HOUR = 3600
E18 = 10**18


def ts(text):
    return time.mktime(time.strptime(text, "%Y-%m-%d %H:%M:%S"))


def cycle(at, *lines):
    return "\n".join(["", "-" * 42, "TIMESTAMP: " + at, "-" * 42] + list(lines))


HEARTBEAT = (
    "[ascend] running...",
    "claim 0xabc: Transaction sent: 0x11 (nonce 100, attempt 1)",
    "claim 0xabc: Transaction mined in block: 5. Chain id: 16661",
    "\x1b[32mDistributed 1700000000000000000000 of 1700000000000000000000 across 2 recipient(s)\x1b[0m",
    "oracle setValue: Transaction sent: 0x22 (nonce 101, attempt 1)",
    "oracle setValue: Transaction mined in block: 6. Chain id: 16661",
    "\x1b[32mOG: setValue(1124566812590002992) confirmed in 0x5f07 (was 1124449223349950467, +1.05 bps)\x1b[0m",
    "[rebalance] declined to act; still due next cycle",
)
RESYNC_NOOP = (
    "[oracle_update] resync: rebalancing found the oracle out of line",
    "Oracle(...) is up to date (value=1124566812590002992). Remaining time: 503.9 hours",
)


class TestBotLog(unittest.TestCase):
    def parse(self, *cycles, since="2026-10-12 00:00:00"):
        return dr.parse_bot_log("\n".join(cycles), ts(since))

    def test_heartbeat_then_resync_is_the_expected_noop(self):
        stats = self.parse(
            cycle("2026-10-13 08:02:00", *HEARTBEAT),
            cycle("2026-10-13 08:07:00", *RESYNC_NOOP),
        )
        self.assertEqual(len(stats.heartbeats), 1)
        self.assertEqual(stats.heartbeats[0][3], 1.05)
        self.assertEqual(stats.recent_steps, [1.05])
        self.assertEqual(stats.recent_distributions, [1700 * E18])
        self.assertEqual(len(stats.resyncs), 1)
        self.assertTrue(stats.resyncs[0].after_heartbeat)
        self.assertEqual(stats.resyncs[0].outcome, "noop")

    def test_resync_without_a_heartbeat_before_it_is_flagged(self):
        stats = self.parse(
            cycle("2026-10-13 09:00:00", "[handle_epoch] running..."),
            cycle("2026-10-13 09:05:00", *RESYNC_NOOP),
        )
        self.assertFalse(stats.resyncs[0].after_heartbeat)

    def test_resync_outcomes(self):
        stats = self.parse(
            cycle(
                "2026-10-13 09:05:00",
                "[oracle_update] resync: rebalancing found the oracle out of line",
                "OG: resync setValue(5) confirmed in 0x99 (was 4, +0.1000 bps)",
            ),
            cycle(
                "2026-10-13 09:10:00",
                "[oracle_update] resync: rebalancing found the oracle out of line",
                "OG: resync refused: would lower the value below 3",
            ),
        )
        self.assertEqual([r.outcome for r in stats.resyncs], ["write", "refused"])
        # A resync write is not a heartbeat.
        self.assertEqual(stats.heartbeats, [])
        self.assertEqual(stats.refusals, [])

    def test_failures_tracebacks_and_fee_bumps(self):
        stats = self.parse(
            cycle(
                "2026-10-13 09:00:00",
                "[handle_epoch] failed (1 in a row): 502 Server Error",
                "Traceback (most recent call last):",
                "claim: Transaction sent: 0x1 (nonce 7, attempt 2)",
                "claim: Transaction mined in block: 1. Chain id: 1",
            )
        )
        self.assertEqual([(t, n) for _, t, n in stats.failures], [("handle_epoch", 1)])
        self.assertEqual(stats.tracebacks, 1)
        self.assertEqual(stats.retries, 1)
        self.assertEqual(stats.last_nonce, {1: 7})

    def test_window_bounds_the_counts_but_not_the_latest_state(self):
        stats = self.parse(
            cycle("2026-10-01 08:02:00", *HEARTBEAT),
            cycle("2026-10-13 09:00:00", "[handle_epoch] running..."),
        )
        self.assertEqual(stats.heartbeats, [])
        self.assertEqual(stats.cycles, 1)
        # Nonces and steps are about the latest state, wherever they are.
        self.assertEqual(stats.last_nonce, {16661: 101})
        self.assertEqual(stats.recent_steps, [1.05])

    def test_cross_chain_actions(self):
        stats = self.parse(
            cycle(
                "2026-10-13 16:08:00",
                "Assets surplus. Current ratio: 10.2%",
                "\x1b[33mSourceCore.pushToTarget{value:5}()\x1b[0m",
                "\x1b[33mTargetCore.deposit(1390524961995000000000000)\x1b[0m",
            )
        )
        self.assertEqual(
            [what for _, what in stats.cross_chain],
            [
                "Assets surplus. Current ratio: 10.2%",
                "SourceCore.pushToTarget",
                "TargetCore.deposit",
            ],
        )

    def test_heartbeat_refusal(self):
        stats = self.parse(
            cycle(
                "2026-10-13 08:02:00",
                "OG: refused: OG would move the value by 150.00 bps (limit 100 bps)",
                "OG: refused: OG would move the value by 150.00 bps (limit 100 bps)",
            )
        )
        self.assertEqual(len(stats.refusals), 1)


class TestServiceLogs(unittest.TestCase):
    def test_service_log_window_and_counts(self):
        text = "\n".join(
            [
                "2026-10-04T07:00:00.1Z ERROR app::poller: old",
                "\x1b[2m2026-10-13T07:37:23.6Z\x1b[0m \x1b[31mERROR\x1b[0m app::poller: iteration failed: <title>502 Bad Gateway</title>",
                "2026-10-13T07:38:00.0Z  INFO chain_utils::transactor: new transaction sent with hash 0xab, tx_info: Deposit(0, 1",
                "2026-10-13T07:38:07.0Z  INFO chain_utils::transactor: transaction 0xab success.",
            ]
        )
        import calendar

        since = calendar.timegm(
            time.strptime("2026-10-13T00:00:00", "%Y-%m-%dT%H:%M:%S")
        )
        stats = dr.parse_service_log(text, since)
        self.assertEqual((stats.warnings, stats.bad_gateway), (1, 1))
        self.assertEqual((stats.tx_sent, stats.tx_ok, stats.tx_failed), (1, 1, 0))

    def test_vault_monitor(self):
        text = "\n".join(
            [
                "[2026-10-03 22:46:14] FAILED  | beacon API unreachable",
                "[2026-10-13 09:00:00] SUCCESS | Index: 0",
                "    Logs:   0g staked: 1",
                "[2026-10-13 09:00:01] FAILED  | Index: 1",
            ]
        )
        stats = dr.parse_vault_monitor_log(text, ts("2026-10-13 00:00:00"))
        self.assertEqual((stats.vm_success, stats.vm_failed), (1, 1))

    def test_tail_since_reaches_back_far_enough(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            for h in range(48):
                f.write(
                    "2026-10-{}T{:02d}:00:00.0Z INFO line {}\n".format(
                        12 + h // 24, h % 24, h
                    )
                    * 1
                )
                f.write("x" * 200 + "\n")
            path = f.name
        try:
            import calendar

            since = calendar.timegm(
                time.strptime("2026-10-12T20:00:00", "%Y-%m-%dT%H:%M:%S")
            )
            text = dr.tail_since(path, since, dr._iso_first_ts, start=512)
            self.assertIn("line 20", text)
        finally:
            os.unlink(path)


def healthy_chain():
    c = ChainFacts(
        source_value=1_137_035 * E18,
        target_value=13_599_969 * E18,
        total_supply=13_104_605 * E18,
        withdrawal_demand=677_000 * E18,
        mellow_pending=0,
        mellow_claimable=0,
        gas={"operator_og": 920.0, "operator_eth": 0.179, "service_og": 19.7},
        chain_ids={"0G": 16661, "Ethereum": 1},
        next_nonce={16661: 102, 1: 44},
        stakes=[
            Stake("0", *[4_623_989 * E18] * 4),
            Stake("1", *[4_487_990 * E18] * 4),
            Stake("3", *[4_487_990 * E18] * 4),
        ],
        relayed_block=26_438_832,
        eth_head=26_438_960,
    )
    c.oracle_value = int(dr.share_price(c) * E18)
    return c


def healthy_bot():
    return BotStats(
        heartbeats=[(0, 1, "0x1", 1.05)] * 3,
        recent_steps=[1.05] * 8,
        last_nonce={16661: 101, 1: 43},
    )


PROCS = [Process(n, 100) for n, _ in dr.PROCESSES]
AGES = {"bot": 60, "restaking": 300, "monitor": 5, "vault-monitor": 120}


def run_eval(bot=None, chain=None, procs=PROCS, ages=AGES, **logs):
    return dr.evaluate(
        bot or healthy_bot(),
        chain or healthy_chain(),
        procs,
        ages,
        logs.get("restaking", ServiceLogStats()),
        logs.get("monitor", ServiceLogStats()),
        logs.get("vault_monitor", ServiceLogStats()),
        expected_beats=3,
    )


class TestRules(unittest.TestCase):
    def test_healthy_has_no_findings(self):
        self.assertEqual(run_eval(), [])

    def test_down_process_and_silent_log(self):
        procs = [Process("bot", None)] + PROCS[1:]
        ages = dict(AGES, monitor=3600)
        findings = run_eval(procs=procs, ages=ages)
        self.assertEqual({f.level for f in findings}, {RED})
        self.assertEqual(len(findings), 2)

    def test_missing_heartbeat(self):
        bot = healthy_bot()
        bot.heartbeats = bot.heartbeats[:2]
        self.assertIn(
            "2 oracle heartbeat(s), expected 3", [f.text for f in run_eval(bot=bot)]
        )

    def test_failure_is_red_until_the_task_recovers(self):
        bot = healthy_bot()
        bot.failures = [(100, "handle_epoch", 1)]
        self.assertEqual(run_eval(bot=bot)[0].level, RED)
        bot.recoveries = [(400, "handle_epoch")]
        [f] = run_eval(bot=bot)
        self.assertEqual(f.level, YELLOW)
        self.assertIn("recovered", f.text)
        # A recovery of another task, or one from before, does not count.
        bot.recoveries = [(400, "rebalance"), (50, "handle_epoch")]
        self.assertEqual(run_eval(bot=bot)[0].level, RED)

    def test_foreign_operator_transaction(self):
        chain = healthy_chain()
        chain.next_nonce[1] = 46
        [f] = run_eval(chain=chain)
        self.assertEqual(f.level, RED)
        self.assertIn("2 transaction(s) on Ethereum", f.text)

    def test_bot_ahead_of_chain_is_not_flagged(self):
        """The chain is read first; the bot may send in between."""
        bot = healthy_bot()
        bot.last_nonce[16661] = 102
        self.assertEqual(run_eval(bot=bot), [])

    def test_stake_mismatch(self):
        chain = healthy_chain()
        chain.stakes[1].cl -= 1
        [f] = run_eval(chain=chain)
        self.assertEqual(f.level, RED)
        self.assertIn("validator 1", f.text)

    def test_unrelayed_ethereum_change_is_not_a_mismatch(self):
        """A withdrawal on Ethereum the service has not relayed yet moves the
        head but not the relayed block: 0G and the CL are judged against the
        latter."""
        chain = healthy_chain()
        for s in chain.stakes:
            s.eth -= 16_830_000_000_000_000
        self.assertEqual(run_eval(chain=chain), [])

    def test_stake_mismatch_at_the_relayed_block(self):
        chain = healthy_chain()
        chain.stakes[2].eth_relayed -= 1
        [f] = run_eval(chain=chain)
        self.assertEqual(f.level, RED)
        self.assertIn("validator 3 stake differs at block 26438832", f.text)

    def test_stuck_relay(self):
        chain = healthy_chain()
        chain.eth_head = chain.relayed_block + dr.MAX_RELAY_LAG_BLOCKS
        self.assertEqual(run_eval(chain=chain), [])
        chain.eth_head += 1
        [f] = run_eval(chain=chain)
        self.assertEqual(f.level, RED)
        self.assertIn("relayed Ethereum only to block 26438832", f.text)

    def test_relayed_block_is_one_before_the_commit_watermark(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "db")
            con = sqlite3.connect(path)
            con.execute(
                "CREATE TABLE t_progress (domain INTEGER, sync INTEGER, committed INTEGER)"
            )
            con.execute("INSERT INTO t_progress VALUES (0, 26439633, 26439633)")
            con.commit()
            con.close()
            self.assertEqual(dr.read_relayed_block(path), 26439632)
        with self.assertRaises(ValueError):
            dr.read_relayed_block("")

    def test_read_is_retried_while_the_watermark_moves(self):
        marks = iter([10, 11, 11, 11])
        reads = []
        relayed, value = dr.read_at_stable_watermark(
            lambda: next(marks), lambda: reads.append(1) or len(reads)
        )
        self.assertEqual((relayed, value), (11, 2))
        moving = iter(range(100))
        with self.assertRaises(RuntimeError):
            dr.read_at_stable_watermark(lambda: next(moving), lambda: None)

    def test_untracked_cl_validator(self):
        chain = healthy_chain()
        chain.unexpected_cl = ["7"]
        self.assertEqual(run_eval(chain=chain)[0].level, RED)

    def test_ratio_out_of_band_is_a_note(self):
        chain = healthy_chain()
        chain.withdrawal_demand = chain.source_value + E18
        [f] = run_eval(chain=chain)
        self.assertEqual(f.level, YELLOW)

    def test_resync_between_heartbeats(self):
        bot = healthy_bot()
        bot.resyncs = [dr.Resync(0, "noop", False), dr.Resync(0, "refused", True)]
        levels = sorted(f.level for f in run_eval(bot=bot))
        self.assertEqual(levels, [RED, YELLOW])

    def test_low_gas(self):
        chain = healthy_chain()
        chain.gas["operator_eth"] = 0.01
        self.assertEqual(run_eval(chain=chain)[0].level, RED)

    def test_recon_gap(self):
        chain = healthy_chain()
        chain.mellow_pending = 10 * E18
        self.assertEqual(run_eval(chain=chain)[0].level, YELLOW)

    def test_read_errors_are_red_and_reds_come_first(self):
        chain = healthy_chain()
        chain.errors = ["could not read vault: boom"]
        monitor = ServiceLogStats(warnings=2, bad_gateway=2)
        findings = run_eval(chain=chain, monitor=monitor)
        self.assertEqual([f.level for f in findings], [RED, YELLOW])

    def test_expected_heartbeats(self):
        interval = 8 * HOUR
        now = 1_791_000_000 - (1_791_000_000 % interval) + 2 * HOUR  # 2h after a slot
        self.assertEqual(dr.expected_heartbeats(now - 24 * HOUR, now, interval), 3)
        # Within the grace period the latest slot is not yet owed.
        self.assertEqual(
            dr.expected_heartbeats(now - 24 * HOUR, now - 2 * HOUR + 60, interval), 2
        )


class _Call:
    def __init__(self, value, log, name):
        self.value, self.log, self.name = value, log, name

    def call(self):
        self.log.append(self.name)
        return self.value


class _Vault:
    """activeStake() and activeStakeAt(ts, hints) as web3 contract functions."""

    def __init__(self, head, at, log):
        self.head, self.at, self.log = head, at, log

    def activeStake(self):
        return _Call(self.head, self.log, "activeStake")

    def activeStakeAt(self, ts, hints):
        self.log.append(("activeStakeAt", ts, hints))
        return _Call(self.at[ts], self.log, "activeStakeAt.call")


class _Eth:
    def __init__(self, head, timestamps):
        self.block_number = head
        self.timestamps = timestamps

    def get_block(self, number):
        return {"timestamp": self.timestamps[number]}


class _W3:
    def __init__(self, head, timestamps):
        self.eth = _Eth(head, timestamps)


class TestReadStakes(unittest.TestCase):
    def test_reads_ethereum_at_the_relayed_blocks_timestamp(self):
        log = []
        vaults = {
            "0": _Vault(5 * E18, {1_700_000_012: 7 * E18}, log),
            "3": _Vault(6 * E18, {1_700_000_012: 8 * E18}, log),
        }
        w3 = _W3(500, {99: 1_700_000_012})
        marks = iter([100, 100])

        def zg_and_cl():
            log.append("zg_and_cl")
            return {"0": 7 * E18, "3": 8 * E18}, {"0": 7 * E18, "3": 8 * E18, "9": 1}

        read = dr.read_stakes(lambda: next(marks) - 1, zg_and_cl, w3, vaults)
        self.assertEqual((read.relayed_block, read.eth_head), (99, 500))
        self.assertEqual(
            [(s.index, s.eth, s.eth_relayed, s.zg, s.cl) for s in read.stakes],
            [
                ("0", 5 * E18, 7 * E18, 7 * E18, 7 * E18),
                ("3", 6 * E18, 8 * E18, 8 * E18, 8 * E18),
            ],
        )
        self.assertEqual(read.unexpected_cl, ["9"])
        self.assertTrue(read.consistent)
        self.assertIn(("activeStakeAt", 1_700_000_012, b""), log)
        # 0G and the CL are read before any Ethereum stake.
        self.assertEqual(log[0], "zg_and_cl")

    def test_a_validator_missing_from_the_cl_reads_as_zero(self):
        vaults = {"1": _Vault(E18, {7: E18}, [])}
        read = dr.read_stakes(
            lambda: 3, lambda: ({"1": E18}, {}), _W3(10, {3: 7}), vaults
        )
        self.assertEqual(read.stakes[0].cl, 0)
        self.assertFalse(read.consistent)


def _stake_read(consistent):
    s = Stake("0", E18, E18, E18 if consistent else E18 - 1, E18)
    return dr.StakeRead(1, 2, [s], [])


class TestReadStakesSettled(unittest.TestCase):
    def test_consistent_read_is_not_repeated(self):
        sleeps = []
        reads = iter([_stake_read(True)])
        read, n = dr.read_stakes_settled(lambda: next(reads), sleep=sleeps.append)
        self.assertTrue(read.consistent)
        self.assertEqual((n, sleeps), (0, []))

    def test_transient_mismatch_settles_after_a_wait(self):
        sleeps = []
        reads = iter([_stake_read(False), _stake_read(True)])
        read, n = dr.read_stakes_settled(lambda: next(reads), sleep=sleeps.append)
        self.assertTrue(read.consistent)
        self.assertEqual((n, sleeps), (1, [dr.STAKE_RECHECK_WAIT]))

    def test_persistent_mismatch_is_returned_after_the_last_recheck(self):
        sleeps = []
        reads = iter([_stake_read(False)] * (dr.STAKE_RECHECKS + 1))
        read, n = dr.read_stakes_settled(lambda: next(reads), sleep=sleeps.append)
        self.assertFalse(read.consistent)
        self.assertEqual(n, dr.STAKE_RECHECKS)
        self.assertEqual(len(sleeps), dr.STAKE_RECHECKS)
        chain = healthy_chain()
        chain.stakes = read.stakes
        self.assertEqual(run_eval(chain=chain)[0].level, RED)


class TestRender(unittest.TestCase):
    def render(self, findings, chain=None):
        return dr.render(
            findings,
            healthy_bot(),
            chain or healthy_chain(),
            PROCS,
            AGES,
            ServiceLogStats(),
            ServiceLogStats(),
            ServiceLogStats(),
            3,
            24,
            time.time(),
        )

    def test_clean_report(self):
        text = self.render([])
        self.assertIn("✅ no issues", text)
        self.assertLessEqual(len(text), dr.TELEGRAM_LIMIT)
        self.assertEqual(text.count("```"), 2)

    def test_stake_rows_show_the_relayed_block_and_what_is_in_flight(self):
        chain = healthy_chain()
        chain.stakes[0].eth -= 16_830_000_000_000_000
        text = self.render([], chain)
        self.assertIn("at block 26438832, head 26438960", text)
        self.assertIn("4,623,989.000000 ok  relaying -0.016830", text)
        self.assertEqual(text.count("relaying"), 1)
        self.assertNotIn("re-read", text)
        chain.stake_rechecks = 2
        self.assertIn("head 26438960, re-read 2x)", self.render([], chain))

    def test_findings_lead_and_backticks_cannot_break_the_block(self):
        text = self.render([dr.Finding(RED, "bad `thing`")])
        self.assertIn("🔴 1", text)
        self.assertEqual(text.count("```"), 2)
        self.assertLess(text.index("bad 'thing'"), text.index("Services"))

    def test_long_report_is_truncated(self):
        text = self.render([dr.Finding(YELLOW, "x" * 200)] * 40)
        self.assertLessEqual(len(text), dr.TELEGRAM_LIMIT)
        self.assertTrue(text.endswith("```"))


if __name__ == "__main__":
    unittest.main()
