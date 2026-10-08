"""Relay's router: a solver's fill and a wallet's sell, one intent each.

Relay is how FOMO trades reach this chain. A buy is paid on Solana and *filled* here by
a solver spending its own money through `permit2TransferAndMulticall`; the wallet
appears only as `refundTo`. A sell runs inside the wallet's own 4337 operation through
`transferAndMulticall`, and the proceeds go to Relay, not back to the wallet.

Both carry a `calls` array of [approve, AllowanceHolder.exec, executor.<sweep|deposit>].
The swap inside is decoded through the table like any other, but what comes out of it
is folded into the one Relay intent rather than returned beside it — a consumer counting
trades should see one.

The executor's calls are unverified contracts read by shape: four dynamic arrays, the
first of them `address[] tokens`. Confirmed 2026-10-07 against receipts:
- fill sweep `tokens[0]` is the delivered token and `refundTo` is the wallet delivered to
  (`0x85037993…` log #17: token `0x69984Ad3…` to `0x005DC591…`; `0xfb6e65e9…` log #8:
  token `0xc2362AfF…` to `0xdE0600d6…`);
- sell deposit `tokens[0]` is what Relay receives (`0x28e8a4cc…`: USDG reaches the
  depository at log #14, the user's token leaves the wallet at log #2);
- Relay appends one 32-byte word after the ABI body of both router calls, and that word
  is the order id (on the sell it equals the id in the executor's depository call). The
  `requestId` argument is something else and is not read.
"""

from __future__ import annotations

from ..abi import WORD, address, dynamic, static_array, tuple_array, tuple_at, uint
from ..intents import Call, Intent, decode_call, decodes, inner

CALLS = "(address,bool,uint256,bytes)[]"
PERMIT = "((address,uint256)[],uint256,uint256)"

#: Executor entry points seen under the router. Read by shape, never dispatched.
EXECUTOR_SWEEP = bytes.fromhex("9bb43718")
EXECUTOR_DEPOSIT = bytes.fromhex("73b7bb2f")


def _first_token(executor_data: bytes) -> bytes | None:
    """First address of the first array in an executor call."""
    tokens = static_array(executor_data[4:], 0)
    return address(tokens[0], 0) if tokens else None


def _walk(call: Call, calls: list[bytes], depth: int) -> tuple[list[Intent], bytes | None]:
    """Decode the inner calls; return (swap intents found, token named by the executor)."""
    found: list[Intent] = []
    executor_token = None
    for c in calls:
        target, value, data = address(c, 0), uint(c, 2), dynamic(c, 3)
        if data[:4] in (EXECUTOR_SWEEP, EXECUTOR_DEPOSIT):
            executor_token = _first_token(data) or executor_token
            continue
        found.extend(decode_call(inner(call, target, value, data), depth + 1))
    return found, executor_token


def _trailing_word(d: bytes, last_slot: int) -> bytes | None:
    """The 32-byte word Relay appends after the ABI body, or None when there is none.

    `last_slot` holds the offset of the final dynamic argument; the body ends where
    that argument's padded bytes end. Exactly one word past that is the request id.
    """
    off = uint(d, last_slot)
    n = uint(d[off:], 0)  # raises Malformed via word() if off is out of range
    end = off + WORD + -(-n // WORD) * WORD
    return d[end : end + WORD] if len(d) - end == WORD else None


def _swap_out(found: list[Intent]) -> tuple[bytes | None, int | None]:
    """Output named by an inner swap's slippage check, when there was one."""
    for i in found:
        if i.kind == "swap" and i.token_out is not None:
            return i.token_out, i.amount_out
    return None, None


@decodes(f"permit2TransferAndMulticall(address,{PERMIT},{CALLS},address,address,bytes,bytes)")
def fill(call: Call, depth: int) -> list[Intent]:
    d = call.data[4:]
    permit = tuple_at(d, 1)
    permitted = static_array(permit, 0, width=2)
    if not permitted:
        return []
    wallet = address(d, 3)
    found, executor_token = _walk(call, tuple_array(d, 2), depth)
    token_out, amount_out = _swap_out(found)
    return [
        Intent(
            "relay_fill",
            wallet,
            call.via,
            payer=address(d, 0),
            token_in=address(permitted[0], 0),
            token_out=executor_token or token_out,
            amount_in=uint(permitted[0], 1),
            amount_out=amount_out,
            recipient=wallet,
            order_id=_trailing_word(d, 6),
        )
    ]


@decodes(f"transferAndMulticall(address[],uint256[],{CALLS},address,address,bytes)")
def sell(call: Call, depth: int) -> list[Intent]:
    d = call.data[4:]
    tokens, amounts = static_array(d, 0), static_array(d, 1)
    if not tokens or not amounts:
        return []
    found, executor_token = _walk(call, tuple_array(d, 2), depth)
    token_out, amount_out = _swap_out(found)
    return [
        Intent(
            "relay_sell",
            call.actor,
            call.via,
            token_in=address(tokens[0], 0),
            token_out=executor_token or token_out,
            amount_in=uint(amounts[0], 0),
            amount_out=amount_out,
            order_id=_trailing_word(d, 5),
        )
    ]
