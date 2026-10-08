"""Universal Router commands, including V4 swap actions, against eth_abi and the capture."""

from __future__ import annotations

import pytest
from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_abi.exceptions import DecodingError

from rhfeed.abi import Malformed
from rhfeed.decoders import universal_router as decoder
from rhfeed.intents import Call, decode_call

from .helpers import a, by_selector, encode

UR = a("0x6c12fe29eea504b26c8ae60227e2cb5a2c78c778")
WETH = a("0x0bd7d308f8e1639fab988df18a8011f41eacad73")
MEME = a("0x8763becb4fb7539cbb579e933a46cf9979cc9527")
ME = a("0x" + "11" * 20)
SENDER = bytes(19) + b"\x01"
ROUTER_SENTINEL = bytes(19) + b"\x02"
ACTOR = a("0x" + "55" * 20)
OTHER = a("0x" + "44" * 20)

V3_IN = ["address", "uint256", "uint256", "bytes", "bool"]
V2_IN = ["address", "uint256", "uint256", "address[]", "bool"]
POOLKEY = "(address,address,uint24,int24,address)"
SINGLE = f"({POOLKEY},bool,uint128,uint128,bytes)"
PATHKEY = "(address,uint24,int24,address,bytes)"
MULTI = f"(address,{PATHKEY}[],uint128,uint128)"


def execute(commands: bytes, inputs: list[bytes], value=0, actor=None):
    data = encode(
        "execute(bytes,bytes[],uint256)", ["bytes", "bytes[]", "uint256"], [commands, inputs, 1]
    )
    return Call(UR, value, data, actor, (UR,))


def v3_path(*hops):
    out = b""
    for i, hop in enumerate(hops):
        out += hop if i % 2 == 0 else hop.to_bytes(3, "big")
    return out


def test_v3_exact_in_resolves_msg_sender_to_actor():
    inputs = [abi_encode(V3_IN, [SENDER, 10, 9, v3_path(WETH, 10000, MEME), True])]
    (i,) = decode_call(execute(b"\x00", inputs))
    assert (i.token_in, i.token_out, i.amount_in, i.amount_out, i.recipient) == (
        WETH,
        MEME,
        10,
        9,
        None,
    )


def test_v3_exact_out_reverses_path():
    inputs = [abi_encode(V3_IN, [ME, 100, 200, v3_path(MEME, 10000, WETH), True])]
    (i,) = decode_call(execute(b"\x01", inputs))
    assert (i.token_in, i.token_out, i.amount_out, i.amount_in, i.exact_in, i.recipient) == (
        WETH,
        MEME,
        100,
        200,
        False,
        ME,
    )


def test_v2_commands():
    inputs = [
        abi_encode(V2_IN, [ME, 10, 9, [WETH, MEME], True]),
        abi_encode(V2_IN, [ME, 100, 200, [WETH, MEME], True]),
    ]
    one, two = decode_call(execute(b"\x08\x09", inputs))
    assert (one.token_in, one.token_out, one.amount_in, one.amount_out) == (WETH, MEME, 10, 9)
    assert (two.token_in, two.token_out, two.amount_out, two.amount_in, two.exact_in) == (
        WETH,
        MEME,
        100,
        200,
        False,
    )


def test_flag_bit_and_unknown_commands_are_skipped():
    inputs = [b"\x00" * 64, abi_encode(V3_IN, [ME, 1, 2, v3_path(WETH, 500, MEME), True]), b""]
    out = decode_call(execute(b"\x0b\x80\x0c", inputs))  # WRAP_ETH, V3_SWAP_EXACT_IN|flag, UNWRAP
    assert len(out) == 1 and out[0].amount_in == 1


def test_commands_without_inputs_are_malformed():
    with pytest.raises(Malformed):
        decoder.execute(
            execute(b"\x00\x00", [abi_encode(V3_IN, [ME, 1, 2, v3_path(WETH, 500, MEME), True])]), 0
        )
    assert (
        decode_call(
            execute(b"\x00\x00", [abi_encode(V3_IN, [ME, 1, 2, v3_path(WETH, 500, MEME), True])])
        )
        == []
    )


def test_v4_exact_in_single():
    params = abi_encode(
        [SINGLE], [((bytes(20), MEME, 10000, 200, bytes(20)), True, 10**16, 5, b"")]
    )
    v4 = abi_encode(["bytes", "bytes[]"], [b"\x06\x0c\x0f", [params, b"\x00" * 64, b"\x00" * 64]])
    (i,) = decode_call(execute(b"\x10", [v4], value=10**16))
    assert (i.token_in, i.token_out, i.amount_in, i.amount_out, i.exact_in) == (
        None,
        MEME,
        10**16,
        5,
        True,
    )


def test_v4_exact_in_single_one_for_zero():
    params = abi_encode([SINGLE], [((bytes(20), MEME, 10000, 200, bytes(20)), False, 7, 6, b"")])
    v4 = abi_encode(["bytes", "bytes[]"], [b"\x06", [params]])
    (i,) = decode_call(execute(b"\x10", [v4]))
    assert (i.token_in, i.token_out) == (MEME, None)


def test_v4_exact_in_multi_hop():
    params = abi_encode(
        [MULTI],
        [(WETH, [(MEME, 3000, 60, bytes(20), b""), (bytes(20), 500, 10, bytes(20), b"")], 11, 10)],
    )
    v4 = abi_encode(["bytes", "bytes[]"], [b"\x07", [params]])
    (i,) = decode_call(execute(b"\x10", [v4]))
    assert (i.token_in, i.token_out, i.amount_in, i.amount_out) == (WETH, None, 11, 10)


def test_v4_exact_out_shapes():
    single = abi_encode([SINGLE], [((bytes(20), MEME, 10000, 200, bytes(20)), True, 100, 200, b"")])
    path = [(WETH, 3000, 60, bytes(20), b""), (OTHER, 500, 10, bytes(20), b"")]
    multi = abi_encode([MULTI], [(MEME, path, 100, 200)])
    v4 = abi_encode(["bytes", "bytes[]"], [b"\x08\x09", [single, multi]])
    one, two = decode_call(execute(b"\x10", [v4]))
    assert (one.token_in, one.token_out, one.amount_out, one.amount_in, one.exact_in) == (
        None,
        MEME,
        100,
        200,
        False,
    )
    assert (two.token_in, two.token_out, two.amount_out, two.amount_in, two.exact_in) == (
        WETH,
        MEME,
        100,
        200,
        False,
    )


def test_captured_universal_router_calls_match_reference():
    txs = by_selector("0x3593564c")
    assert txs
    seen_v4 = False
    for tx in txs:
        commands, inputs, _ = abi_decode(["bytes", "bytes[]", "uint256"], tx.data[4:])
        out = decode_call(Call(tx.to_bytes, tx.value, tx.data, None, (tx.to_bytes,)))
        for cmd, inp in zip(commands, inputs, strict=True):
            if cmd & 0x7F == 0x00:
                # UNVERIFIED: some captured inputs omit the trailing bool (path offset 0x80),
                # possibly a different router version; read four slots to cover both.
                _, amount_in, amount_out, path = abi_decode(V3_IN[:4], inp)
                match = [i for i in out if i.amount_in == amount_in and i.amount_out == amount_out]
                assert match and match[0].token_in == path[:20] and match[0].token_out == path[-20:]
            if cmd & 0x7F == 0x10:
                try:
                    actions, params = abi_decode(["bytes", "bytes[]"], inp)
                except DecodingError:
                    # UNVERIFIED: likely a different router version or fork reusing 0x10
                    # with an (address, bytes) layout. 0x10 is their sole command, so the
                    # whole-call Malformed leaves nothing else to lose.
                    assert len(commands) == 1
                    with pytest.raises(Malformed):
                        decoder.execute(
                            Call(tx.to_bytes, tx.value, tx.data, None, (tx.to_bytes,)), 0
                        )
                    assert out == []
                    continue
                seen_v4 = True
                if actions[0] == 0x06:
                    (p,) = abi_decode([SINGLE], params[0])
                    match = [i for i in out if i.amount_in == p[2] and i.amount_out == p[3]]
                    assert match
    assert seen_v4, "capture should hold a V4 swap through the Universal Router"


def test_v3_msg_sender_sentinel_resolves_to_actor():
    inputs = [abi_encode(V3_IN, [SENDER, 10, 9, v3_path(WETH, 10000, MEME), True])]
    (i,) = decode_call(execute(b"\x00", inputs, actor=ACTOR))
    assert i.recipient == ACTOR


def test_v3_address_this_sentinel_resolves_to_router():
    inputs = [abi_encode(V3_IN, [ROUTER_SENTINEL, 10, 9, v3_path(WETH, 10000, MEME), True])]
    (i,) = decode_call(execute(b"\x00", inputs, actor=ACTOR))
    assert i.recipient == UR


def test_v2_recipient_sentinels():
    inputs = [
        abi_encode(V2_IN, [SENDER, 10, 9, [WETH, MEME], True]),
        abi_encode(V2_IN, [ROUTER_SENTINEL, 10, 9, [WETH, MEME], True]),
        abi_encode(V2_IN, [ME, 10, 9, [WETH, MEME], True]),
    ]
    out = decode_call(execute(b"\x08\x08\x08", inputs, actor=ACTOR))
    assert [i.recipient for i in out] == [ACTOR, UR, ME]


def test_v4_more_actions_than_params_is_malformed():
    params = abi_encode([SINGLE], [((bytes(20), MEME, 10000, 200, bytes(20)), True, 1, 1, b"")])
    v4 = abi_encode(["bytes", "bytes[]"], [b"\x06\x06", [params]])
    call = execute(b"\x10", [v4])
    with pytest.raises(Malformed):
        decoder.execute(call, 0)
    assert decode_call(call) == []
