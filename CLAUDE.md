# CLAUDE.md - mellow-interop-bot

## Project Overview

A Python bot that keeps the Mellow interop protocol's cross-chain oracle fresh and its vault asset ratios balanced. It writes the oracle directly with a dedicated key on a short heartbeat, claims and distributes restaking rewards on the same cadence, rebalances across chains via LayerZero OFT transfers, and advances the withdrawal queue.

This bot is the operational automation layer for the Mellow interop protocol.

## Architecture

### Core Workflow (main.py)

1. Load config from `config.json` (with env var substitution)
2. For each source chain and deployment, call on-chain helper contracts to read oracle state
3. Compute the "secure value" (source + target TVL / total supply)
4. Skip if an OFT transfer is in flight -- the two sides are counted at different points, so their sum is wrong until it settles
5. Run the guards; refuse and alert if the value would move too far or fall
6. Otherwise call `Oracle.setValue(newValue)` with `ORACLE_UPDATER_PK` and wait for the receipt
7. Send Telegram **only** when a person is needed -- a refusal, a failure, or a low gas balance. A successful heartbeat is silent

Safe multisig proposals still exist but are no longer on any schedule: `cli.py oracle-propose` is the manual recovery path when a guard refuses. See "Oracle updates" in `README.md`.

### Key Components

- **`src/main.py`** -- Entry point. Orchestrates oracle validation, Telegram messaging, and Safe transaction proposals.
- **`src/config/`** -- Configuration loading and validation.
  - `read_config.py` -- Parses `config.json` with kebab-to-snake conversion and recursive `${VAR:default}` env substitution (supports nesting and circular reference detection). Defines `Config`, `SourceConfig`, `Deployment`, `SafeGlobal` dataclasses.
  - `validate_config.py` -- Validates config against live on-chain state: RPC endpoints, helper contracts, source/target core cross-references, Safe contract version/nonce, multi-send contract deployment, symbol consistency. Runnable standalone.
  - `mask_sensitive_data.py` -- Masks private keys, API keys, and RPC URL credentials in error messages.
- **`src/web3_scripts/`** -- On-chain interaction logic.
  - `base.py` -- Shared Web3 utilities: `get_w3()`, `get_contract()` (loads ABI from `./abi/`), `execute()` (build+sign+send transactions with EIP-1559 gas), `get_block_before_timestamp()` (binary search for block by timestamp).
  - `oracle_script.py` -- Core oracle validation with retry/backoff. Reads source/target nonces (detects in-flight OFT transfers), oracle value/timestamp/maxAge, computes `secure_value = (sourceValue + targetValue) * 1e18 / totalSupply`, checks expiry and value correctness. Read-only.
  - `oracle_update.py` -- The heartbeat. Validates via `oracle_script`, applies the deviation and decrease guards, then writes `Oracle.setValue`. Guard arithmetic (`exceeds_deviation`, `is_decrease`) is separated out so it is testable without any chain access.
  - `operator_script.py` -- Read-only analysis of vault asset ratios. Determines if rebalancing actions are needed (redeem, claim, pushToSource, pushToTarget, deposit).
  - `operator_bot.py` -- Automated version of operator_script that actually executes rebalancing transactions using an operator private key, with LayerZero finalization waiting.
- **`src/safe_global/`** -- Safe multisig transaction management.
  - `propose_tx.py` -- Creates and proposes Safe transactions. Supports both single calls and multi-send batches. Checks for existing queued transactions before proposing new ones.
  - `client_gateway_api.py` -- Safe Client Gateway API integration (used for self-hosted Safe instances).
  - `transaction_api.py` -- Safe Transaction Service API integration (used with API key auth).
  - `multi_send_call.py` -- Encodes multiple calls into a single `multiSend(bytes)` call for Safe contracts (supports versions 1.3.0, 1.4.1, 1.5.0).
  - `common.py` -- Shared data structures (`PendingTransactionInfo`, `ThresholdWithOwners`), validation helpers, retry utility.
- **`src/telegram_bot/`** -- Telegram message sending with Markdown formatting and dry-run support.
- **`abi/`** -- JSON ABI files for smart contracts: `Oracle`, `SourceCore`, `TargetCore`, `SourceHelper`, `TargetHelper`, `Safe`, `SafeMultiSend`, `Rewarder`, `AscendRouter`, `WithdrawalQueue`.

### Scheduler and CLI

- **`src/scheduler.py`** -- Production task loop, replacing the former `run_bot.sh`. Runs four tasks
  on independent intervals in one process: ascend (8 hours), oracle-update (8 hours), rebalance
  (2 hours), handle-epoch (5 minutes). Tasks fire on multiples of their interval measured from the
  Unix epoch, so a restart neither shifts the schedule nor skips a beat. Each task is isolated so
  one failure cannot stop the others, and repeated failures or repeated guard-skips raise a
  Telegram alert.
  `TASK_ORDER` is `ascend -> oracle_update -> rebalance -> handle_epoch`, and the order is
  load-bearing: ascend moves the vault's value, rebalance refuses while the oracle disagrees with
  the computed value, so refreshing the oracle in between is what stops every post-distribution
  rebalance from being skipped.
  Task intervals come from `scheduler.tasks` in `config.json`, falling back to built-in
  defaults. Omitting a task does **not** stop it — it runs on its default — and no interval
  value means "off" (a non-positive one is rejected, because it would otherwise make the task
  due every cycle). `ascend` and `handle-epoch` are stopped by removing the source section each
  needs (`ascend`, `withdrawal-queue`); `rebalance` and `oracle-update` have no off switch.
- **`src/cli.py`** -- One-shot entry points for the same code paths: `ascend` (with `--dry-run`),
  `handle-epoch`, `oracle` (with `--dry-run` / `--force`), `oracle-propose` (with `--value` /
  `--dry-run`), `rebalance`, `validate-config`.
- **`src/process_lock.py`** -- Single-holder lock shared by the scheduler, the CLI and the
  standalone `main.py`. One account signs every broadcast transaction, so two processes running at
  once would collide over its nonce.
  `validate-config` and `oracle-propose` are exempt (`LOCK_FREE`): neither broadcasts anything.
  That exemption matters for `oracle-propose` in particular — it is the recovery path used while
  the oracle is going unwritten, and requiring the lock would mean stopping the rest of the bot
  to use it.

The bot no longer shells out to `forge` or `cast`, and no longer needs a checkout of
`0g-restaking-contracts`.

## Build and Test Commands

```bash
# Setup
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Run the scheduler (production)
python -u ./src/scheduler.py

# One-shot commands
python ./src/cli.py ascend --dry-run   # simulate claims, broadcast nothing
python ./src/cli.py ascend
python ./src/cli.py handle-epoch
python ./src/cli.py oracle
python ./src/cli.py rebalance -y

# Run one oracle pass standalone. This WRITES the oracle; DRY_RUN only
# suppresses Telegram. Takes the shared lock, so it refuses while the
# scheduler is up, and exits non-zero if the run failed.
python ./src/main.py
python ./src/cli.py oracle --dry-run  # what it would do, broadcasting nothing

# Run standalone scripts
python ./src/web3_scripts/oracle_script.py
python ./src/web3_scripts/operator_script.py
OPERATOR_PK=<pk> DEPLOYMENTS=BSC:CYC python ./src/web3_scripts/operator_bot.py

# Validate config against on-chain state
python ./src/config/validate_config.py

# Run tests
python -m unittest discover -s tests -p "test_*.py" -v

# Check formatting
black --check .
black .             # auto-format

# Generate the tapp gRPC stubs (gitignored -- pinned to the grpcio that made
# them). Without these the tapp signer's end-to-end tests skip themselves.
pip install grpcio-tools==1.83.0
./scripts/gen_proto.sh

# Docker
docker build -t mellow-interop-bot .
docker run --env-file .env mellow-interop-bot
```

## Configuration

### config.json

Central configuration file with `${VAR:default}` env var substitution. Structure:
- Top-level: Telegram settings, oracle thresholds, target chain RPC + helper address
- `sources[]`: Array of source chains, each with name, RPC, helper address, deployments[], and optional safe-global config
- `sources[].deployments[]`: Each has name, source-core address, target-core address, and optional safe-global-overrides
- `sources[].safe-global`: Safe multisig config (address, proposer key, API URL, web client URL, EIP-3770 prefix)

### Key Environment Variables

Credentialed RPC URLs belong in `.env`, never as defaults in `config.json`: that file is tracked,
so a default there would enter public history.

| Variable | Purpose | Default |
|---|---|---|
| `TELEGRAM_BOT_API_KEY` | Telegram bot token | (required unless DRY_RUN) |
| `TELEGRAM_GROUP_CHAT_ID` | Target chat ID | (required unless DRY_RUN) |
| `TELEGRAM_OWNER_NICKNAMES` | Safe signer nicknames, optionally with addresses | (optional) |
| `ORACLE_UPDATER_PK` | Signs `Oracle.setValue`. Must hold `SET_VALUE_ROLE` on the SourceCore — granted from the Safe, checked by `validate-config`. Separate from `OPERATOR_PK` on purpose: it is the only key that can move the share price | (required for oracle-update) |
| `ORACLE_MAX_DEVIATION_BPS` | Refuse to write a value further than this from the one on chain | 100 (1%) |
| `ORACLE_DECREASE_TOLERANCE_WEI` | Refuse to write a value that fell by more than this; below it a dip is rounding | 1e9 |
| `ORACLE_UPDATER_MIN_BALANCE_WEI` | Warn (but still write) below this gas balance | 1e17 |
| `ORACLE_EXPIRY_THRESHOLD_SECONDS` | How close to expiry counts as urgent. No longer triggers the write — the heartbeat is unconditional — but it decides how loudly a refusal is worded. Must exceed the oracle-update interval or the window can be stepped over | 172800 |
| `ORACLE_RECENT_UPDATE_THRESHOLD_SECONDS` | Window for "recently updated" notifications | 0 |
| `TARGET_RPC` | Target chain (Ethereum) RPC | (required, no default in config) |
| `ZG_RPC` | 0G chain RPC | (required, no default in config) |
| `SAFE_PROPOSER_PK` | Global Safe proposer private key | (optional) |
| `SAFE_API_KEY` | Global Safe API key | (optional) |
| `DRY_RUN` | Skip Telegram messages | false |
| `OPERATOR_PK` | Signs every transaction: rebalancing, ascend claims, epoch advances | (required) |
| `OG_EXECUTOR_PK` | Overrides `OPERATOR_PK` for the OG source, signing **both** legs of its rebalance — the target-chain calls included | `OPERATOR_PK` |
| `OG_RECEIPT_TIMEOUT` / `TARGET_RECEIPT_TIMEOUT` | Seconds to wait before raising the fee and re-signing. Not a budget: a send runs until the chain settles it | 60 / 600 |
| `ASCEND_INTERVAL_SECONDS` / `REBALANCE_INTERVAL_SECONDS` / `ORACLE_UPDATE_INTERVAL_SECONDS` / `HANDLE_EPOCH_INTERVAL_SECONDS` | Task intervals. Keep ascend **≤** oracle-update, or most writes record a value nothing has changed | 28800 / 7200 / 28800 / 300 |
| `ALERT_AFTER_FAILURES` | Consecutive failures before a Telegram alert, and how often it repeats after that | 3 |
| `SCHEDULER_STATE_FILE` | Where the scheduler records each task's last run, so a restart cannot skip a due slot | `.scheduler-state.json` |
| `DEPLOYMENTS` | Comma-separated SOURCE:SYMBOL pairs | (required for operator_bot) |
| `FORCE_WITHDRAWAL` | Standalone `operator_bot.py` only: pull the **entire** target-chain position back. The scheduler passes `False` explicitly so a stray export cannot trigger it unattended | 0 |
| `SOURCE_RATIO_D3` | Target source asset ratio (per mille) | 50 |
| `MAX_SOURCE_RATIO_D3` | Max source ratio before surplus rebalance | 100 |
| `SCHEDULER_LOCK_FILE` | Where the process lock lives. Must not be on a persistent volume — it exists to stop two schedulers sharing one nonce sequence, and the kernel releases it on death | `.scheduler.lock` |
| `TAPP_APP_ID` | Presence switches the bot to the TEE signer (see below). Must match the `--app-id` given to `start-app`: the key derives from it | (unset — keys come from `.env`) |
| `TAPP_SOCKET` | The tapp Unix socket to fetch the key over | `/run/tapp/tapp.sock` |
| `TAPP_KEY_MATERIAL` | Hex derivation material forwarded to the KMS. **Changing it changes the bot's address**, stranding its gas and voiding its roles | hex(`mellow-operator`) |
| `OPERATOR_MIN_BALANCE_WEI` / `TARGET_OPERATOR_MIN_BALANCE_WEI` | Gas floors the startup gate holds against | 1e17 / 2e16 |

Chain-specific overrides (e.g., `BSC_RPC`, `BSC_SAFE_API_KEY`, `FRAX_SAFE_PROPOSER_PK`) take precedence over global values.

## CI/CD (GitHub Actions)

- **check-code.yml** -- On push to `*.py`: Black formatter check, generate the tapp gRPC stubs, run unit tests. The stub step sits *after* the Black check on purpose: protoc output is not Black-clean, and it is gitignored because it is pinned to the grpcio that produced it. Without the step the tapp signer's end-to-end tests skip themselves, and a skipped test guarding the signing key reads as a passing one.

This deployment fork deliberately keeps no Actions secrets, so the two upstream workflows that
need them were removed. `validate-config.yml` checked the config against live chains and could
only ever fail here; run `python ./src/cli.py validate-config` locally instead, where the RPC
credentials already live in `.env`. `scheduled-bot-execution.yml` ran `main.py` on a cron, which
would have proposed Safe transactions in parallel with the scheduler on the box -- two oracle
proposers for one Safe.

## Running under 0G Tapp

`src/tapp/signer.py` replaces the file-based key when `TAPP_APP_ID` is set: it
fetches a KMS-derived secret over the tapp Unix socket, derives a secp256k1 key
from it, and exports `OPERATOR_PK` and `ORACLE_UPDATER_PK` before `read_config`
runs. Deliberately *not* `SAFE_PROPOSER_PK` — proposing Safe transactions stays
a human-run CLI command outside the TEE, so the derived address needs to be
neither a Safe owner nor a delegate.

Called from every entrypoint right after `dotenv.load_dotenv()`. A no-op when
`TAPP_APP_ID` is unset, so local development and the current server deployment
are unaffected.

**Do not switch to `GetAppSecretKey`.** That is a tapp's default signer and it is
generated with `OsRng` in the tapp-server process, so it changes on every restart
of that process — a package upgrade, a crash, a reboot. Any address funded and
granted roles under it is stranded on the next restart. `GetSecretResource` is
derived by the KMS from `(app_id, material)` and is stable across all of those,
which is the whole reason the deployment can be funded and authorised once.

The price is that the KMS authenticates by the tapp node's on-chain registered
signer, so the app must be registered in TappRegistry and anything that restarts
tapp-server needs `update-node-onchain` (a CVM reboot needs `claim-config` first)
before the fetch works again. The bot's own address is unchanged throughout; the
fetch retries indefinitely rather than crash-looping.

`check_operator_requirements()` in `src/config/validate_config.py` reports the
six roles and two gas balances the signer needs, as a list rather than raising on
the first — whoever is running the multisig ceremony needs all of them at once.
`Scheduler.wait_until_ready()` holds before the first cycle until that list is
empty. Both are new; `validate_oracle_updater` still checks one role and raises,
because `validate-config` is a pass/fail deploy gate and the other is a loop.

Role bytes32 values are read off the contracts (`PUSH_ROLE()` and friends are
views) rather than derived from guessed preimages — a wrong guess yields a role
nobody holds, which reads as "not granted". `SET_VALUE_ROLE` is the exception and
stays hard-coded, for the reason already documented at its definition.

`docker-compose.yml` and everything it mounts is hashed into the runtime
measurement, and `GetAppInfo` publishes the compose text to unauthenticated
callers. Non-secret tuning goes in `environment:`; credentials go in `bot.env`,
whose hash is measured but whose contents are not published. Nothing else.

## Relationship to Other 0G Ecosystem Repos

- **mellow-interop** -- The smart contracts (Solidity) that this bot monitors and manages. Contains SourceCore, TargetCore, Oracle, and WithdrawalQueue contracts deployed across chains.
- **0g-restaking-contracts** -- Source of the Rewarder and AscendRouter contracts. The ascend task used to run a Forge script from this repo; it is now implemented in `src/web3_scripts/ascend.py` and this repo is no longer a runtime dependency.
- **0g-chain-v2 / 0g-geth / 0g-reth** -- The 0G blockchain nodes (consensus + execution layers) that serve as one of the source chains monitored by this bot.
- **0g-restaking-service** -- Complementary service; while that handles restaking event bridging, this bot handles oracle freshness and vault rebalancing.

## Important Implementation Details

- Oracle "secure value" is computed as `(sourceValue + targetValue) * 1e18 / totalSupply`, queried at a block 15 seconds before latest (SECURE_INTERVAL) to avoid using very recent state.
- OFT transfer detection: compares source chain inbound/outbound nonces with target chain outbound/inbound nonces; mismatch means a LayerZero cross-chain transfer is in flight.
- The oracle write is **unconditional** — rewriting an unchanged value is the point, because it
  refreshes `lastUpdated` and `getValue()` reverts once `maxAge` passes. A heartbeat that only
  fired when the number changed would let the oracle drift to expiry during any quiet period.
- The vault's assets only move when `AscendRouter.distribute()` transfers rewards into the
  SourceCore, so the share price is a step function whose stride is the **ascend** interval, not
  the oracle interval. Writing the oracle more often than ascend runs just rewrites the same
  number; a test binds `ascend <= oracle_update`.
- `send_and_confirm`'s label for the oracle write is the constant `SET_VALUE_LABEL`. It names the
  operation in logs and in the fee-bump messages; it carries no nonce semantics, because a send now
  runs until the chain settles it and no two operations can be in flight on one nonce.
- The guards refuse rather than write, and a refusal **does not self-heal** — ascend keeps adding
  rewards, so the gap widens daily, and rebalance starts refusing too. `maxAge` is the deadline.
- Safe transaction proposals: the bot first checks for an existing queued transaction with matching calldata before proposing a new one, to avoid duplicates.
- Safe nonces do not advance for queued-but-unexecuted proposals, so a proposal carrying a newer
  oracle value lands on the same nonce as the pending one and voids it when executed. That is the
  intended outcome, and the Telegram message names the displaced proposal so signers pick the current one.
- A target-chain executor key does not exist: each source's `executor-private-key` signs both legs of
  that source's rebalance, including the
  target-chain calls. Today every task shares one account (`OPERATOR_PK`), so this is the same key
  either way; the per-source indirection exists so the roles can be split later without a code
  change, and there is deliberately no separate target-chain executor key until one is needed.
- Transaction settings are per chain and reach every send through `TxConfig.as_kwargs()`. The single
  accessor is the point: a setting added to the dataclass but forgotten at one call site is exactly
  how the long target-chain receipt budget ended up configured and ignored.
- Transactions are confirmed by polling for a receipt, never by whether the broadcast call returned.
  A receipt lookup can legitimately fail for a mined transaction while the node is still indexing the
  block; a broadcast can raise after the node accepted the payload. A timeout replaces the transaction
  at a higher fee under the same nonce, and every hash sent for that nonce stays in the poll set.
- **A send does not return until the chain has decided** -- a receipt (mined, or mined and reverted)
  or the nonce consumed by something else. There is no attempt limit and no time budget; the fee cap
  is the only ceiling, and once it is reached the send stops signing and waits, because the cap is
  ours rather than the network's so nothing higher can ever be offered. An unreachable node is a
  reason to wait, not to stop.
  Everything follows from that: a caller always finds the next nonce free, so two operations in one
  process cannot contend for one, and there is no bookkeeping about abandoned nonces. An earlier
  version gave up after a fixed number of attempts and needed a guard band (`NonceBlocked` and a
  per-process record of transactions left in flight) to stop the next operation signing onto the
  nonce; not abandoning it removed the problem rather than policing it, along with about 150 lines.
- `should_stop` is the only way out with a transaction still in flight, and it exists so SIGTERM
  still works. It travels in the same dict as the transaction settings, because that dict is what
  reaches every send site -- a call site that missed it would make the process unstoppable while a
  transaction is stuck.
- Multi-send is used automatically when multiple oracle updates are needed for the same Safe address.
- Error messages are sanitized to mask RPC URLs, private keys, and API keys before logging or sending to Telegram.
