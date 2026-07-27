"""The command line and the consumer's failure reporting.

`test_codec.py` checks that the decoder is right. This file checks the things a
newcomer hits before the decoder ever runs: argument placement, what a mistyped
address says, and whether a relay that is not there stays silent about it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

import pytest

from rhfeed import FeedConsumer, addr, sel
from rhfeed.cli import ADDR_WIDTH, Filter, build_parser, resolve_feed, short

# --------------------------------------------------------------------------- #
# arguments
# --------------------------------------------------------------------------- #


def test_feed_defaults_to_the_local_relay():
    assert build_parser().parse_args([]).feed == "ws://127.0.0.1:9642"


@pytest.mark.parametrize("name", ["mainnet", "testnet"])
def test_named_feeds_resolve_to_robinhoods_endpoints(name):
    assert resolve_feed(name).startswith("wss://feed.")


def test_an_unrecognised_feed_is_passed_through_as_a_url():
    assert resolve_feed("ws://10.0.0.5:9642") == "ws://10.0.0.5:9642"


# --------------------------------------------------------------------------- #
# what a mistake says
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("value", "message"),
    [
        # The shape you get by copying an address out of the terminal output.
        ("0xcaf681a6...", "not a hex address"),
        ("nonsense", "not a hex address"),
        ("0xcaf681a66d", "not a 20-byte address"),
        ("", "not a 20-byte address"),
    ],
)
def test_addr_says_what_was_wrong(value, message):
    with pytest.raises(ValueError, match=message):
        addr(value)


@pytest.mark.parametrize(
    ("value", "message"),
    [("0xzz", "not a hex selector"), ("0xdead", "not a 4-byte selector")],
)
def test_sel_says_what_was_wrong(value, message):
    with pytest.raises(ValueError, match=message):
        sel(value)


def test_filter_rejects_a_bad_address_with_a_value_error():
    args = build_parser().parse_args(["--to", "0xcaf681a6..."])
    with pytest.raises(ValueError, match="not a hex address"):
        Filter(args)


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #


def test_short_marks_that_it_elided():
    """Elided, not silently truncated — a shortened address is not a valid filter."""
    out = short("0xCaf681a66D020601342297493863E78C959e5cB2")
    assert out.startswith("0xCaf681a66D")
    assert out.endswith("…")
    assert len(out) == ADDR_WIDTH + 3


def test_short_pads_a_deploy_to_the_same_width():
    assert len(short(None)) == len(short("0x" + "ab" * 20))


# --------------------------------------------------------------------------- #
# not failing silently
# --------------------------------------------------------------------------- #


async def _drain(consumer: FeedConsumer, seconds: float) -> None:
    async with contextlib.aclosing(consumer.live()) as stream:
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(seconds):
                async for _ in stream:
                    pass


def test_an_unreachable_relay_is_reported_not_swallowed(caplog):
    """The retry loop must not turn a missing relay into an empty terminal."""
    consumer = FeedConsumer("ws://127.0.0.1:1", reconnect_delay=0.05)
    with caplog.at_level(logging.WARNING, logger="rhfeed.consume"):
        asyncio.run(_drain(consumer, 0.6))

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "a relay that cannot be reached produced no warning"
    assert "cannot read ws://127.0.0.1:1" in warnings[0]
    assert "is the relay running" in warnings[0]
    assert consumer.stats["reconnects"] > 0


def test_seconds_stops_a_run_that_never_receives_anything():
    """--seconds bounds the run itself, not the gap between messages.

    Without the timeout wrapping the whole stream this hangs forever, because the
    deadline used to be checked only on arrival of a message that never comes.
    """
    consumer = FeedConsumer("ws://127.0.0.1:1", reconnect_delay=0.05)
    asyncio.run(asyncio.wait_for(_drain(consumer, 0.3), timeout=5))


def test_stall_warning_fires_once_per_episode(caplog):
    """A connection that goes quiet warns, and does not then warn every poll."""
    consumer = FeedConsumer("ws://127.0.0.1:1", stall_warning=0.2)

    async def run() -> None:
        # Pretend a connection is open and delivered its last frame a while ago.
        watchdog = asyncio.create_task(consumer._monitor())
        consumer._last_frame_at = time.monotonic() - 10
        await asyncio.sleep(0.5)
        watchdog.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watchdog

    with caplog.at_level(logging.WARNING, logger="rhfeed.consume"):
        asyncio.run(run())

    stalls = [r for r in caplog.records if "nothing is arriving" in r.getMessage()]
    assert len(stalls) == 1, f"expected one stall warning, got {len(stalls)}"


def test_a_draining_backlog_reports_progress_instead_of_warning(caplog):
    """Frames arriving but none current yet is normal on a relay that just started."""
    consumer = FeedConsumer("ws://127.0.0.1:1", stall_warning=0.4)
    consumer.stats["backlog_messages"] = 500

    async def run() -> None:
        monitor = asyncio.create_task(consumer._monitor())
        # Frames are flowing — just none of them live.
        for _ in range(5):
            consumer._last_frame_at = time.monotonic()
            await asyncio.sleep(0.1)
        monitor.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await monitor

    with caplog.at_level(logging.INFO, logger="rhfeed.consume"):
        asyncio.run(run())

    messages = [r.getMessage() for r in caplog.records]
    assert any("draining backlog" in m for m in messages), messages
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)
