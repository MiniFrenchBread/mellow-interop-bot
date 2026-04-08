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
- **`abi/`** -- JSON ABI files for smart contracts: `Oracle`, `SourceCore`, `TargetCore`, `SourceHelper`, `TargetHelper`, `Safe`, `SafeMultiSend`.

### Shell Scripts

- **`run_bot.sh`** -- Production scheduler loop running 4 tasks on different intervals:
  - Task 1: Run `ascend.sh` from `0g-restaking-contracts` every 2 weeks
  - Task 2: Run `operator_bot.py` every 2 hours (rebalancing)
  - Task 3: Run `main.py` every 1 day (oracle monitoring + Telegram alerts)
  - Task 4: Process mature withdrawal epochs on WithdrawalQueue contract using `cast send`
- **`run_bot_testnet.sh`** -- Testnet scheduler: runs ascend every 4h, operator_bot every 5m, and triggers Forge scripts from `mellow-interop` repo to update oracles.
- **`handle_epoch.sh`** -- Standalone script to process mature withdrawal epochs on the WithdrawalQueue contract.

## Build and Test Commands

```bash
# Setup
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

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

| Variable | Purpose | Default |
|---|---|---|
| `TELEGRAM_BOT_API_KEY` | Telegram bot token | (required unless DRY_RUN) |
| `TELEGRAM_GROUP_CHAT_ID` | Target chat ID | (required unless DRY_RUN) |
| `TELEGRAM_OWNER_NICKNAMES` | Safe signer nicknames, optionally with addresses | (optional) |
| `ORACLE_EXPIRY_THRESHOLD_SECONDS` | When to alert about near-expiry | 3600 |
| `ORACLE_RECENT_UPDATE_THRESHOLD_SECONDS` | Window for "recently updated" notifications | 0 |
| `TARGET_RPC` | Target chain (Ethereum) RPC | QuikNode default |
| `ZG_RPC` | 0G chain RPC | default in config |
| `SAFE_PROPOSER_PK` | Global Safe proposer private key | (optional) |
| `SAFE_API_KEY` | Global Safe API key | (optional) |
| `DRY_RUN` | Skip Telegram messages | false |
| `OPERATOR_PK` | Operator private key for rebalancing | (required for operator_bot) |
| `DEPLOYMENTS` | Comma-separated SOURCE:SYMBOL pairs | (required for operator_bot) |
| `SOURCE_RATIO_D3` | Target source asset ratio (per mille) | 50 |
| `MAX_SOURCE_RATIO_D3` | Max source ratio before surplus rebalance | 100 |

Chain-specific overrides (e.g., `BSC_RPC`, `BSC_SAFE_API_KEY`, `FRAX_SAFE_PROPOSER_PK`) take precedence over global values.

## CI/CD (GitHub Actions)

- **check-code.yml** -- On push to `*.py`: run Black formatter check + unit tests
- **validate-config.yml** -- On push to `config.json` or `abi/`: validate config against live chains
- **scheduled-bot-execution.yml** -- Cron every 4 hours (or manual): run `main.py` with production secrets

## Relationship to Other 0G Ecosystem Repos

- **mellow-interop** -- The smart contracts (Solidity) that this bot monitors and manages. Contains SourceCore, TargetCore, Oracle contracts deployed across chains. The testnet script (`run_bot_testnet.sh`) calls Forge scripts from this repo to update oracles.
- **0g-restaking-contracts** -- Contains `ascend.sh` which is called periodically by `run_bot.sh` for restaking protocol epoch advancement. The bot also interacts with the WithdrawalQueue contract from this repo.
- **0g-chain-v2 / 0g-geth / 0g-reth** -- The 0G blockchain nodes (consensus + execution layers) that serve as one of the source chains monitored by this bot.
- **0g-restaking-service** -- Complementary service; while that handles restaking event bridging, this bot handles oracle freshness and vault rebalancing.

## Important Implementation Details

- Oracle "secure value" is computed as `(sourceValue + targetValue) * 1e18 / totalSupply`, queried at a block 15 seconds before latest (SECURE_INTERVAL) to avoid using very recent state.
- OFT transfer detection: compares source chain inbound/outbound nonces with target chain outbound/inbound nonces; mismatch means a LayerZero cross-chain transfer is in flight.
- Safe transaction proposals: the bot first checks for an existing queued transaction with matching calldata before proposing a new one, to avoid duplicates.
- Multi-send is used automatically when multiple oracle updates are needed for the same Safe address.
- Error messages are sanitized to mask RPC URLs, private keys, and API keys before logging or sending to Telegram.
