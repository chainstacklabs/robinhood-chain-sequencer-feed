"""0x AllowanceHolder -> Settler: token in from exec, token out from the slippage tuple."""

from __future__ import annotations

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode

from rhfeed.codec import selector_of
from rhfeed.intents import Call, decode_call

from .helpers import a, by_selector, encode

AH = a("0x0000000000001ff3684f28c67538d4d072c22734")
SETTLER = a("0x1d4b86491ec211257cbedd77a4380a7494624eff")
WETH = a("0x0bd7d308f8e1639fab988df18a8011f41eacad73")
MEME = a("0x77581054581b9c525e7dd7a0155de43867532d03")
ME = a("0x" + "11" * 20)
EXEC = ["address", "address", "uint256", "address", "bytes"]
SETTLE = "execute((address,address,uint256),bytes[],bytes32)"


def settler(recipient, buy, min_out):
    return selector_of(SETTLE) + abi_encode(
        ["(address,address,uint256)", "bytes[]", "bytes32"],
        [(recipient, buy, min_out), [b"\x01" * 36], bytes(32)],
    )


def test_eth_in_token_out():
    data = encode(
        "exec(address,address,uint256,address,bytes)",
        EXEC,
        [SETTLER, bytes(20), 10**17, SETTLER, settler(ME, MEME, 999)],
    )
    (i,) = decode_call(Call(AH, 10**17, data, None, (AH,)))
    assert (i.kind, i.token_in, i.amount_in, i.token_out, i.amount_out, i.recipient) == (
        "swap",
        None,
        10**17,
        MEME,
        999,
        ME,
    )
    assert i.via == (AH, SETTLER)


def test_token_in_eth_out():
    data = encode(
        "exec(address,address,uint256,address,bytes)",
        EXEC,
        [SETTLER, MEME, 5, SETTLER, settler(ME, b"\xee" * 20, 1)],
    )
    (i,) = decode_call(Call(AH, 0, data, None, (AH,)))
    assert (i.token_in, i.token_out, i.amount_in) == (MEME, None, 5)


def test_zero_slippage_leaves_token_out_unknown():
    data = encode(
        "exec(address,address,uint256,address,bytes)",
        EXEC,
        [SETTLER, MEME, 5, SETTLER, settler(bytes(20), bytes(20), 0)],
    )
    (i,) = decode_call(Call(AH, 0, data, None, (AH,)))
    assert (i.token_in, i.token_out, i.amount_out, i.recipient) == (
        MEME,
        None,
        None,
        None,
    )


def test_non_settler_target_still_names_the_input():
    data = encode(
        "exec(address,address,uint256,address,bytes)",
        EXEC,
        [SETTLER, MEME, 5, ME, b"\xde\xad\xbe\xef"],
    )
    (i,) = decode_call(Call(AH, 0, data, None, (AH,)))
    assert (i.token_in, i.amount_in, i.token_out) == (MEME, 5, None)


def test_captured_exec_calls_match_reference():
    txs = by_selector("0x2213bc0b")
    assert txs
    for tx in txs:
        _, tok, amount, target, inner = abi_decode(EXEC, tx.data[4:])
        slippage, _, _ = abi_decode(["(address,address,uint256)", "bytes[]", "bytes32"], inner[4:])
        (i,) = decode_call(Call(tx.to_bytes, tx.value, tx.data, None, (tx.to_bytes,)))
        assert i.token_in == (None if a(tok) == bytes(20) else a(tok)) and i.amount_in == amount
        assert i.recipient == a(slippage[0]) and i.amount_out == slippage[2]
        buy = a(slippage[1])
        assert i.token_out == (None if buy == b"\xee" * 20 else buy)
        assert i.via == (tx.to_bytes, a(target))
