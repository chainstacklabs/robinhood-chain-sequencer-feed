"""The unnamed `swap(steps[])` aggregator, two ABI versions."""

from __future__ import annotations

from eth_abi import decode as abi_decode

from rhfeed.intents import Call, decode_call

from .helpers import a, by_selector, encode

ROUTER = a("0x65050a9b7e5075a2ba5ced7b1b64ee66262c40dc")
WETH = a("0x0bd7d308f8e1639fab988df18a8011f41eacad73")
MEME = a("0xaebf781437e77123dd209f3156ad7e950fda9418")
POOL = a("0x95434b898c8b95f492c25c4223dea47594ec8fd5")
ME = a("0x" + "11" * 20)

STEP2 = "(uint8,address,address,address,uint24,int24,address,bytes,address,bytes32)"
STEP1 = "(uint8,address,address,address,uint24,int24,address,bytes)"
SIG2 = f"swap({STEP2}[],address,uint256,uint256,uint256)"
SIG1 = f"swap({STEP1}[],address,uint256,uint256)"


def test_v2_single_step_eth_in():
    step = (1, WETH, MEME, POOL, 10000, 200, bytes(20), b"", bytes(20), bytes(32))
    types = [f"{STEP2}[]", "address", "uint256", "uint256", "uint256"]
    data = encode(SIG2, types, [[step], bytes(20), 10**16, 5, 1])
    (i,) = decode_call(Call(ROUTER, 10**16, data, None, (ROUTER,)))
    assert (i.kind, i.token_in, i.token_out, i.amount_in, i.amount_out, i.recipient) == (
        "swap",
        WETH,
        MEME,
        10**16,
        5,
        None,
    )


def test_v2_two_steps_route_ends():
    steps = [
        (1, MEME, WETH, POOL, 10000, 200, bytes(20), b"", bytes(20), bytes(32)),
        (1, WETH, POOL, POOL, 500, 10, bytes(20), b"", bytes(20), bytes(32)),
    ]
    types = [f"{STEP2}[]", "address", "uint256", "uint256", "uint256"]
    data = encode(SIG2, types, [steps, ME, 7, 6, 1])
    (i,) = decode_call(Call(ROUTER, 0, data, None, (ROUTER,)))
    assert (i.token_in, i.token_out, i.recipient) == (MEME, POOL, ME)


def test_v1_layout():
    step = (1, WETH, MEME, bytes(20), 10000, 200, bytes(20), b"")
    types = [f"{STEP1}[]", "address", "uint256", "uint256"]
    data = encode(SIG1, types, [[step], ME, 3, 2])
    (i,) = decode_call(Call(ROUTER, 0, data, None, (ROUTER,)))
    expected = (WETH, MEME, 3, 2, ME)
    assert (i.token_in, i.token_out, i.amount_in, i.amount_out, i.recipient) == expected


def test_empty_steps_yield_nothing():
    data = encode(SIG1, [f"{STEP1}[]", "address", "uint256", "uint256"], [[], ME, 3, 2])
    assert decode_call(Call(ROUTER, 0, data, None, (ROUTER,))) == []


def test_captured_calls_match_reference():
    v2_sig_types = [f"{STEP2}[]", "address", "uint256", "uint256", "uint256"]
    v1_sig_types = [f"{STEP1}[]", "address", "uint256", "uint256"]
    selectors = [("0x4d819a2a", v2_sig_types), ("0xe6cb474f", v1_sig_types)]
    for selector, sig_types in selectors:
        txs = by_selector(selector)
        assert txs
        for tx in txs:
            steps, recipient, amount_in, amount_out, *_ = abi_decode(sig_types, tx.data[4:])
            (i,) = decode_call(Call(tx.to_bytes, tx.value, tx.data, None, (tx.to_bytes,)))
            assert i.token_in == a(steps[0][1]) and i.token_out == a(steps[-1][2])
            assert (i.amount_in, i.amount_out) == (amount_in, amount_out)
            expected_recipient = None if a(recipient) == bytes(20) else a(recipient)
            assert i.recipient == expected_recipient
