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

from config import mask_all_sensitive_config_data, read_config
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


# Held so the top-level handler can mask an error it did not catch in context.
# RPC URLs carry API keys and reach exception text verbatim, and this is the
# other path -- alongside the scheduler's Telegram alerts -- where that text
# leaves the machine.
_loaded_config = None


def _sanitise(error: Exception) -> str:
    return mask_all_sensitive_config_data(str(error), _loaded_config)


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
        tx=source.tx.as_kwargs(),
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
        tx=source.tx.as_kwargs(),
    )
    print("Epochs processed: {}".format(processed))


def cmd_oracle(config: Config, args) -> None:
    from main import run_oracle_update

    # The raising variant, so a failure leaves a non-zero exit status. Delivery
    # failures do not raise in the scheduler -- a retry would only repeat the
    # announcement -- so they are surfaced here instead, where a person is
    # watching.
    summary = asyncio.run(
        run_oracle_update(
            config,
            force=args.force,
            dry_run=args.dry_run,
            source_name=args.source,
        )
    )
    if not args.dry_run and not summary.written:
        # A person ran this and nothing reached the chain -- a guard refused, or
        # a transfer was in flight. Exiting zero would report that as done.
        raise Exception(
            "The oracle was not written; see the output above for which guard "
            "refused or what blocked it"
        )
    if not summary.notified:
        raise Exception(
            "The oracle run needs attention but could not notify anyone; check "
            "the Telegram token, the group id, and that the bot is still in the "
            "group"
        )


def cmd_oracle_propose(config: Config, args) -> None:
    from main import run_oracle_propose

    # The recovery path for a guard that refused. It signs off chain and posts
    # to the Safe service, so unlike every other write here it takes no nonce
    # and needs no lock -- see LOCK_FREE below.
    value = int(args.value) if args.value is not None else None
    if not asyncio.run(
        run_oracle_propose(
            config,
            value_override=value,
            dry_run=args.dry_run,
            source_name=args.source,
        )
    ):
        raise Exception(
            "The proposal is queued but nobody could be told; send the Safe link "
            "to the signers by hand"
        )


def cmd_rebalance(config: Config, args) -> None:
    run_rebalance(config, interactive=not args.yes)


def cmd_validate_config(config: Config, args) -> None:
    from config.validate_config import validate_config

    validate_config(config)


COMMANDS = {
    "ascend": cmd_ascend,
    "handle-epoch": cmd_handle_epoch,
    "oracle": cmd_oracle,
    "oracle-propose": cmd_oracle_propose,
    "rebalance": cmd_rebalance,
    "validate-config": cmd_validate_config,
}

# Commands that do not need the lock: they broadcast nothing, so they cannot
# collide with the scheduler over a nonce.
#
# oracle-propose belongs here and it matters that it does. It signs a Safe
# transaction off chain and posts it to the Safe service; the only chain access
# is reading the Safe's version and nonce. Requiring the lock would mean
# stopping the scheduler to queue a proposal, and this is the path someone
# reaches for when the heartbeat has refused to write and the oracle is heading
# for expiry -- exactly when the rest of the bot should be left running.
LOCK_FREE = {"validate-config", "oracle-propose"}


def build_parser() -> argparse.ArgumentParser:
    # Shared options live on a parent parser so they are accepted both before and
    # after the subcommand. Registered only on the top-level parser they would be
    # rejected in the position an operator naturally types them, which is the
    # position `pick_source` suggests when it asks for --source.
    # SUPPRESS matters: a subparser parses into its own namespace and copies every
    # key back over the outer one, so an ordinary default here would overwrite a
    # value already given before the subcommand. Suppressed options land in that
    # namespace only when actually passed; parse_args() fills in the rest.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--source",
        default=argparse.SUPPRESS,
        help="Source chain name (default: the only one)",
    )
    common.add_argument(
        "--no-lock",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Skip the single-holder lock. Only safe when the scheduler is stopped.",
    )

    parser = argparse.ArgumentParser(prog="cli", description=__doc__, parents=[common])
    subparsers = parser.add_subparsers(dest="command", required=True)

    ascend = subparsers.add_parser(
        "ascend", help="Claim rewards and distribute them", parents=[common]
    )
    ascend.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate the calls without broadcasting",
    )

    subparsers.add_parser(
        "handle-epoch", help="Advance matured withdrawal epochs", parents=[common]
    )
    oracle = subparsers.add_parser(
        "oracle", help="Refresh the oracle directly", parents=[common]
    )
    oracle.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute the value and run the guards without broadcasting",
    )
    oracle.add_argument(
        "--force",
        action="store_true",
        help=(
            "Write even if the deviation or decrease guard would refuse. Only "
            "after checking the reading with --dry-run; prefer oracle-propose, "
            "which puts the value in front of the Safe signers instead."
        ),
    )

    propose = subparsers.add_parser(
        "oracle-propose",
        help="Queue an oracle update through the Safe for signers to approve",
        parents=[common],
    )
    propose.add_argument(
        "--value",
        default=None,
        help=(
            "Value in wei to propose, instead of the computed one. For when a "
            "guard refused precisely because the computed value looks wrong."
        ),
    )
    propose.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be proposed without posting it to the Safe",
    )

    rebalance = subparsers.add_parser(
        "rebalance", help="Rebalance across chains", parents=[common]
    )
    rebalance.add_argument(
        "-y", "--yes", action="store_true", help="Skip the confirmation prompt"
    )

    subparsers.add_parser(
        "validate-config", help="Validate config against chains", parents=[common]
    )
    return parser


# Deliberately not parser.set_defaults(): parents share the action objects, and
# set_defaults rewrites the shared action's default, which would undo the
# SUPPRESS above and reintroduce the clobbering it exists to prevent.
SHARED_DEFAULTS = {"source": None, "no_lock": False}


def parse_args(argv=None):
    args = build_parser().parse_args(argv)
    for name, default in SHARED_DEFAULTS.items():
        if not hasattr(args, name):
            setattr(args, name, default)
    return args


def main() -> int:
    global _loaded_config
    args = parse_args()
    config = load()
    _loaded_config = config
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
        print_colored(_sanitise(e), "red")
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)
    except Exception as error:
        print_colored(_sanitise(error), "red")
        sys.exit(1)
