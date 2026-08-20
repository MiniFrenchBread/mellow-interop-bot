import json
import time
from web3 import Web3
from web3.contract import Contract
from web3.providers.rpc import HTTPProvider
from eth_account import Account
from web3.middleware import ExtraDataToPOAMiddleware

try:
    from .tx import (
        DEFAULT_FEE_BUMP_PERCENT,
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
    # Loaded outside its package -- as a standalone script, or by file path, which
    # is how the config validator and some tests pull this module in. Neither puts
    # this directory on the path, so the sibling import needs it added first.
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    from tx import (
        DEFAULT_FEE_BUMP_PERCENT,
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


RETRY_ATTEMPTS_PER_ENDPOINT = 3
RETRY_DELAY_SECONDS = 0.25
RETRY_BACKOFF_MULTIPLIER = 2


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


class FallbackHTTPProvider(HTTPProvider):
    """HTTPProvider that fails over across multiple endpoints per request.

    Each endpoint is attempted ``attempts_per_endpoint`` times before moving on
    to the next one. Between retries of the SAME endpoint the delay starts at
    ``retry_delay_seconds`` and is multiplied by ``backoff_multiplier`` after
    each wait; the backoff resets when failing over to the next endpoint, which
    happens immediately (no delay on switch). For two endpoints with the
    defaults (3 attempts, 0.25s, x2) the schedule is:
    try 0 -> 0.25s -> try 0 -> 0.5s -> try 0 -> try 1 -> 0.25s -> try 1 -> 0.5s -> try 1.
    """

    def __init__(
        self,
        endpoint_uris,
        attempts_per_endpoint=RETRY_ATTEMPTS_PER_ENDPOINT,
        retry_delay_seconds=RETRY_DELAY_SECONDS,
        backoff_multiplier=RETRY_BACKOFF_MULTIPLIER,
        **kwargs,
    ):
        uris = [uri.strip() for uri in endpoint_uris if uri and uri.strip()]
        if not uris:
            raise ValueError("FallbackHTTPProvider requires at least one endpoint")
        self._attempts_per_endpoint = attempts_per_endpoint
        self._retry_delay_seconds = retry_delay_seconds
        self._backoff_multiplier = backoff_multiplier
        # Disable web3's built-in per-request retry so this class fully controls
        # the attempt/backoff schedule below.
        kwargs.setdefault("exception_retry_configuration", None)
        super().__init__(uris[0], **kwargs)
        self._providers = [HTTPProvider(uri, **kwargs) for uri in uris]

    def make_request(self, method, params):
        last_error = None
        for index, provider in enumerate(self._providers):
            delay = self._retry_delay_seconds
            for attempt in range(self._attempts_per_endpoint):
                if attempt > 0:
                    time.sleep(delay)
                    delay *= self._backoff_multiplier
                # Log every attempt except the very first (RPC index only, never
                # the URL, which may contain an API key).
                if index > 0 or attempt > 0:
                    print_colored(
                        f"Retrying with RPC #{index}, attempt {attempt + 1}...",
                        "yellow",
                    )
                try:
                    response = provider.make_request(method, params)
                    self.endpoint_uri = provider.endpoint_uri
                    return response
                except Exception as e:
                    last_error = e
        raise ConnectionError(
            f"All {len(self._providers)} RPC endpoint(s) failed for method {method} "
            f"after {self._attempts_per_endpoint} attempt(s) each"
        ) from last_error


def get_w3(rpc: str) -> Web3:
    endpoints = [uri.strip() for uri in rpc.split(",") if uri.strip()]
    if not endpoints:
        raise ValueError("No RPC endpoint provided")
    provider = (
        Web3.HTTPProvider(endpoints[0])
        if len(endpoints) == 1
        else FallbackHTTPProvider(endpoints)
    )
    w3 = Web3(provider)
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
    fee_bump_percent: int = DEFAULT_FEE_BUMP_PERCENT,
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
        fee_bump_percent=fee_bump_percent,
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
