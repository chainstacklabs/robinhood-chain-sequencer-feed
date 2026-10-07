"""The intent registry: dispatch by selector, depth cap, and never raising."""

from __future__ import annotations

import pytest

from rhfeed.codec import selector_of
from rhfeed.intents import (
    DECODERS,
    MAX_DEPTH,
    Call,
    Intent,
    decode_call,
    decode_intents,
    decodes,
    inner,
)

from .helpers import a, captured_txs

TO = a("0xcaf681a66d020601342297493863e78c959e5cb2")


@pytest.fixture
def scratch_registry():
    saved = dict(DECODERS)
    yield
    DECODERS.clear()
    DECODERS.update(saved)


def test_unknown_selector_yields_nothing():
    assert decode_call(Call(TO, 0, b"\xde\xad\xbe\xef" + b"\x00" * 64, None, (TO,))) == []


def test_short_data_yields_nothing():
    assert decode_call(Call(TO, 0, b"\xde\xad", None, (TO,))) == []


def test_decodes_registers_under_every_signature(scratch_registry):
    @decodes("foo(uint256)", "bar(address)")
    def fake(call, depth):
        return [Intent("swap", call.actor, call.via)]

    assert DECODERS[selector_of("foo(uint256)")] is fake
    assert DECODERS[selector_of("bar(address)")] is fake
    out = decode_call(Call(TO, 0, selector_of("foo(uint256)") + b"\x00" * 32, None, (TO,)))
    assert out and out[0].kind == "swap" and out[0].via == (TO,)


def test_inner_extends_via_and_keeps_actor():
    outer = Call(TO, 5, b"", b"\x11" * 20, (TO,))
    nested = inner(outer, b"\x22" * 20, 0, b"\xaa")
    assert nested.via == (TO, b"\x22" * 20)
    assert nested.actor == b"\x11" * 20
    assert inner(outer, None, 0, b"", actor=b"\x33" * 20).actor == b"\x33" * 20


def test_depth_cap_stops_recursion(scratch_registry):
    sel = selector_of("loop()")

    @decodes("loop()")
    def loop(call, depth):
        return [Intent("swap", None, call.via), *decode_call(inner(call, TO, 0, sel), depth + 1)]

    out = decode_call(Call(TO, 0, sel, None, (TO,)))
    assert len(out) == MAX_DEPTH + 1


def test_malformed_inside_a_decoder_yields_nothing(scratch_registry):
    from rhfeed.abi import uint

    @decodes("needs_a_word()")
    def needs(call, depth):
        uint(call.data[4:], 0)
        return []

    assert decode_call(Call(TO, 0, selector_of("needs_a_word()"), None, (TO,))) == []


def test_decode_intents_builds_the_top_call(scratch_registry):
    """The top-level call: `to` is the first hop of `via`, the actor is unresolved."""
    tx = next(t for t in captured_txs() if t.selector is not None)
    seen = []
    DECODERS[tx.selector] = lambda call, depth: seen.append(call) or []
    assert decode_intents(tx) == []
    (call,) = seen
    assert (call.to, call.value, call.data, call.actor, call.via) == (
        tx.to_bytes,
        tx.value,
        tx.data,
        None,
        (tx.to_bytes,),
    )


def test_truncated_calldata_never_raises():
    """Every prefix of every captured transaction's calldata decodes or yields nothing."""
    for tx in captured_txs():
        for n in range(len(tx.data) + 1):
            decode_call(Call(tx.to_bytes, tx.value, tx.data[:n], None, (tx.to_bytes or b"",)))


def test_intent_as_dict_is_json_ready():
    i = Intent("swap", a("0x" + "11" * 20), (TO,), token_in=None, token_out=TO, amount_in=1)
    d = i.as_dict()
    assert d["actor"].startswith("0x") and d["token_in"] is None and d["via"] == [d["token_out"]]
    assert d["amount_in"] == 1 and d["exact_in"] is True


def test_labels_name_known_contracts_and_shorten_the_rest():
    from rhfeed.labels import label

    assert label(a("0x4337084d9e255ff0702461cf8895ce9e3b5ff108")) == "entrypoint_v08"
    assert label(a("0xccc88a9d1b4ed6b0eaba998850414b24f1c315be")) == "relay_router"
    assert label(a("0x" + "ab" * 20)) == "0xabababab…"
