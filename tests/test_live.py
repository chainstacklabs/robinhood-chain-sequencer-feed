"""Where the backlog ends and the live stream starts.

A relay replays its backlog in frames of hundreds of messages. On 2026-10-29 one frame
from the local relay carried 240 messages stamped 28.5 s down to 0.7 s old, so a frame's
last entry being current says nothing about its first.
"""

from __future__ import annotations

import logging
import time

from rhfeed import FeedConsumer, consume, parse_frame

from .test_reorg import entry, frame, hash_of


def stamped(seq: int, age: float) -> dict:
    e = entry(seq, hash_of(seq % 256))
    e["message"]["message"]["header"]["timestamp"] = int(time.time() - age)
    return e


def run(consumer: FeedConsumer, *frames: dict) -> list:
    started = time.time()
    return [m for f in frames for m in consumer._frame(f, started, False)]


def test_a_frame_that_ends_live_does_not_make_its_backlog_live():
    consumer = FeedConsumer()
    out = run(consumer, frame(stamped(1, 30), stamped(2, 20), stamped(3, 1), stamped(4, 0)))
    assert [m.live for m in out] == [False, False, True, True]
    assert consumer.stats["backlog_messages"] == 2
    assert consumer.stats["live_messages"] == 2


def test_only_the_live_part_of_a_frame_is_decoded(monkeypatch):
    calls = []

    def spy(frame, decode_txs=True, chain_id=None):
        calls.append(([e["sequenceNumber"] for e in frame["messages"]], decode_txs))
        return parse_frame(frame, decode_txs, chain_id)

    monkeypatch.setattr(consume, "parse_frame", spy)
    run(FeedConsumer(), frame(stamped(1, 30), stamped(2, 0)))
    assert sorted(calls) == [([1], False), ([2], True)]


def test_live_stays_live_for_the_rest_of_the_connection():
    # A delayed-inbox message carries its L1 timestamp, minutes old, on the live stream.
    out = run(FeedConsumer(), frame(stamped(1, 0)), frame(stamped(2, 700), stamped(3, 0)))
    assert [m.live for m in out] == [True, True, True]


def test_the_live_log_names_the_first_live_message(caplog):
    with caplog.at_level(logging.INFO, logger="rhfeed.consume"):
        run(FeedConsumer(), frame(stamped(41, 30), stamped(42, 0)))
    (line,) = [r.getMessage() for r in caplog.records if r.getMessage().startswith("live")]
    assert line == "live at seq 42 — 1 backlog messages skipped"


def test_the_live_log_counts_only_this_connections_backlog(caplog):
    consumer = FeedConsumer()
    run(consumer, frame(stamped(1, 30), stamped(2, 0)))
    # A reconnect: messages() resets these for the new connection.
    consumer._live = False
    consumer._backlog_at_connect = consumer.stats["backlog_messages"]
    with caplog.at_level(logging.INFO, logger="rhfeed.consume"):
        run(consumer, frame(stamped(3, 0)))
    (line,) = [r.getMessage() for r in caplog.records if r.getMessage().startswith("live")]
    assert line == "live at seq 3 — 0 backlog messages skipped"


def test_backlog_is_delivered_before_the_consumer_reports_live():
    consumer = FeedConsumer()
    seen = [
        (m.seq, consumer.is_live)
        for m in consumer._frame(
            frame(stamped(1, 30), stamped(2, 20), stamped(3, 0)), time.time(), False
        )
    ]
    assert seen == [(1, False), (2, False), (3, True)]


class _AcceptsAllBut:
    """A verifier that rejects one sequence number: an injected, unsigned entry."""

    chain_id = 4663
    signers = frozenset({b"\x01" * 20})

    def __init__(self, forged: int):
        self.forged = forged

    def accepts(self, entry: dict) -> bool:
        return entry["sequenceNumber"] != self.forged

    def signer_of(self, entry: dict) -> None:
        return None


def test_an_unverified_entry_cannot_make_the_backlog_live():
    """A fresh timestamp on a forged entry must not promote the real backlog after it."""
    consumer = FeedConsumer(verify=_AcceptsAllBut(forged=2))
    out = run(consumer, frame(stamped(1, 30), stamped(2, 0), stamped(3, 20), stamped(4, 0)))
    assert [(m.seq, m.live) for m in out] == [(1, False), (3, False), (4, True)]
    assert consumer.stats["unverified_messages"] == 1
