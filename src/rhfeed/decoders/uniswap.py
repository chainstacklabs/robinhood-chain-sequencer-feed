"""Uniswap V3 SwapRouter / SwapRouter02, the V2 router, and their multicall wrappers.

A V3 path is `token (20) fee (3) token (20) [fee token ...]`. For exact-output calls
Uniswap reverses it, so the output token comes first — the two `*_ends` readers below
exist for that reason.
"""

from __future__ import annotations

from ..abi import bytes_array, dynamic, static_array, token, tuple_at, uint
from ..intents import Call, Intent, decode_call, decodes

SINGLE02 = "(address,address,uint24,address,uint256,uint256,uint160)"
SINGLE01 = "(address,address,uint24,address,uint256,uint256,uint256,uint160)"
PATH02 = "(bytes,address,uint256,uint256)"
PATH01 = "(bytes,address,uint256,uint256,uint256)"


def v3_path_ends(path: bytes) -> tuple[bytes | None, bytes | None]:
    """(first token, last token) of a V3 path. None for the 0xeeee… native marker."""
    if len(path) < 20:
        return None, None
    first, last = path[:20], path[-20:]
    native = b"\xee" * 20
    return (None if first == native else first), (None if last == native else last)


def _single(call: Call, slots: tuple[int, int, int], exact_in: bool) -> list[Intent]:
    """`*Single` structs are static, so the tuple sits inline right after the selector."""
    d = call.data[4:]
    recipient_i, first_i, second_i = slots
    first, second = uint(d, first_i), uint(d, second_i)
    return [
        Intent(
            "swap",
            call.actor,
            call.via,
            token_in=token(d, 0),
            token_out=token(d, 1),
            amount_in=first if exact_in else second,
            amount_out=second if exact_in else first,
            exact_in=exact_in,
            recipient=token(d, recipient_i),
        )
    ]


def _path_call(call: Call, slots: tuple[int, int, int], exact_in: bool) -> list[Intent]:
    body = tuple_at(call.data[4:], 0)
    recipient_i, first_i, second_i = slots
    a, b = v3_path_ends(dynamic(body, 0))
    first, second = uint(body, first_i), uint(body, second_i)
    return [
        Intent(
            "swap",
            call.actor,
            call.via,
            token_in=a if exact_in else b,
            token_out=b if exact_in else a,
            amount_in=first if exact_in else second,
            amount_out=second if exact_in else first,
            exact_in=exact_in,
            recipient=token(body, recipient_i),
        )
    ]


@decodes(f"exactInputSingle({SINGLE02})")
def exact_input_single(call: Call, depth: int) -> list[Intent]:
    return _single(call, (3, 4, 5), exact_in=True)


@decodes(f"exactInputSingle({SINGLE01})")
def exact_input_single_v1(call: Call, depth: int) -> list[Intent]:
    return _single(call, (3, 5, 6), exact_in=True)


@decodes(f"exactOutputSingle({SINGLE02})")
def exact_output_single(call: Call, depth: int) -> list[Intent]:
    return _single(call, (3, 4, 5), exact_in=False)


@decodes(f"exactOutputSingle({SINGLE01})")
def exact_output_single_v1(call: Call, depth: int) -> list[Intent]:
    return _single(call, (3, 5, 6), exact_in=False)


@decodes(f"exactInput({PATH02})")
def exact_input(call: Call, depth: int) -> list[Intent]:
    return _path_call(call, (1, 2, 3), exact_in=True)


@decodes(f"exactInput({PATH01})")
def exact_input_v1(call: Call, depth: int) -> list[Intent]:
    return _path_call(call, (1, 3, 4), exact_in=True)


@decodes(f"exactOutput({PATH02})")
def exact_output(call: Call, depth: int) -> list[Intent]:
    return _path_call(call, (1, 2, 3), exact_in=False)


@decodes(f"exactOutput({PATH01})")
def exact_output_v1(call: Call, depth: int) -> list[Intent]:
    return _path_call(call, (1, 3, 4), exact_in=False)


# -- V2 router ---------------------------------------------------------------- #
# (amount_in slot, amount_out slot, path slot, recipient slot, exact_in, eth_in,
# eth_out). An amount slot of None means msg.value.

_V2 = {
    "swapExactTokensForTokens(uint256,uint256,address[],address,uint256)": (
        0,
        1,
        2,
        3,
        True,
        False,
        False,
    ),
    "swapExactTokensForTokensSupportingFeeOnTransferTokens(uint256,uint256,address[],address,uint256)": (  # noqa: E501
        0,
        1,
        2,
        3,
        True,
        False,
        False,
    ),
    "swapExactTokensForETH(uint256,uint256,address[],address,uint256)": (
        0,
        1,
        2,
        3,
        True,
        False,
        True,
    ),
    "swapExactTokensForETHSupportingFeeOnTransferTokens(uint256,uint256,address[],address,uint256)": (  # noqa: E501
        0,
        1,
        2,
        3,
        True,
        False,
        True,
    ),
    "swapExactETHForTokens(uint256,address[],address,uint256)": (None, 0, 1, 2, True, True, False),
    "swapExactETHForTokensSupportingFeeOnTransferTokens(uint256,address[],address,uint256)": (
        None,
        0,
        1,
        2,
        True,
        True,
        False,
    ),
    "swapTokensForExactTokens(uint256,uint256,address[],address,uint256)": (
        1,
        0,
        2,
        3,
        False,
        False,
        False,
    ),
    "swapTokensForExactETH(uint256,uint256,address[],address,uint256)": (
        1,
        0,
        2,
        3,
        False,
        False,
        True,
    ),
    "swapETHForExactTokens(uint256,address[],address,uint256)": (None, 0, 1, 2, False, True, False),
}


def _v2(call: Call, layout: tuple) -> list[Intent]:
    in_i, out_i, path_i, to_i, exact_in, eth_in, eth_out = layout
    d = call.data[4:]
    hops = static_array(d, path_i)
    if not hops:
        return []
    return [
        Intent(
            "swap",
            call.actor,
            call.via,
            token_in=None if eth_in else token(hops[0], 0),
            token_out=None if eth_out else token(hops[-1], 0),
            amount_in=call.value if in_i is None else uint(d, in_i),
            amount_out=uint(d, out_i),
            exact_in=exact_in,
            recipient=token(d, to_i),
        )
    ]


for _signature, _layout in _V2.items():
    decodes(_signature)(lambda call, depth, _l=_layout: _v2(call, _l))


# -- multicall ---------------------------------------------------------------- #


def _multicall(call: Call, depth: int, blobs_slot: int) -> list[Intent]:
    out: list[Intent] = []
    for blob in bytes_array(call.data[4:], blobs_slot):
        # Same contract, same msg.value: multicall is `delegatecall` to self.
        out.extend(decode_call(Call(call.to, call.value, blob, call.actor, call.via), depth + 1))
    return out


@decodes("multicall(bytes[])")
def multicall(call: Call, depth: int) -> list[Intent]:
    return _multicall(call, depth, 0)


@decodes("multicall(uint256,bytes[])", "multicall(bytes32,bytes[])")
def multicall_with_guard(call: Call, depth: int) -> list[Intent]:
    return _multicall(call, depth, 1)
