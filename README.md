# Mellow Interop Bot

A cross-chain oracle monitoring and validation bot that tracks oracle states across multiple blockchain networks and sends alerts via Telegram when intervention is needed.

## Prerequisites

- Python 3.13+ (or Docker)
- Access to blockchain RPC endpoints
- Telegram Bot API key (Optional)
- Smart contract addresses for monitoring

### Environment Variables

All environment variables can be optional.

- `ORACLE_UPDATER_PK` - Private key that writes the oracle, through the deployment's `oracle-updater` contract (see "Oracle updates"). Must hold `UPDATER_ROLE` on that OracleUpdater, which in turn holds `SET_VALUE_ROLE` on the SourceCore. Configured separately from `OPERATOR_PK` so the roles can be split — it is the only key that can move the share price. Pointing both at one account works and is a fine way to start; it just means one nonce sequence shared between the oracle write and everything else.
- `ORACLE_MAX_DEVIATION_BPS` - Refuse to write a value more than this far from the one on chain (default: `100`, i.e. 1%).
- `ORACLE_DECREASE_TOLERANCE_WEI` - Refuse to write a value that fell by more than this. Below it, a dip is rounding noise (default: `1000000000`).
- `ORACLE_UPDATER_MIN_BALANCE_WEI` - Warn when the updater's gas balance drops below this. It still writes; the point is warning while there is runway (default: `100000000000000000`).
- `ORACLE_EXPIRY_THRESHOLD_SECONDS` - How close to expiry counts as urgent. No longer triggers the write — the heartbeat is unconditional — but it decides how loudly a refusal is described (default: `172800`).
- `POST_ASCEND_GAP_SECONDS` - Wait between a reward distribution and the oracle write that prices it. Deposits in that wait take shares at the old price, so it is kept short; it must exceed the oracle's 15s read lag, or the write reads a block from before the distribution and puts the old price back. Values of 15 or less are rejected (default: `20`).
- `ORACLE_RECENT_UPDATE_THRESHOLD_SECONDS` - Threshold in seconds to determine if an oracle was recently updated. When an oracle was updated within this timeframe, the bot sends a confirmation message to notify that the oracle has been updated (default: `0`).
- `TELEGRAM_OWNER_NICKNAMES` - Comma-separated telegram nicknames of safe signers. Supports two formats: simple nicknames (`@josh,@anna,@dexter`) or `nickname:address` pairs (`@josh:0x123...,@anna:0xabc...`). Nicknames are mentioned in proposal messages when their confirmation is needed.
- `TARGET_RPC` - Target blockchain RPC endpoint (see default in `config.json`).
- `BSC_RPC` - BSC RPC endpoint (see default in `config.json`).
- `FRAX_RPC` - Fraxtal RPC endpoint (see default in `config.json`).
- `LISK_RPC` - Lisk RPC endpoint (see default in `config.json`).
- `DRY_RUN` - Run without sending telegram messages (default: `false`).

Optional if `DRY_RUN` is `true`:

- `TELEGRAM_BOT_API_KEY` - Your Telegram bot API key
- `TELEGRAM_GROUP_CHAT_ID` - Target Telegram group chat ID

#### Safe Global Variables

The bot uses Safe Global for proposing multi-signature transactions. You can configure these globally or per-chain:

**Global Safe Variables (fallback for all chains):**

- `SAFE_PROPOSER_PK` - Private key of the Safe proposer/signer.
- `SAFE_API_KEY` - Safe Global API key for transaction service access.

There are some chain-specific variables, like: `BSC_SAFE_API_KEY`, `FRAX_SAFE_PROPOSER_PK` etc., see `config.json`.

> Chain-specific variables take precedence over global variables. If not set, the system falls back to the global `SAFE_*` variables.

## Usage

### Running locally

1. Create a virtual environment:

    ```bash
    python -m venv .venv
    source .venv/bin/activate 
    ```

2. Install dependencies:

    ```bash
    pip install -r requirements.txt
    ```

3. Set up environment variables if needed (you can use `.env` file). It is recommended to explicitly set `*_RPC` variables to use custom RPC endpoints to avoid 400/429 network errors.

4. Run one of the entry points.

    `src/main.py` is a single oracle pass. It **writes the oracle directly**
    with `ORACLE_UPDATER_PK`; use `cli.py oracle --dry-run` if you want to see
    what it would do without broadcasting:

    ```bash
    python ./src/main.py
    DRY_RUN=true python ./src/main.py   # same, without sending Telegram messages
    ```

    > `DRY_RUN` only suppresses Telegram. It does not stop the oracle write.

    It takes the same lock as the scheduler, so it will refuse to run while the
    scheduler is up rather than sign a second transaction from the same
    account. It exits non-zero if the run failed, so a supervisor can tell.

    `src/scheduler.py` is the production loop. It **signs and broadcasts real
    transactions on both chains** — advancing the withdrawal queue within
    minutes and rebalancing within hours — so run it only where that is
    intended. `DRY_RUN` does not gate this; it only suppresses Telegram.

    ```bash
    python -u ./src/scheduler.py
    ```

    `src/cli.py` runs any one of those operations once. Pass `--dry-run` to
    ascend to simulate the calls without broadcasting:

    ```bash
    python ./src/cli.py ascend --dry-run
    python ./src/cli.py handle-epoch
    python ./src/cli.py oracle --dry-run   # compute + run the guards, broadcast nothing
    python ./src/cli.py oracle
    python ./src/cli.py rebalance -y
    python ./src/cli.py validate-config
    ```

    `validate-config` checks that each deployment's `oracle-updater` points at
    its SourceCore, that `ORACLE_UPDATER_PK`'s address holds `UPDATER_ROLE` on
    it, and that the OracleUpdater holds `SET_VALUE_ROLE` on the SourceCore.
    Run it after any key or role change — the grants are manual multisig steps,
    and a missing one makes every write revert.

5. Optionally, run tests:

    ```bash
    python -m unittest discover -s tests -p "test_*.py" -v
    ```

## Oracle updates

The oracle is written by a dedicated account (`ORACLE_UPDATER_PK`) on every
`oracle-update` tick, whether or not the value has changed — the write refreshes
`lastUpdated`, and `getValue()` reverts once `maxAge` passes, which stops
deposits, withdrawals and rebalancing.

The account does not hold the oracle's role itself. It calls `setValue` on the
deployment's **OracleUpdater** (`oracle-updater` in `config.json`; contract in
`0g-restaking-contracts`, `src/ascend/OracleUpdater.sol`), which holds
`SET_VALUE_ROLE` and forwards a value only if it lies in
`[oracle.value(), maxValue()]` — never lower than the oracle, never higher than
the share price the vault's W0G on 0G backs (`(W0G in SourceCore + W0G locked in
the OFT adapter) / aOG supply`). That is what limits a leaked key. The bot sends
a reading a hair under the oracle as the oracle's own value, and a reading over
`maxValue()` as `maxValue()` (logged, not alerted: a deposit at the stale price
between the read and the send is the usual cause). If such a deposit lands
after `maxValue()` was read, the send reverts with `OracleUpdaterValueAboveNav`
and is retried once at the NAV the revert reports. **Lowering the value is only
possible from the Safe** — see `cli.py oracle-propose` below, which warns when a
proposed value is above `maxValue()`, since the heartbeat could not refresh it.

`ascend` runs on the same interval, and must not run less often: the vault's
assets only move when ascend transfers rewards into it, so the share price
steps at the ascend interval no matter how often the oracle is written.

Nothing reviews these writes, so two guards stand in for the signers who used
to. Both **refuse to write** and raise a Telegram alert:

| Guard | Default | Fires when |
|---|---|---|
| deviation | `ORACLE_MAX_DEVIATION_BPS=100` (1%) | the new value is more than 1% from the one on chain |
| decrease | `ORACLE_DECREASE_TOLERANCE_WEI=1e9` | the value fell by more than rounding |
| NAV | — | the OracleUpdater's `maxValue()` is below the oracle value |

A refusal **does not fix itself, and gets worse**: ascend keeps adding rewards,
so the gap widens every day. Rebalancing also starts refusing once the oracle
disagrees with the computed value. `maxAge` is the deadline — after that the
vault is frozen until someone acts.

A refusal is announced once, then again once per `oracle-update` interval while
it lasts. The write itself is retried every cycle; its alert is not.

### Between heartbeats: resync

The vault is priced against the oracle (`totalAssets = totalSupply × oracle`),
so anything that moves the vault's real value between two writes leaves the two
apart until the next one. Two things do:

- **A reward distribution or W0G sent straight to the SourceCore** between two
  heartbeats, which raises the value without minting anything.
- **A deposit in the window after a reward distribution** used to be the other:
  it takes shares at the old price and dilutes the share price toward it, and
  a heartbeat that read the value before such a deposit wrote a figure the
  vault no longer backed. Writes now go through the OracleUpdater, which caps
  them at the NAV of the block they land in, so the oracle can no longer end up
  above the vault's value this way.

Either used to leave rebalancing refusing until the next write, up to eight
hours.

Now, when rebalancing finds the oracle out of line, it declines and retries on
the next cycle, and asks for a **resync** in between. A resync re-reads the value
and writes only if the gap is really there:

| Reading | Resync |
|---|---|
| within 1 gwei | nothing — this is most often a read taken seconds before the last write |
| transfer in flight | waits for the next cycle |
| higher (a donation) | writes, if within the deviation guard, capped at `maxValue()` |
| higher, but `maxValue()` is not | refuses — the chain does not back the rise |
| lower | refuses — only the Safe can lower the oracle; the rebalance skip alert names the reason |
| past the deviation guard | refuses |

A resync sends no Telegram of its own. Rebalancing is what is blocked, and its
skip alert already speaks on its own cadence.

It also runs one cycle after every heartbeat, as a no-op. The rebalance in the
heartbeat's own cycle reads a block from before the write, so it always sees the
old value and declines; the resync on the next cycle finds the oracle in line,
writes nothing, and rebalance acts. Before this, that rebalance waited out its
two-hour interval instead.

### When a guard refuses

1. Look at the reading. This broadcasts nothing:

    ```bash
    python ./src/cli.py oracle --dry-run
    ```

    Check the source value, target value and total supply against what you
    expect. A helper returning zero, a halved position or a decimals mismatch
    all look like exactly what the guard is built to stop.

2. **If the value is right**, put it in front of the Safe signers. This signs
   off chain and posts to the Safe service — it broadcasts nothing, takes no
   nonce, and **needs no lock, so leave the scheduler running**:

    ```bash
    python ./src/cli.py oracle-propose
    python ./src/cli.py oracle-propose --value 1107091510212295064   # a hand-checked number
    ```

    Use `--value` when the guard fired *because* the computed value cannot be
    trusted; proposing the bad reading would only launder it through the
    multisig.

3. **If the reading is wrong**, fix the RPC or the helper. Do not force it.

4. `cli.py oracle --force` skips the deviation guard and writes with the bot's
   own key, through the OracleUpdater. It cannot lower the value or exceed
   `maxValue()` — the contract refuses both — so a fall always goes through
   `oracle-propose`. It requires stopping the scheduler (it takes the lock), and
   it will stop being available at all once the key moves somewhere a person
   cannot reach — prefer `oracle-propose`.

## Daily report

`src/daily_report.py` posts one message a day to the Telegram group: the
problems first, then the numbers they were judged on. It reads and never
writes -- no key, no transaction, no process lock -- so it runs beside the
scheduler, on the host whose logs it reads.

```bash
python ./src/daily_report.py --dry-run          # print, send nothing
python ./src/daily_report.py --hours 72         # widen the window
python ./src/daily_report.py                    # post
```

What it checks, and what it flags:

| Area | Flagged when |
|---|---|
| bot, restaking service, monitor, vault-monitor | a process is down, or its log has gone quiet |
| oracle | fewer heartbeats than the schedule owes; the latest step outside 0.5-2 bps; the share price more than 5 bps off the oracle |
| bot log | a task failed (yellow if it later recovered), a write was refused, a transaction needed a fee bump, a resync wrote, refused, or ran outside the cycle after a heartbeat |
| operator account | its nonce on either chain is ahead of the last one the bot logged -- something else signed with the key |
| gas | operator below 20 OG / 0.05 ETH, restaking service below 2 OG |
| source ratio | outside 0-10%, the band rebalance holds it in |
| restaking stake | a validator's Symbiotic `activeStake`, 0G `RestakingStates` balance and CL restaking balance differ, or the CL has a restaking validator the config does not list |
| target value | it differs from stake plus in-flight Mellow withdrawals |
| service logs | warnings (502s counted apart), failed restaking transactions, failed vault-monitor checks |

Addresses are under `daily-report` in `config.json`. The beacon API and the
log paths come from the environment: `ZG_BEACON_API` (required),
`BOT_LOG`, `RESTAKING_LOG`, `MONITOR_LOG`, `VAULT_MONITOR_LOG`.

Run it from cron at a fixed time, for example:

```
7 10 * * * cd /data/mellow-interop-bot && ./.venv/bin/python src/daily_report.py >> /data/daily-report.log 2>&1
```

### Running with Docker

1. Build the container:

    ```bash
    docker build -t mellow-interop-bot .
    ```

2. Run with environment variables. The image's default command is the
   **scheduler**, which broadcasts real transactions, so mount a volume for the
   file that records when each task last ran — without it a redeploy just after
   an interval boundary silently forfeits that run:

    ```bash
    docker run --env-file .env -v mellow-bot-state:/app/state mellow-interop-bot
    ```

   To run a single monitoring pass instead, override the command:

    ```bash
    docker run --env-file .env mellow-interop-bot python ./src/main.py
    ```

### Running scripts

The `./src/web3_scripts` folder contains scripts that can be run separately. These scripts are also based on the configuration from the `config.json` file, but have their own settings provided through environment variables.

#### `oracle_script.py`

```bash
python ./src/web3_scripts/oracle_script.py
```

---

#### `operator_script.py`

```bash
python ./src/web3_scripts/oracle_script.py
```

Environment variables

- SOURCE_RATIO_D3 - Determines if rebalance is required due to asset deficit (default: `50`)
- MAX_SOURCE_RATIO_D3 - Maximum asset ratio threshold that triggers surplus rebalancing (default: `100`)

---

#### `operator_bot.py`

```bash
OPERATOR_PK=<pk> DEPLOYMENTS=<source:symbol> python ./src/web3_scripts/operator_bot.py
```

Run non-interactively (skip the confirmation prompt):

```bash
NON_INTERACTIVE=true OPERATOR_PK=<pk> DEPLOYMENTS=<source:symbol> python ./src/web3_scripts/operator_bot.py
```

If you want to run a script on a newly added deployment, you may need to validate it first by executing the following command:

```bash
python ./src/config/validate_config.py 
```

Environment variables

Same `SOURCE_RATIO_D3` and `MAX_SOURCE_RATIO_D3`, plus:

- OPERATOR_PK - Private key to send transactions (required)
- DEPLOYMENTS - Comma-separated list of deployments for which the script needs to be run (required, example: `BSC:CYC,FRAXTAL:FRAX`). See the `config.json` for all avaiable pairs.
- NON_INTERACTIVE - If set to `true`, skips the interactive confirmation prompt (default: prompt enabled).
