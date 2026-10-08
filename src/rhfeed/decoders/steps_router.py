"""An unnamed aggregator router: `swap(steps[], recipient, amountIn, amountOutMin[, deadline])`.

Three deployments on Robinhood Chain (`0x65050a…`, `0xe49291…` on the newer ABI; `0x5b8d85…`
on the older) carry about a seventh of the chain's transactions. The source is not
published, so the step layout is read from captured calls: slot 1 is the input token
(it is WETH exactly when msg.value is non-zero and equals amountIn), slot 2 the output.
Confirmed against receipts 2026-10-07 (22 captured calls, e.g. 0xd23ff04a…, 0x425ab8e2…):
the first Transfer out of the sender or router is of slot 1's token, the Transfer to the
recipient is of slot 2's.
"""

from __future__ import annotations

from ..abi import token, tuple_array, uint
from ..intents import Call, Intent, decodes

STEP2 = "(uint8,address,address,address,uint24,int24,address,bytes,address,bytes32)"
STEP1 = "(uint8,address,address,address,uint24,int24,address,bytes)"


def _swap(call: Call) -> list[Intent]:
    d = call.data[4:]
    steps = tuple_array(d, 0)
    if not steps:
        return []
    return [
        Intent(
            "swap",
            call.actor,
            call.via,
            token_in=token(steps[0], 1),
            token_out=token(steps[-1], 2),
            amount_in=uint(d, 2),
            amount_out=uint(d, 3),
            # The zero address means the caller, so None resolves to the actor downstream.
            recipient=token(d, 1),
        )
    ]


@decodes(
    f"swap({STEP2}[],address,uint256,uint256,uint256)",
    f"swap({STEP1}[],address,uint256,uint256)",
)
def swap(call: Call, depth: int) -> list[Intent]:
    return _swap(call)
