"""abi.py reads slots by hand. Every reader is checked against eth_abi's encoder."""

from __future__ import annotations

import pytest
from eth_abi import encode

from rhfeed.abi import (
    NATIVE,
    Malformed,
    address,
    bytes_array,
    dynamic,
    static_array,
    token,
    tuple_array,
    tuple_at,
    uint,
    word,
)

A1 = bytes.fromhex("d0601ce157db5bdc3162bbac2a2c8af5320d9eec")
A2 = bytes.fromhex("0bd7d308f8e1639fab988df18a8011f41eacad73")


def test_word_and_uint():
    data = encode(["uint256", "uint256"], [7, 2**255])
    assert word(data, 0) == (7).to_bytes(32, "big")
    assert uint(data, 1) == 2**255


def test_word_past_end_is_malformed():
    with pytest.raises(Malformed):
        word(encode(["uint256"], [1]), 1)


def test_address_requires_zero_left_pad():
    assert address(encode(["address"], [A1]), 0) == A1
    with pytest.raises(Malformed):
        address(encode(["uint256"], [2**200]), 0)


def test_token_maps_native_sentinels_to_none():
    assert token(encode(["address"], [A1]), 0) == A1
    assert token(encode(["address"], [bytes(20)]), 0) is None
    assert token(encode(["address"], [NATIVE]), 0) is None


def test_dynamic_bytes():
    data = encode(["uint256", "bytes"], [1, b"hello world" * 5])
    assert dynamic(data, 1) == b"hello world" * 5


def test_dynamic_offset_past_end_is_malformed():
    data = bytearray(encode(["bytes"], [b"x"]))
    data[31] = 0xFF  # offset now points far past the payload
    with pytest.raises(Malformed):
        dynamic(bytes(data), 0)


def test_tuple_at_gives_a_body_with_relative_slots():
    data = encode(["(address,uint256,bytes)"], [(A1, 42, b"abc")])
    body = tuple_at(data, 0)
    assert address(body, 0) == A1
    assert uint(body, 1) == 42
    assert dynamic(body, 2) == b"abc"


def test_tuple_array_one_body_per_element():
    data = encode(["(address,uint256,bytes)[]"], [[(A1, 1, b"a"), (A2, 2, b"bb")]])
    bodies = tuple_array(data, 0)
    assert [address(b, 0) for b in bodies] == [A1, A2]
    assert [dynamic(b, 2) for b in bodies] == [b"a", b"bb"]


def test_bytes_array():
    data = encode(["uint256", "bytes[]"], [9, [b"", b"\x01\x02", b"z" * 40]])
    assert bytes_array(data, 1) == [b"", b"\x01\x02", b"z" * 40]


def test_static_array_of_addresses_and_pairs():
    data = encode(["address[]", "(address,uint256)[]"], [[A1, A2], [(A1, 5), (A2, 6)]])
    assert [address(e, 0) for e in static_array(data, 0)] == [A1, A2]
    pairs = static_array(data, 1, width=2)
    assert [(address(e, 0), uint(e, 1)) for e in pairs] == [(A1, 5), (A2, 6)]


def test_array_length_is_bounded_by_payload():
    data = bytearray(encode(["bytes[]"], [[b"a"]]))
    data[32:64] = (2**200).to_bytes(32, "big")  # length word of the array
    with pytest.raises(Malformed):
        bytes_array(bytes(data), 0)
    data = bytearray(encode(["address[]"], [[A1]]))
    data[32:64] = (2**200).to_bytes(32, "big")
    with pytest.raises(Malformed):
        static_array(bytes(data), 0)
