import sys
import os
from packaging.version import Version
from web3 import constants

if __name__ == "__main__":
    sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from web3_scripts import get_w3, print_colored, get_contract, Account, Web3
from safe_global import client_gateway_api, transaction_api
from safe_global.multi_send_call import multi_send_contracts

# Handle both relative and absolute imports
try:
    from .read_config import Config, SourceConfig, Deployment, SafeGlobal, read_config
    from .mask_sensitive_data import mask_url_credentials, mask_source_sensitive_data
except ImportError:
    from config.read_config import (
        Config,
        SourceConfig,
        Deployment,
        SafeGlobal,
        read_config,
    )
    from config.mask_sensitive_data import (
        mask_url_credentials,
        mask_source_sensitive_data,
    )


def validate_config(config: Config):
    w3 = get_w3(config.target_rpc)
    validate_rpc_url(w3, "target")
    validate_target_helper(w3, config)
    for source in config.sources:
        validate_source(w3, source)


def validate_source(target_w3: Web3, source: SourceConfig):
    w3 = get_w3(source.rpc)
    validate_rpc_url(w3, source.name)
    validate_source_helper(w3, source)
    # Before validate_deployments, and deliberately so. This exists to answer
    # "was the role granted?" in one line before a deploy, and it only needs the
    # source-core addresses from config -- not the cross-reference checks. Run
    # after them, any unrelated failure in that pass (the symbol assertion is
    # currently one) aborts the run before the question is ever asked, which is
    # precisely when someone is relying on the answer.
    validate_oracle_updater(w3, source)
    validate_deployments(w3, target_w3, source)
    validate_all_safe_globals(w3, source)


# keccak256("ORACLE:SET_VALUE_ROLE"), the role Oracle.setValue checks against the
# SourceCore's access control. Hard-coded rather than read from the Oracle so
# that a wrong or unreachable oracle address cannot make this check pass by
# accident.
SET_VALUE_ROLE = Web3.keccak(text="ORACLE:SET_VALUE_ROLE")


def validate_oracle_updater(w3: Web3, source: SourceConfig):
    """Check the heartbeat key can actually write the oracle.

    Granting the role is a separate, manual multisig step, and forgetting it
    produces a bot that starts cleanly and then fails every eight hours with
    "Oracle: forbidden". Checking it here turns that into one line at deploy
    time.
    """
    config = getattr(source, "oracle_update", None)
    if config is None:
        print(f"No oracle-update section for {source.name}, skipping the role check...")
        return
    if not config.updater_private_key:
        # Declared and unresolved, which is a missing environment variable
        # rather than a source that does not write the oracle. Skipping it here
        # is how a deploy that forgot ORACLE_UPDATER_PK got a clean bill of
        # health and then failed every cycle.
        raise Exception(
            f"{source.name} declares oracle-update but its private key is "
            f"empty; set ORACLE_UPDATER_PK"
        )

    updater = Account.from_key(config.updater_private_key).address
    for deployment in source.deployments:
        core = get_contract(w3, deployment.source_core, "SourceCore")
        if not core.functions.hasRole(SET_VALUE_ROLE, updater).call():
            raise Exception(
                f"Oracle updater {updater} does not hold SET_VALUE_ROLE on "
                f"{deployment.source_core} ({source.name}/{deployment.name}). "
                f"Grant it from the Safe: "
                f"grantRole({SET_VALUE_ROLE.to_0x_hex()}, {updater})"
            )
        print(f"Oracle updater {updater} can write {deployment.name}'s oracle")


# Gas floors for the readiness check below. Deliberately not in config.json:
# they are a smoke test ("has anyone funded this account?"), not a budget, and
# the codebase already reads tuning of this kind straight from the environment
# (see operator_bot's SOURCE_RATIO_D3 and friends).
#
# The target-chain floor is the larger one because pushToSource and pushToTarget
# carry a LayerZero fee as msg.value on top of gas, quoted per call, and a run
# that cannot pay it fails at the send rather than here.
DEFAULT_SOURCE_MIN_BALANCE_WEI = 10**17  # 0.1 native
DEFAULT_TARGET_MIN_BALANCE_WEI = 2 * 10**16  # 0.02 ETH


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw else default


def check_operator_requirements(config: Config) -> list:
    """Everything the signers need before any task can succeed, as a list of
    what is still missing. Empty means ready.

    Reports rather than raises, and keeps going after each failure, because the
    caller is someone who has just been handed a fresh address and wants the
    whole list of grants to make -- not the first one alphabetically. An RPC
    that will not answer is reported as an unchecked item rather than swallowed:
    "could not check" and "granted" must never look the same.

    This covers the six roles the bot needs. validate_oracle_updater above
    checks one of them and raises; it stays as it is because `validate-config`
    is a pass/fail gate at deploy time, and this is a loop that runs until the
    grants land.
    """
    missing = []

    try:
        target_w3 = get_w3(config.target_rpc)
        target_w3.eth.block_number
    except Exception as e:
        return ["target chain RPC unreachable: {}".format(e)]

    _check_deployments(missing, config)

    for source in config.sources:
        try:
            source_w3 = get_w3(source.rpc)
            source_w3.eth.block_number
        except Exception as e:
            missing.append("{} RPC unreachable: {}".format(source.name, e))
            continue
        _check_source(missing, source_w3, target_w3, source)

    return missing


def _check_deployments(missing: list, config: Config) -> None:
    """DEPLOYMENTS has to parse, or the rebalance task fails on every run.

    It is read straight from the environment by operator_bot rather than through
    config.json, so nothing else validates it, and the failure mode is the one
    this whole function exists to prevent: the bot starts, the gate says the
    signer is ready, and then one of four tasks raises "No valid deployments
    found" every two hours forever.

    The format is comma-separated SOURCE:DEPLOYMENT. A bare source name parses
    to nothing and is rejected, which is indistinguishable from leaving the
    variable unset.
    """
    # Imported here rather than at module scope to keep config independent of
    # web3_scripts.operator_bot, and reused rather than reimplemented so this
    # cannot drift from what run_all will actually accept.
    from web3_scripts.operator_bot import parse_deployments

    raw = os.getenv("DEPLOYMENTS")
    if not raw:
        missing.append(
            "DEPLOYMENTS is unset -- the rebalance task needs comma-separated "
            "SOURCE:DEPLOYMENT pairs, e.g. {}".format(_available_pairs(config))
        )
        return
    try:
        parsed = parse_deployments(config, raw)
    except Exception as e:
        missing.append("DEPLOYMENTS could not be parsed: {}".format(e))
        return
    if not parsed:
        missing.append(
            "DEPLOYMENTS='{}' matches no deployment -- expected comma-separated "
            "SOURCE:DEPLOYMENT pairs from {}".format(raw, _available_pairs(config))
        )


def _available_pairs(config: Config) -> str:
    return ", ".join(
        "{}:{}".format(source.name, deployment.name)
        for source in config.sources
        for deployment in source.deployments
    )


def _check_source(
    missing: list, source_w3: Web3, target_w3: Web3, source: SourceConfig
) -> None:
    if not source.executor_private_key:
        missing.append(
            "{}: no executor key (set OPERATOR_PK, or TAPP_APP_ID to derive "
            "one in the TEE)".format(source.name)
        )
        return
    operator = Account.from_key(source.executor_private_key).address

    oracle_update = getattr(source, "oracle_update", None)
    updater = (
        Account.from_key(oracle_update.updater_private_key).address
        if oracle_update and oracle_update.updater_private_key
        else None
    )

    for deployment in source.deployments:
        label = "{}/{}".format(source.name, deployment.name)

        if updater:
            # Defined by the Oracle but enforced against the SourceCore's
            # access control, so grantRole goes to the SourceCore.
            _check_role(
                missing,
                source_w3,
                deployment.source_core,
                "SourceCore",
                "SET_VALUE_ROLE",
                SET_VALUE_ROLE,
                updater,
                "{} oracle updater".format(label),
            )

        _check_named_role(
            missing,
            source_w3,
            deployment.source_core,
            "SourceCore",
            "PUSH_ROLE",
            operator,
            "{} operator".format(label),
        )
        for role in ("PUSH_ROLE", "REDEEM_ROLE", "DEPOSIT_ROLE"):
            _check_named_role(
                missing,
                target_w3,
                deployment.target_core,
                "TargetCore",
                role,
                operator,
                "{} operator".format(label),
            )
        _check_claim_permission(missing, target_w3, deployment, operator, label)

    _check_balance(
        missing,
        source_w3,
        operator,
        "{} operator".format(source.name),
        _int_env("OPERATOR_MIN_BALANCE_WEI", DEFAULT_SOURCE_MIN_BALANCE_WEI),
    )
    _check_balance(
        missing,
        target_w3,
        operator,
        "target-chain operator",
        _int_env("TARGET_OPERATOR_MIN_BALANCE_WEI", DEFAULT_TARGET_MIN_BALANCE_WEI),
    )
    if updater and updater != operator:
        _check_balance(
            missing,
            source_w3,
            updater,
            "{} oracle updater".format(source.name),
            oracle_update.min_balance_wei,
        )


def _check_named_role(
    missing: list,
    w3: Web3,
    address: str,
    contract_name: str,
    role_name: str,
    holder: str,
    who: str,
) -> None:
    """Read the role's bytes32 off the contract, then check it.

    Read rather than derived from a guessed preimage: these constants are
    exposed as views precisely so callers do not have to know the string, and a
    wrong guess produces a role nobody holds -- which reads as "not granted"
    and sends the operator off to grant something that does not exist.
    """
    try:
        contract = get_contract(w3, address, contract_name)
        role = getattr(contract.functions, role_name)().call()
    except Exception as e:
        missing.append(
            "could not read {}.{} at {}: {}".format(
                contract_name, role_name, address, e
            )
        )
        return
    _check_role(missing, w3, address, contract_name, role_name, role, holder, who)


def _check_role(
    missing: list,
    w3: Web3,
    address: str,
    contract_name: str,
    role_name: str,
    role: bytes,
    holder: str,
    who: str,
) -> None:
    try:
        contract = get_contract(w3, address, contract_name)
        held = contract.functions.hasRole(role, holder).call()
    except Exception as e:
        missing.append(
            "could not check {} on {} {}: {}".format(
                role_name, contract_name, address, e
            )
        )
        return
    if not held:
        missing.append(
            "{} lacks {} on {} {} -- grant it from the Safe: "
            "grantRole({}, {})".format(
                who,
                role_name,
                contract_name,
                address,
                Web3.to_hex(role),
                holder,
            )
        )


def _check_claim_permission(
    missing: list, w3: Web3, deployment: Deployment, operator: str, label: str
) -> None:
    """TargetCore.claim is gated twice over -- a role and a single named
    claimer -- and either one is enough. Checking only the role would report a
    working deployment as broken."""
    try:
        core = get_contract(w3, deployment.target_core, "TargetCore")
        if core.functions.claimer().call() == operator:
            return
        role = core.functions.CLAIM_ROLE().call()
        if core.functions.hasRole(role, operator).call():
            return
    except Exception as e:
        missing.append(
            "could not check claim permission on TargetCore {}: {}".format(
                deployment.target_core, e
            )
        )
        return
    missing.append(
        "{} operator cannot claim on TargetCore {} -- grant CLAIM_ROLE or set it "
        "as claimer() from the Safe".format(label, deployment.target_core)
    )


def _check_balance(
    missing: list, w3: Web3, address: str, who: str, minimum: int
) -> None:
    try:
        balance = w3.eth.get_balance(address)
    except Exception as e:
        missing.append("could not read {} balance for {}: {}".format(who, address, e))
        return
    if balance < minimum:
        missing.append(
            "{} {} has {} wei, below the {} wei floor -- send it gas".format(
                who, address, balance, minimum
            )
        )


def validate_all_safe_globals(w3: Web3, source: SourceConfig):
    """
    Validate all unique SafeGlobal configs for a source chain.
    This includes the chain-level safe_global and any deployment-level overrides.
    Each unique safe address is validated only once.
    """
    # Track validated safe addresses to avoid duplicate validation
    validated_safe_addresses = set()

    # Validate chain-level safe_global
    if source.safe_global and source.safe_global.safe_address:
        validate_safe_global(w3, source.safe_global, f"{source.name} (chain-level)")
        validated_safe_addresses.add(source.safe_global.safe_address)
    else:
        print(
            f"No chain-level safe global config is set for {source.name}, skipping..."
        )

    # Validate deployment-level safe_global overrides (if different from chain-level)
    for deployment in source.deployments:
        if deployment.safe_global and deployment.safe_global.safe_address:
            if deployment.safe_global.safe_address not in validated_safe_addresses:
                validate_safe_global(
                    w3,
                    deployment.safe_global,
                    f"{source.name}/{deployment.name} (deployment override)",
                )
                validated_safe_addresses.add(deployment.safe_global.safe_address)


def validate_safe_global(w3: Web3, safe: SafeGlobal, label: str):
    """
    Validate a SafeGlobal configuration.

    Args:
        w3: Web3 instance for the source chain
        safe: SafeGlobal configuration to validate
        label: Label for error messages (e.g., "BSC (chain-level)" or "BSC/CYC (deployment override)")
    """
    if not safe:
        print(f"No safe global config is set for {label}, skipping validation...")
        return

    if not safe.safe_address:
        print(f"No safe address is set for {label}, skipping validation...")
        return

    print(f"Validating safe global {safe.safe_address} for {label}...")
    safe_contract = get_contract(w3, safe.safe_address, "Safe")

    min_version = Version("1.3.0")
    version = Version(safe_contract.functions.VERSION().call())
    if version < min_version:
        raise Exception(
            f"Safe contract version {version} is not supported for {label}, support for {min_version} or higher is required"
        )

    if safe.proposer_private_key:
        proposer_address = Account.from_key(safe.proposer_private_key).address
    else:
        proposer_address = "N/A"

    nonce = safe_contract.functions.nonce().call()
    print(f"Proposer address: {proposer_address}, version: {version}, nonce: {nonce}")

    validate_multi_send_contract_compatibility(w3, safe)

    if validate_safe_client_gateway_api_url(w3, safe, nonce):
        pass
    elif not validate_safe_transaction_api_url(safe):
        # Mask API URL which might contain credentials
        error_msg = f"Invalid safe API URL for {label}: {safe.api_url}"
        masked_error = mask_url_credentials(error_msg, safe.api_url)
        raise Exception(masked_error)


def validate_safe_owner_addresses(config: Config):
    owners = config.telegram_owner_nicknames
    if len(owners) == 0:
        print("No telegram nicknames for safe owners are set, skipping validation...")
        return

    all_zero = True
    all_non_zero = True
    for nickname, address in owners.items():
        if not address.startswith("0x") or not Web3.is_address(address):
            raise ValueError(f"Invalid address for nickname {nickname}!")
        if address != constants.ADDRESS_ZERO:
            all_zero = False
        else:
            all_non_zero = False

    if not all_zero and not all_non_zero:
        raise ValueError("All addresses must be set or all must be omitted!")

    if all_non_zero and len(owners) != len(set(owners.values())):
        raise ValueError("Duplicate owner addresses found!")


def validate_multi_send_contract_compatibility(w3: Web3, safe: SafeGlobal):
    print(
        f"Validating multi-send contract compatibility for safe {safe.safe_address}..."
    )

    safe_contract = get_contract(w3, safe.safe_address, "Safe")
    version_str = safe_contract.functions.VERSION().call()
    version = Version(version_str)

    base_version = version.base_version
    if base_version not in multi_send_contracts:
        supported_versions = ", ".join(multi_send_contracts.keys())
        raise Exception(
            f"Safe contract version {base_version} is not supported by multi-send contracts. "
            f"Supported versions: {supported_versions}"
        )

    multi_send_address = multi_send_contracts[base_version]
    # Check that the contract is deployed on the current network
    code = w3.eth.get_code(Web3.to_checksum_address(multi_send_address))
    bytecode = code.hex()
    if not bytecode or bytecode == "0x":
        raise Exception(
            f"Multi-send contract {multi_send_address} (version {base_version}) "
            f"is not deployed on the current network (chain ID: {w3.eth.chain_id})"
        )

    # Validate bytecode contains `multiSend` function selector
    MULTISEND_FUNCTION_SELECTOR = "8d80ff0a"  # multiSend(bytes)
    clean_bytecode = bytecode.lower().replace("0x", "")
    if MULTISEND_FUNCTION_SELECTOR not in clean_bytecode:
        raise Exception(
            f"Multi-send contract {multi_send_address} (version {base_version}) "
            f"does not contain multiSend function (selector: {MULTISEND_FUNCTION_SELECTOR})"
        )

    print(
        f"Multi-send contract compatibility validated ✅ (version: {base_version}, address: {multi_send_address})"
    )


def validate_safe_client_gateway_api_url(
    w3: Web3, safe: SafeGlobal, contract_nonce: int
) -> bool:
    chainId = w3.eth.chain_id
    version = None
    try:
        version = client_gateway_api.get_version(safe.api_url)
        nonce = client_gateway_api.get_nonce(safe.api_url, chainId, safe.safe_address)
    except Exception as e:
        return False
    if contract_nonce != nonce:
        raise Exception(
            f"Safe contract nonce {contract_nonce} does not match the nonce from client gateway {nonce}"
        )
    print(f"Client gateway API URL is valid (version: {version}), nonce is aligned ✅")
    return True


def validate_safe_transaction_api_url(safe: SafeGlobal):
    try:
        version = transaction_api.get_version(safe.api_url, safe.api_key)
    except Exception:
        return False
    print(f"Transaction API URL is valid (version: {version}) ✅")
    return True


def validate_rpc_url(w3: Web3, label: str):
    """
    Validate the RPC URL is an active RPC endpoint.
    """
    print(f"Validating RPC URL for {label}...")
    if w3.eth.get_block("latest").number <= 0:
        # Mask RPC URL which might contain credentials
        rpc_url = str(w3.provider.endpoint_uri)
        error_msg = f"RPC URL {rpc_url} is not valid"
        masked_error = mask_url_credentials(error_msg, rpc_url)
        raise Exception(masked_error)


def validate_deployments(source_w3: Web3, target_w3: Web3, source: SourceConfig):
    # Track unique values for validation
    names = set()
    source_cores = set()
    target_cores = set()

    for deployment in source.deployments:
        # Validate deployment.source_core is not empty
        if not deployment.source_core or deployment.source_core.strip() == "":
            raise Exception(
                f"Source core cannot be empty for deployment {deployment.name} in source {source.name}"
            )

        # Validate deployment.target_core is not empty
        if not deployment.target_core or deployment.target_core.strip() == "":
            raise Exception(
                f"Target core cannot be empty for deployment {deployment.name} in source {source.name}"
            )

        # Validate source_core != target_core
        if deployment.source_core == deployment.target_core:
            raise Exception(
                f"Source core and target core must be different for deployment {deployment.name} in source {source.name}"
            )

        # Validate that deployment.name is unique in source.deployments array
        if deployment.name in names:
            raise Exception(
                f"Deployment name '{deployment.name}' is not unique in source {source.name}"
            )
        names.add(deployment.name)

        # Validate that deployment.source_core is unique in source.deployments array
        if deployment.source_core in source_cores:
            raise Exception(
                f"Source core '{deployment.source_core}' is not unique in source {source.name}"
            )
        source_cores.add(deployment.source_core)

        # Validate that deployment.target_core is unique in source.deployments array
        if deployment.target_core in target_cores:
            raise Exception(
                f"Target core '{deployment.target_core}' is not unique in source {source.name}"
            )
        target_cores.add(deployment.target_core)

        # Validate source <-> target core addresses refer to each other
        print(
            f"Validating deployment pair {deployment.name} ({deployment.source_core} <-> {deployment.target_core}) for source {source.name}..."
        )
        validate_deployment_pair(source_w3, target_w3, deployment)


def validate_deployment_pair(source_w3: Web3, target_w3: Web3, deployment: Deployment):
    """
    Validate that the source and target core addresses are correct (refer to each other).
    """
    source_contract = get_contract(source_w3, deployment.source_core, "SourceCore")
    target_contract = get_contract(target_w3, deployment.target_core, "TargetCore")

    source_core_address_bytes32 = Web3.to_checksum_address(
        target_contract.functions.sourceCoreAddress().call()[-20:].hex()
    )
    target_core_address_bytes32 = Web3.to_checksum_address(
        source_contract.functions.targetCoreAddress().call()[-20:].hex()
    )

    if source_core_address_bytes32 != deployment.source_core:
        raise Exception(f"Source core address mismatch for {deployment.name}")
    if target_core_address_bytes32 != deployment.target_core:
        raise Exception(f"Target core address mismatch for {deployment.name}")

    validate_symbol(source_w3, target_w3, deployment)


def validate_source_helper(w3: Web3, source: SourceConfig):
    """
    Validate the source helper address is valid SourceHelper contract.
    """
    print(f"Validating source helper {source.source_core_helper}...")
    try:
        source_helper_contract = get_contract(
            w3, source.source_core_helper, "SourceHelper"
        )
        for deployment in source.deployments:
            value = source_helper_contract.functions.getSourceValue(
                deployment.source_core
            ).call()
            if value == 0:
                print_colored(
                    f"Source value is 0 for {deployment.name} on {source.name}",
                    "yellow",
                )
    except Exception as e:
        # Mask any RPC URLs or sensitive data in the error message
        error_msg = f"Source helper ({source.source_core_helper}) is not valid: {e}"
        masked_error = mask_source_sensitive_data(error_msg, source)
        raise Exception(masked_error)


def validate_target_helper(w3: Web3, config: Config):
    """
    Validate the target helper address is valid TargetHelper contract.
    """
    print(f"Validating target helper {config.target_core_helper}...")
    try:
        target_helper_contract = get_contract(
            w3, config.target_core_helper, "TargetHelper"
        )
        for source in config.sources:
            for deployment in source.deployments:
                value = target_helper_contract.functions.getTargetValue(
                    deployment.target_core
                ).call()
                if value == 0:
                    print_colored(
                        f"Target value is 0 for {deployment.name} on {source.name}",
                        "yellow",
                    )
    except Exception as e:
        # Mask any RPC URLs or sensitive data in the error message
        error_msg = f"Target helper ({config.target_core_helper}) is not valid: {e}"
        masked_error = mask_url_credentials(error_msg, config.target_rpc)
        raise Exception(masked_error)


def validate_symbol(source_w3: Web3, target_w3: Web3, deployment: Deployment):
    """
    Validate that the deployment name (from config.json) matches the symbol of the source core, target OFT, and target vault.
    """
    print(f"Validating symbol matching for {deployment.name}...")
    if deployment.name.startswith("_"):
        print(
            f"Skipping symbol validation for {deployment.name} (due to '_' prefix)..."
        )
        return

    source_contract = get_contract(source_w3, deployment.source_core, "SourceCore")

    target_oft_address = (
        get_contract(target_w3, deployment.target_core, "TargetCore")
        .functions.oft()
        .call()
    )
    target_vault_address = (
        get_contract(target_w3, deployment.target_core, "TargetCore")
        .functions.vault()
        .call()
    )

    target_oft_contract = get_contract(target_w3, target_oft_address, "SourceCore")
    target_vault_contract = get_contract(target_w3, target_vault_address, "SourceCore")

    source_core_symbol = source_contract.functions.symbol().call()
    target_oft_symbol = target_oft_contract.functions.symbol().call()
    target_vault_symbol = target_vault_contract.functions.symbol().call()

    unique_symbols = set([source_core_symbol, target_oft_symbol, target_vault_symbol])
    for symbol in unique_symbols:
        if not deployment.name in symbol:
            raise Exception(
                f"Deployment name {deployment.name} should be substring of every symbol: {', '.join(unique_symbols)}. "
                f"Source core: {source_core_symbol}, Target OFT: {target_oft_symbol}, Target Vault: {target_vault_symbol}"
            )
    print(
        f"Deployment name {deployment.name} matches every symbol: {', '.join(unique_symbols)} ✅"
    )


if __name__ == "__main__":
    import os
    import dotenv

    from tapp import inject_tee_keys

    dotenv.load_dotenv()
    inject_tee_keys()

    config = read_config(os.getcwd() + "/config.json")

    validate_config(config)
