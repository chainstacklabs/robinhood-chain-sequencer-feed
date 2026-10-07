"""Pons launchpad buy: ten static words, matched by the V4 PoolManager in the second."""

from __future__ import annotations

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode

from rhfeed.codec import sel
from rhfeed.decoders.pons import V4_POOL_MANAGER
from rhfeed.intents import Call, decode_call

from .helpers import a, by_selector

SELECTOR = sel("0xc1120e3d")
TYPES = [
    "address",
    "address",
    "address",
    "uint256",
    "uint256",
    "address",
    "uint256",
    "uint24",
    "int24",
    "address",
]
LAUNCHPAD = a("0x1cbaF24D53fe930fCe8EFF149fA797D2611Da149")
ROUTER = a("0x" + "88" * 20)
TOKEN = a("0x" + "77" * 20)
ME = a("0x" + "11" * 20)


def words(manager=V4_POOL_MANAGER):
    return [ROUTER, manager, TOKEN, 1, 12345, ME, 1_800_000_000, 10000, 200, bytes(20)]


def call(data):
    return Call(LAUNCHPAD, 10**17, data, None, (LAUNCHPAD,))


def test_buy_reads_token_amount_and_recipient():
    (i,) = decode_call(call(SELECTOR + abi_encode(TYPES, words())))
    assert (i.kind, i.token_in, i.amount_in) == ("swap", None, 10**17)
    assert (i.token_out, i.amount_out, i.recipient) == (TOKEN, 12345, ME)
    assert i.via == (LAUNCHPAD,) and i.actor is None


def test_other_pool_manager_is_not_a_buy():
    assert decode_call(call(SELECTOR + abi_encode(TYPES, words(a("0x" + "99" * 20))))) == []


def test_nine_words_is_not_a_buy():
    assert decode_call(call(SELECTOR + abi_encode(TYPES, words())[:-32])) == []


def test_captured_buys_match_reference():
    txs = by_selector("0xc1120e3d")
    assert len(txs) == 3
    for tx in txs:
        ref = abi_decode(TYPES, tx.data[4:])
        (i,) = decode_call(Call(tx.to_bytes, tx.value, tx.data, None, (tx.to_bytes,)))
        assert (i.kind, i.token_in, i.amount_in) == ("swap", None, tx.value)
        assert (i.token_out, i.amount_out, i.recipient) == (a(ref[2]), ref[4], a(ref[5]))
        assert i.recipient == a(tx.sender)
