"""Envelope fields the decoder used to drop, and the reorg they make detectable.

A reorg is the one case where a sequence number you have already seen is *not* a
duplicate. Nitro offers no message type for it, so the only signal is the same sequence
number arriving with a different block hash — which the consumer can only see if the
decoder keeps the hash. These two things are tested together because neither is useful
alone.
"""

from __future__ import annotations

import base64
import time

import pytest

from rhfeed import FeedConsumer, is_filtered_call, parse_frame

L2MSG = base64.b64encode(b"\x04" + b"\x00" * 8).decode()


def entry(seq: int, block_hash: str, delayed: int = 500, l1_block: int = 25_000_000) -> dict:
    return {
        "sequenceNumber": seq,
        "blockHash": block_hash,
        "message": {
            "delayedMessagesRead": delayed,
            "message": {
                "header": {
                    "kind": 3,
                    "sender": "0xa4b000000000000000000073657175656e636572",
                    "blockNumber": l1_block,
                    "timestamp": int(time.time()),
                },
                "l2Msg": L2MSG,
            },
        },
    }


def frame(*entries) -> dict:
    return {"version": 1, "messages": list(entries)}


def hash_of(n: int) -> str:
    return "0x" + f"{n:02x}" * 32


# --------------------------------------------------------------------------- #
# the fields themselves
# --------------------------------------------------------------------------- #


def test_the_envelope_fields_survive_decoding():
    """These are signed, so `verify.py` already read them; `codec.py` dropped them."""
    msg = parse_frame(frame(entry(7, hash_of(0xAB), delayed=78_049, l1_block=25_647_511)))[0]
    assert msg.block_hash == hash_of(0xAB)
    assert msg.delayed_messages_read == 78_049
    assert msg.l1_block_number == 25_647_511


def test_the_fields_are_absent_rather_than_wrong_when_the_envelope_omits_them():
    bare = {
        "sequenceNumber": 1,
        "message": {"message": {"header": {"kind": 3, "timestamp": 1}, "l2Msg": L2MSG}},
    }
    msg = parse_frame(frame(bare))[0]
    assert msg.block_hash is None
    assert msg.delayed_messages_read == 0
    assert msg.l1_block_number == 0


def test_a_delayed_inbox_read_is_visible_as_a_jump():
    """A jump means the sequencer pulled messages out of the delayed inbox."""
    consumer = FeedConsumer()
    seen = [
        m.delayed_messages_read
        for e in (entry(1, hash_of(1), delayed=500), entry(2, hash_of(2), delayed=503))
        for m in consumer._frame(frame(e), time.time(), True)
    ]
    assert seen == [500, 503]


# --------------------------------------------------------------------------- #
# reorgs
# --------------------------------------------------------------------------- #


def test_the_same_sequence_number_with_the_same_hash_is_still_a_duplicate():
    consumer = FeedConsumer()
    repeated = entry(10, hash_of(1))
    list(consumer._frame(frame(repeated), time.time(), True))
    assert list(consumer._frame(frame(repeated), time.time(), True)) == []
    assert consumer.stats["duplicate_messages"] == 1
    assert consumer.stats["reorgs"] == 0


def test_the_same_sequence_number_with_a_different_hash_is_a_reorg():
    consumer = FeedConsumer()
    list(consumer._frame(frame(entry(10, hash_of(1))), time.time(), True))
    out = list(consumer._frame(frame(entry(10, hash_of(2))), time.time(), True))
    assert [m.seq for m in out] == [10]
    assert out[0].reorg is True
    assert consumer.stats["reorgs"] == 1
    assert consumer.stats["duplicate_messages"] == 0


def test_a_reorg_delivers_the_replacements_that_follow_it():
    """Nitro re-broadcasts *from* the reorg point, so 11 and 12 come again too."""
    consumer = FeedConsumer()
    for seq in (10, 11, 12):
        list(consumer._frame(frame(entry(seq, hash_of(1))), time.time(), True))
    replayed = [
        m.seq
        for seq in (11, 12)
        for m in consumer._frame(frame(entry(seq, hash_of(2))), time.time(), True)
    ]
    assert replayed == [11, 12]
    assert consumer.highest_seq == 12


def test_an_unverified_message_cannot_force_a_rewind():
    """The reorg path moves the watermark backwards, so it must sit behind verification."""
    from rhfeed import MAINNET_VERIFIER

    consumer = FeedConsumer(verify=MAINNET_VERIFIER)
    consumer._highest_seq = 5_000
    consumer._hashes[100] = hash_of(1)
    assert list(consumer._frame(frame(entry(100, hash_of(2))), time.time(), True)) == []
    assert consumer.highest_seq == 5_000
    assert consumer.stats["reorgs"] == 0


def test_an_uncomparable_replay_is_treated_as_a_duplicate():
    """No hash on either side means no evidence, and no evidence must not rewind."""
    consumer = FeedConsumer()
    bare = {
        "sequenceNumber": 3,
        "message": {"message": {"header": {"kind": 3, "timestamp": 1}, "l2Msg": L2MSG}},
    }
    list(consumer._frame(frame(bare), time.time(), True))
    assert list(consumer._frame(frame(bare), time.time(), True)) == []
    assert consumer.stats["reorgs"] == 0
    assert consumer.highest_seq == 3


def test_the_hash_window_stays_bounded():
    consumer = FeedConsumer(reorg_window=8)
    for seq in range(200):
        list(consumer._frame(frame(entry(seq, hash_of(seq % 251))), time.time(), True))
    assert len(consumer._hashes) <= consumer.reorg_window * 2


# --------------------------------------------------------------------------- #
# the filter call
# --------------------------------------------------------------------------- #


def test_a_well_formed_hash_builds_the_expected_call():
    body = is_filtered_call("0x" + "ab" * 32)
    assert body["params"][0]["data"] == "0x85c733a4" + "ab" * 32
    assert body["params"][0]["to"].endswith("74")


@pytest.mark.parametrize("bad", ["0x", "0xabcd", "0x" + "ab" * 31, "0x" + "ab" * 33, "nonsense"])
def test_a_malformed_hash_is_refused_rather_than_asked_about(bad):
    """A short hash still encodes into a valid call, and the node would answer it."""
    with pytest.raises(ValueError):
        is_filtered_call(bad)
