"""Uniswap Universal Router: `execute(commands, inputs[, deadline])`.

One byte per command, one ABI-encoded blob per command. Only the swap commands are
read; wraps, sweeps, permits and NFT commands are skipped by position. `V4_SWAP` nests
another (actions, params) pair with the same shape, read the same way.

A `Malformed` raised while reading one command voids the whole `execute` and yields
nothing. That matches the chain for an unflagged failing command (the router reverts the
whole call) and errs toward silence for routers with a different command table, at the
cost of under-reporting swaps beside an allow-revert (0x80) command that failed or beside
an unknown layout.

Constants from universal-router `Commands.sol` (main @ 543e1a19d6e21e31ced2512eec5792b50f13a0ba)
and v4-periphery `Actions.sol` (main @ 9969eec44cfdf07e24b41de47f40276a58401976), read
2026-10-07. The command byte is masked with 0x7f; 0x80 is the allow-revert flag.
"""

from __future__ import annotations

from ..abi import (
    Malformed,
    address,
    bytes_array,
    dynamic,
    static_array,
    token,
    tuple_array,
    tuple_at,
    uint,
)
from ..intents import Call, Intent, decodes
from .uniswap import v3_path_ends

COMMAND_MASK = 0x7F
V3_SWAP_EXACT_IN, V3_SWAP_EXACT_OUT = 0x00, 0x01
V2_SWAP_EXACT_IN, V2_SWAP_EXACT_OUT = 0x08, 0x09
V4_SWAP = 0x10

SWAP_EXACT_IN_SINGLE, SWAP_EXACT_IN = 0x06, 0x07
SWAP_EXACT_OUT_SINGLE, SWAP_EXACT_OUT = 0x08, 0x09

#: Recipient sentinels in router inputs.
MSG_SENDER = bytes(19) + b"\x01"
ADDRESS_THIS = bytes(19) + b"\x02"


def _recipient(raw: bytes, call: Call) -> bytes | None:
    if raw == MSG_SENDER:
        return call.actor
    if raw == ADDRESS_THIS:
        return call.to
    return raw


def _v3(call: Call, p: bytes, exact_in: bool) -> Intent:
    first, second = uint(p, 1), uint(p, 2)
    a, b = v3_path_ends(dynamic(p, 3))
    return Intent(
        "swap",
        call.actor,
        call.via,
        token_in=a if exact_in else b,
        token_out=b if exact_in else a,
        amount_in=first if exact_in else second,
        amount_out=second if exact_in else first,
        exact_in=exact_in,
        recipient=_recipient(address(p, 0), call),
    )


def _v2(call: Call, p: bytes, exact_in: bool) -> Intent | None:
    hops = static_array(p, 3)
    if not hops:
        return None
    first, second = uint(p, 1), uint(p, 2)
    return Intent(
        "swap",
        call.actor,
        call.via,
        token_in=token(hops[0], 0),
        token_out=token(hops[-1], 0),
        amount_in=first if exact_in else second,
        amount_out=second if exact_in else first,
        exact_in=exact_in,
        recipient=_recipient(address(p, 0), call),
    )


def _v4_single(call: Call, p: bytes, exact_in: bool) -> Intent:
    # (poolKey{currency0,currency1,fee,tickSpacing,hooks}, zeroForOne, amount, amount, hookData)
    body = tuple_at(p, 0)
    c0, c1 = token(body, 0), token(body, 1)
    zero_for_one = uint(body, 5) != 0
    first, second = uint(body, 6), uint(body, 7)
    return Intent(
        "swap",
        call.actor,
        call.via,
        token_in=c0 if zero_for_one else c1,
        token_out=c1 if zero_for_one else c0,
        amount_in=first if exact_in else second,
        amount_out=second if exact_in else first,
        exact_in=exact_in,
    )


def _v4_multi(call: Call, p: bytes, exact_in: bool) -> Intent | None:
    # (currency, PathKey[] path, amount, amount). Exact-in names the input currency and
    # the path ends at the output; exact-out names the output and path[0] is the input.
    body = tuple_at(p, 0)
    named = token(body, 0)
    path = tuple_array(body, 1)
    if not path:
        return None
    first, second = uint(body, 2), uint(body, 3)
    if exact_in:
        token_in, token_out = named, token(path[-1], 0)
    else:
        token_in, token_out = token(path[0], 0), named
    return Intent(
        "swap",
        call.actor,
        call.via,
        token_in=token_in,
        token_out=token_out,
        amount_in=first if exact_in else second,
        amount_out=second if exact_in else first,
        exact_in=exact_in,
    )


def _v4(call: Call, p: bytes) -> list[Intent]:
    actions = dynamic(p, 0)
    params = bytes_array(p, 1)
    if len(params) < len(actions):
        raise Malformed(f"{len(actions)} actions but {len(params)} params")
    out: list[Intent] = []
    for action, param in zip(actions, params, strict=False):
        if action == SWAP_EXACT_IN_SINGLE:
            out.append(_v4_single(call, param, True))
        elif action == SWAP_EXACT_OUT_SINGLE:
            out.append(_v4_single(call, param, False))
        elif action == SWAP_EXACT_IN:
            intent = _v4_multi(call, param, True)
            if intent:
                out.append(intent)
        elif action == SWAP_EXACT_OUT:
            intent = _v4_multi(call, param, False)
            if intent:
                out.append(intent)
    return out


def _execute(call: Call, commands: bytes, inputs: list[bytes]) -> list[Intent]:
    if len(inputs) < len(commands):
        raise Malformed(f"{len(commands)} commands but {len(inputs)} inputs")
    out: list[Intent] = []
    for command, p in zip(commands, inputs, strict=False):
        kind = command & COMMAND_MASK
        if kind == V3_SWAP_EXACT_IN:
            out.append(_v3(call, p, True))
        elif kind == V3_SWAP_EXACT_OUT:
            out.append(_v3(call, p, False))
        elif kind in (V2_SWAP_EXACT_IN, V2_SWAP_EXACT_OUT):
            intent = _v2(call, p, kind == V2_SWAP_EXACT_IN)
            if intent:
                out.append(intent)
        elif kind == V4_SWAP:
            out.extend(_v4(call, p))
    return out


@decodes("execute(bytes,bytes[],uint256)", "execute(bytes,bytes[])")
def execute(call: Call, depth: int) -> list[Intent]:
    d = call.data[4:]
    return _execute(call, dynamic(d, 0), bytes_array(d, 1))
