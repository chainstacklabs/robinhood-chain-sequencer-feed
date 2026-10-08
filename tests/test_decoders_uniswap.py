"""Uniswap V3/V2 router shapes and multicall, against eth_abi and the capture."""

from __future__ import annotations

import pytest
from eth_abi import decode as abi_decode

from rhfeed.codec import selector_of
from rhfeed.decoders.uniswap import v3_path_ends
from rhfeed.intents import Call, decode_call

from .helpers import a, by_selector, encode

ROUTER = a("0xcaf681a66d020601342297493863e78c959e5cb2")
WETH = a("0x0bd7d308f8e1639fab988df18a8011f41eacad73")
USDG = a("0x5fc5360d0400a0fd4f2af552add042d716f1d168")
MEME = a("0x8763becb4fb7539cbb579e933a46cf9979cc9527")
ME = a("0x" + "11" * 20)

SINGLE02 = "(address,address,uint24,address,uint256,uint256,uint160)"
SINGLE01 = "(address,address,uint24,address,uint256,uint256,uint256,uint160)"
PATH02 = "(bytes,address,uint256,uint256)"
PATH01 = "(bytes,address,uint256,uint256,uint256)"


def call(data, value=0, to=ROUTER):
    return Call(to, value, data, None, (to,))


def path(*hops):
    out = b""
    for i, hop in enumerate(hops):
        out += hop if i % 2 == 0 else hop.to_bytes(3, "big")
    return out


def test_v3_path_ends():
    assert v3_path_ends(path(WETH, 10000, MEME)) == (WETH, MEME)
    assert v3_path_ends(path(WETH, 10000, USDG, 100, MEME)) == (WETH, MEME)


def test_exact_input_single_router02():
    data = encode(
        f"exactInputSingle({SINGLE02})", [SINGLE02], [(WETH, MEME, 10000, ME, 10**16, 5, 0)]
    )
    (i,) = decode_call(call(data, value=10**16))
    assert (
        i.kind,
        i.token_in,
        i.token_out,
        i.amount_in,
        i.amount_out,
        i.recipient,
        i.exact_in,
    ) == ("swap", WETH, MEME, 10**16, 5, ME, True)


def test_exact_input_single_router01_has_deadline_slot():
    data = encode(
        f"exactInputSingle({SINGLE01})", [SINGLE01], [(WETH, MEME, 10000, ME, 99, 10**16, 5, 0)]
    )
    (i,) = decode_call(call(data))
    assert (i.amount_in, i.amount_out) == (10**16, 5)


def test_exact_output_single_is_exact_out():
    data = encode(
        f"exactOutputSingle({SINGLE02})", [SINGLE02], [(WETH, MEME, 10000, ME, 1000, 10**16, 0)]
    )
    (i,) = decode_call(call(data))
    assert (i.exact_in, i.amount_out, i.amount_in) == (False, 1000, 10**16)


def test_exact_input_path():
    data = encode(
        f"exactInput({PATH02})", [PATH02], [(path(WETH, 10000, USDG, 100, MEME), ME, 3, 4)]
    )
    (i,) = decode_call(call(data))
    assert (i.token_in, i.token_out, i.amount_in, i.amount_out) == (WETH, MEME, 3, 4)


def test_exact_output_path_is_reversed():
    data = encode(f"exactOutput({PATH02})", [PATH02], [(path(MEME, 10000, WETH), ME, 1000, 2000)])
    (i,) = decode_call(call(data))
    assert (i.token_in, i.token_out, i.amount_out, i.amount_in, i.exact_in) == (
        WETH,
        MEME,
        1000,
        2000,
        False,
    )


def test_v2_exact_tokens_for_tokens():
    sig = "swapExactTokensForTokens(uint256,uint256,address[],address,uint256)"
    data = encode(
        sig,
        ["uint256", "uint256", "address[]", "address", "uint256"],
        [10, 9, [USDG, WETH, MEME], ME, 1],
    )
    (i,) = decode_call(call(data))
    assert (i.token_in, i.token_out, i.amount_in, i.amount_out, i.recipient) == (
        USDG,
        MEME,
        10,
        9,
        ME,
    )


def test_v2_eth_in_uses_msg_value():
    sig = "swapExactETHForTokens(uint256,address[],address,uint256)"
    data = encode(sig, ["uint256", "address[]", "address", "uint256"], [9, [WETH, MEME], ME, 1])
    (i,) = decode_call(call(data, value=10**18))
    assert (i.token_in, i.amount_in, i.token_out, i.amount_out) == (None, 10**18, MEME, 9)


def test_v2_exact_out_eth():
    sig = "swapTokensForExactETH(uint256,uint256,address[],address,uint256)"
    data = encode(
        sig, ["uint256", "uint256", "address[]", "address", "uint256"], [5, 6, [MEME, WETH], ME, 1]
    )
    (i,) = decode_call(call(data))
    assert (i.token_in, i.token_out, i.amount_out, i.amount_in, i.exact_in) == (
        MEME,
        None,
        5,
        6,
        False,
    )


def test_multicall_recurses_with_the_router_in_via():
    single = encode(f"exactInputSingle({SINGLE02})", [SINGLE02], [(WETH, MEME, 10000, ME, 1, 2, 0)])
    data = encode("multicall(uint256,bytes[])", ["uint256", "bytes[]"], [1, [single, single]])
    out = decode_call(call(data))
    assert len(out) == 2 and all(i.via == (ROUTER,) and i.kind == "swap" for i in out)


def test_captured_swap_router02_calls_match_reference():
    singles = by_selector("0x04e45aaf")
    assert singles
    for tx in singles:
        (p,) = abi_decode([SINGLE02], tx.data[4:])
        (i,) = decode_call(call(tx.data, value=tx.value, to=tx.to_bytes))
        assert (i.token_in, i.token_out, i.amount_in, i.amount_out, i.recipient) == (
            a(p[0]),
            a(p[1]),
            p[4],
            p[5],
            a(p[3]),
        )
    multis = by_selector("0xac9650d8")
    assert multis
    for tx in multis:
        (blobs,) = abi_decode(["bytes[]"], tx.data[4:])
        wanted = sum(b[:4] == selector_of(f"exactInputSingle({SINGLE02})") for b in blobs)
        assert len(decode_call(call(tx.data, to=tx.to_bytes))) >= wanted


R2 = a("0x" + "22" * 20)
ACTOR = a("0x" + "33" * 20)


def test_exact_output_single_router01_has_deadline_slot():
    data = encode(
        f"exactOutputSingle({SINGLE01})", [SINGLE01], [(WETH, MEME, 500, ME, 99, 1000, 2000, 0)]
    )
    (i,) = decode_call(call(data))
    got = (i.token_in, i.token_out, i.amount_out, i.amount_in, i.recipient, i.exact_in)
    assert got == (WETH, MEME, 1000, 2000, ME, False)


def test_exact_input_path_router01_has_deadline_slot():
    data = encode(f"exactInput({PATH01})", [PATH01], [(path(WETH, 10000, MEME), ME, 99, 3, 4)])
    (i,) = decode_call(call(data))
    got = (i.token_in, i.token_out, i.amount_in, i.amount_out, i.recipient, i.exact_in)
    assert got == (WETH, MEME, 3, 4, ME, True)


def test_exact_output_path_router01_has_deadline_slot():
    data = encode(
        f"exactOutput({PATH01})", [PATH01], [(path(MEME, 10000, WETH), ME, 99, 1000, 2000)]
    )
    (i,) = decode_call(call(data))
    got = (i.token_in, i.token_out, i.amount_out, i.amount_in, i.recipient, i.exact_in)
    assert got == (WETH, MEME, 1000, 2000, ME, False)


TOK = ["uint256", "uint256", "address[]", "address", "uint256"]
ETH = ["uint256", "address[]", "address", "uint256"]
P2 = [USDG, WETH, MEME]
# (signature, types, args, value, (token_in, token_out, amount_in, amount_out, exact_in))
V2_CASES = [
    (
        "swapExactTokensForTokens(uint256,uint256,address[],address,uint256)",
        TOK,
        [10, 9, P2, R2, 1],
        0,
        (USDG, MEME, 10, 9, True),
    ),
    (
        "swapExactTokensForTokensSupportingFeeOnTransferTokens(uint256,uint256,address[],address,uint256)",
        TOK,
        [10, 9, P2, R2, 1],
        0,
        (USDG, MEME, 10, 9, True),
    ),
    (
        "swapExactTokensForETH(uint256,uint256,address[],address,uint256)",
        TOK,
        [10, 9, P2, R2, 1],
        0,
        (USDG, None, 10, 9, True),
    ),
    (
        "swapExactTokensForETHSupportingFeeOnTransferTokens(uint256,uint256,address[],address,uint256)",
        TOK,
        [10, 9, P2, R2, 1],
        0,
        (USDG, None, 10, 9, True),
    ),
    (
        "swapExactETHForTokens(uint256,address[],address,uint256)",
        ETH,
        [9, [WETH, MEME], R2, 1],
        77,
        (None, MEME, 77, 9, True),
    ),
    (
        "swapExactETHForTokensSupportingFeeOnTransferTokens(uint256,address[],address,uint256)",
        ETH,
        [9, [WETH, MEME], R2, 1],
        77,
        (None, MEME, 77, 9, True),
    ),
    (
        "swapTokensForExactTokens(uint256,uint256,address[],address,uint256)",
        TOK,
        [5, 6, P2, R2, 1],
        0,
        (USDG, MEME, 6, 5, False),
    ),
    (
        "swapTokensForExactETH(uint256,uint256,address[],address,uint256)",
        TOK,
        [5, 6, P2, R2, 1],
        0,
        (USDG, None, 6, 5, False),
    ),
    (
        "swapETHForExactTokens(uint256,address[],address,uint256)",
        ETH,
        [5, [WETH, MEME], R2, 1],
        77,
        (None, MEME, 77, 5, False),
    ),
]


@pytest.mark.parametrize(("sig", "types", "args", "value", "want"), V2_CASES)
def test_v2_every_signature(sig, types, args, value, want):
    (i,) = decode_call(call(encode(sig, types, args), value=value))
    assert (i.token_in, i.token_out, i.amount_in, i.amount_out, i.exact_in) == want
    assert i.recipient == R2


@pytest.mark.parametrize("sig", ["multicall(bytes[])", "multicall(bytes32,bytes[])"])
def test_multicall_other_variants(sig):
    single = encode(f"exactInputSingle({SINGLE02})", [SINGLE02], [(WETH, MEME, 10000, ME, 1, 2, 0)])
    if sig == "multicall(bytes[])":
        data = encode(sig, ["bytes[]"], [[single, single]])
    else:
        data = encode(sig, ["bytes32", "bytes[]"], [b"\x01" * 32, [single, single]])
    out = decode_call(call(data))
    assert len(out) == 2 and all(i.via == (ROUTER,) and i.kind == "swap" for i in out)


def test_multicall_passes_value_and_actor_through():
    sig = "swapExactETHForTokens(uint256,address[],address,uint256)"
    inner = encode(sig, ETH, [9, [WETH, MEME], ME, 1])
    data = encode("multicall(bytes[])", ["bytes[]"], [[inner]])
    outer = Call(ROUTER, 10**18, data, ACTOR, (ROUTER,))
    (i,) = decode_call(outer)
    assert (i.actor, i.via, i.amount_in, i.token_in) == (ACTOR, (ROUTER,), 10**18, None)
