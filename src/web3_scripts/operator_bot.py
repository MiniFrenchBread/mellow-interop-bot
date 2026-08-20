try:
    from .base import *
    from .oracle_script import run_oracle_validation
except ImportError:
    from base import *
    from oracle_script import run_oracle_validation
import time
from typing import List, Optional, Tuple

LAYER_ZERO_DUST = 1000_000_000_000

# Observed finalization takes 1-5 minutes. The ceiling exists so a message that
# never arrives cannot pin this call open forever: the scheduler runs the other
# tasks in the same process, so an unbounded wait here would stop oracle
# monitoring and epoch handling too.
LAYER_ZERO_FINALIZATION_TIMEOUT = 1800
LAYER_ZERO_POLL_INTERVAL = 60


class LayerZeroFinalizationTimeout(Exception):
    pass


def wait_for_layer_zero_finalization(
    source_helper,
    target_helper,
    source_core_address: str,
    target_core_address: str,
    timeout: float = LAYER_ZERO_FINALIZATION_TIMEOUT,
) -> None:
    iteration = 1
    deadline = time.monotonic() + timeout
    time.sleep(min(LAYER_ZERO_POLL_INTERVAL, timeout))
    while True:
        source_nonces = source_helper.getNonces(source_core_address).call()
        target_nonces = target_helper.getNonces(target_core_address).call()
        # requirement: source.inboundNonce == target.outboundNonce && source.outboundNonce == target.inboundNonce
        if (
            source_nonces[0] == target_nonces[1]
            and source_nonces[1] == target_nonces[0]
        ):
            time.sleep(15)
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LayerZeroFinalizationTimeout(
                "LayerZero transfer did not finalize within {}s. "
                "source nonces {}, target nonces {}".format(
                    timeout, source_nonces, target_nonces
                )
            )
        print("Waiting for LayerZero finalization ({})...".format(iteration))
        iteration += 1
        time.sleep(min(LAYER_ZERO_POLL_INTERVAL, remaining))


def run(
    source_core_address: str,
    target_core_address: str,
    source_rpc: str,
    target_rpc: str,
    source_core_helper: str,
    target_core_helper: str,
    operator_pk: str,
    source_ratio_d3: int,
    max_source_ratio_d3: int,
    force_withdrawal: bool = False,
) -> Optional[str]:
    """Rebalance one deployment.

    Returns None when the pass completed, or a short reason when a guard stopped
    it before any transaction was sent. The reason is what lets the scheduler
    notice that this task has been skipping silently: nothing is wrong with a
    single skip, but repeated ones mean the bot has quietly stopped rebalancing.
    """
    oracle_validation_result = run_oracle_validation(
        source_core_address,
        target_core_address,
        source_rpc,
        target_rpc,
        source_core_helper,
        target_core_helper,
        oracle_expiry_threshold_seconds=3600,
        oracle_recent_update_threshold_seconds=0,
    )

    if oracle_validation_result.transfer_in_progress:
        print_colored("OFT transfers in progress", "yellow")
        return "OFT transfers in progress"

    if oracle_validation_result.almost_expired:
        print_colored("Oracle is almost expired", "red")
        return "oracle almost expired"

    if oracle_validation_result.incorrect_value:
        print_colored("Oracle value is incorrect", "red")
        return "oracle value is incorrect"

    source_w3 = get_w3(source_rpc)
    target_w3 = get_w3(target_rpc)

    source_helper = get_contract(
        source_w3, source_core_helper, "SourceHelper"
    ).functions
    target_helper = get_contract(
        target_w3, target_core_helper, "TargetHelper"
    ).functions

    source_core = get_contract(source_w3, source_core_address, "SourceCore").functions
    target_core = get_contract(target_w3, target_core_address, "TargetCore").functions

    source_block = source_w3.eth.get_block("latest").number
    target_block = target_w3.eth.get_block("latest").number

    source_nonces = source_helper.getNonces(source_core_address).call(
        block_identifier=source_block
    )
    target_nonces = target_helper.getNonces(target_core_address).call(
        block_identifier=target_block
    )
    # requirement: source.inboundNonce == target.outboundNonce && source.outboundNonce == target.inboundNonce
    if source_nonces[0] != target_nonces[1] or source_nonces[1] != target_nonces[0]:
        print_colored(
            "OFT transfers in progress. source chain {} target chain {}".format(
                source_nonces, target_nonces
            ),
            "yellow",
        )
        return "OFT transfers in progress"

    source_value = source_helper.getSourceValue(source_core_address).call(
        block_identifier=source_block
    )
    target_value = target_helper.getTargetValue(target_core_address).call(
        block_identifier=target_block
    )
    withdrawal_data = source_helper.getWithdrawalData(source_core_address).call(
        block_identifier=source_block
    )
    withdrawal_demand = withdrawal_data[0]
    total_supply = withdrawal_data[1]
    withdrawal_demand = (
        (source_value + target_value) * withdrawal_demand // total_supply
    )

    if source_value + target_value <= withdrawal_demand:
        print_colored(
            "Withdrawal demand is greater or equal to total assets. Invalid state."
        )
        return "withdrawal demand exceeds total assets"

    current_ratio_d3 = int(
        1000
        * (source_value - withdrawal_demand)
        // (source_value + target_value - withdrawal_demand)
    )

    print(
        "Source value: {}. Target value: {}. Withdrawal demand: {}".format(
            source_value, target_value, withdrawal_demand
        )
    )

    if force_withdrawal:
        assets_deficit = target_value
        assets_deficit = (assets_deficit // LAYER_ZERO_DUST + 1) * LAYER_ZERO_DUST
        print("Assets deficit: {}.".format(assets_deficit))
        data = target_helper.getAmounts(target_core_address, assets_deficit).call()
        if data[2] > 0:
            print_colored("TargetCore.redeem({})".format(data[2]))
            execute(target_core.redeem(data[2]), 0, operator_pk)
        if data[1]:
            print_colored("TargetCore.claim({})".format(data[1].hex()))
            execute(target_core.claim(data[1]), 0, operator_pk)
        if data[0] > 0:
            value = target_helper.quotePushToSource(target_core_address).call()
            print_colored(
                "TargetCore.pushToSource{{value:{}}}({})".format(value, data[0])
            )
            execute(
                target_core.pushToSource(data[0]),
                value,
                operator_pk,
            )
            print("Waiting for LayerZero finalization...")
            wait_for_layer_zero_finalization(
                source_helper, target_helper, source_core_address, target_core_address
            )
            print("LayerZero finalization completed.")
    elif current_ratio_d3 < 0:
        assets_deficit = (
            (source_ratio_d3 - current_ratio_d3)
            * (source_value + target_value - withdrawal_demand)
            // 1000
        )
        assets_deficit = (assets_deficit // LAYER_ZERO_DUST + 1) * LAYER_ZERO_DUST
        print(
            "Assets deficit: {}. Current ratio: {}%".format(
                assets_deficit, current_ratio_d3 / 10
            )
        )
        data = target_helper.getAmounts(target_core_address, assets_deficit).call()
        if data[2] > 0:
            print_colored("TargetCore.redeem({})".format(data[2]))
            execute(target_core.redeem(data[2]), 0, operator_pk)
        if data[1]:
            print_colored("TargetCore.claim({})".format(data[1].hex()))
            execute(target_core.claim(data[1]), 0, operator_pk)
        if data[0] > 0:
            value = target_helper.quotePushToSource(target_core_address).call()
            print_colored(
                "TargetCore.pushToSource{{value:{}}}({})".format(value, data[0])
            )
            execute(
                target_core.pushToSource(data[0]),
                value,
                operator_pk,
            )
            print("Waiting for LayerZero finalization...")
            wait_for_layer_zero_finalization(
                source_helper, target_helper, source_core_address, target_core_address
            )
            print("LayerZero finalization completed.")
    elif current_ratio_d3 > max_source_ratio_d3:
        print("Assets surplus. Current ratio: {}%".format(current_ratio_d3 / 10))
        value = source_helper.quotePushToTarget(source_core_address).call()
        print_colored("SourceCore.pushToTarget{{value:{}}}()".format(value))
        execute(
            source_core.pushToTarget(),
            value,
            operator_pk,
        )
        print("Waiting for LayerZero finalization...")
        wait_for_layer_zero_finalization(
            source_helper, target_helper, source_core_address, target_core_address
        )
        print("LayerZero finalization completed.")
    else:
        print_colored(
            "No crosschain action required. Current ratio: {}%. Target SourceCore ratio: {}%. Max SourceCore ratio: {}%.".format(
                current_ratio_d3 / 10,
                source_ratio_d3 / 10,
                max_source_ratio_d3 / 10,
            ),
            "green",
        )

    data = target_helper.getAmounts(target_core_address, 0).call()

    if data[1]:
        print_colored("TargetCore.claim({})".format(data[1].hex()))
        execute(target_core.claim(data[1]), 0, operator_pk)
    if data[3] >= LAYER_ZERO_DUST:
        print_colored("TargetCore.deposit({})".format(data[3]))
        execute(target_core.deposit(data[3]), 0, operator_pk)


def parse_deployments(config, deployments_raw):
    if not deployments_raw:
        print_colored("DEPLOYMENTS environment variable not set", "red")
        return []

    # Get all available pairs (e.g. BSC:CYCLE, FRAX:FRAX, ...) for error messages
    available_pairs = []
    for source in config.sources:
        for deployment in source.deployments:
            available_pairs.append(f"{source.name}:{deployment.name}")

    valid_deployments = []
    for deployment_pair in deployments_raw.split(","):
        deployment_pair = deployment_pair.strip()

        try:
            source_label, deployment_label = deployment_pair.split(":")
        except ValueError:
            print_colored(
                f"Invalid deployment format '{deployment_pair}'. Expected format: SOURCE:DEPLOYMENT. "
                f"Available pairs: {', '.join(available_pairs)}",
                "red",
            )
            continue

        # Find source
        source = next((s for s in config.sources if s.name == source_label), None)
        if not source:
            print_colored(
                f"Source '{source_label}' not found. Available pairs: {', '.join(available_pairs)}",
                "red",
            )
            continue

        # Find deployment within source
        deployment = next(
            (d for d in source.deployments if d.name == deployment_label), None
        )
        if not deployment:
            print_colored(
                f"Deployment '{source_label}:{deployment_label}' not found. Available pairs: {', '.join(available_pairs)}",
                "red",
            )
            continue

        valid_deployments.append((source, deployment))

    return valid_deployments


def run_all(
    config,
    operator_pk: str = None,
    raw_deployments: str = None,
    source_ratio_d3: int = None,
    max_source_ratio_d3: int = None,
    force_withdrawal: bool = None,
    interactive: bool = True,
) -> List[Tuple[str, Optional[str]]]:
    """Rebalance every configured deployment.

    Lifted out of the __main__ block so the scheduler and the CLI can call it
    in-process rather than spawning a subprocess.
    """
    import os

    operator_pk = operator_pk or os.getenv("OPERATOR_PK")
    raw_deployments = raw_deployments or os.getenv("DEPLOYMENTS")
    if source_ratio_d3 is None:
        source_ratio_d3 = int(os.getenv("SOURCE_RATIO_D3", 50))
    if max_source_ratio_d3 is None:
        max_source_ratio_d3 = int(os.getenv("MAX_SOURCE_RATIO_D3", 100))
    if force_withdrawal is None:
        force_withdrawal = int(os.getenv("FORCE_WITHDRAWAL", 0)) != 0

    deployments = parse_deployments(config, raw_deployments)
    if not deployments:
        raise Exception("No valid deployments found")

    operator_address = Account.from_key(operator_pk).address

    print("Configuration:\n")
    print(f"Operator Address: {add_color(operator_address, 'yellow')}")
    print(f"Source Ratio D3: {add_color(str(source_ratio_d3), 'yellow')}")
    print(f"Max Source Ratio D3: {add_color(str(max_source_ratio_d3), 'yellow')}")
    print(
        f"Target chain ID: {add_color(str(get_w3(config.target_rpc).eth.chain_id), 'yellow')}"
    )
    print("\nDeployments:")

    source_chain_ids = {}
    for source, deployment in deployments:
        if source.name not in source_chain_ids:
            source_chain_ids[source.name] = get_w3(source.rpc).eth.chain_id
        print(f"- {source.name} ({source_chain_ids[source.name]}): {deployment.name}")
        print(f"\tSource Core Helper: {add_color(source.source_core_helper, 'yellow')}")
        print(f"\tSource Core: {add_color(deployment.source_core, 'yellow')}")
        print(f"\tTarget Core Helper: {add_color(config.target_core_helper, 'yellow')}")
        print(f"\tTarget Core: {add_color(deployment.target_core, 'yellow')}")

    if interactive and os.getenv("NON_INTERACTIVE", "").strip().lower() != "true":
        user_input = (
            input("\nEnter 'y' to continue, any other key to quit: ").strip().lower()
        )
        if user_input != "y":
            print("Operation cancelled by user.")
            return []
        print("Continuing with operations...")
    elif interactive:
        print_colored(
            "NON_INTERACTIVE enabled; skipping confirmation prompt.", "yellow"
        )

    skipped = []
    for source, deployment in deployments:
        print(f"\nProcessing deployment {deployment.name} of {source.name}...")
        reason = run(
            source_core_address=deployment.source_core,
            target_core_address=deployment.target_core,
            source_rpc=source.rpc,
            target_rpc=config.target_rpc,
            source_core_helper=source.source_core_helper,
            target_core_helper=config.target_core_helper,
            operator_pk=operator_pk,
            source_ratio_d3=source_ratio_d3,
            max_source_ratio_d3=max_source_ratio_d3,
            force_withdrawal=force_withdrawal,
        )
        skipped.append(("{}:{}".format(source.name, deployment.name), reason))
    return skipped


if __name__ == "__main__":
    import dotenv
    import sys
    from pathlib import Path

    # Add src directory to path to import config module
    src_path = Path(__file__).parent.parent
    sys.path.insert(0, str(src_path))

    from config.read_config import read_config

    dotenv.load_dotenv()

    config_path = src_path.parent / "config.json"

    try:
        run_all(read_config(str(config_path)))
    except KeyboardInterrupt:
        print("\nOperation cancelled by user (Ctrl+C).")
        sys.exit(0)
    except Exception as e:
        print_colored(str(e), "red")
        sys.exit(1)
