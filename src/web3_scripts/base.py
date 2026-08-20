import json
from web3 import Web3
from web3.contract import Contract
from eth_account import Account
from web3.middleware import ExtraDataToPOAMiddleware

try:
    from .tx import (
        DEFAULT_FEE_CAP_GWEI,
        DEFAULT_MAX_ATTEMPTS,
        DEFAULT_RECEIPT_TIMEOUT,
        NonceAlreadyUsed,
        TxNotConfirmed,
        TxOutcome,
        TxReverted,
        send_and_confirm,
    )
except ImportError:
    from tx import (
        DEFAULT_FEE_CAP_GWEI,
        DEFAULT_MAX_ATTEMPTS,
        DEFAULT_RECEIPT_TIMEOUT,
        NonceAlreadyUsed,
        TxNotConfirmed,
        TxOutcome,
        TxReverted,
        send_and_confirm,
    )


BLOCK_GAP = 10000
SECURE_INTERVAL = 15
ORACLE_VALUE_TOLERANCE = 10**9  # 1 gwei


def is_oracle_value_incorrect(
    oracle_value: int, actual_value: int, tolerance: int = ORACLE_VALUE_TOLERANCE
) -> bool:
    """Returns True if oracle_value deviates from actual_value beyond tolerance."""
    return abs(oracle_value - actual_value) > tolerance


def add_color(text: str, color="yellow") -> str:
    if color == "red":
        text = "\033[31m" + text + "\033[0m"
    elif color == "green":
        text = "\033[32m" + text + "\033[0m"
    elif color == "yellow":
        text = "\033[33m" + text + "\033[0m"
    return text


def print_colored(text: str, color="yellow") -> str:
    print(add_color(text, color))


def get_w3(rpc: str) -> Web3:
    w3 = Web3(Web3.HTTPProvider(rpc))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    return w3


def get_contract(w3: Web3, address: str, name: str) -> Contract:
    with open("./abi/{}.json".format(name), "r") as f:
        abi = json.load(f)
        return w3.eth.contract(address=w3.to_checksum_address(address), abi=abi)


def execute(
    contractFunction,
    value: int,
    operator_pk: str,
    nonce: int = None,
    receipt_timeout: float = DEFAULT_RECEIPT_TIMEOUT,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    fee_cap_gwei: int = DEFAULT_FEE_CAP_GWEI,
    label: str = "",
) -> TxOutcome:
    return send_and_confirm(
        contractFunction,
        value,
        operator_pk,
        nonce=nonce,
        receipt_timeout=receipt_timeout,
        max_attempts=max_attempts,
        fee_cap_gwei=fee_cap_gwei,
        label=label,
    )


def get_block_before_timestamp(w3: Web3, timestamp: int) -> int:
    latest_block = w3.eth.get_block("latest")
    from_block = w3.eth.get_block(latest_block.number - BLOCK_GAP)
    timespan = latest_block.timestamp - from_block.timestamp
    while timespan == 0:
        from_block = w3.eth.get_block(from_block.number - BLOCK_GAP)
        timespan = latest_block.timestamp - from_block.timestamp
    seconds_per_block = timespan / (latest_block.number - from_block.number)
    block_number_estimate = latest_block.number - int(
        (latest_block.timestamp - timestamp) / seconds_per_block
    )
    block_number_estimate = min(latest_block.number, block_number_estimate)
    block = w3.eth.get_block(block_identifier=block_number_estimate)
    if block.timestamp > timestamp:
        while block.timestamp > timestamp:
            prev_block = w3.eth.get_block(block.number - 1)
            if prev_block.timestamp <= timestamp:
                return prev_block.number
            block = prev_block
    else:
        while block.timestamp <= timestamp:
            if block.number == latest_block.number:
                return block.number
            next_block = w3.eth.get_block(block.number + 1)
            if next_block.timestamp > timestamp:
                return block.number
            block = next_block
    raise Exception("Block not found for timestamp {}".format(timestamp))
