"""Signature verification, against a message actually taken off the feed.

There is no second implementation of Nitro's `SignatureHash` in Python to compare
against, so the cross-check here is the chain itself: `MESSAGE` was captured off
Robinhood's mainnet feed, and the only way `recover_signer` returns
`0xDaa5…87F4` for it is if every byte of the preimage is in the right place and the
right length. An address that is independently confirmed as this chain's batch poster
on L1 is not something a wrong preimage produces by accident.

The tampering tests are the other half. A preimage that silently drops a field would
still recover the right signer for the untouched message — it is only visible when you
change that field and verification keeps passing.

    uv run --extra dev pytest tests/test_verify.py
"""

from __future__ import annotations

import base64
import copy
import json
import time
from pathlib import Path

import pytest

from rhfeed import (
    FEED_PREFIX,
    MAINNET_CHAIN_ID,
    MAINNET_SIGNER,
    MAINNET_VERIFIER,
    FeedConsumer,
    Verifier,
    recover_signer,
    signature_payload,
)

#: One message captured off wss://feed.mainnet.chain.robinhood.com on 2026-07-27,
#: kept as JSON rather than as a Python literal on purpose: the `l2Msg` and signature
#: are long base64 strings, and re-wrapping them by hand to fit a line limit corrupts
#: them in ways that look like a verification bug.
MESSAGE = json.loads((Path(__file__).parent / "verified_message.json").read_text())


def tweak(**changes) -> dict:
    """A copy of MESSAGE with top-level or header fields replaced."""
    out = copy.deepcopy(MESSAGE)
    header = out["message"]["message"]["header"]
    for key, value in changes.items():
        if key in header:
            header[key] = value
        elif key in out["message"]:
            out["message"][key] = value
        else:
            out[key] = value
    return out


# --------------------------------------------------------------------------- #
# the real message
# --------------------------------------------------------------------------- #


def test_a_real_message_recovers_the_batch_poster():
    assert recover_signer(MESSAGE, MAINNET_CHAIN_ID) == MAINNET_SIGNER


def test_the_mainnet_verifier_accepts_a_real_message():
    assert MAINNET_VERIFIER.accepts(MESSAGE)


def test_the_signed_payload_is_domain_separated():
    assert signature_payload(MESSAGE, MAINNET_CHAIN_ID).startswith(FEED_PREFIX)


def test_the_payload_ends_with_the_l2_message():
    l2 = base64.b64decode(MESSAGE["message"]["message"]["l2Msg"])
    assert signature_payload(MESSAGE, MAINNET_CHAIN_ID).endswith(l2)


# --------------------------------------------------------------------------- #
# the preimage traps
# --------------------------------------------------------------------------- #


def test_a_zero_base_fee_contributes_no_bytes():
    """`big.Int.Bytes()`, not a 32-byte word. Getting this wrong breaks every recovery."""
    without = tweak(baseFeeL1=None)
    assert signature_payload(without, MAINNET_CHAIN_ID) == signature_payload(
        MESSAGE, MAINNET_CHAIN_ID
    )


def test_a_nonzero_base_fee_contributes_its_minimal_bytes():
    base = len(signature_payload(MESSAGE, MAINNET_CHAIN_ID))
    assert len(signature_payload(tweak(baseFeeL1=1), MAINNET_CHAIN_ID)) == base + 1
    assert len(signature_payload(tweak(baseFeeL1=256), MAINNET_CHAIN_ID)) == base + 2


def test_a_null_request_id_is_omitted_rather_than_zero_padded():
    zeros = tweak(requestId="0x" + "00" * 32)
    payload = signature_payload(MESSAGE, MAINNET_CHAIN_ID)
    assert len(signature_payload(zeros, MAINNET_CHAIN_ID)) == len(payload) + 32


@pytest.mark.parametrize("metadata", ["AAA=", "AAAA", "AAAAAAAAAAAAAA=="])
def test_block_metadata_is_signed_verbatim(metadata):
    """Absent on this chain, present on Arbitrum One in these lengths, signed either way."""
    base = signature_payload(MESSAGE, MAINNET_CHAIN_ID)
    grown = signature_payload(tweak(blockMetadata=metadata), MAINNET_CHAIN_ID)
    assert len(grown) == len(base) + len(base64.b64decode(metadata))


def test_absent_block_metadata_contributes_no_bytes():
    assert signature_payload(tweak(blockMetadata=None), MAINNET_CHAIN_ID) == signature_payload(
        MESSAGE, MAINNET_CHAIN_ID
    )


# --------------------------------------------------------------------------- #
# tampering
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sequenceNumber", 20839681),
        ("blockHash", "0x" + "11" * 32),
        ("delayedMessagesRead", 68505),
        ("blockMetadata", "AAA="),
        ("kind", 4),
        ("sender", "0x000000000000000000000000000000000000dead"),
        ("blockNumber", 25625039),
        ("timestamp", 1785166091),
        ("baseFeeL1", 1),
        ("requestId", "0x" + "22" * 32),
    ],
)
def test_changing_any_signed_field_breaks_verification(field, value):
    assert not MAINNET_VERIFIER.accepts(tweak(**{field: value}))


def test_rewriting_the_transactions_breaks_verification():
    """The point of the whole exercise: you cannot swap in your own l2Msg."""
    forged = copy.deepcopy(MESSAGE)
    raw = bytearray(base64.b64decode(forged["message"]["message"]["l2Msg"]))
    raw[-1] ^= 0xFF
    forged["message"]["message"]["l2Msg"] = base64.b64encode(bytes(raw)).decode()
    assert not MAINNET_VERIFIER.accepts(forged)


def test_the_wrong_chain_id_does_not_recover_the_signer():
    assert recover_signer(MESSAGE, 42161) != MAINNET_SIGNER


def test_a_different_signer_set_rejects_a_genuine_message():
    someone_else = Verifier(MAINNET_CHAIN_ID, [bytes(20)])
    assert not someone_else.accepts(MESSAGE)
    # ...and still reports who really signed it, which is how you debug the rejection.
    assert someone_else.signer_of(MESSAGE) == MAINNET_SIGNER


# --------------------------------------------------------------------------- #
# unusable signatures
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "signature",
    [
        None,
        "",
        base64.b64encode(b"\x01" * 64).decode(),  # too short
        base64.b64encode(b"\x01" * 66).decode(),  # too long
        base64.b64encode(b"\x01" * 64 + b"\x09").decode(),  # impossible recovery id
        "not base64 at all!!",
    ],
)
def test_an_unusable_signature_is_not_accepted(signature):
    entry = copy.deepcopy(MESSAGE)
    entry["signatureV2"] = signature
    assert recover_signer(entry, MAINNET_CHAIN_ID) is None
    assert not MAINNET_VERIFIER.accepts(entry)


def test_a_missing_signature_field_is_not_accepted():
    entry = copy.deepcopy(MESSAGE)
    del entry["signatureV2"]
    assert not MAINNET_VERIFIER.accepts(entry)


def test_a_verifier_rejects_a_signer_that_is_not_an_address():
    with pytest.raises(ValueError, match="20-byte"):
        Verifier(MAINNET_CHAIN_ID, [b"\x01" * 19])


# --------------------------------------------------------------------------- #
# wiring into the consumer
# --------------------------------------------------------------------------- #


def frame(*entries) -> dict:
    return {"version": 1, "messages": list(entries)}


def test_a_verifying_consumer_passes_a_genuine_message_through():
    consumer = FeedConsumer(verify=MAINNET_VERIFIER)
    out = list(consumer._frame(frame(MESSAGE), time.time(), True))
    assert [m.seq for m in out] == [MESSAGE["sequenceNumber"]]
    assert consumer.stats["unverified_messages"] == 0


def test_a_verifying_consumer_drops_a_forged_message():
    consumer = FeedConsumer(verify=MAINNET_VERIFIER)
    assert list(consumer._frame(frame(tweak(blockHash="0x" + "11" * 32)), time.time(), True)) == []
    assert consumer.stats["unverified_messages"] == 1


def test_a_dropped_message_does_not_advance_the_sequence_watermark():
    """Otherwise one injected frame makes the next reconnect skip the real messages."""
    consumer = FeedConsumer(verify=MAINNET_VERIFIER)
    list(consumer._frame(frame(tweak(sequenceNumber=99_999_999)), time.time(), True))
    assert consumer.highest_seq == -1


def test_verification_is_off_by_default():
    consumer = FeedConsumer()
    forged = tweak(blockHash="0x" + "11" * 32)
    assert [m.seq for m in consumer._frame(frame(forged), time.time(), True)] == [
        forged["sequenceNumber"]
    ]
