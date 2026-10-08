"""ERC-20 transfers and approvals, and Permit2's approve.

The token is the contract called, except on Permit2 where it is the first argument.
"""

from __future__ import annotations

from ..abi import address, uint
from ..intents import Call, Intent, decodes


@decodes("transfer(address,uint256)")
def transfer(call: Call, depth: int) -> list[Intent]:
    d = call.data[4:]
    return [
        Intent(
            "transfer",
            call.actor,
            call.via,
            token_in=call.to,
            amount_in=uint(d, 1),
            recipient=address(d, 0),
        )
    ]


@decodes("transferFrom(address,address,uint256)")
def transfer_from(call: Call, depth: int) -> list[Intent]:
    d = call.data[4:]
    return [
        Intent(
            "transfer",
            call.actor,
            call.via,
            payer=address(d, 0),
            token_in=call.to,
            amount_in=uint(d, 2),
            recipient=address(d, 1),
        )
    ]


@decodes("approve(address,uint256)")
def approve(call: Call, depth: int) -> list[Intent]:
    d = call.data[4:]
    return [
        Intent(
            "approve",
            call.actor,
            call.via,
            token_in=call.to,
            amount_in=uint(d, 1),
            recipient=address(d, 0),
        )
    ]


@decodes("approve(address,address,uint160,uint48)")
def permit2_approve(call: Call, depth: int) -> list[Intent]:
    d = call.data[4:]
    return [
        Intent(
            "approve",
            call.actor,
            call.via,
            token_in=address(d, 0),
            amount_in=uint(d, 2),
            recipient=address(d, 1),
        )
    ]
