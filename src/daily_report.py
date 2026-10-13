"""Daily health report, posted to the Telegram group.

Runs on the host that runs the bot and the restaking services, because half of
what it checks is their logs. It reads and never writes: no key is loaded, no
transaction is signed, and it does not take the process lock.

Collection (logs, processes, chain) is kept apart from the rules and the
rendering, which are pure and tested on their own. Every rule that fires is a
Finding; the message leads with those, then the numbers they were judged on.

    python src/daily_report.py --dry-run     # print, send nothing
    python src/daily_report.py               # post to TELEGRAM_GROUP_CHAT_ID
"""

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import dotenv

sys.path.insert(0, str(Path(__file__).parent))

from config import mask_all_sensitive_config_data, read_config
from config.read_config import Config, _transform_config
from web3_scripts import get_contract, get_w3

CONFIG_PATH = Path(__file__).parent.parent / "config.json"

RED = "red"
YELLOW = "yellow"

# Each process: what to call it, and a fragment of its command line.
PROCESSES = (
    ("bot", "src/scheduler.py"),
    ("restaking service", "release/service --config"),
    ("monitor", "release/monitor --config"),
    ("vault-monitor", "vault-monitor/monitor.sh"),
)
# A log quieter than this is a process that is up but stuck. The restaking
# service only logs when Ethereum finalizes another 32 blocks (~6.4 min).
MAX_LOG_AGE = {
    "bot": 15 * 60,
    "restaking": 20 * 60,
    "monitor": 10 * 60,
    "vault-monitor": 15 * 60,
}

# Oracle step per heartbeat. Outside this band the reward flow or the write
# changed, which is worth a look even when every guard passed.
STEP_BPS_RANGE = (0.5, 2.0)
# The vault's computed share price may trail the oracle by at most one step
# between heartbeats; this is several.
MAX_PRICE_DRIFT_BPS = 5.0
# Rebalance keeps the free source share between these (per mille of assets
# net of queued withdrawals); outside them it acts on its next run.
RATIO_BAND = (0.0, 0.10)
MIN_GAS = {"operator_og": 20.0, "operator_eth": 0.05, "service_og": 2.0}
# Sum of activeStake plus in-flight Mellow withdrawals against the target value.
MAX_RECON_GAP = 1.0
# Last epochs summed for "settled, not yet claimed".
CLAIMABLE_LOOKBACK_EPOCHS = 60
BOT_LOG_MAX_BYTES = 64 * 1024 * 1024
TELEGRAM_LIMIT = 4096

ANSI = re.compile(r"\x1b\[[0-9;]*m")
CYCLE = re.compile(r"^TIMESTAMP: (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
HEARTBEAT_WRITE = re.compile(
    r"(?<!resync )setValue\((\d+)\) confirmed in (0x[0-9a-fA-F]+) "
    r"\(was (\d+), ([+-][0-9.]+) bps\)"
)
DISTRIBUTED = re.compile(r"Distributed (\d+) of")
FAILED = re.compile(r"\[(\w+)\] failed \((\d+) in a row\)")
RECOVERED = re.compile(r"\[(\w+)\] recovered after")
SENT = re.compile(r"\(nonce (\d+), attempt (\d+)\)")
MINED = re.compile(r"Transaction mined in block: \d+\. Chain id: (\d+)")
CROSS_CHAIN = re.compile(
    r"(Assets (?:surplus|deficit)[^\n]*|"
    r"(?:TargetCore|SourceCore)\.(?:deposit|redeem|claim|pushToTarget|pushToSource))"
)
ISO_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")
BRACKET_TS = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]")


@dataclass
class Finding:
    level: str
    text: str


@dataclass
class Resync:
    at: float
    outcome: str  # noop / write / refused / failed
    after_heartbeat: bool


@dataclass
class BotStats:
    cycles: int = 0
    last_cycle: Optional[float] = None
    heartbeats: List[Tuple[float, int, str, float]] = field(default_factory=list)
    recent_steps: List[float] = field(default_factory=list)
    recent_distributions: List[int] = field(default_factory=list)
    failures: List[Tuple[float, str, int]] = field(default_factory=list)
    recoveries: List[Tuple[float, str]] = field(default_factory=list)
    tracebacks: int = 0
    retries: int = 0
    refusals: List[str] = field(default_factory=list)
    resyncs: List[Resync] = field(default_factory=list)
    cross_chain: List[Tuple[float, str]] = field(default_factory=list)
    epochs_processed: int = 0
    # chain id -> highest nonce the bot saw mined, over the whole log
    last_nonce: Dict[int, int] = field(default_factory=dict)


@dataclass
class ServiceLogStats:
    warnings: int = 0
    bad_gateway: int = 0
    tx_sent: int = 0
    tx_ok: int = 0
    tx_failed: int = 0
    vm_success: int = 0
    vm_failed: int = 0


@dataclass
class Process:
    name: str
    uptime: Optional[int]  # seconds; None when not running


@dataclass
class Stake:
    index: str
    eth: Optional[int] = None
    zg: Optional[int] = None
    cl: Optional[int] = None


@dataclass
class ChainFacts:
    source_value: Optional[int] = None
    target_value: Optional[int] = None
    total_supply: Optional[int] = None
    oracle_value: Optional[int] = None
    oracle_age: Optional[int] = None
    withdrawal_demand: Optional[int] = None
    queued_shares: Optional[int] = None
    claimable: Optional[int] = None
    epoch_iterator: Optional[int] = None
    current_epoch: Optional[int] = None
    mellow_pending: Optional[int] = None
    mellow_claimable: Optional[int] = None
    gas: Dict[str, float] = field(default_factory=dict)
    # chain id -> next nonce of the operator account
    next_nonce: Dict[int, int] = field(default_factory=dict)
    chain_ids: Dict[str, int] = field(default_factory=dict)
    stakes: List[Stake] = field(default_factory=list)
    unexpected_cl: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


# -- log parsing (pure) ---------------------------------------------------------


def _local_ts(text: str, fmt: str) -> float:
    return time.mktime(time.strptime(text, fmt))


def _utc_ts(text: str) -> float:
    import calendar

    return calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%S"))


def parse_bot_log(text: str, since: float) -> BotStats:
    """Read the scheduler log. Counts cover cycles at or after `since`; the
    recent steps, distributions and nonces are taken from the whole text,
    because they are not about the window but about the latest state."""
    stats = BotStats()
    cycles: List[Tuple[float, List[str]]] = []
    current: Optional[List[str]] = None
    pending_nonce: Optional[int] = None
    steps: List[float] = []
    distributions: List[int] = []

    for raw in text.splitlines():
        line = ANSI.sub("", raw)
        m = CYCLE.match(line)
        if m:
            current = []
            cycles.append((_local_ts(m.group(1), "%Y-%m-%d %H:%M:%S"), current))
            continue
        if current is not None:
            current.append(line)
        m = SENT.search(line)
        if m:
            pending_nonce = int(m.group(1))
        m = MINED.search(line)
        if m and pending_nonce is not None:
            chain = int(m.group(1))
            stats.last_nonce[chain] = max(
                stats.last_nonce.get(chain, -1), pending_nonce
            )
            pending_nonce = None
        m = HEARTBEAT_WRITE.search(line)
        if m:
            steps.append(float(m.group(4)))
        m = DISTRIBUTED.search(line)
        if m:
            distributions.append(int(m.group(1)))

    stats.recent_steps = steps[-8:]
    stats.recent_distributions = distributions[-6:]

    previous_had_heartbeat = False
    for at, lines in cycles:
        body = "\n".join(lines)
        had_heartbeat = HEARTBEAT_WRITE.search(body) is not None
        if at >= since:
            stats.cycles += 1
            stats.last_cycle = at
            for m in HEARTBEAT_WRITE.finditer(body):
                stats.heartbeats.append(
                    (at, int(m.group(1)), m.group(2), float(m.group(4)))
                )
            for m in FAILED.finditer(body):
                stats.failures.append((at, m.group(1), int(m.group(2))))
            for m in RECOVERED.finditer(body):
                stats.recoveries.append((at, m.group(1)))
            stats.tracebacks += body.count("Traceback (most recent call last)")
            stats.retries += sum(1 for m in SENT.finditer(body) if int(m.group(2)) > 1)
            for l in dict.fromkeys(l.strip() for l in lines):
                if "refused:" in l and "resync" not in l:
                    stats.refusals.append(l)
            if "[oracle_update] resync" in body:
                if "resync setValue(" in body:
                    outcome = "write"
                elif "resync refused" in body:
                    outcome = "refused"
                elif "resync failed" in body or "Could not resync" in body:
                    outcome = "failed"
                else:
                    outcome = "noop"
                stats.resyncs.append(Resync(at, outcome, previous_had_heartbeat))
            for l in lines:
                m = CROSS_CHAIN.search(l)
                if m:
                    stats.cross_chain.append((at, m.group(1)))
            stats.epochs_processed += len(re.findall(r"Epoch \d+ processed", body))
        previous_had_heartbeat = had_heartbeat
    return stats


def parse_service_log(text: str, since: float) -> ServiceLogStats:
    """The restaking service and monitor log ISO UTC timestamps. Lines without
    one (wrapped output) belong to the last line that had one."""
    stats = ServiceLogStats()
    inside = False
    for raw in text.splitlines():
        line = ANSI.sub("", raw)
        m = ISO_TS.match(line)
        if m:
            inside = _utc_ts(m.group(1)) >= since
        if not inside:
            continue
        if " WARN " in line or " ERROR " in line:
            stats.warnings += 1
            if "502 Bad Gateway" in line:
                stats.bad_gateway += 1
        if "new transaction sent with hash" in line:
            stats.tx_sent += 1
        if re.search(r"transaction 0x[0-9a-fA-F]+ success", line):
            stats.tx_ok += 1
        if re.search(r"transaction 0x[0-9a-fA-F]+ (failed|reverted)", line):
            stats.tx_failed += 1
    return stats


def parse_vault_monitor_log(text: str, since: float) -> ServiceLogStats:
    stats = ServiceLogStats()
    for line in text.splitlines():
        m = BRACKET_TS.match(line)
        if not m or _local_ts(m.group(1), "%Y-%m-%d %H:%M:%S") < since:
            continue
        if "] SUCCESS" in line:
            stats.vm_success += 1
        elif "] FAILED" in line:
            stats.vm_failed += 1
    return stats


def tail_since(
    path: str,
    since: float,
    first_ts: Callable[[str], Optional[float]],
    start: int = 8 * 1024 * 1024,
) -> str:
    """The end of a log, reaching back at least to `since`.

    The monitor log runs to gigabytes, so it is read from the end in growing
    chunks until the oldest timestamp in the chunk is older than `since`.
    """
    size = os.path.getsize(path)
    want = min(start, size)
    while True:
        with open(path, "rb") as f:
            f.seek(size - want)
            chunk = f.read(want).decode("utf-8", "replace")
        if want == size:
            return chunk
        # The first line is likely cut in half; the first whole one decides.
        lines = chunk.split("\n", 1)
        body = lines[1] if len(lines) > 1 else ""
        oldest = None
        for line in body.splitlines():
            oldest = first_ts(ANSI.sub("", line))
            if oldest is not None:
                break
        if oldest is not None and oldest < since:
            return body
        want = min(want * 2, size)


def _iso_first_ts(line: str) -> Optional[float]:
    m = ISO_TS.match(line)
    return _utc_ts(m.group(1)) if m else None


def _bracket_first_ts(line: str) -> Optional[float]:
    m = BRACKET_TS.match(line)
    return _local_ts(m.group(1), "%Y-%m-%d %H:%M:%S") if m else None


def read_tail(path: str, max_bytes: int) -> str:
    """The whole file, or its last `max_bytes` from the first whole line."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if size <= max_bytes:
            return f.read().decode("utf-8", "replace")
        f.seek(size - max_bytes)
        return f.read().decode("utf-8", "replace").split("\n", 1)[-1]


# -- rules (pure) ---------------------------------------------------------------


def expected_heartbeats(
    since: float, now: float, interval: int, grace: int = 300
) -> int:
    """Heartbeat slots that fall inside the window and have had time to land."""
    first = -(-int(since) // interval) * interval
    last = int(now) - grace
    return max(0, (last - first) // interval + 1) if last >= first else 0


def evaluate(
    bot: BotStats,
    chain: ChainFacts,
    processes: List[Process],
    log_ages: Dict[str, Optional[float]],
    restaking: ServiceLogStats,
    monitor: ServiceLogStats,
    vault_monitor: ServiceLogStats,
    expected_beats: int,
) -> List[Finding]:
    out: List[Finding] = []

    for p in processes:
        if p.uptime is None:
            out.append(Finding(RED, "{} is not running".format(p.name)))
    for name, age in log_ages.items():
        if age is None:
            out.append(Finding(RED, "{} log not found".format(name)))
        elif age > MAX_LOG_AGE[name]:
            out.append(Finding(RED, "{} log silent for {}".format(name, _dur(age))))

    if len(bot.heartbeats) < expected_beats:
        out.append(
            Finding(
                RED,
                "{} oracle heartbeat(s), expected {}".format(
                    len(bot.heartbeats), expected_beats
                ),
            )
        )
    for at, task, n in bot.failures:
        # One the scheduler has since recovered from was a blip, not an outage.
        recovered = any(t == task and r > at for r, t in bot.recoveries)
        out.append(
            Finding(
                YELLOW if recovered else RED,
                "{} failed ({} in a row) at {}{}".format(
                    task, n, _clock(at), ", recovered" if recovered else ""
                ),
            )
        )
    if bot.tracebacks and not bot.failures:
        out.append(
            Finding(RED, "{} traceback(s) in the bot log".format(bot.tracebacks))
        )
    for text in bot.refusals:
        out.append(Finding(RED, "oracle write {}".format(text)))
    if bot.retries:
        out.append(
            Finding(YELLOW, "{} transaction(s) needed a fee bump".format(bot.retries))
        )
    for r in bot.resyncs:
        if r.outcome in ("refused", "failed"):
            out.append(Finding(RED, "resync {} at {}".format(r.outcome, _clock(r.at))))
        elif r.outcome == "write":
            out.append(
                Finding(YELLOW, "resync wrote the oracle at {}".format(_clock(r.at)))
            )
        elif not r.after_heartbeat:
            out.append(
                Finding(YELLOW, "resync between heartbeats at {}".format(_clock(r.at)))
            )
    if bot.recent_steps:
        step = bot.recent_steps[-1]
        if not STEP_BPS_RANGE[0] <= step <= STEP_BPS_RANGE[1]:
            out.append(Finding(YELLOW, "last oracle step {:+.2f} bps".format(step)))

    for err in chain.errors:
        out.append(Finding(RED, err))

    price = share_price(chain)
    if price is not None and chain.oracle_value:
        drift = abs(price * 1e18 - chain.oracle_value) / chain.oracle_value * 1e4
        if drift > MAX_PRICE_DRIFT_BPS:
            out.append(
                Finding(RED, "share price {:.2f} bps off the oracle".format(drift))
            )
    ratio = source_ratio(chain)
    if ratio is not None:
        if ratio < RATIO_BAND[0]:
            out.append(
                Finding(
                    YELLOW,
                    "source ratio {:.1%}: queued withdrawals exceed source balance; "
                    "rebalance pulls from Ethereum".format(ratio),
                )
            )
        elif ratio > RATIO_BAND[1]:
            out.append(
                Finding(
                    YELLOW,
                    "source ratio {:.1%}: rebalance pushes the surplus".format(ratio),
                )
            )

    for key, minimum in MIN_GAS.items():
        have = chain.gas.get(key)
        if have is not None and have < minimum:
            out.append(
                Finding(RED, "{} gas {:.4f} below {}".format(key, have, minimum))
            )

    for label, chain_id in chain.chain_ids.items():
        nxt = chain.next_nonce.get(chain_id)
        last = bot.last_nonce.get(chain_id)
        if nxt is None:
            continue
        if last is None:
            if nxt > 0:
                out.append(
                    Finding(
                        YELLOW,
                        "operator nonce on {} not found in the bot log".format(label),
                    )
                )
        elif nxt > last + 1:
            out.append(
                Finding(
                    RED,
                    "operator sent {} transaction(s) on {} that the bot did not".format(
                        nxt - last - 1, label
                    ),
                )
            )

    for s in chain.stakes:
        if None in (s.eth, s.zg, s.cl):
            continue
        if not s.eth == s.zg == s.cl:
            out.append(
                Finding(
                    RED,
                    "validator {} stake differs: eth {} / 0g {} / cl {}".format(
                        s.index, _og(s.eth), _og(s.zg), _og(s.cl)
                    ),
                )
            )
    for idx in chain.unexpected_cl:
        out.append(
            Finding(
                RED,
                "CL validator {} has restaking stake but is not tracked".format(idx),
            )
        )
    gap = recon_gap(chain)
    if gap is not None and abs(gap) > MAX_RECON_GAP:
        out.append(
            Finding(
                YELLOW,
                "target value differs from stake + in-flight by {:+,.2f}".format(gap),
            )
        )

    if restaking.warnings:
        out.append(
            Finding(
                YELLOW, "restaking service: {} warning(s)".format(restaking.warnings)
            )
        )
    if restaking.tx_failed:
        out.append(
            Finding(
                RED,
                "restaking service: {} failed transaction(s)".format(
                    restaking.tx_failed
                ),
            )
        )
    if monitor.warnings:
        detail = (
            " ({} × 502)".format(monitor.bad_gateway) if monitor.bad_gateway else ""
        )
        out.append(
            Finding(YELLOW, "monitor: {} warning(s){}".format(monitor.warnings, detail))
        )
    if vault_monitor.vm_failed:
        out.append(
            Finding(
                YELLOW,
                "vault-monitor: {} failed check(s)".format(vault_monitor.vm_failed),
            )
        )

    out.sort(key=lambda f: f.level != RED)
    return out


def share_price(c: ChainFacts) -> Optional[float]:
    if None in (c.source_value, c.target_value) or not c.total_supply:
        return None
    return (c.source_value + c.target_value) / c.total_supply


def source_ratio(c: ChainFacts) -> Optional[float]:
    if None in (c.source_value, c.target_value, c.withdrawal_demand):
        return None
    net = c.source_value + c.target_value - c.withdrawal_demand
    return (c.source_value - c.withdrawal_demand) / net if net > 0 else None


def recon_gap(c: ChainFacts) -> Optional[float]:
    if c.target_value is None or c.mellow_pending is None or not c.stakes:
        return None
    if any(s.eth is None for s in c.stakes):
        return None
    staked = sum(s.eth for s in c.stakes)
    return (
        c.target_value - staked - c.mellow_pending - (c.mellow_claimable or 0)
    ) / 1e18


# -- rendering (pure) -----------------------------------------------------------


def _og(wei: Optional[int], places: int = 2) -> str:
    return "-" if wei is None else "{:,.{}f}".format(wei / 1e18, places)


def _dur(seconds: float) -> str:
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return "{}d{}h".format(d, h)
    if h:
        return "{}h{}m".format(h, m)
    return "{}m".format(m)


def _clock(ts: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def render(
    findings: List[Finding],
    bot: BotStats,
    chain: ChainFacts,
    processes: List[Process],
    log_ages: Dict[str, Optional[float]],
    restaking: ServiceLogStats,
    monitor: ServiceLogStats,
    vault_monitor: ServiceLogStats,
    expected_beats: int,
    hours: int,
    now: float,
) -> str:
    reds = sum(1 for f in findings if f.level == RED)
    yellows = len(findings) - reds
    head = "*mellow-interop daily report* {} ({}h)\n".format(
        time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(now)), hours
    )
    head += "✅ no issues" if not findings else "🔴 {}  🟡 {}".format(reds, yellows)

    rows: List[str] = []
    for f in findings:
        rows.append(("🔴 " if f.level == RED else "🟡 ") + f.text)
    if findings:
        rows.append("")

    rows.append("Services")
    log_of = {
        "bot": "bot",
        "restaking service": "restaking",
        "monitor": "monitor",
        "vault-monitor": "vault-monitor",
    }
    for p in processes:
        age = log_ages.get(log_of[p.name])
        rows.append(
            "  {:<18}{:<10}log {}".format(
                p.name,
                "down" if p.uptime is None else "up " + _dur(p.uptime),
                "-" if age is None else _dur(age) + " ago",
            )
        )

    rows.append("Bot")
    rows.append(
        "  heartbeats {}/{}  failures {}  bumps {}  refusals {}".format(
            len(bot.heartbeats),
            expected_beats,
            len(bot.failures),
            bot.retries,
            len(bot.refusals),
        )
    )
    after = sum(1 for r in bot.resyncs if r.after_heartbeat and r.outcome == "noop")
    rows.append(
        "  resync     {} after heartbeat (no-op), {} other".format(
            after, len(bot.resyncs) - after
        )
    )
    rows.append(
        "  steps bps  " + " ".join("{:.2f}".format(s) for s in bot.recent_steps)
    )
    rows.append(
        "  distrib.   "
        + " ".join("{:.0f}".format(d / 1e18) for d in bot.recent_distributions)
    )
    if bot.cross_chain:
        for at, what in bot.cross_chain[-4:]:
            rows.append("  x-chain    {} {}".format(_clock(at), what[:60]))
    else:
        rows.append("  x-chain    none")
    rows.append("  epochs     {} processed".format(bot.epochs_processed))

    rows.append("Vault")
    total = (
        None
        if None in (chain.source_value, chain.target_value)
        else chain.source_value + chain.target_value
    )
    rows.append(
        "  assets     {} (src {} / tgt {})".format(
            _og(total), _og(chain.source_value), _og(chain.target_value)
        )
    )
    price = share_price(chain)
    rows.append(
        "  price      {}  oracle {}  age {}".format(
            "-" if price is None else "{:.9f}".format(price),
            (
                "-"
                if chain.oracle_value is None
                else "{:.9f}".format(chain.oracle_value / 1e18)
            ),
            "-" if chain.oracle_age is None else _dur(chain.oracle_age),
        )
    )
    ratio = source_ratio(chain)
    rows.append(
        "  ratio      {}  queued {} shares".format(
            "-" if ratio is None else "{:.1%}".format(ratio), _og(chain.queued_shares)
        )
    )
    rows.append(
        "  queue      epoch {}/{}  claimable {}".format(
            chain.epoch_iterator, chain.current_epoch, _og(chain.claimable)
        )
    )
    rows.append(
        "  mellow q   pending {}  claimable {}".format(
            _og(chain.mellow_pending), _og(chain.mellow_claimable)
        )
    )

    rows.append("Gas")
    rows.append(
        "  operator   {} OG  {} ETH   service {} OG".format(
            *(
                "{:.4f}".format(chain.gas[k]) if k in chain.gas else "-"
                for k in ("operator_og", "operator_eth", "service_og")
            )
        )
    )

    rows.append("Restaking stake (eth = 0g = cl)")
    for s in chain.stakes:
        ok = None not in (s.eth, s.zg, s.cl) and s.eth == s.zg == s.cl
        rows.append(
            "  idx {:<6} {} {}".format(
                s.index, _og(s.eth, 6), "ok" if ok else "MISMATCH"
            )
        )
    gap = recon_gap(chain)
    rows.append(
        "  recon      {}".format("-" if gap is None else "{:+,.2f}".format(gap))
    )

    rows.append("Service logs ({}h)".format(hours))
    rows.append(
        "  restaking  warn {}  tx {} ok {} failed {}".format(
            restaking.warnings, restaking.tx_sent, restaking.tx_ok, restaking.tx_failed
        )
    )
    rows.append(
        "  monitor    warn {}  502 {}".format(monitor.warnings, monitor.bad_gateway)
    )
    rows.append(
        "  vault-mon  ok {}  failed {}".format(
            vault_monitor.vm_success, vault_monitor.vm_failed
        )
    )

    body = "\n".join(rows).replace("`", "'")
    message = head + "\n```\n" + body + "\n```"
    if len(message) > TELEGRAM_LIMIT:
        cut = TELEGRAM_LIMIT - len(head) - 40
        message = head + "\n```\n" + body[:cut] + "\n…(truncated)\n```"
    return message


# -- collection (I/O) -------------------------------------------------------------


def list_processes() -> List[Process]:
    out = subprocess.run(
        ["ps", "-eo", "etimes=,args="], capture_output=True, text=True, check=True
    ).stdout
    found: Dict[str, int] = {}
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        age, args = int(parts[0]), parts[1]
        # Skip the shells that launched them and anything grepping for them.
        if args.startswith(("bash -c", "sh -c", "grep", "ps ")):
            continue
        for name, needle in PROCESSES:
            if needle in args:
                found[name] = max(found.get(name, 0), age)
    return [Process(name, found.get(name)) for name, _ in PROCESSES]


def log_age(path: str, now: float) -> Optional[float]:
    try:
        # A log written while the report runs is newer than `now`.
        return max(0.0, now - os.path.getmtime(path))
    except OSError:
        return None


def collect_chain(config: Config, report: dict, now: float) -> ChainFacts:
    facts = ChainFacts()
    source = config.sources[0]
    deployment = source.deployments[0]

    def attempt(label, fn):
        try:
            fn()
        except Exception as e:
            facts.errors.append(
                "could not read {}: {}".format(
                    label, mask_all_sensitive_config_data(str(e), config)[:160]
                )
            )

    zg = get_w3(source.rpc)
    eth = get_w3(config.target_rpc)

    def vault():
        helper = get_contract(zg, source.source_core_helper, "SourceHelper").functions
        core = get_contract(zg, deployment.source_core, "SourceCore").functions
        facts.source_value = helper.getSourceValue(deployment.source_core).call()
        facts.target_value = (
            get_contract(eth, config.target_core_helper, "TargetHelper")
            .functions.getTargetValue(deployment.target_core)
            .call()
        )
        facts.total_supply = core.totalSupply().call()
        shares, supply = helper.getWithdrawalData(deployment.source_core).call()[:2]
        facts.withdrawal_demand = (
            (facts.source_value + facts.target_value) * shares // supply
            if supply
            else 0
        )
        oracle = get_contract(zg, core.oracle().call(), "Oracle").functions
        facts.oracle_value = oracle.getValue().call()
        facts.oracle_age = int(now - oracle.lastUpdated().call())

    def queue():
        q = get_contract(
            zg, source.withdrawal_queue.address, "WithdrawalQueue"
        ).functions
        it = q.epochIterator().call()
        facts.epoch_iterator = it
        facts.current_epoch = q.currentEpoch().call()
        facts.queued_shares = q.totalShares().call()
        facts.claimable = sum(
            q.withdrawals(e).call()
            for e in range(max(0, it - CLAIMABLE_LOOKBACK_EPOCHS), it)
        )

    def mellow():
        target_core = get_contract(eth, deployment.target_core, "TargetCore").functions
        mv = get_contract(eth, target_core.vault().call(), "MellowMultiVault").functions
        pending = claimable = 0
        for i in range(mv.subvaultsCount().call()):
            _, _, wq = mv.subvaultAt(i).call()
            if int(wq, 16) == 0:
                continue
            qf = get_contract(eth, wq, "MellowWithdrawalQueue").functions
            pending += qf.pendingAssetsOf(deployment.target_core).call()
            claimable += qf.claimableAssetsOf(deployment.target_core).call()
        facts.mellow_pending, facts.mellow_claimable = pending, claimable

    def gas_and_nonces():
        # The address, not the key: the report runs with the keys unset.
        op = zg.to_checksum_address(report["operator"])
        svc = zg.to_checksum_address(report["service_account"])
        facts.gas["operator_og"] = zg.eth.get_balance(op) / 1e18
        facts.gas["operator_eth"] = eth.eth.get_balance(op) / 1e18
        facts.gas["service_og"] = zg.eth.get_balance(svc) / 1e18
        # Read before the bot log, so a transaction the bot sends in between
        # makes the log run ahead of the chain, never behind it.
        for label, w3 in (("0G", zg), ("Ethereum", eth)):
            chain_id = w3.eth.chain_id
            facts.chain_ids[label] = chain_id
            facts.next_nonce[chain_id] = w3.eth.get_transaction_count(op)

    def stakes():
        states = get_contract(
            zg, report["restaking_states"], "RestakingStates"
        ).functions
        router = source.ascend.router
        cl = read_cl_restaking(report["beacon_api"])
        tracked = set()
        for v in report["validators"]:
            s = Stake(index=str(v["index"]))
            tracked.add(s.index)
            s.eth = (
                get_contract(eth, v["vault"], "SymbioticVault")
                .functions.activeStake()
                .call()
            )
            s.zg = sum(
                b[2]
                for b in states.getBalances(
                    zg.to_checksum_address(v["rewarder"]), router
                ).call()
            )
            s.cl = cl.get(s.index, 0)
            facts.stakes.append(s)
        facts.unexpected_cl = sorted((i for i in cl if i not in tracked), key=int)

    attempt("vault", vault)
    attempt("withdrawal queue", queue)
    attempt("Mellow withdrawal queues", mellow)
    attempt("gas/nonces", gas_and_nonces)
    attempt("restaking stake", stakes)
    return facts


def read_cl_restaking(beacon_api: str) -> Dict[str, int]:
    if not beacon_api:
        raise ValueError("ZG_BEACON_API is not set")
    url = beacon_api.rstrip("/") + "/eth/v1/beacon/states/head/validator_balances"
    # The beacon node sits on a private address; a proxy configured for the
    # host must not be used to reach it.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=30) as resp:
        data = json.load(resp)["data"]
    return {
        str(v["index"]): sum(int(e["amount"]) for e in v["symbiotic_balance"])
        for v in data
        if v.get("symbiotic_balance")
    }


def load_report_config(config: Config) -> dict:
    with open(CONFIG_PATH) as f:
        raw = json.load(f)
    return _transform_config(raw["daily-report"])


def build(config: Config, report: dict, hours: int, now: float) -> str:
    since = now - hours * 3600
    logs = report["logs"]

    chain = collect_chain(config, report, now)

    # Whole log: the latest nonces and steps can be older than the window.
    bot_text = (
        read_tail(logs["bot"], BOT_LOG_MAX_BYTES) if os.path.exists(logs["bot"]) else ""
    )
    bot = parse_bot_log(bot_text, since)

    def service(path, first_ts, parse):
        if not os.path.exists(path):
            return ServiceLogStats()
        return parse(tail_since(path, since, first_ts), since)

    restaking = service(logs["restaking"], _iso_first_ts, parse_service_log)
    monitor = service(logs["monitor"], _iso_first_ts, parse_service_log)
    vault_monitor = service(
        logs["vault_monitor"], _bracket_first_ts, parse_vault_monitor_log
    )
    processes = list_processes()
    ages = {
        "bot": log_age(logs["bot"], now),
        "restaking": log_age(logs["restaking"], now),
        "monitor": log_age(logs["monitor"], now),
        "vault-monitor": log_age(logs["vault_monitor"], now),
    }
    beats = expected_heartbeats(since, now, config.scheduler.interval("oracle_update"))
    findings = evaluate(
        bot, chain, processes, ages, restaking, monitor, vault_monitor, beats
    )
    return render(
        findings,
        bot,
        chain,
        processes,
        ages,
        restaking,
        monitor,
        vault_monitor,
        beats,
        hours,
        now,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print, do not send")
    parser.add_argument("--hours", type=int, default=24, help="window (default 24)")
    args = parser.parse_args()

    dotenv.load_dotenv()
    config = read_config(str(CONFIG_PATH))
    report = load_report_config(config)
    try:
        message = build(config, report, args.hours, time.time())
    except Exception as e:
        message = "*mellow-interop daily report*\n🔴 report failed: {}".format(
            mask_all_sensitive_config_data(str(e), config)[:300]
        )

    if args.dry_run:
        print(message)
        return 0
    if not (config.telegram_bot_api_key and config.telegram_group_chat_id):
        print(message)
        print(
            "TELEGRAM_BOT_API_KEY / TELEGRAM_GROUP_CHAT_ID not set; not sent",
            file=sys.stderr,
        )
        return 1
    from telegram_bot import send_message

    asyncio.run(
        send_message(
            config.telegram_bot_api_key, config.telegram_group_chat_id, message
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
