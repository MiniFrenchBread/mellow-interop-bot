"""Command line entry points for every operation the bot performs.

Each subcommand is the same code path the scheduler runs, so a manual run and a
scheduled run cannot drift apart. All of them take the shared process lock first,
because every task signs with the same account.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

import dotenv

sys.path.insert(0, str(Path(__file__).parent))

from config import read_config
from config.read_config import Config, SourceConfig
from process_lock import LockHeld, ProcessLock
from web3_scripts import get_w3, print_colored
from web3_scripts.ascend import run_ascend
from web3_scripts.operator_bot import run_all as run_rebalance
from web3_scripts.withdrawal_queue import handle_epochs

CONFIG_PATH = Path(__file__).parent.parent / "config.json"


def load() -> Config:
    dotenv.load_dotenv()
    return read_config(str(CONFIG_PATH))


def pick_source(config: Config, name: str = None) -> SourceConfig:
    if name:
        for source in config.sources:
            if source.name.lower() == name.lower():
                return source
        raise Exception(
            "Unknown source '{}'. Available: {}".format(
                name, ", ".join(s.name for s in config.sources)
            )
        )
    if len(config.sources) != 1:
        raise Exception(
            "Config has {} sources; pass --source to choose one".format(
                len(config.sources)
            )
        )
    return config.sources[0]


def _require(value, what: str):
    if not value:
        raise Exception("Source has no {} configured".format(what))
    return value


def cmd_ascend(config: Config, args) -> None:
    source = pick_source(config, args.source)
    ascend = _require(source.ascend, "ascend section")
    result = run_ascend(
        get_w3(source.rpc),
        router=ascend.router,
        rewarders=list(ascend.rewarders),
        private_key=_require(source.executor_private_key, "executor private key"),
        claim_account=ascend.resolved_claim_account(),
        dry_run=args.dry_run,
        receipt_timeout=source.tx.receipt_timeout_seconds,
        max_attempts=source.tx.max_attempts,
        fee_cap_gwei=source.tx.fee_cap_gwei,
    )
    print_colored(
        "Claimed {} in total across {} rewarder(s); distributed {}".format(
            result.total_claimed, len(result.claims), result.distributed
        ),
        "green",
    )


def cmd_handle_epoch(config: Config, args) -> None:
    source = pick_source(config, args.source)
    queue = _require(source.withdrawal_queue, "withdrawal-queue section")
    processed = handle_epochs(
        get_w3(source.rpc),
        address=queue.address,
        private_key=_require(source.executor_private_key, "executor private key"),
        max_iterations=queue.max_iterations,
        receipt_timeout=source.tx.receipt_timeout_seconds,
        max_attempts=source.tx.max_attempts,
        fee_cap_gwei=source.tx.fee_cap_gwei,
    )
    print("Epochs processed: {}".format(processed))


def cmd_oracle(config: Config, args) -> None:
    from main import main as oracle_main

    asyncio.run(oracle_main())


def cmd_rebalance(config: Config, args) -> None:
    run_rebalance(config, interactive=not args.yes)


def cmd_validate_config(config: Config, args) -> None:
    from config.validate_config import validate_config

    validate_config(config)


COMMANDS = {
    "ascend": cmd_ascend,
    "handle-epoch": cmd_handle_epoch,
    "oracle": cmd_oracle,
    "rebalance": cmd_rebalance,
    "validate-config": cmd_validate_config,
}

# Read-only commands do not need the lock: they sign nothing, so they cannot
# collide with the scheduler over a nonce.
LOCK_FREE = {"validate-config"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cli", description=__doc__)
    parser.add_argument("--source", help="Source chain name (default: the only one)")
    parser.add_argument(
        "--no-lock",
        action="store_true",
        help="Skip the single-holder lock. Only safe when the scheduler is stopped.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    ascend = subparsers.add_parser("ascend", help="Claim rewards and distribute them")
    ascend.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate the calls without broadcasting",
    )

    subparsers.add_parser("handle-epoch", help="Advance matured withdrawal epochs")
    subparsers.add_parser("oracle", help="Report oracle status and propose updates")

    rebalance = subparsers.add_parser("rebalance", help="Rebalance across chains")
    rebalance.add_argument(
        "-y", "--yes", action="store_true", help="Skip the confirmation prompt"
    )

    subparsers.add_parser("validate-config", help="Validate config against chains")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config = load()
    handler = COMMANDS[args.command]

    # A dry run signs nothing, so it is safe alongside a running scheduler.
    needs_lock = args.command not in LOCK_FREE and not getattr(args, "dry_run", False)
    if args.no_lock or not needs_lock:
        handler(config, args)
        return 0

    lock = ProcessLock(config.scheduler.lock_file)
    try:
        with lock:
            handler(config, args)
    except LockHeld as e:
        print_colored(str(e), "red")
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)
    except Exception as error:
        print_colored(str(error), "red")
        sys.exit(1)
