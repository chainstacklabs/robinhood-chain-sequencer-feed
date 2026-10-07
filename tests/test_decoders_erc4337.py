"""handleOps -> account execute/executeBatch -> whatever the wallet called."""

from __future__ import annotations

from eth_abi import decode as abi_decode

from rhfeed.intents import MAX_DEPTH, Call, decode_call

from .helpers import a, by_selector, encode

ENTRYPOINT = a("0x4337084d9e255ff0702461cf8895ce9e3b5ff108")
BUNDLER = a("0x4337026d731283f9917fa3c9194d74809bfbec26")
WALLET_A = a("0x9b5e82e3bcde529bbfba26e0b9e7044cef866a79")
WALLET_B = a("0x" + "22" * 20)
TOKEN = a("0x69984ad3322300039f2855f81c44dbc532efe744")
SPENDER = a("0x" + "33" * 20)

OP = "(address,uint256,bytes,bytes,bytes32,uint256,bytes32,bytes,bytes)"
HANDLE_OPS = f"handleOps({OP}[],address)"
EXECUTE = "execute(address,uint256,bytes)"
BATCH = "executeBatch((address,uint256,bytes)[])"


def op(sender, call_data):
    return (sender, 0, b"", call_data, bytes(32), 0, bytes(32), b"", b"")


def approve(amount):
    return encode("approve(address,uint256)", ["address", "uint256"], [SPENDER, amount])


def bundle(ops):
    data = encode(HANDLE_OPS, [f"{OP}[]", "address"], [ops, BUNDLER])
    return Call(ENTRYPOINT, 0, data, None, (ENTRYPOINT,))


def test_each_operation_yields_its_own_actor():
    a_call = encode(EXECUTE, ["address", "uint256", "bytes"], [TOKEN, 0, approve(1)])
    b_call = encode(EXECUTE, ["address", "uint256", "bytes"], [TOKEN, 0, approve(2)])
    one, two = decode_call(bundle([op(WALLET_A, a_call), op(WALLET_B, b_call)]))
    assert (one.actor, one.amount_in, one.via) == (WALLET_A, 1, (ENTRYPOINT, WALLET_A, TOKEN))
    assert (two.actor, two.amount_in, two.via) == (WALLET_B, 2, (ENTRYPOINT, WALLET_B, TOKEN))


def test_execute_batch_yields_in_order():
    calls = [(TOKEN, 0, approve(1)), (TOKEN, 0, approve(2))]
    batch = encode(BATCH, ["(address,uint256,bytes)[]"], [calls])
    out = decode_call(bundle([op(WALLET_A, batch)]))
    assert [i.amount_in for i in out] == [1, 2] and all(i.actor == WALLET_A for i in out)


def test_execute_with_value_passes_it_down():
    sig = "swapExactETHForTokens(uint256,address[],address,uint256)"
    swap = encode(
        sig, ["uint256", "address[]", "address", "uint256"], [1, [TOKEN, SPENDER], WALLET_A, 1]
    )
    call = encode(EXECUTE, ["address", "uint256", "bytes"], [SPENDER, 10**18, swap])
    (i,) = decode_call(bundle([op(WALLET_A, call)]))
    assert (i.amount_in, i.actor) == (10**18, WALLET_A)


def test_unknown_inner_call_yields_nothing_for_that_op():
    call = encode(EXECUTE, ["address", "uint256", "bytes"], [TOKEN, 0, b"\xde\xad\xbe\xef"])
    assert decode_call(bundle([op(WALLET_A, call)])) == []


def nest(levels):
    """A bundle whose wallet calls the EntryPoint again, `levels` deep, ending in one approve."""
    leaf = encode(EXECUTE, ["address", "uint256", "bytes"], [TOKEN, 0, approve(1)])
    data = encode(HANDLE_OPS, [f"{OP}[]", "address"], [[op(WALLET_A, leaf)], BUNDLER])
    for _ in range(levels):
        call = encode(EXECUTE, ["address", "uint256", "bytes"], [ENTRYPOINT, 0, data])
        data = encode(HANDLE_OPS, [f"{OP}[]", "address"], [[op(WALLET_A, call)], BUNDLER])
    return Call(ENTRYPOINT, 0, data, None, (ENTRYPOINT,))


def test_depth_cap_stops_self_nesting():
    # Each level costs two hops (handleOps, execute); one level fits, many do not.
    assert len(decode_call(nest(1))) == 1
    assert decode_call(nest(MAX_DEPTH)) == []


def test_captured_fomo_bundle_names_the_wallet():
    txs = by_selector("0x765e827f")
    assert txs, "capture should hold a v0.8 handleOps bundle"
    for tx in txs:
        ops, _ = abi_decode([f"{OP}[]", "address"], tx.data[4:])
        out = decode_call(Call(tx.to_bytes, tx.value, tx.data, None, (tx.to_bytes,)))
        senders = {a(o[0]) for o in ops}
        assert out and {i.actor for i in out} <= senders
        assert all(i.via[:2] == (tx.to_bytes, i.actor) for i in out)
