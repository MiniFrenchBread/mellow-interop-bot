import os
import json
from web3 import Web3, constants
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field

# Chains differ enough that one set of transaction settings cannot serve both.
# 0G produces blocks in about a second, so a transaction that has not appeared
# after a minute is genuinely stuck. Ethereum's inclusion depends on fee
# competition and routinely takes several minutes, which is why the previous
# two-minute wait kept abandoning transactions that were merely slow.
DEFAULT_RECEIPT_TIMEOUT_SECONDS = 180
DEFAULT_TX_MAX_ATTEMPTS = 3
DEFAULT_TX_FEE_BUMP_PERCENT = 115
DEFAULT_TX_FEE_CAP_GWEI = 4

DEFAULT_LOOP_SLEEP_SECONDS = 300
DEFAULT_POST_ASCEND_GAP_SECONDS = 60
DEFAULT_LOCK_FILE = ".scheduler.lock"
DEFAULT_ALERT_AFTER_FAILURES = 3
DEFAULT_TASK_INTERVALS = {
    "ascend": 1209600,
    "rebalance": 7200,
    "oracle_report": 86400,
    "handle_epoch": 300,
}
DEFAULT_HANDLE_EPOCH_MAX_ITERATIONS = 8


@dataclass(frozen=True)
class TxConfig:
    receipt_timeout_seconds: float = DEFAULT_RECEIPT_TIMEOUT_SECONDS
    max_attempts: int = DEFAULT_TX_MAX_ATTEMPTS
    fee_bump_percent: int = DEFAULT_TX_FEE_BUMP_PERCENT
    fee_cap_gwei: int = DEFAULT_TX_FEE_CAP_GWEI


@dataclass(frozen=True)
class AscendConfig:
    router: str
    rewarders: Tuple[str, ...]
    claim_account: Optional[str] = None

    def resolved_claim_account(self) -> str:
        return self.claim_account or self.router


@dataclass(frozen=True)
class WithdrawalQueueConfig:
    address: str
    max_iterations: int = DEFAULT_HANDLE_EPOCH_MAX_ITERATIONS


@dataclass(frozen=True)
class SchedulerConfig:
    loop_sleep_seconds: int = DEFAULT_LOOP_SLEEP_SECONDS
    post_ascend_gap_seconds: int = DEFAULT_POST_ASCEND_GAP_SECONDS
    lock_file: str = DEFAULT_LOCK_FILE
    alert_after_failures: int = DEFAULT_ALERT_AFTER_FAILURES
    task_intervals: Dict[str, int] = field(
        default_factory=lambda: dict(DEFAULT_TASK_INTERVALS)
    )

    def interval(self, task: str) -> int:
        return int(self.task_intervals.get(task, DEFAULT_TASK_INTERVALS.get(task, 0)))


@dataclass(frozen=True)
class SafeGlobal:
    safe_address: str
    proposer_private_key: str
    api_url: str
    api_key: str = None
    web_client_url: str = None
    eip_3770: str = None


@dataclass(frozen=True)
class Deployment:
    name: str
    source_core: str
    target_core: str
    safe_global: Optional[SafeGlobal] = None


def _ensure_safe_global_fields(
    safe_global: SafeGlobal, source_name: str, label: str
) -> None:
    """
    Validate that required SafeGlobal fields are present after merging overrides.
    Note: proposer_private_key is excluded as it comes from env vars that may not
    be available during CI validation.

    Raises:
        ValueError: if any required field is missing or empty.
    """
    required_fields = {
        "safe_address": safe_global.safe_address,
        "api_url": safe_global.api_url,
        "web_client_url": safe_global.web_client_url,
    }

    missing = [name for name, value in required_fields.items() if not value]
    if missing:
        field_list = ", ".join(missing)
        raise ValueError(
            f"Safe config for {source_name} ({label}) is missing required field(s): {field_list}"
        )


@dataclass(frozen=True)
class SourceConfig:
    name: str
    rpc: str
    source_core_helper: str
    deployments: Tuple[Deployment, ...]  # Changed from List to tuple for hashability
    safe_global: SafeGlobal = None
    # Signs transactions sent to this chain. Defaults to OPERATOR_PK, which today
    # is the same key that claims rewards, rebalances, advances the withdrawal
    # queue and signs Safe proposals; the indirection is here so those roles can
    # be split onto separate keys without a code change.
    executor_private_key: str = None
    tx: TxConfig = field(default_factory=TxConfig)
    ascend: Optional[AscendConfig] = None
    withdrawal_queue: Optional[WithdrawalQueueConfig] = None


@dataclass
class Config:
    telegram_bot_api_key: str
    telegram_group_chat_id: str
    telegram_owner_nicknames: Dict[str, str]
    telegram_proposal_message_prefix: str
    oracle_expiry_threshold_seconds: int
    oracle_recent_update_threshold_seconds: int
    target_rpc: str
    target_core_helper: str
    sources: List[SourceConfig]
    target_tx: TxConfig = field(default_factory=TxConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)


def read_config(config_path: str) -> Config:
    """
    Read and parse the config.json file with the following transformations:
    1. Convert kebab-case keys to snake_case
    2. Replace ${VAR:default} patterns with environment variables or defaults
    3. Support nested variable substitution (e.g., ${BSC_SAFE_API_KEY:${SAFE_API_KEY}})

    Args:
        config_path: Path to the config.json file

    Returns:
        Config object with parsed and transformed configuration
    """
    # Read the JSON file
    with open(config_path, "r") as file:
        config = json.load(file)

    # Transform the configuration
    transformed_config = _transform_config(config)

    # Convert dictionary to typed Config object
    return _dict_to_config(transformed_config)


def _transform_config(obj: Any) -> Any:
    """
    Recursively transform the configuration object:
    - Convert kebab-case keys to snake_case
    - Replace ${VAR:default} patterns with environment variables (supports nested substitution)
    """
    if isinstance(obj, dict):
        transformed = {}
        for key, value in obj.items():
            # Convert kebab-case to snake_case
            snake_key = key.replace("-", "_")
            # Recursively transform the value
            transformed[snake_key] = _transform_config(value)
        return transformed
    elif isinstance(obj, list):
        # Transform each item in the list
        return [_transform_config(item) for item in obj]
    elif isinstance(obj, str):
        # Handle environment variable substitution
        return _substitute_env_vars(obj)
    else:
        # Return primitive types as-is
        return obj


def _substitute_env_vars(value: str, visited_vars: set = None) -> str:
    """
    Replace ${VAR:default} patterns with environment variables or default values.
    Supports nested variable substitution with circular reference detection.

    Examples:
        ${TELEGRAM_BOT_API_KEY} -> os.getenv('TELEGRAM_BOT_API_KEY', '')
        ${TARGET_RPC:https://eth.drpc.org} -> os.getenv('TARGET_RPC', 'https://eth.drpc.org')
        ${BSC_SAFE_API_KEY:${SAFE_API_KEY}} -> tries BSC_SAFE_API_KEY first, then SAFE_API_KEY
        ${VAR1:${VAR2:${VAR3:fallback}}} -> tries VAR1, then VAR2, then VAR3, then uses 'fallback'

    Args:
        value: The string containing variable patterns to substitute
        visited_vars: Set of variable names being processed (for circular reference detection)

    Returns:
        String with all variable patterns substituted

    Raises:
        ValueError: If a circular reference is detected in variable substitution
    """
    if visited_vars is None:
        visited_vars = set()

    result = value
    max_iterations = 64
    iteration = 0

    while iteration < max_iterations:
        iteration += 1
        start = result.rfind("${")
        if start == -1:
            break  # No more patterns

        # Parse variable name until ':' or '}'
        name_start = start + 2
        i = name_start
        while i < len(result) and result[i] not in ":}":
            i += 1
        if i >= len(result):
            break  # Incomplete pattern; leave as-is

        var_name = result[name_start:i]
        if not var_name:
            # Skip malformed and continue searching earlier occurrences
            result = result[:start] + result[start + 2 :]
            continue

        # Determine default value and closing brace position
        if result[i] == "}":
            default_value = ""
            close = i
        else:
            # We started from the last '${', so the next '}' is the matching close
            default_start = i + 1
            close = result.find("}", default_start)
            if close == -1:
                break  # Unmatched; leave as-is
            default_value = result[default_start:close]

        if var_name in visited_vars:
            raise ValueError(
                f"Circular reference detected in variable substitution: {var_name}"
            )

        env_value = os.getenv(var_name)
        replacement_source = env_value if env_value else default_value

        # Recursively resolve nested variables in the replacement source
        new_visited = visited_vars.copy()
        new_visited.add(var_name)
        replacement = _substitute_env_vars(replacement_source, new_visited)

        # Splice the resolved value back into the string
        result = result[:start] + replacement + result[close + 1 :]

    if iteration >= max_iterations:
        raise ValueError(
            f"Maximum substitution iterations exceeded. Possible complex circular reference in: {value}"
        )

    return result


def _parse_telegram_owners(telegram_owner_nicknames: str) -> Dict[str, str]:
    """
    Parse telegram owner nicknames string into a dictionary mapping nicknames to addresses.

    Supports two formats:
    1. Comma-separated nicknames: "@josh,@anna,@dexter"
    2. Comma-separated nickname:address pairs: "@josh:0x00..000,@anna:0xab..412"

    Args:
        telegram_owner_nicknames: String containing telegram nicknames

    Returns:
        Dictionary mapping telegram nicknames to addresses (zero address if not specified)
    """
    result = {}

    if not telegram_owner_nicknames:
        return result

    # Split by comma and process each entry
    entries = [
        entry.strip() for entry in telegram_owner_nicknames.split(",") if entry.strip()
    ]

    for entry in entries:
        # Check if entry contains address (format: @nickname:0xaddress)
        if ":" in entry:
            nickname, address = entry.split(":", 1)
            result[nickname.strip().lstrip("@")] = address.strip()
        else:
            # Format: @nickname (no address specified)
            result[entry.strip().lstrip("@")] = constants.ADDRESS_ZERO

    return result


def _merge_safe_global_overrides(
    base: Optional[SafeGlobal],
    overrides_dict: Optional[Dict[str, Any]],
    source_name: str,
) -> Optional[SafeGlobal]:
    """
    Merge safe-global-overrides with the chain-level safe_global config.

    If overrides_dict is None or empty, returns base as-is.
    For each field in overrides_dict, it takes precedence over the base value.
    Fields not specified in overrides fall back to base values.

    Args:
        base: The chain-level SafeGlobal config (can be None)
        overrides_dict: Dictionary of override values from deployment config
        source_name: Name of the source (used for eip_3770 fallback)

    Returns:
        Merged SafeGlobal or None if both base and overrides are empty
    """
    if not overrides_dict:
        return base

    if base is None:
        # No base config, create from overrides only
        # Handle eip_3770 fallback: use lowercased source name if not provided
        eip_3770 = overrides_dict.get("eip_3770")
        if eip_3770 is None:
            eip_3770 = source_name.lower()

        merged = SafeGlobal(
            safe_address=overrides_dict.get("safe_address", ""),
            proposer_private_key=overrides_dict.get("proposer_private_key", ""),
            api_url=overrides_dict.get("api_url", ""),
            api_key=overrides_dict.get("api_key"),
            web_client_url=overrides_dict.get("web_client_url"),
            eip_3770=eip_3770,
        )
        _ensure_safe_global_fields(merged, source_name, "deployment override")
        return merged

    # Merge: overrides take precedence, fall back to base values
    # Handle eip_3770 fallback: use lowercased source name if not provided
    eip_3770 = overrides_dict.get("eip_3770", base.eip_3770)
    if eip_3770 is None:
        eip_3770 = source_name.lower()

    merged = SafeGlobal(
        safe_address=overrides_dict.get("safe_address", base.safe_address),
        proposer_private_key=overrides_dict.get(
            "proposer_private_key", base.proposer_private_key
        ),
        api_url=overrides_dict.get("api_url", base.api_url),
        api_key=overrides_dict.get("api_key", base.api_key),
        web_client_url=overrides_dict.get("web_client_url", base.web_client_url),
        eip_3770=eip_3770,
    )
    _ensure_safe_global_fields(merged, source_name, "deployment override")
    return merged


def _create_tx_config(tx_dict: Optional[Dict[str, Any]]) -> TxConfig:
    if not tx_dict:
        return TxConfig()
    return TxConfig(
        receipt_timeout_seconds=float(
            tx_dict.get("receipt_timeout_seconds", DEFAULT_RECEIPT_TIMEOUT_SECONDS)
        ),
        max_attempts=int(tx_dict.get("max_attempts", DEFAULT_TX_MAX_ATTEMPTS)),
        fee_bump_percent=int(
            tx_dict.get("fee_bump_percent", DEFAULT_TX_FEE_BUMP_PERCENT)
        ),
        fee_cap_gwei=int(tx_dict.get("fee_cap_gwei", DEFAULT_TX_FEE_CAP_GWEI)),
    )


def _create_ascend_config(
    ascend_dict: Optional[Dict[str, Any]],
) -> Optional[AscendConfig]:
    if not ascend_dict:
        return None
    rewarders = tuple(ascend_dict.get("rewarders") or ())
    if not ascend_dict.get("router"):
        raise ValueError("ascend config is missing required field: router")
    if not rewarders:
        raise ValueError("ascend config is missing required field: rewarders")
    return AscendConfig(
        router=ascend_dict["router"],
        rewarders=rewarders,
        claim_account=ascend_dict.get("claim_account") or None,
    )


def _create_withdrawal_queue_config(
    queue_dict: Optional[Dict[str, Any]],
) -> Optional[WithdrawalQueueConfig]:
    if not queue_dict:
        return None
    if not queue_dict.get("address"):
        raise ValueError("withdrawal-queue config is missing required field: address")
    return WithdrawalQueueConfig(
        address=queue_dict["address"],
        max_iterations=int(
            queue_dict.get("max_iterations", DEFAULT_HANDLE_EPOCH_MAX_ITERATIONS)
        ),
    )


def _create_scheduler_config(
    scheduler_dict: Optional[Dict[str, Any]],
) -> SchedulerConfig:
    if not scheduler_dict:
        return SchedulerConfig()
    intervals = dict(DEFAULT_TASK_INTERVALS)
    for task, interval in (scheduler_dict.get("tasks") or {}).items():
        if isinstance(interval, dict):
            interval = interval.get("interval_seconds")
        if interval is not None and str(interval) != "":
            intervals[task] = int(interval)
    return SchedulerConfig(
        loop_sleep_seconds=int(
            scheduler_dict.get("loop_sleep_seconds", DEFAULT_LOOP_SLEEP_SECONDS)
        ),
        post_ascend_gap_seconds=int(
            scheduler_dict.get(
                "post_ascend_gap_seconds", DEFAULT_POST_ASCEND_GAP_SECONDS
            )
        ),
        lock_file=scheduler_dict.get("lock_file") or DEFAULT_LOCK_FILE,
        alert_after_failures=int(
            scheduler_dict.get("alert_after_failures", DEFAULT_ALERT_AFTER_FAILURES)
        ),
        task_intervals=intervals,
    )


def _dict_to_config(config_dict: Dict[str, Any]) -> Config:
    """
    Convert a dictionary to a typed Config object.
    """

    # Convert source configurations
    def create_source_config(source_dict: Dict[str, Any]) -> SourceConfig:
        source_name = source_dict["name"]

        # Handle optional chain-level safe_global configuration
        chain_safe_global = None
        if "safe_global" in source_dict:
            safe_global_dict = source_dict["safe_global"]

            # Handle eip_3770 fallback: use lowercased source name if not provided
            eip_3770 = safe_global_dict.get("eip_3770")
            if eip_3770 is None:
                eip_3770 = source_name.lower()

            chain_safe_global = SafeGlobal(
                safe_address=safe_global_dict["safe_address"],
                proposer_private_key=safe_global_dict["proposer_private_key"],
                api_url=safe_global_dict["api_url"],
                api_key=safe_global_dict.get("api_key"),
                web_client_url=safe_global_dict.get("web_client_url"),
                eip_3770=eip_3770,
            )

        # Create deployments with optional safe_global_overrides merged
        def create_deployment(dep: Dict[str, Any]) -> Deployment:
            # Check for safe_global_overrides and merge with chain-level config
            overrides_dict = dep.get("safe_global_overrides")
            deployment_safe_global = _merge_safe_global_overrides(
                chain_safe_global, overrides_dict, source_name
            )

            return Deployment(
                name=dep["name"],
                source_core=dep["source_core"],
                target_core=dep["target_core"],
                safe_global=deployment_safe_global,
            )

        deployments = tuple(
            create_deployment(dep) for dep in source_dict["deployments"]
        )

        return SourceConfig(
            name=source_name,
            rpc=source_dict["rpc"],
            source_core_helper=source_dict["source_core_helper"],
            deployments=deployments,
            safe_global=chain_safe_global,
            executor_private_key=source_dict.get("executor_private_key")
            or os.getenv("OPERATOR_PK"),
            tx=_create_tx_config(source_dict.get("tx")),
            ascend=_create_ascend_config(source_dict.get("ascend")),
            withdrawal_queue=_create_withdrawal_queue_config(
                source_dict.get("withdrawal_queue")
            ),
        )

    # Convert all sources
    sources = [create_source_config(source) for source in config_dict["sources"]]

    return Config(
        telegram_bot_api_key=config_dict["telegram_bot_api_key"],
        telegram_group_chat_id=config_dict["telegram_group_chat_id"],
        telegram_owner_nicknames=_parse_telegram_owners(
            config_dict["telegram_owner_nicknames"]
        ),
        telegram_proposal_message_prefix=config_dict[
            "telegram_proposal_message_prefix"
        ],
        oracle_expiry_threshold_seconds=int(
            config_dict["oracle_expiry_threshold_seconds"]
        ),
        oracle_recent_update_threshold_seconds=int(
            config_dict["oracle_recent_update_threshold_seconds"]
        ),
        target_rpc=config_dict["target_rpc"],
        target_core_helper=config_dict["target_core_helper"],
        sources=sources,
        target_tx=_create_tx_config(config_dict.get("tx", {}).get("target")),
        scheduler=_create_scheduler_config(config_dict.get("scheduler")),
    )
