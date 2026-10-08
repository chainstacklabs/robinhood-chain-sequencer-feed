"""ERC-20 and Permit2 shapes, checked against eth_abi encodings and the capture."""

from __future__ import annotations

from eth_abi import decode as abi_decode

from rhfeed.intents import Call, decode_call

from .helpers import a, by_selector, encode

TOKEN = a("0x5fc5360d0400a0fd4f2af552add042d716f1d168")
PERMIT2 = a("0x000000000022d473030f116ddee9f6b43ac78ba3")
ME = a("0x" + "11" * 20)
YOU = a("0x" + "22" * 20)


def call(to, data, value=0, actor=None):
    return Call(to, value, data, actor, (to,))


def test_transfer():
    data = encode("transfer(address,uint256)", ["address", "uint256"], [YOU, 1_500_000])
    (i,) = decode_call(call(TOKEN, data))
    assert (i.kind, i.token_in, i.amount_in, i.recipient, i.payer) == (
        "transfer",
        TOKEN,
        1_500_000,
        YOU,
        None,
    )
    assert i.actor is None and i.via == (TOKEN,)


def test_transfer_from_names_the_payer():
    data = encode(
        "transferFrom(address,address,uint256)", ["address", "address", "uint256"], [ME, YOU, 7]
    )
    (i,) = decode_call(call(TOKEN, data))
    assert (i.kind, i.payer, i.recipient, i.amount_in) == ("transfer", ME, YOU, 7)


def test_approve():
    data = encode("approve(address,uint256)", ["address", "uint256"], [YOU, 2**256 - 1])
    (i,) = decode_call(call(TOKEN, data))
    assert (i.kind, i.token_in, i.recipient, i.amount_in) == ("approve", TOKEN, YOU, 2**256 - 1)


def test_permit2_approve_names_the_token_from_arguments():
    data = encode(
        "approve(address,address,uint160,uint48)",
        ["address", "address", "uint160", "uint48"],
        [TOKEN, YOU, 10**6, 1_800_000_000],
    )
    (i,) = decode_call(call(PERMIT2, data))
    assert (i.kind, i.token_in, i.recipient, i.amount_in) == ("approve", TOKEN, YOU, 10**6)
    assert i.via == (PERMIT2,)


def test_captured_approvals_match_reference():
    txs = by_selector("0x095ea7b3")
    assert txs, "capture should hold approvals"
    for tx in txs:
        spender, amount = abi_decode(["address", "uint256"], tx.data[4:])
        (i,) = decode_call(call(tx.to_bytes, tx.data))
        assert i.recipient == a(spender) and i.amount_in == amount and i.token_in == tx.to_bytes
