"""The bot's task loop.

Replaces the shell scheduler. Two properties of that script are deliberately
preserved and two gaps are deliberately closed.

Preserved: tasks fire on multiples of their interval measured from the Unix
epoch, not on time elapsed since the last run, so restarting neither shifts the
schedule nor skips a beat. And ascend still gets a settling gap before the tasks
that read the state it just changed.

Closed: the shell got crash isolation for free by running each task as its own
process, so one traceback could not stop the loop; in a single process that has
to be explicit. And nothing ever reported a task that kept failing, or one that
kept declining to act -- a bot that quietly stops working looks exactly like a
bot with nothing to do.
"""

import asyncio
import signal
import sys
import time
import traceback
from pathlib import Path

import dotenv

sys.path.insert(0, str(Path(__file__).parent))

from config import read_config
from config.mask_sensitive_data import mask_all_sensitive_config_data
from config.read_config import Config
from process_lock import LockHeld, ProcessLock
from telegram_bot import send_message
from web3_scripts import get_w3, print_colored
from web3_scripts.ascend import run_ascend
from web3_scripts.operator_bot import run_all as run_rebalance
from web3_scripts.withdrawal_queue import handle_epochs

CONFIG_PATH = Path(__file__).parent.parent / "config.json"

# Order matters: ascend changes the vault's value, so everything that reads that
# value runs after it, and after the settling gap.
TASK_ORDER = ("ascend", "rebalance", "oracle_report", "handle_epoch")


def is_due(last_run: float, now: float, interval: int) -> bool:
    """True when now has crossed into a later interval bucket than last_run.

    Anchoring buckets to the Unix epoch rather than to the previous run is what
    keeps a restart from moving the schedule: a daily task stays on its original
    time of day no matter how often the process is restarted.
    """
    if interval <= 0:
        return True
    return int(last_run) // interval < int(now) // interval


def next_due(last_run: float, interval: int) -> float:
    if interval <= 0:
        return last_run
    return (int(last_run) // interval + 1) * interval


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    parts = []
    if days:
        parts.append("{}d".format(days))
    if hours:
        parts.append("{}h".format(hours))
    if minutes:
        parts.append("{}m".format(minutes))
    return " ".join(parts) or "<1m"


class Scheduler:
    def __init__(self, config: Config, now=time.time, sleep=time.sleep):
        self.config = config
        self._now = now
        self._sleep = sleep
        self.stopping = False
        started = now()
        # Starting from "now" means a task whose bucket already passed waits for
        # the next one, so restarting never replays work that was already done.
        self.last_run = {task: started for task in TASK_ORDER}
        self.failures = {task: 0 for task in TASK_ORDER}
        self.skips = {task: 0 for task in TASK_ORDER}

    # -- notifications -----------------------------------------------------

    def notify(self, text: str) -> None:
        api_key = self.config.telegram_bot_api_key
        chat_id = self.config.telegram_group_chat_id
        if not api_key or not chat_id:
            return
        try:
            asyncio.run(send_message(api_key, chat_id, text))
        except Exception as e:
            print_colored("Could not send Telegram alert: {}".format(e), "yellow")

    def record_failure(self, task: str, error: Exception) -> None:
        self.failures[task] += 1
        message = mask_all_sensitive_config_data(str(error), self.config)
        print_colored(
            "[{}] failed ({} in a row): {}".format(task, self.failures[task], message),
            "red",
        )
        traceback.print_exc()
        threshold = self.config.scheduler.alert_after_failures
        if threshold and self.failures[task] == threshold:
            self.notify(
                "⚠️ `{}` has failed {} times in a row.\n```\n{}\n```".format(
                    task, self.failures[task], message
                )
            )

    def record_success(self, task: str) -> None:
        if self.failures[task]:
            print_colored(
                "[{}] recovered after {} failure(s)".format(task, self.failures[task]),
                "green",
            )
        self.failures[task] = 0

    def record_skip(self, task: str, reason: str) -> None:
        """Track a task that ran but declined to act.

        Rebalancing refuses whenever the oracle is stale or a cross-chain
        transfer is in flight. One refusal is routine. A run of them means the
        bot has stopped rebalancing entirely, which is invisible otherwise.
        """
        self.skips[task] += 1
        threshold = self.config.scheduler.alert_after_failures
        if threshold and self.skips[task] == threshold:
            self.notify(
                "⚠️ `{}` has been skipped {} times in a row: {}".format(
                    task, self.skips[task], reason
                )
            )

    # -- tasks -------------------------------------------------------------

    def task_ascend(self) -> None:
        for source in self.config.sources:
            if not source.ascend or not source.executor_private_key:
                continue
            run_ascend(
                get_w3(source.rpc),
                router=source.ascend.router,
                rewarders=list(source.ascend.rewarders),
                private_key=source.executor_private_key,
                claim_account=source.ascend.resolved_claim_account(),
                receipt_timeout=source.tx.receipt_timeout_seconds,
                max_attempts=source.tx.max_attempts,
                fee_cap_gwei=source.tx.fee_cap_gwei,
            )

    def task_rebalance(self) -> None:
        results = run_rebalance(self.config, interactive=False) or []
        reasons = [reason for _, reason in results if reason]
        if reasons and len(reasons) == len(results):
            self.record_skip("rebalance", "; ".join(sorted(set(reasons))))
        else:
            self.skips["rebalance"] = 0

    def task_oracle_report(self) -> None:
        from main import main as oracle_main

        asyncio.run(oracle_main())

    def task_handle_epoch(self) -> None:
        for source in self.config.sources:
            if not source.withdrawal_queue or not source.executor_private_key:
                continue
            handle_epochs(
                get_w3(source.rpc),
                address=source.withdrawal_queue.address,
                private_key=source.executor_private_key,
                max_iterations=source.withdrawal_queue.max_iterations,
                receipt_timeout=source.tx.receipt_timeout_seconds,
                max_attempts=source.tx.max_attempts,
                fee_cap_gwei=source.tx.fee_cap_gwei,
            )

    def handler(self, task: str):
        return getattr(self, "task_" + task)

    # -- loop --------------------------------------------------------------

    def run_cycle(self) -> None:
        now = self._now()
        print("\n" + "-" * 42)
        print("TIMESTAMP: {}".format(time.strftime("%Y-%m-%d %H:%M:%S")))
        print("-" * 42)

        for task in TASK_ORDER:
            interval = self.config.scheduler.interval(task)
            if not is_due(self.last_run[task], now, interval):
                remaining = next_due(self.last_run[task], interval) - now
                print(
                    "[{}] skipped. Next run in {}".format(
                        task, format_duration(remaining)
                    )
                )
                continue

            print("[{}] running...".format(task))
            try:
                self.handler(task)()
                self.record_success(task)
            except Exception as e:
                # Isolated per task: the shell scheduler got this from running
                # each task in its own process, and losing it would let one
                # failure stop every other task.
                self.record_failure(task, e)
            finally:
                self.last_run[task] = now

            if task == "ascend":
                gap = self.config.scheduler.post_ascend_gap_seconds
                print("[ascend] settling for {}s".format(gap))
                self._sleep(gap)

    def interruptible_sleep(self, seconds: float) -> None:
        """Sleep in slices so a stop request is honoured promptly."""
        remaining = seconds
        while remaining > 0 and not self.stopping:
            slice_seconds = min(1.0, remaining)
            self._sleep(slice_seconds)
            remaining -= slice_seconds

    def run_forever(self) -> None:
        while not self.stopping:
            self.run_cycle()
            if self.stopping:
                break
            sleep_for = self.config.scheduler.loop_sleep_seconds
            print(
                "Cycle complete. Sleeping for {}...".format(format_duration(sleep_for))
            )
            self.interruptible_sleep(sleep_for)

    def request_stop(self) -> None:
        self.stopping = True


def main() -> int:
    dotenv.load_dotenv()
    config = read_config(str(CONFIG_PATH))
    scheduler = Scheduler(config)

    lock = ProcessLock(config.scheduler.lock_file)
    try:
        lock.acquire()
    except LockHeld as e:
        print_colored(str(e), "red")
        return 1

    def stop(signum, _frame):
        # First signal finishes the task in flight rather than dying mid-broadcast,
        # which would leave a transaction sent but unrecorded. A second signal is
        # taken as "I meant now" and gets the default behaviour.
        if scheduler.stopping:
            signal.signal(signum, signal.SIG_DFL)
            lock.release()
            raise KeyboardInterrupt
        print_colored("\nStopping after the current task...", "yellow")
        scheduler.request_stop()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    print("Starting scheduler. Intervals: {}".format(config.scheduler.task_intervals))
    try:
        scheduler.run_forever()
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(130)
