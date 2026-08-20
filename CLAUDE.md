# CLAUDE.md - mellow-interop-bot

## Project Overview

A Python bot that monitors cross-chain oracle states for the Mellow interop protocol across multiple blockchain networks (0G, BSC, Fraxtal, Lisk). When oracle values are stale, expired, or incorrect, the bot proposes multisig transactions via Safe Global to update them, and sends Telegram alerts to notify signers. It also includes operator scripts for cross-chain asset rebalancing via LayerZero OFT transfers.

This bot is the operational automation layer for the Mellow interop protocol -- it keeps cross-chain oracle prices fresh and vault asset ratios balanced.

## Architecture

### Core Workflow (main.py)

1. Load config from `config.json` (with env var substitution)
2. For each source chain and deployment, call on-chain helper contracts to validate oracle state
3. Compare oracle value vs computed "secure value" (source + target TVL / total supply)
4. If oracle is almost expired, already expired, has incorrect value, or has in-flight OFT transfers, compose a status message
5. Send status message to Telegram
6. For each source chain needing oracle updates, propose a Safe multisig transaction (single or batched multi-send) calling `Oracle.setValue(newValue)`
7. Send Telegram message with Safe transaction link, confirmation status, and @-mentions of signers who still need to confirm

### Key Components

- **`src/main.py`** -- Entry point. Orchestrates oracle validation, Telegram messaging, and Safe transaction proposals.
- **`src/config/`** -- Configuration loading and validation.
  - `read_config.py` -- Parses `config.json` with kebab-to-snake conversion and recursive `${VAR:default}` env substitution (supports nesting and circular reference detection). Defines `Config`, `SourceConfig`, `Deployment`, `SafeGlobal` dataclasses.
  - `validate_config.py` -- Validates config against live on-chain state: RPC endpoints, helper contracts, source/target core cross-references, Safe contract version/nonce, multi-send contract deployment, symbol consistency. Runnable standalone.
  - `mask_sensitive_data.py` -- Masks private keys, API keys, and RPC URL credentials in error messages.
- **`src/web3_scripts/`** -- On-chain interaction logic.
  - `base.py` -- Shared Web3 utilities: `get_w3()`, `get_contract()` (loads ABI from `./abi/`), `execute()` (build+sign+send transactions with EIP-1559 gas), `get_block_before_timestamp()` (binary search for block by timestamp).
  - `oracle_script.py` -- Core oracle validation with retry/backoff. Reads source/target nonces (detects in-flight OFT transfers), oracle value/timestamp/maxAge, computes `secure_value = (sourceValue + targetValue) * 1e18 / totalSupply`, checks expiry and value correctness.
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
  on independent intervals in one process: ascend (2 weeks), rebalance (2 hours), oracle report
  (1 day), handle-epoch (5 minutes). Tasks fire on multiples of their interval measured from the
  Unix epoch, so a restart neither shifts the schedule nor skips a beat. Each task is isolated so
  one failure cannot stop the others, and repeated failures or repeated guard-skips raise a
  Telegram alert.
- **`src/cli.py`** -- One-shot entry points for the same code paths: `ascend` (with `--dry-run`),
  `handle-epoch`, `oracle`, `rebalance`, `validate-config`.
- **`src/process_lock.py`** -- Single-holder lock shared by the scheduler and the CLI. One account
  signs every transaction, so two processes running at once would collide over its nonce.

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

# Run main bot (oracle monitoring + alerts)
python ./src/main.py
DRY_RUN=true python ./src/main.py    # without sending Telegram messages

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
| `ORACLE_EXPIRY_THRESHOLD_SECONDS` | When to alert about near-expiry | 3600 |
| `ORACLE_RECENT_UPDATE_THRESHOLD_SECONDS` | Window for "recently updated" notifications | 0 |
| `TARGET_RPC` | Target chain (Ethereum) RPC | (required, no default in config) |
| `ZG_RPC` | 0G chain RPC | (required, no default in config) |
| `SAFE_PROPOSER_PK` | Global Safe proposer private key | (optional) |
| `SAFE_API_KEY` | Global Safe API key | (optional) |
| `DRY_RUN` | Skip Telegram messages | false |
| `OPERATOR_PK` | Signs every transaction: rebalancing, ascend claims, epoch advances | (required) |
| `OG_EXECUTOR_PK` | Overrides `OPERATOR_PK` for the 0G chain only | `OPERATOR_PK` |
| `OG_RECEIPT_TIMEOUT` / `TARGET_RECEIPT_TIMEOUT` | Seconds to wait for a receipt before replacing the transaction at a higher fee | 60 / 600 |
| `ASCEND_INTERVAL_SECONDS` / `REBALANCE_INTERVAL_SECONDS` / `ORACLE_REPORT_INTERVAL_SECONDS` / `HANDLE_EPOCH_INTERVAL_SECONDS` | Task intervals | 1209600 / 7200 / 86400 / 300 |
| `ALERT_AFTER_FAILURES` | Consecutive failures or skips before a Telegram alert | 3 |
| `DEPLOYMENTS` | Comma-separated SOURCE:SYMBOL pairs | (required for operator_bot) |
| `SOURCE_RATIO_D3` | Target source asset ratio (per mille) | 50 |
| `MAX_SOURCE_RATIO_D3` | Max source ratio before surplus rebalance | 100 |

Chain-specific overrides (e.g., `BSC_RPC`, `BSC_SAFE_API_KEY`, `FRAX_SAFE_PROPOSER_PK`) take precedence over global values.

## CI/CD (GitHub Actions)

- **check-code.yml** -- On push to `*.py`: run Black formatter check + unit tests

This deployment fork deliberately keeps no Actions secrets, so the two upstream workflows that
need them were removed. `validate-config.yml` checked the config against live chains and could
only ever fail here; run `python ./src/cli.py validate-config` locally instead, where the RPC
credentials already live in `.env`. `scheduled-bot-execution.yml` ran `main.py` on a cron, which
would have proposed Safe transactions in parallel with the scheduler on the box -- two oracle
proposers for one Safe.

## Relationship to Other 0G Ecosystem Repos

- **mellow-interop** -- The smart contracts (Solidity) that this bot monitors and manages. Contains SourceCore, TargetCore, Oracle, and WithdrawalQueue contracts deployed across chains.
- **0g-restaking-contracts** -- Source of the Rewarder and AscendRouter contracts. The ascend task used to run a Forge script from this repo; it is now implemented in `src/web3_scripts/ascend.py` and this repo is no longer a runtime dependency.
- **0g-chain-v2 / 0g-geth / 0g-reth** -- The 0G blockchain nodes (consensus + execution layers) that serve as one of the source chains monitored by this bot.
- **0g-restaking-service** -- Complementary service; while that handles restaking event bridging, this bot handles oracle freshness and vault rebalancing.

## Important Implementation Details

- Oracle "secure value" is computed as `(sourceValue + targetValue) * 1e18 / totalSupply`, queried at a block 15 seconds before latest (SECURE_INTERVAL) to avoid using very recent state.
- OFT transfer detection: compares source chain inbound/outbound nonces with target chain outbound/inbound nonces; mismatch means a LayerZero cross-chain transfer is in flight.
- Safe transaction proposals: the bot first checks for an existing queued transaction with matching calldata before proposing a new one, to avoid duplicates.
- Safe nonces do not advance for queued-but-unexecuted proposals, so a proposal carrying a newer
  oracle value lands on the same nonce as the pending one and voids it when executed. That is the
  intended outcome, and the Telegram message names the displaced proposal so signers pick the current one.
- Each source's `executor-private-key` signs both legs of that source's rebalance, including the
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
- Multi-send is used automatically when multiple oracle updates are needed for the same Safe address.
- Error messages are sanitized to mask RPC URLs, private keys, and API keys before logging or sending to Telegram.
