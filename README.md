# Mellow Interop Bot

A cross-chain oracle monitoring and validation bot that tracks oracle states across multiple blockchain networks and sends alerts via Telegram when intervention is needed.

## Prerequisites

- Python 3.13+ (or Docker)
- Access to blockchain RPC endpoints
- Telegram Bot API key (Optional)
- Smart contract addresses for monitoring

### Environment Variables

All environment variables can be optional.

- `ORACLE_UPDATER_PK` - Private key that writes `Oracle.setValue`. Must hold `SET_VALUE_ROLE` on the SourceCore (granted from the Safe; see "Oracle updates"). Configured separately from `OPERATOR_PK` so the roles can be split — it is the only key that can move the share price. Pointing both at one account works and is a fine way to start; it just means one nonce sequence shared between the oracle write and everything else.
- `ORACLE_MAX_DEVIATION_BPS` - Refuse to write a value more than this far from the one on chain (default: `100`, i.e. 1%).
- `ORACLE_DECREASE_TOLERANCE_WEI` - Refuse to write a value that fell by more than this. Below it, a dip is rounding noise (default: `1000000000`).
- `ORACLE_UPDATER_MIN_BALANCE_WEI` - Warn when the updater's gas balance drops below this. It still writes; the point is warning while there is runway (default: `100000000000000000`).
- `ORACLE_EXPIRY_THRESHOLD_SECONDS` - How close to expiry counts as urgent. No longer triggers the write — the heartbeat is unconditional — but it decides how loudly a refusal is described (default: `172800`).
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

    `validate-config` checks that `ORACLE_UPDATER_PK`'s address actually holds
    `SET_VALUE_ROLE`. Run it after any key change — granting the role is a
    manual multisig step, and without it every write reverts with
    `Oracle: forbidden`.

5. Optionally, run tests:

    ```bash
    python -m unittest discover -s tests -p "test_*.py" -v
    ```

## Oracle updates

The oracle is written directly by a dedicated account (`ORACLE_UPDATER_PK`) on
every `oracle-update` tick, whether or not the value has changed — the write
refreshes `lastUpdated`, and `getValue()` reverts once `maxAge` passes, which
stops deposits, withdrawals and rebalancing.

`ascend` runs on the same interval, and must not run less often: the vault's
assets only move when ascend transfers rewards into it, so the share price
steps at the ascend interval no matter how often the oracle is written.

Nothing reviews these writes, so two guards stand in for the signers who used
to. Both **refuse to write** and raise a Telegram alert:

| Guard | Default | Fires when |
|---|---|---|
| deviation | `ORACLE_MAX_DEVIATION_BPS=100` (1%) | the new value is more than 1% from the one on chain |
| decrease | `ORACLE_DECREASE_TOLERANCE_WEI=1e9` | the value fell by more than rounding |

A refusal **does not fix itself, and gets worse**: ascend keeps adding rewards,
so the gap widens every day. Rebalancing also starts refusing once the oracle
disagrees with the computed value. `maxAge` is the deadline — after that the
vault is frozen until someone acts.

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

4. `cli.py oracle --force` skips both guards and writes with the bot's own key.
   It requires stopping the scheduler (it takes the lock), and it will stop
   being available at all once the key moves somewhere a person cannot reach —
   prefer `oracle-propose`.

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
    docker run --env-file .env -v mellow-bot-state:/state mellow-interop-bot
    ```

   To run a single monitoring pass instead, override the command:

    ```bash
    docker run --env-file .env mellow-interop-bot python ./src/main.py
    ```

## Running on 0G Tapp (keys inside a TEE)

Deployed as a [0G Tapp](https://github.com/0glabs/0g-tapp) application, the bot
does not read its signing key from anywhere. It fetches one from the tapp Unix
socket at every start, and that key exists only in the process's memory: nothing
is written to disk, and nobody with shell on the host can read it back.

Set `TAPP_APP_ID` and the bot switches to that path. Leave it unset — locally,
and on the current server deployment — and nothing changes: keys come from
`.env` exactly as before.

### Which key, and why it survives a restart

A tapp offers two. The default one (`GetAppSecretKey`) is generated with
`OsRng` inside the tapp-server process and is regenerated whenever that process
restarts — a package upgrade, a crash, a reboot. An address funded and granted
roles under it is stranded on the next restart, so it is unusable here.

The bot uses `GetSecretResource` instead: a key the KMS cluster derives from
`(app_id, material)`. It is identical across container restarts, tapp-server
restarts, CVM reboots, image upgrades and every node of the app — fund it once,
grant its roles once.

The catch is that the KMS authenticates the caller by the tapp node's on-chain
registered signer, so the app must be registered in TappRegistry before a fetch
succeeds, and a fresh registration answers `401` for a while as the cluster's
view of the chain catches up. The bot retries indefinitely through that window
rather than crash-looping, and says so on Telegram.

After anything that restarts tapp-server, the node's registered signer is stale
and the fetch fails until it is re-registered:

```bash
tapp-cli -s http://<tapp>:50051 -k 0x<owner> update-node-onchain \
  --app-id mellow-interop-bot --rpc-url <0G RPC> --contract 0x<TappRegistry>
```

After a CVM reboot the tapp is unclaimed as well, so `claim-config` comes first.
The bot's own address is unchanged throughout.

### What goes where

Everything `tapp-cli` uploads is hashed into the runtime measurement, and
`GetAppInfo` — an unauthenticated RPC — hands the compose text back to anyone
who asks. So:

| | where | published |
|---|---|---|
| intervals, thresholds, `DEPLOYMENTS` | `docker-compose.yml` `environment:` | yes, in full |
| Telegram token, credentialed RPC URLs | `bot.env` (see `bot.env.example`) | hash only |
| the signing key | derived in the TEE, never transmitted | no |

Changing any of the first two changes the measurement, so `update-onchain` has
to follow or `verify-app` reports a mismatch.

`bot.env` travels to the CVM over plaintext gRPC (tapp-server's `tls_enabled`
defaults to false) — run `start-app` over an SSH tunnel, or configure server TLS.

### Deploying

```bash
# 1. Claim the node, pointing it at the chain, the KMS cluster and a verifier.
tapp-cli -s http://<tapp>:50051 -k 0x<owner> claim-config \
  --chain-rpc-url <0G RPC> --chain-contract 0x<TappRegistry> \
  --kbs-urls "https://kms-1:9443,https://kms-2:9443" \
  --scan-url https://<scan> --scan-pubkey 0x<sha256>

# 2. Log in to the registry holding the bot image.
tapp-cli -s http://<tapp>:50051 -k 0x<owner> docker-login -r ghcr.io -u <user> -p <pat>

# 3. Register on chain and start. Idempotent, and re-registers a stale signer.
tapp-cli -s http://<tapp>:50051 -k 0x<owner> start-app \
  -f docker-compose.yml --app-id mellow-interop-bot \
  --register-onchain --rpc-url <0G RPC> --contract 0x<TappRegistry> \
  --stake-wei 1000000000000000000

# 4. Read the address it derived. It also announces itself on Telegram.
tapp-cli -s http://<tapp>:50051 -k 0x<owner> get-app-logs --app-id mellow-interop-bot -n 50
```

The bot then holds before its first cycle, listing what it still needs, until
all of it is done — see below. Finish by revoking the old EOA's roles and
sweeping its balances.

### What the new address needs

One address does both jobs (`OPERATOR_PK` and `ORACLE_UPDATER_PK` are set from
the same derived key). It needs six grants, all made by the Safe, plus gas on
both chains:

| chain | contract | grant |
|---|---|---|
| 0G | SourceCore | `SET_VALUE_ROLE` (`keccak256("ORACLE:SET_VALUE_ROLE")`) |
| 0G | SourceCore | `PUSH_ROLE` |
| Ethereum | TargetCore | `PUSH_ROLE`, `REDEEM_ROLE`, `DEPOSIT_ROLE` |
| Ethereum | TargetCore | `CLAIM_ROLE`, or being set as `claimer()` |

`Rewarder.claim`, `AscendRouter.distribute` and `WithdrawalQueue.handleEpoch`
carry no access control. The Safe proposer stays outside the TEE, so the derived
address needs to be neither a Safe owner nor a delegate — `oracle-propose`
remains a command a person runs locally.

### The startup gate

Under a tapp the scheduler will not begin its first cycle until every one of
those grants is in place and both balances clear their floor. It re-checks each
minute and reports the outstanding list on Telegram, at first and then every
half hour.

This exists because a tapp's first boot is the one case where the bot is
guaranteed to be unable to act: it derives an address nobody has seen, so nobody
can have funded or authorised it beforehand. Without the gate that state is
indistinguishable from an ordinary failure — a silent retry loop that the oracle
task would report only after three failures at eight-hour intervals.

The floors default to 0.1 native on the source chain and 0.02 ETH on the target;
override with `OPERATOR_MIN_BALANCE_WEI` and `TARGET_OPERATOR_MIN_BALANCE_WEI`.
The target floor is the larger one because `pushToSource` and `pushToTarget`
carry a LayerZero fee as `msg.value` on top of gas.

### Working on the tapp path locally

The gRPC stubs are generated, not committed — they are pinned to the grpcio that
produced them:

```bash
pip install grpcio-tools==1.83.0
./scripts/gen_proto.sh
```

Without them the tapp tests skip themselves; CI generates them so they do not.

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
