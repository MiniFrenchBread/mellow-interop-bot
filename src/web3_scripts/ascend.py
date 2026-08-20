"""Claim restaking rewards into the AscendRouter and distribute them.

Replaces a Forge script that ran the same calls. Bundling them mattered there
because each `forge script` invocation read the sender nonce independently and a
stale read got the later transaction rejected; here the starting nonce is read
once and incremented locally, so the ordering is guaranteed without bundling.
"""

try:
    from .base import *
    from .tx import send_and_confirm
except ImportError:
    from base import *
    from tx import send_and_confirm

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from eth_account import Account
from web3 import Web3
from web3.logs import DISCARD


@dataclass
class AscendResult:
    claims: List[Tuple[str, int]] = field(default_factory=list)
    distributed: int = 0
    router_balance: int = 0
    tx_hashes: List[str] = field(default_factory=list)
    dry_run: bool = False

    @property
    def total_claimed(self) -> int:
        return sum(reward for _, reward in self.claims)


def _claimed_amount(rewarder, receipt, account: str) -> int:
    """Read the reward actually transferred, from the Claimed event.

    The amount has to come from the receipt rather than from a pre-flight
    simulation: a simulated number describes a transaction that may never be
    mined, and reporting it as if it had been is what made two earlier ascend
    failures look like successes in the logs.
    """
    events = rewarder.events.Claimed().process_receipt(receipt, errors=DISCARD)
    account = Web3.to_checksum_address(account)
    for event in events:
        if Web3.to_checksum_address(event["args"]["account"]) == account:
            return event["args"]["reward"]
    return 0


def run_ascend(
    w3,
    router: str,
    rewarders: List[str],
    private_key: str,
    claim_account: Optional[str] = None,
    dry_run: bool = False,
    tx: dict = None,
) -> AscendResult:
    router = Web3.to_checksum_address(router)
    claim_account = Web3.to_checksum_address(claim_account or router)
    sender = Web3.to_checksum_address(Account.from_key(private_key).address)

    tx_options = tx or {}

    result = AscendResult(dry_run=dry_run)
    router_contract = get_contract(w3, router, "AscendRouter")

    if dry_run:
        for address in rewarders:
            rewarder = get_contract(w3, address, "Rewarder")
            reward = rewarder.functions.claim(claim_account).call({"from": sender})
            result.claims.append((address, reward))
            print("Claimable from {}: {}".format(address, reward))
        result.router_balance = w3.eth.get_balance(router)
        print(
            "Router balance {}. Would distribute: {}".format(
                result.router_balance, result.router_balance > 0
            )
        )
        return result

    # One read, then local increments: every claim is signed before the previous
    # one is mined, so re-reading the nonce per call would hand back a stale value.
    nonce = w3.eth.get_transaction_count(sender, "pending")

    for address in rewarders:
        rewarder = get_contract(w3, address, "Rewarder")
        outcome = send_and_confirm(
            rewarder.functions.claim(claim_account),
            0,
            private_key,
            w3=w3,
            nonce=nonce,
            label="claim {}".format(address),
            **tx_options,
        )
        nonce += 1
        reward = _claimed_amount(rewarder, outcome.receipt, claim_account)
        result.claims.append((address, reward))
        result.tx_hashes.append(outcome.tx_hash)
        print_colored("Claimed {} from {}".format(reward, address), "green")

    result.router_balance = w3.eth.get_balance(router)
    if result.router_balance == 0:
        print_colored("Router balance is zero, nothing to distribute", "yellow")
        return result

    outcome = send_and_confirm(
        router_contract.functions.distribute(),
        0,
        private_key,
        w3=w3,
        nonce=nonce,
        label="distribute",
        **tx_options,
    )
    result.tx_hashes.append(outcome.tx_hash)
    events = router_contract.events.Distributed().process_receipt(
        outcome.receipt, errors=DISCARD
    )
    result.distributed = sum(event["args"]["amount"] for event in events)

    remaining = w3.eth.get_balance(router)
    print_colored(
        "Distributed {} across {} recipient(s). Router balance now {}".format(
            result.distributed, len(events), remaining
        ),
        "green",
    )
    if remaining != 0:
        print_colored(
            "Router still holds {} after distribute".format(remaining), "yellow"
        )
    return result
