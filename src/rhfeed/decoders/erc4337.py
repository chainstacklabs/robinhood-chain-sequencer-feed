"""ERC-4337 bundles and the account calls inside them.

The transaction sender is a bundler and tells you nothing. Each operation's `sender` is
the wallet, and becomes `actor` for everything decoded under it; its `callData` is a
call *on the wallet*: `execute`, or one of the `executeBatch` shapes — Simple7702Account's
tuple array, SimpleAccount v0.7's three parallel arrays, and the older two-array form
without values. Those carry the calls the user actually wanted.

v0.7 and v0.8 share the packed struct and the selector. v0.6 is unpacked, differently
keyed, and not used by FOMO, so it is not here.
"""

from __future__ import annotations

from ..abi import Malformed, address, bytes_array, dynamic, static_array, tuple_array, uint
from ..intents import Call, Intent, decode_call, decodes, inner

OP = "(address,uint256,bytes,bytes,bytes32,uint256,bytes32,bytes,bytes)"


@decodes(f"handleOps({OP}[],address)")
def handle_ops(call: Call, depth: int) -> list[Intent]:
    out: list[Intent] = []
    for op in tuple_array(call.data[4:], 0):
        wallet = address(op, 0)
        out.extend(decode_call(inner(call, wallet, 0, dynamic(op, 3), actor=wallet), depth + 1))
    return out


@decodes("execute(address,uint256,bytes)")
def execute(call: Call, depth: int) -> list[Intent]:
    d = call.data[4:]
    return decode_call(inner(call, address(d, 0), uint(d, 1), dynamic(d, 2)), depth + 1)


@decodes("executeBatch((address,uint256,bytes)[])")
def execute_batch(call: Call, depth: int) -> list[Intent]:
    out: list[Intent] = []
    for c in tuple_array(call.data[4:], 0):
        out.extend(decode_call(inner(call, address(c, 0), uint(c, 1), dynamic(c, 2)), depth + 1))
    return out


@decodes("executeBatch(address[],uint256[],bytes[])")
def execute_batch_parallel(call: Call, depth: int) -> list[Intent]:
    d = call.data[4:]
    targets, values, datas = static_array(d, 0), static_array(d, 1), bytes_array(d, 2)
    if not len(targets) == len(values) == len(datas):
        raise Malformed("executeBatch arrays differ in length")
    out: list[Intent] = []
    for t, v, data in zip(targets, values, datas, strict=True):
        out.extend(decode_call(inner(call, address(t, 0), uint(v, 0), data), depth + 1))
    return out


@decodes("executeBatch(address[],bytes[])")
def execute_batch_no_values(call: Call, depth: int) -> list[Intent]:
    d = call.data[4:]
    targets, datas = static_array(d, 0), bytes_array(d, 1)
    if len(targets) != len(datas):
        raise Malformed("executeBatch arrays differ in length")
    out: list[Intent] = []
    for t, data in zip(targets, datas, strict=True):
        out.extend(decode_call(inner(call, address(t, 0), 0, data), depth + 1))
    return out
