"""Read frames from a Nitro relay and hand them to the decoder.

    async for msg in FeedConsumer().live():
        for tx in msg.txs:
            ...

This is deliberately thin — the relay does the work of holding the upstream
connection, and `codec` does the work of decoding. What is left is the three things
a bare `websockets.connect` loop still gets wrong against a relay.

**Backlog.** Every new client is replayed the relay's backlog before live messages
start, and requesting a sequence number is the only way to skip it. Measured
against a local relay carrying Robinhood Chain: a default connection is handed
~1,200 messages about two minutes old, and because the relay is local they all
arrive within ~50 ms. So the backlog costs decode time, not wall-clock time — which
is why `live()` skips decoding transactions for messages it is going to drop
anyway. Anything you *time* before the drain is history racing the present.

**Reconnects.** On reconnect we ask for the highest sequence number already seen,
which trims the replay to messages we actually missed. We ask for that number
rather than the next one on purpose: a sequence number past the relay's tail is not
found in its lookup map, and Nitro's documented fallback for a failed lookup is to
send *the entire backlog* (`clientconnection.go`, "error finding requested sequence
number in backlog: sending the entire backlog instead"). Re-requesting the last
seen number is always in range, and costs exactly one duplicate, which is dropped.

**Sender recovery on the hot path.** Never done here. `tx.sender` is lazy — ask for
it on the handful of transactions that survive your filter, not on all of them.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

import websockets

from .codec import FeedMessage, parse_frame

try:  # 2-3x faster than the stdlib on frames this size; optional
    from orjson import loads as _loads
except ImportError:  # pragma: no cover
    from json import loads as _loads

#: A relay you run. This is the default because it is what you should be pointing at.
DEFAULT_RELAY = "ws://127.0.0.1:9642"

#: Robinhood's public endpoints. Rate-limited per client and not recommended for
#: production by Robinhood themselves — point a relay at these, not your bot.
MAINNET_FEED = "wss://feed.mainnet.chain.robinhood.com"
TESTNET_FEED = "wss://feed.testnet.chain.robinhood.com"

FEED_CLIENT_VERSION = "Arbitrum-Feed-Client-Version"
REQUESTED_SEQ = "Arbitrum-Requested-Sequence-Number"


class FeedConsumer:
    """One connection to a relay, reconnecting, with backlog and duplicate handling."""

    def __init__(
        self,
        url: str = DEFAULT_RELAY,
        *,
        live_threshold: float = 5.0,
        max_backlog_seconds: float = 120.0,
        reconnect_delay: float = 0.5,
        max_reconnect_delay: float = 30.0,
    ) -> None:
        self.url = url
        self.live_threshold = live_threshold
        self.max_backlog_seconds = max_backlog_seconds
        self.reconnect_delay = reconnect_delay
        self.max_reconnect_delay = max_reconnect_delay

        self.stats = {
            "frames": 0,
            "backlog_messages": 0,
            "live_messages": 0,
            "duplicate_messages": 0,
            "reconnects": 0,
        }
        self._live = False
        self._highest_seq = -1

    @property
    def is_live(self) -> bool:
        """Whether the backlog has drained. `live()` is the stream; this is the flag."""
        return self._live

    @property
    def highest_seq(self) -> int:
        """Highest sequence number seen. On this chain, the L2 block number."""
        return self._highest_seq

    def _headers(self) -> dict[str, str]:
        headers = {FEED_CLIENT_VERSION: "2"}
        if self._highest_seq >= 0:
            # Re-request the last seen number, not the next one — see module docstring.
            headers[REQUESTED_SEQ] = str(self._highest_seq)
        return headers

    def _judge_live(self, timestamp: int, started: float) -> bool:
        if self._live:
            return True
        now = time.time()
        self._live = (
            # Primary: the sequencer stamps each message; a live one is seconds old.
            (bool(timestamp) and (now - timestamp) <= self.live_threshold)
            # Fallback for a skewed clock: the backlog is finite, so stop waiting.
            or (now - started) > self.max_backlog_seconds
        )
        return self._live

    async def messages(self, decode_backlog: bool = True) -> AsyncIterator[FeedMessage]:
        """Yield every message, backlog first, reconnecting until cancelled.

        Each message carries `.live`. With `decode_backlog=False`, messages from
        before the drain arrive with an empty `.txs` — the frame is still parsed for
        its sequence number, but no transaction is decoded.
        """
        delay = self.reconnect_delay
        while True:
            try:
                async with websockets.connect(
                    self.url, additional_headers=self._headers(), max_size=2**24
                ) as ws:
                    delay = self.reconnect_delay
                    started = time.time()
                    self._live = False
                    async for raw in ws:
                        self.stats["frames"] += 1
                        received = time.time()
                        for msg in self._frame(_loads(raw), started, decode_backlog):
                            msg.received_at = received
                            yield msg
            except asyncio.CancelledError:
                raise
            except Exception:
                self.stats["reconnects"] += 1
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.max_reconnect_delay)

    def _frame(self, frame: dict, started: float, decode_backlog: bool):
        # Decide liveness from the frame's own timestamps before deciding whether the
        # transactions inside are worth decoding.
        entries = frame.get("messages") or ()
        if not entries:
            return
        last = entries[-1]
        stamp = (((last.get("message") or {}).get("message") or {}).get("header") or {}).get(
            "timestamp", 0
        )
        live = self._judge_live(stamp, started)
        for msg in parse_frame(frame, decode_txs=live or decode_backlog):
            if msg.seq <= self._highest_seq:
                self.stats["duplicate_messages"] += 1
                continue
            self._highest_seq = msg.seq
            msg.live = live
            self.stats["live_messages" if live else "backlog_messages"] += 1
            yield msg

    async def live(self) -> AsyncIterator[FeedMessage]:
        """Only messages from after the backlog drained, and only those decoded."""
        async for msg in self.messages(decode_backlog=False):
            if msg.live:
                yield msg
