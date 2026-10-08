"""Relay's router on Robinhood Chain: solver fills and user sells, one intent each."""

from __future__ import annotations

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode

from rhfeed.codec import selector_of
from rhfeed.intents import Call, decode_call

from .helpers import a, by_selector, encode

ROUTER = a("0xccc88a9d1b4ed6b0eaba998850414b24f1c315be")
EXECUTOR = a("0xb92fe925dc43a0ecde6c8b1a2709c170ec4fff4f")
TREASURY = a("0xf70da97812cb96acdf810712aa562db8dfa3dbef")
AH = a("0x0000000000001ff3684f28c67538d4d072c22734")
USDG = a("0x5fc5360d0400a0fd4f2af552add042d716f1d168")
MEME = a("0x69984ad3322300039f2855f81c44dbc532efe744")
WALLET = a("0x9b5e82e3bcde529bbfba26e0b9e7044cef866a79")
ORDER = bytes(range(32))

CALLS = "(address,bool,uint256,bytes)[]"
PERMIT = "((address,uint256)[],uint256,uint256)"
FILL = f"permit2TransferAndMulticall(address,{PERMIT},{CALLS},address,address,bytes,bytes)"
SELL = f"transferAndMulticall(address[],uint256[],{CALLS},address,address,bytes)"
SWEEP = bytes.fromhex("9bb43718")
DEPOSIT = bytes.fromhex("73b7bb2f")


def executor_call(selector, tokens):
    return selector + abi_encode(
        ["address[]", "uint256[]", "address[]", "bytes[]"], [tokens, [1] * len(tokens), [], []]
    )


def fill(token_out=MEME, suffix=ORDER, calls=None):
    calls = (
        calls
        if calls is not None
        else [
            (USDG, False, 0, encode("approve(address,uint256)", ["address", "uint256"], [AH, 5])),
            (EXECUTOR, False, 0, executor_call(SWEEP, [token_out])),
        ]
    )
    data = (
        encode(
            FILL,
            ["address", PERMIT, CALLS, "address", "address", "bytes", "bytes"],
            [
                TREASURY,
                ([(USDG, 1910238)], 1, 2),
                calls,
                WALLET,
                WALLET,
                ORDER + b"\x00",
                b"\x00" * 65,
            ],
        )
        + suffix
    )
    return Call(ROUTER, 0, data, None, (ROUTER,))


def test_fill():
    (i,) = decode_call(fill())
    assert i.kind == "relay_fill"
    assert (i.actor, i.payer, i.recipient) == (WALLET, TREASURY, WALLET)
    assert (i.token_in, i.amount_in, i.token_out) == (USDG, 1910238, MEME)
    assert i.order_id == ORDER and i.via == (ROUTER,)


def test_fill_without_sweep_falls_back_to_settler_slippage():
    settle = selector_of("execute((address,address,uint256),bytes[],bytes32)") + abi_encode(
        ["(address,address,uint256)", "bytes[]", "bytes32"], [(WALLET, MEME, 7), [], bytes(32)]
    )
    exec_ = encode(
        "exec(address,address,uint256,address,bytes)",
        ["address", "address", "uint256", "address", "bytes"],
        [EXECUTOR, USDG, 1910238, a("0x" + "44" * 20), settle],
    )
    (i,) = decode_call(fill(calls=[(AH, False, 0, exec_)]))
    assert (i.token_out, i.amount_out) == (MEME, 7)


def test_fill_without_trailing_word_has_no_order():
    (i,) = decode_call(fill(suffix=b""))
    assert i.order_id is None


def test_sell_keeps_the_wallet_as_actor():
    calls = [
        (MEME, False, 0, encode("approve(address,uint256)", ["address", "uint256"], [AH, 5])),
        (EXECUTOR, False, 0, executor_call(DEPOSIT, [USDG])),
    ]
    data = (
        encode(
            SELL,
            ["address[]", "uint256[]", CALLS, "address", "address", "bytes"],
            [[MEME], [1588651804434692220595], calls, TREASURY, TREASURY, ORDER + b"\x00"],
        )
        + ORDER
    )
    (i,) = decode_call(Call(ROUTER, 0, data, WALLET, (ROUTER,)))
    assert i.kind == "relay_sell"
    assert (i.actor, i.token_in, i.amount_in, i.token_out, i.order_id) == (
        WALLET,
        MEME,
        1588651804434692220595,
        USDG,
        ORDER,
    )
    assert i.payer is None and i.recipient is None


def test_sell_with_no_tokens_yields_nothing():
    data = encode(
        SELL,
        ["address[]", "uint256[]", CALLS, "address", "address", "bytes"],
        [[], [], [], TREASURY, TREASURY, b""],
    )
    assert decode_call(Call(ROUTER, 0, data, WALLET, (ROUTER,))) == []


def test_captured_fills_match_reference():
    txs = by_selector("0x0a2b8f36")
    assert len(txs) >= 2
    for tx in txs:
        types = ["address", PERMIT, CALLS, "address", "address", "bytes", "bytes"]
        owner, permit, calls, refund_to, _, _, _ = args = abi_decode(types, tx.data[4:])
        (i,) = decode_call(Call(tx.to_bytes, tx.value, tx.data, None, (tx.to_bytes,)))
        assert (i.payer, i.recipient, i.actor) == (a(owner), a(refund_to), a(refund_to))
        assert (i.token_in, i.amount_in) == (a(permit[0][0][0]), permit[0][0][1])
        sweep = next(c for c in calls if c[3][:4] == SWEEP)
        # Only the first array is known to be `address[] tokens`; eth_abi reads it from the
        # first offset and ignores what follows.
        (tokens,) = abi_decode(["address[]"], sweep[3][4:])
        assert i.token_out == a(tokens[0])
        # Relay appends one word after the ABI body; it is the order id.
        assert len(tx.data) - 4 - len(abi_encode(types, args)) == 32
        assert i.order_id == tx.data[-32:]


def test_captured_fomo_sell_bundle():
    (tx,) = by_selector("0x765e827f")
    out = decode_call(Call(tx.to_bytes, tx.value, tx.data, None, (tx.to_bytes,)))
    sells = [i for i in out if i.kind == "relay_sell"]
    assert len(sells) == 1
    (s,) = sells
    assert s.actor == WALLET and s.token_in == MEME and s.token_out == USDG
    assert s.via[:2] == (tx.to_bytes, WALLET) and s.via[-1] == ROUTER
    assert s.order_id == bytes.fromhex(
        "4b293fad54212888de4a42bbf05dc42cb2c59b31877c5b9587ae5f24e0f4c935"
    )
