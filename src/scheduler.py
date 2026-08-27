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
import json
import os
import signal
import sys
import time
import traceback
from pathlib import Path
from typing import Callable, Optional

import dotenv

sys.path.insert(0, str(Path(__file__).parent))

from config import check_operator_requirements, read_config
from config.mask_sensitive_data import mask_all_sensitive_config_data
from config.read_config import Config
from process_lock import LockHeld, ProcessLock, resolve_lock_path
from tapp import inject_tee_keys
from telegram_bot import send_message
from web3_scripts import get_w3, print_colored
from web3_scripts.ascend import run_ascend
from web3_scripts.operator_bot import run_all as run_rebalance
from web3_scripts.withdrawal_queue import handle_epochs

CONFIG_PATH = Path(__file__).parent.parent / "config.json"

# Order matters: ascend changes the vault's value, so everything that reads that
# value runs after it, and after the settling gap.
#
# oracle_update sits between ascend and rebalance rather than after both.
# Rebalancing refuses whenever the oracle disagrees with the computed value, and
# ascend is the thing that makes them disagree -- so with rebalance running
# first, the pass immediately after every reward distribution was guaranteed to
# refuse. Refreshing the oracle in between removes that.
TASK_ORDER = ("ascend", "oracle_update", "rebalance", "handle_epoch")

# How the startup gate paces itself. The check is a handful of eth_calls, so a
# minute is cheap -- the moment the last grant lands, the bot starts.
#
# The reminder is a day apart, which looks slow next to that and is not. It
# repeats one unchanging sentence: the same roles are still ungranted. Anyone
# able to act on it is already acting, and the first alert -- sent immediately --
# is the one that carries the information. A shorter interval only buries the
# messages that say something in ones that do not.
READY_CHECK_INTERVAL_SECONDS = 60
READY_ALERT_EVERY_SECONDS = 86400

# A task that owes a run whenever the task it depends on has run more recently
# than it has, regardless of where the interval boundaries fall.
#
# Distributing rewards is the only thing that moves the share price, so between
# ascend and the write that follows it the oracle is knowingly wrong: rebalance
# refuses against it, and deposits and withdrawals price against the stale
# figure. Sharing an interval is not enough to prevent that, because the two
# drift apart exactly when it matters -- on a first start, where the oracle is
# seeded to now while ascend is already overdue, and after the oracle has failed
# onto a retry backoff while ascend keeps to its schedule.
#
# Expressed as a rule over the recorded times rather than one task marking
# another: nothing has to remember to set a flag, and it stays true however the
# two came to be out of step. It costs nothing in the steady state, where they
# come due together anyway.
TASK_DEPENDENCIES = {"oracle_update": "ascend"}

# How long a run of skips may last before it stops being routine, per task.
#
# Measured in time rather than in attempts. A task that retries every loop until
# its blocker clears racks up attempts at a rate set by loop_sleep_seconds, not
# by how wrong anything is, so counting them makes the alert threshold mean
# whatever the loop interval happens to be -- three attempts became fifteen
# minutes where it was meant to be a day.
#
# The default preserves the original intent: as long as `alert_after_failures`
# of the task's own scheduled runs. oracle_update is given a shorter fuse
# because a transfer settles in one to five minutes and operator_bot stops
# waiting for one at thirty, so past half an hour it is stuck rather than
# settling -- and this is the task whose silence leaves the share price
# unpriced and rebalancing refusing.
SKIP_ALERT_AFTER_SECONDS = {"oracle_update": 1800}


def is_due(last_run: float, now: float, interval: int) -> bool:
    """True when now has crossed into a later interval bucket than last_run.

    Anchoring buckets to the Unix epoch rather than to the previous run is what
    keeps a restart from moving the schedule: a daily task stays on its original
    time of day no matter how often the process is restarted.
    """
    if interval <= 0:
        # Config rejects this; if one ever reaches here, not running is the
        # direction that cannot cause harm.
        return False
    return int(last_run) // interval < int(now) // interval


def next_due(last_run: float, interval: int) -> float:
    if interval <= 0:
        return last_run
    return (int(last_run) // interval + 1) * interval


def require_executor(source) -> str:
    if not source.executor_private_key:
        raise Exception(
            "Source {} has no executor private key; set OPERATOR_PK".format(source.name)
        )
    return source.executor_private_key


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
    def __init__(
        self, config: Config, now=time.time, sleep=time.sleep, state_path=None
    ):
        self.config = config
        self._now = now
        self._sleep = sleep
        self.stopping = False
        started = now()
        self.state_path = (
            state_path
            if state_path is not None
            else resolve_lock_path(config.scheduler.state_file)
        )
        # Seeded from "now" so a first start does not replay a bucket that has
        # already passed, then overridden by whatever the last run recorded.
        # Without that record a restart minutes after a boundary would skip the
        # bucket entirely and say nothing -- for a fortnightly task that is
        # twenty-eight days between reward distributions, invisibly.
        self.last_run = {task: started for task in TASK_ORDER}
        restored = self._load_state()
        self.last_run.update(restored)
        # Which tasks we have an actual record of having run -- seeded from the
        # file and added to as tasks complete. Seeding the rest of last_run to
        # "now" is right for the interval, since it stops a first start
        # replaying a bucket that has already passed, but wrong for a
        # dependency, where having no record means the task has never run rather
        # than having just run. Renaming a task orphans its saved entry, so the
        # task that most needs to catch up is the one that looks freshest.
        #
        # It has to grow as tasks run. Left as "what the file contained", the
        # short-circuit in owes_dependency never stops firing: the task is
        # forever absent from the set, so it is forever owed, and the interval
        # never gets a say. That is a write every loop instead of every eight
        # hours -- silently, because between ascend runs the value is unchanged
        # and neither guard has anything to object to.
        self._has_record = set(restored)
        self.failures = {task: 0 for task in TASK_ORDER}
        self.skips = {task: 0 for task in TASK_ORDER}
        # When the current run of skips began, and how many alerts it has
        # already produced. Both are what make the alert depend on elapsed time
        # rather than on how often the task retried.
        self.skipping_since = {task: 0.0 for task in TASK_ORDER}
        self.skip_alerts = {task: 0 for task in TASK_ORDER}
        # A task that failed is retried on a backoff rather than at its next
        # scheduled slot. Treating a failure as a completed run would mean one
        # transient RPC error costs a fortnightly task a fortnight, and would put
        # three consecutive failures six weeks apart -- long enough that the
        # failure alert would never fire.
        self.retry_after = {task: 0.0 for task in TASK_ORDER}

    # -- persisted schedule ------------------------------------------------

    def _load_state(self) -> dict:
        try:
            with open(self.state_path, "r") as handle:
                stored = json.load(handle)
        except FileNotFoundError:
            return {}
        except (ValueError, OSError) as e:
            # Fails open, so say so: silently reseeding is how a due slot gets
            # skipped without anyone noticing.
            print_colored(
                "Could not read the saved schedule ({}); starting from now".format(e),
                "yellow",
            )
            return {}
        if not isinstance(stored, dict):
            print_colored("Saved schedule is not an object; ignoring it", "yellow")
            return {}
        return {
            task: float(value)
            for task, value in stored.items()
            if task in TASK_ORDER and isinstance(value, (int, float))
        }

    def _save_state(self) -> None:
        # Written to a sibling and renamed: a partial file left by a crash mid
        # write would be read back as corrupt and forfeit the schedule.
        # Only tasks we have an actual run record for. last_run also holds
        # seeded "process start" times for tasks that have never run, and
        # writing those back makes them indistinguishable from real records on
        # the next start -- which is how _has_record gets a task it should not
        # have, the dependency debt is silently cleared, and the write-every-loop
        # bug this distinction exists to prevent comes back after a restart.
        # handle_epoch runs every five minutes, so the file is rewritten almost
        # immediately after any start; there is no window in which this is
        # academic.
        recorded = {
            task: when
            for task, when in self.last_run.items()
            if task in self._has_record
        }
        temporary = self.state_path + ".tmp"
        try:
            with open(temporary, "w") as handle:
                json.dump(recorded, handle)
            os.replace(temporary, self.state_path)
        except OSError as e:
            print_colored("Could not persist the schedule: {}".format(e), "yellow")

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
        # The skip window is deliberately left alone. Failing and declining are
        # both "did not write", and clearing one from the other lets a task
        # alternate between them forever without either threshold arming --
        # which retrying every cycle makes a five-minute flip rather than an
        # eight-hour one.
        message = mask_all_sensitive_config_data(str(error), self.config)
        # Backticks would close the code fence below and Telegram would reject
        # the message, dropping the alert precisely when the error is unusual.
        alert_text = message.replace("`", "'")
        print_colored(
            "[{}] failed ({} in a row): {}".format(task, self.failures[task], message),
            "red",
        )
        traceback.print_exc()
        threshold = self.config.scheduler.alert_after_failures
        # Every threshold-th failure, not only the first. A task that stays
        # broken must keep saying so; one alert followed by silence is the
        # failure mode this exists to close.
        if threshold and self.failures[task] % threshold == 0:
            self.notify(
                "⚠️ `{}` has failed {} times in a row.\n```\n{}\n```".format(
                    task, self.failures[task], alert_text
                )
            )

    def retry_delay(self, task: str) -> float:
        """Backoff after a failure, doubling up to the task's own interval."""
        interval = self.config.scheduler.interval(task)
        base = self.config.scheduler.loop_sleep_seconds
        delay = base * (2 ** max(0, self.failures[task] - 1))
        return min(delay, interval) if interval > 0 else delay

    def record_success(self, task: str) -> None:
        self.retry_after[task] = 0.0
        if self.failures[task]:
            print_colored(
                "[{}] recovered after {} failure(s)".format(task, self.failures[task]),
                "green",
            )
        self.failures[task] = 0

    def skip_alert_after(self, task: str) -> float:
        """How long this task may keep declining to act before anyone is told."""
        configured = SKIP_ALERT_AFTER_SECONDS.get(task)
        if configured is not None:
            return configured
        return (
            self.config.scheduler.alert_after_failures
            * self.config.scheduler.interval(task)
        )

    def record_skip(self, task: str, reason: str) -> None:
        """Track a task that ran but declined to act.

        Rebalancing refuses whenever the oracle is stale or a cross-chain
        transfer is in flight, and the oracle refuses while a transfer is in
        flight. One refusal is routine. A run of them means the bot has quietly
        stopped doing that job, which is invisible otherwise.

        Judged by how long the run has lasted, not how many attempts it took --
        see SKIP_ALERT_AFTER_SECONDS.
        """
        now = self._now()
        self.skips[task] += 1
        if not self.skipping_since[task]:
            self.skipping_since[task] = now

        threshold = self.skip_alert_after(task)
        if not threshold:
            return
        elapsed = now - self.skipping_since[task]
        # Repeats like record_failure does; a task stuck refusing to act has to
        # keep saying so, at the same cadence rather than once.
        due_alerts = int(elapsed // threshold)
        if due_alerts > self.skip_alerts[task]:
            self.skip_alerts[task] = due_alerts
            self.notify(
                "⚠️ `{}` has been unable to act for {} ({} attempt(s)): {}".format(
                    task, format_duration(elapsed), self.skips[task], reason
                )
            )

    def clear_skips(self, task: str) -> None:
        self.skips[task] = 0
        self.skipping_since[task] = 0.0
        self.skip_alerts[task] = 0

    # -- tasks -------------------------------------------------------------

    def task_ascend(self) -> None:
        for source in self.config.sources:
            if not source.ascend:
                continue
            # Raise rather than skip: with OPERATOR_PK unset this task would
            # otherwise report success forever while doing nothing, which is the
            # silence this scheduler exists to break.
            require_executor(source)
            run_ascend(
                get_w3(source.rpc),
                router=source.ascend.router,
                rewarders=list(source.ascend.rewarders),
                private_key=source.executor_private_key,
                claim_account=source.ascend.resolved_claim_account(),
                tx=self.tx_options(source.tx),
            )

    def task_rebalance(self) -> None:
        # force_withdrawal is passed explicitly so a stray FORCE_WITHDRAWAL in
        # the environment cannot make an unattended run pull the entire target
        # position back every two hours.
        results = (
            run_rebalance(
                self.config,
                interactive=False,
                force_withdrawal=False,
                should_stop=lambda: self.stopping,
                on_stuck=lambda text: self.notify("⚠️ " + text),
            )
            or []
        )
        reasons = [reason for _, reason in results if reason]
        if reasons and len(reasons) == len(results):
            self.record_skip("rebalance", "; ".join(sorted(set(reasons))))
        else:
            self.clear_skips("rebalance")

    def task_oracle_update(self) -> bool:
        """Refresh the oracle. Returns whether it actually wrote anything.

        The return value is what tells run_cycle a skip is not a run, so the
        task stays due and tries again next cycle instead of waiting out its
        interval.
        """
        from main import run_oracle_update

        # The raising variant, not main(): main() swallows everything so the
        # scheduler could never see this task fail.
        summary = asyncio.run(
            run_oracle_update(
                self.config,
                should_stop=lambda: self.stopping,
                on_stuck=lambda text: self.notify("⚠️ " + text),
            )
        )

        if not summary.notified:
            # Not raised: the write already happened, so a retry would only
            # repeat the announcement, and it would repeat it over the channel
            # that is down anyway.
            print_colored(
                "[oracle_update] the oracle needs attention and no one was notified",
                "red",
            )

        if not summary.written:
            # Nothing was written, whatever the reason. Keying this on
            # skip_reasons alone missed the case that matters most: a guard
            # refusal populates `alerts`, not `skip_reason`, so a refused write
            # counted as a completed run -- advancing last_run past ascend's,
            # marking the dependency it owes as paid, and clearing the window
            # that would have alerted. That is the one kind of "did not write"
            # which will not resolve on its own.
            self.record_skip(
                "oracle_update",
                "; ".join(sorted(set(summary.skip_reasons))) or "nothing written",
            )
            # Declining to act is not acting. Saying so keeps the task due, so
            # it tries again next cycle and writes as soon as the transfer
            # settles -- minutes, normally.
            #
            # Recorded as a run instead, it would move last_run ahead of
            # ascend's, mark the dependency it owes as paid, and leave the
            # reward distribution ascend just made unpriced until the next
            # boundary: eight hours in which rebalancing also refuses, which is
            # the gap the dependency rule exists to close.
            return False

        self.clear_skips("oracle_update")
        return True

    def task_handle_epoch(self) -> None:
        for source in self.config.sources:
            if not source.withdrawal_queue:
                continue
            require_executor(source)
            handle_epochs(
                get_w3(source.rpc),
                address=source.withdrawal_queue.address,
                private_key=source.executor_private_key,
                max_iterations=source.withdrawal_queue.max_iterations,
                tx=self.tx_options(source.tx),
            )

    def tx_options(self, tx_config) -> dict:
        """Transaction settings plus the shutdown hook, for every send.

        Threaded through the same dict the settings already travel in, because
        that dict is what reaches every call site. A send runs until the chain
        settles the transaction, so a site that missed `should_stop` would make
        the process unstoppable while a transaction is stuck, and one that
        missed `on_stuck` would be silent about it.
        """
        options = tx_config.as_kwargs()
        options["should_stop"] = lambda: self.stopping
        # A send never gives up, so the only thing that turns a transaction
        # nobody can land into something anybody hears about is this. It
        # replaces asking the code to recognise each way a send can be unusual.
        options["on_stuck"] = lambda text: self.notify("⚠️ " + text)
        return options

    def owes_dependency(self, task: str) -> bool:
        """Whether the task this one depends on has run more recently than it.

        Compared against the recorded times, so it is true however the two came
        to be out of step -- a rename that orphaned one task's saved entry, a
        retry backoff, a first start -- and needs nothing to have remembered to
        mark it.
        """
        depends_on = TASK_DEPENDENCIES.get(task)
        if depends_on is None:
            return False
        if task not in self._has_record and depends_on in self._has_record:
            # No record of this one ever running, while the one it follows has a
            # history. Comparing the seeded timestamps would make it look newer
            # than a dependency that ran days ago, which is backwards.
            return True
        return self.last_run[depends_on] > self.last_run[task]

    def handler(self, task: str):
        return getattr(self, "task_" + task)

    # -- loop --------------------------------------------------------------

    def run_cycle(self) -> None:
        now = self._now()
        print("\n" + "-" * 42)
        print("TIMESTAMP: {}".format(time.strftime("%Y-%m-%d %H:%M:%S")))
        print("-" * 42)

        for task in TASK_ORDER:
            # Re-sampled per task: a long task leaves the cycle-start value far
            # behind, which would put a freshly computed backoff in the past and
            # defer a task that became due while the previous one ran.
            now = self._now()
            if self.stopping:
                print("Stopping; skipping the rest of this cycle")
                return

            interval = self.config.scheduler.interval(task)
            retry_at = self.retry_after[task]
            if retry_at:
                # A pending retry replaces the schedule outright: the task owes a
                # run and the only question is whether the backoff has elapsed.
                if now < retry_at:
                    print(
                        "[{}] retrying in {}".format(
                            task, format_duration(retry_at - now)
                        )
                    )
                    continue
            elif self.owes_dependency(task):
                print(
                    "[{}] due: {} has run since it last did".format(
                        task, TASK_DEPENDENCIES[task]
                    )
                )
            elif not is_due(self.last_run[task], now, interval):
                remaining = next_due(self.last_run[task], interval) - now
                print(
                    "[{}] skipped. Next run in {}".format(
                        task, format_duration(remaining)
                    )
                )
                continue

            print("[{}] running...".format(task))
            try:
                acted = self.handler(task)()
                self.record_success(task)
                if acted is False:
                    # It ran and declined. Leaving last_run where it is keeps the
                    # task due, which is what makes it retry rather than wait out
                    # its interval -- and keeps it out of _has_record, so a task
                    # that has never actually run is not credited with one.
                    print("[{}] declined to act; still due next cycle".format(task))
                    continue
                self.last_run[task] = now
                self._has_record.add(task)
                self._save_state()
            except Exception as e:
                # Isolated per task: the shell scheduler got this from running
                # each task in its own process, and losing it would let one
                # failure stop every other task.
                self.record_failure(task, e)
                # Measured from when the task finished, not from when the cycle
                # began. A task that fails because its sends timed out can run
                # for longer than its own backoff, which would put the retry in
                # the past and make it run again every cycle -- the pacing the
                # backoff exists to provide, absent for exactly the failure it
                # was written for.
                self.retry_after[task] = self._now() + self.retry_delay(task)

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

    def wait_until_ready(self, address: Optional[str]) -> None:
        """Hold before the first cycle until the signers can actually transact.

        Without this the bot starts cleanly against an account with no gas and
        no roles, and says so only by failing -- every eighth hour for the
        oracle, and only on the third consecutive failure. Under a tapp that is
        the normal first-boot state, because the address does not exist until
        the bot derives it and nobody can fund or authorise it before then.

        So: announce the address, then say what is still missing until nothing
        is, and only then start. A revocation later is not this function's job;
        the per-task failure path covers that.
        """
        if address:
            self.notify(
                "🔐 TEE signer is `{}`.\nIt needs gas on both chains and its "
                "roles granted from the Safe before the bot can act.".format(address)
            )
            print_colored("TEE signer address: {}".format(address), "green")

        last_alert = 0.0
        first = True
        while not self.stopping:
            try:
                missing = check_operator_requirements(self.config)
            except Exception as e:
                missing = ["readiness check itself failed: {}".format(e)]

            if not missing:
                if not first:
                    self.notify("✅ Signer is ready. Starting the task loop.")
                print_colored("Signer is ready.", "green")
                return

            print_colored("Not ready yet ({} item(s)):".format(len(missing)), "yellow")
            for item in missing:
                print_colored("  - {}".format(item), "yellow")

            now = time.monotonic()
            if first or now - last_alert >= READY_ALERT_EVERY_SECONDS:
                last_alert = now
                self.notify(
                    "⏳ Bot is waiting to start -- {} unmet requirement(s):\n"
                    "```\n{}\n```".format(
                        len(missing),
                        "\n".join(m.replace("`", "'") for m in missing),
                    )
                )
            first = False
            self.interruptible_sleep(READY_CHECK_INTERVAL_SECONDS)

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


def _pre_config_notifier() -> Optional[Callable[[str], None]]:
    """Telegram sender for the window before the config is loaded.

    Fetching the TEE key happens before read_config -- the key is one of the
    things read_config reads -- so the wait for it cannot use the Scheduler's
    notifier, which does not exist yet. Reads the same two variables read_config
    would, and returns None when they are unset so a bot without Telegram
    configured is not a bot that fails to start.
    """
    api_key = os.getenv("TELEGRAM_BOT_API_KEY")
    chat_id = os.getenv("TELEGRAM_GROUP_CHAT_ID")
    if not api_key or not chat_id:
        return None

    def notify(reason: str) -> None:
        text = "⏳ Still waiting for the TEE key.\n```\n{}\n```".format(
            reason.replace("`", "'")
        )
        try:
            asyncio.run(send_message(api_key, chat_id, text))
        except Exception as e:
            print_colored("Could not send Telegram alert: {}".format(e), "yellow")

    return notify


def main() -> int:
    dotenv.load_dotenv()
    tee_address = inject_tee_keys(on_retry=_pre_config_notifier())
    config = read_config(str(CONFIG_PATH))
    scheduler = Scheduler(config)

    lock = ProcessLock(config.scheduler.lock_file)
    try:
        lock.acquire()
    except LockHeld as e:
        print_colored(str(e), "red")
        return 1

    def stop(signum, _frame):
        # First signal finishes the task in flight and then abandons the rest of
        # the cycle, rather than dying mid-broadcast and leaving a transaction
        # sent but unrecorded. A second signal is taken as "I meant now" and gets
        # the default behaviour.
        if scheduler.stopping:
            signal.signal(signum, signal.SIG_DFL)
            lock.release()
            raise KeyboardInterrupt
        print_colored("\nStopping after the current task...", "yellow")
        scheduler.request_stop()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    # After the signal handlers, so the wait can be interrupted, and inside the
    # try/finally so the lock is released if it is.
    try:
        if tee_address:
            scheduler.wait_until_ready(tee_address)
        if scheduler.stopping:
            return 0
        print(
            "Starting scheduler. Intervals: {}".format(config.scheduler.task_intervals)
        )
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
