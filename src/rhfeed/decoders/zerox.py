"""0x Protocol v2: AllowanceHolder.exec hands tokens to a Settler, which runs the route.

`exec` names what is sold. Settler's leading slippage tuple names who receives, which
token, and the minimum — unless it is all zeros, which tells Settler to skip the check
and tells us nothing. The route itself (`actions`) is a list of Settler-internal calls
and is not read here.
"""

from __future__ import annotations

from ..abi import ZERO, address, dynamic, token, uint
from ..codec import selector_of
from ..intents import Call, Intent, decodes

SETTLER_EXECUTE = selector_of("execute((address,address,uint256),bytes[],bytes32)")


@decodes("exec(address,address,uint256,address,bytes)")
def exec_(call: Call, depth: int) -> list[Intent]:
    d = call.data[4:]
    target = address(d, 3)
    intent = Intent(
        "swap",
        call.actor,
        (*call.via, target),
        token_in=token(d, 1),
        amount_in=uint(d, 2),
    )
    inner = dynamic(d, 4)
    if inner[:4] == SETTLER_EXECUTE:
        s = inner[4:]
        recipient = address(s, 0)
        if recipient != ZERO:
            intent.recipient = recipient
            intent.token_out = token(s, 1)
            intent.amount_out = uint(s, 2)
    return [intent]
