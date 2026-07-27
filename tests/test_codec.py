"""Cross-check the fast decoder against the reference implementations.

`codec` hand-rolls RLP scanning, EIP-55 checksumming and signature recovery for
speed. That is only worth doing if it is exactly right, so every one of those paths
is compared here against `rlp`, `eth_utils` and `eth_account` — the libraries the
fast paths replace.

    uv run --extra dev pytest

`tests/frames.jsonl` is optional. When it is present (write one with
`rhfeed capture --seconds 60 tests/frames.jsonl`) the same comparison runs over
every transaction in it, which is the check that actually matters.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
import rlp
from eth_account import Account
from eth_utils import to_checksum_address

from rhfeed.codec import (
    _rlp_list,
    _rlp_uint,
    _scan_list,
    addr,
    checksum,
    decode_transaction,
    parse_frame,
    sel,
    selector_of,
)

FRAMES = Path(__file__).parent / "frames.jsonl"

# Three envelopes signed with a known key, one per layout the decoder models.
KEY = "0x4c0883a69102937d6231471b5dbb6204fe512961708279a4b1e1b0a1b1b1b1b1"
ACCOUNT = Account.from_key(KEY)

TEMPLATES = {
    "legacy": {
        "nonce": 7,
        "gasPrice": 60_000_000,
        "gas": 21_000,
        "to": "0xd0601CE157Db5bdC3162BbaC2a2C8aF5320D9EEC",
        "value": 10**17,
        "data": b"\xa9\x05\x9c\xbb" + b"\x11" * 64,
        "chainId": 4663,
    },
    "eip2930": {
        "type": 1,
        "nonce": 8,
        "gasPrice": 60_000_000,
        "gas": 21_000,
        "to": "0xd0601CE157Db5bdC3162BbaC2a2C8aF5320D9EEC",
        "value": 0,
        "data": b"\x23\xb8\x72\xdd",
        "chainId": 4663,
        "accessList": (
            {
                "address": "0x0000000000000000000000000000000000000074",
                "storageKeys": ("0x" + "00" * 32,),
            },
        ),
    },
    "eip1559": {
        "type": 2,
        "nonce": 9,
        "maxFeePerGas": 100_000_000,
        "maxPriorityFeePerGas": 1,
        "gas": 300_000,
        "to": "0xd0601CE157Db5bdC3162BbaC2a2C8aF5320D9EEC",
        "value": 42,
        "data": b"",
        "chainId": 4663,
        "accessList": (),
    },
    "deploy": {
        "type": 2,
        "nonce": 10,
        "maxFeePerGas": 100_000_000,
        "maxPriorityFeePerGas": 1,
        "gas": 3_000_000,
        "value": 0,
        "data": b"\x60\x80\x60\x40\x52",
        "chainId": 4663,
        "accessList": (),
    },
}


@pytest.fixture(scope="module", params=sorted(TEMPLATES))
def signed(request):
    return request.param, ACCOUNT.sign_transaction(TEMPLATES[request.param]).raw_transaction


# --------------------------------------------------------------------------- #
# the pieces
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "value", [0, 1, 55, 56, 127, 128, 255, 256, 4663, 2**32, 2**64 - 1, 2**255]
)
def test_rlp_uint_matches_reference(value):
    assert _rlp_uint(value) == rlp.encode(value)


@pytest.mark.parametrize("size", [0, 1, 55, 56, 1024, 70_000])
def test_rlp_list_header_matches_reference(size):
    payload = b"\x2a" * size
    assert _rlp_list(rlp.encode(payload)) == rlp.encode([payload])


def test_scan_list_finds_every_field():
    items = [b"", b"\x01", b"\x7f", b"\x80", b"\xff" * 55, b"\xab" * 56, b"\xcd" * 1000]
    encoded = rlp.encode(items)
    scanned = _scan_list(encoded)
    assert [encoded[s:e] for _, s, e in scanned] == items
    # item_start keeps the length prefix, so re-slicing a run round-trips verbatim.
    assert encoded[scanned[0][0] : scanned[-1][2]] == b"".join(rlp.encode(i) for i in items)


def test_scan_list_skips_nested_structures_without_descending():
    encoded = rlp.encode([b"\x01", [[b"\x02", b"\x03"], [b"\x04"]], b"\x05"])
    scanned = _scan_list(encoded)
    assert len(scanned) == 3
    assert encoded[scanned[0][1] : scanned[0][2]] == b"\x01"
    assert encoded[scanned[2][1] : scanned[2][2]] == b"\x05"


@pytest.mark.parametrize("bad", [b"", b"\x80", b"\xc4\x01\x02", b"\xf8\xff\x01"])
def test_scan_list_rejects_malformed_input(bad):
    with pytest.raises(ValueError):
        _scan_list(bad)


@pytest.mark.parametrize(
    "address",
    [
        "0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec",
        "0x0000000000000000000000000000000000000074",
        "0xffffffffffffffffffffffffffffffffffffffff",
        "0x0000000000000000000000000000000000000000",
    ],
)
def test_checksum_matches_eth_utils(address):
    assert checksum(addr(address)) == to_checksum_address(address)


def test_selector_helpers():
    assert selector_of("transfer(address,uint256)") == sel("0xa9059cbb")
    assert selector_of("transferFrom(address,address,uint256)") == sel("23b872dd")


# --------------------------------------------------------------------------- #
# whole transactions
# --------------------------------------------------------------------------- #


def test_decoded_fields_match_the_signed_input(signed):
    name, raw = signed
    spec = TEMPLATES[name]
    tx = decode_transaction(bytes(raw))

    assert tx is not None
    assert tx.tx_type == spec.get("type", 0)
    assert tx.nonce == spec["nonce"]
    assert tx.gas == spec["gas"]
    assert tx.value == spec["value"]
    if "to" in spec:
        assert tx.to == to_checksum_address(spec["to"])
        assert tx.to_bytes == addr(spec["to"])
    else:
        assert tx.to is None and tx.kind == "deploy"
    data = spec["data"]
    assert tx.data_len == len(data)
    assert tx.selector == (data[:4] if len(data) >= 4 else None)


def test_hash_matches_reference(signed):
    from eth_utils import keccak

    _, raw = signed
    assert decode_transaction(bytes(raw)).hash == "0x" + keccak(bytes(raw)).hex()


def test_sender_recovery_matches_eth_account(signed):
    _, raw = signed
    tx = decode_transaction(bytes(raw))
    assert tx.sender == Account.recover_transaction(raw) == ACCOUNT.address


def test_pre_eip155_legacy_signature_recovers():
    """v of 27/28 signs only the first six fields — a different payload shape."""
    spec = {k: v for k, v in TEMPLATES["legacy"].items() if k != "chainId"}
    raw = bytes(ACCOUNT.sign_transaction(spec).raw_transaction)
    tx = decode_transaction(raw)
    assert tx.sender == Account.recover_transaction(raw) == ACCOUNT.address


def test_lazy_fields_are_cached_not_recomputed(signed):
    _, raw = signed
    tx = decode_transaction(bytes(raw))
    assert tx.hash is tx.hash
    assert tx.sender_bytes is tx.sender_bytes


def test_unknown_envelope_type_keeps_the_hash():
    from eth_utils import keccak

    raw = b"\x7a" + rlp.encode([b"\x01", b"\x02"])
    tx = decode_transaction(raw)
    assert tx.tx_type == 0x7A
    assert tx.hash == "0x" + keccak(raw).hex()
    assert tx.to_bytes is None and tx.sender is None


@pytest.mark.parametrize("junk", [b"\x02\xff\xff", b"\x02", b"\xc0", b"\x02\xc0"])
def test_truncated_input_does_not_raise(junk):
    decode_transaction(junk)  # a bad frame must not take the consumer down


# --------------------------------------------------------------------------- #
# real frames, when a capture is present
# --------------------------------------------------------------------------- #


def _captured_transactions():
    if not FRAMES.exists():
        return []
    out = []
    for line in FRAMES.read_text().splitlines():
        if not line.strip():
            continue
        for msg in parse_frame(json.loads(line)):
            out.extend(msg.txs)
    return out


@pytest.mark.skipif(not FRAMES.exists(), reason="no tests/frames.jsonl capture")
def test_captured_frames_decode_identically_to_the_reference():
    from eth_utils import keccak

    txs = _captured_transactions()
    assert txs, "capture contained no transactions"
    for tx in txs:
        assert tx.hash == "0x" + keccak(tx.raw).hex()
        if tx.tx_type in (0, 1, 2, 3, 4):
            assert tx.sender == Account.recover_transaction(tx.raw)


@pytest.mark.skipif(not FRAMES.exists(), reason="no tests/frames.jsonl capture")
def test_captured_frames_field_parity_with_rlp():
    """Positional fields must agree with a full RLP decode of the same envelope."""
    for tx in _captured_transactions():
        if tx.tx_type not in (0, 1, 2):
            continue
        body = tx.raw[1:] if tx.tx_type else tx.raw
        fields = rlp.decode(body)
        ti = {0: 3, 1: 4, 2: 5}[tx.tx_type]
        expected = fields[ti]
        assert tx.to_bytes == (expected or None)


def test_backlog_skipping_leaves_transactions_undecoded():
    frame = {
        "messages": [
            {
                "sequenceNumber": 1,
                "message": {
                    "message": {
                        "header": {"kind": 3, "timestamp": 1},
                        "l2Msg": base64.b64encode(b"\x04" + b"\x00" * 8).decode(),
                    }
                },
            }
        ]
    }
    assert parse_frame(frame, decode_txs=False)[0].txs == []
    assert parse_frame(frame, decode_txs=True)[0].seq == 1
