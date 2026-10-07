"""Pons: the chain's busiest launchpad by fees. `0xc1120e3d` is its buy.

No signature database knows the selector, so there is no ABI; the layout was read from
captured and live calldata and confirmed against two receipts on 2026-10-07. Calls go to
Pons's deployer, `0x1cbaF24D…`, and to a second contract with the same layout. Always
`value > 0`, which is the ETH spent. Calldata is exactly ten static words:

    0 router (a Universal Router deployment)   5 recipient (the sender, on every call seen)
    1 PoolManager (the chain's V4 singleton)   6 deadline, unix seconds
    2 token bought                             7 fee (10000 or 2500)
    3 not read: 1 in older calls, ~1e22 now    8 tickSpacing (200 or 50)
    4 amountOutMin                             9 hooks, zero

Receipts: `0x3e0b4236…` delivered 1921508969472330243572335 of token `0x91bae556…` against
a minimum of 1.73e24, and `0xf64195bf…` delivered 19004999331980924246101107 of
`0x0C62aDBA…` against 1.83e25. In both the token moves PoolManager -> deployer -> caller.

Word 3 is left unread because nothing observed explains it.
"""

from __future__ import annotations

from ..abi import WORD, address, token, uint
from ..codec import addr
from ..intents import Call, Intent, decodes

V4_POOL_MANAGER = addr("0x8366a39CC670B4001A1121B8F6A443A643e40951")


@decodes("0xc1120e3d")
def buy(call: Call, depth: int) -> list[Intent]:
    d = call.data[4:]
    # Ten static words whose second is the V4 PoolManager: that pair is the fingerprint,
    # since the selector alone could belong to anything.
    if len(d) != 10 * WORD or address(d, 1) != V4_POOL_MANAGER:
        return []
    return [
        Intent(
            "swap",
            call.actor,
            call.via,
            token_in=None,
            amount_in=call.value,
            token_out=token(d, 2),
            amount_out=uint(d, 4),
            recipient=address(d, 5),
        )
    ]
