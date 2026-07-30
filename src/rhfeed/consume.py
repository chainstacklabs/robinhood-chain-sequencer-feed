"""Read frames from a Nitro relay and hand them to the decoder.

    async for msg in FeedConsumer().live():
        for tx in msg.txs:
            ...

This is deliberately thin — the relay does the work of holding the upstream
connection, and `codec` does the work of decoding. What is left is the handful of
things a bare `websockets.connect` loop still gets wrong against a relay.

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

**Reorgs.** A re-sent sequence number is not necessarily a duplicate. Nitro has no
reorg message type at all: `addMessagesAndReorg` re-broadcasts from the reorg point, so
the replacement arrives under a sequence number you have already seen, carrying a
different `blockHash`. Comparing that hash is the only detection the protocol offers,
which is why recent hashes are kept here. A real reorg rewinds the watermark, is
counted in `stats["reorgs"]`, warns, and arrives with `msg.reorg` set; a true duplicate
is still dropped silently. Worth knowing that a relay hop destroys this signal —
`broadcastclients.go` dedups by sequence number on a 10-second window and returns the
replacement to nobody — so reorg visibility is a reason to read the sequencer feed
directly even though a relay is the better place to fan out from.

**Sender recovery on the hot path.** Never done here. `tx.sender` is lazy — ask for
it on the handful of transactions that survive your filter, not on all of them.

**Signatures.** Pass `verify=MAINNET_VERIFIER` and messages that do not carry a good
signature from the chain's sequencer key are dropped before you see them. A rejected
message does not advance the sequence watermark, so injecting one cannot make a
reconnect skip the real ones behind it. See `verify.py` for what the signature covers.

Off by default for backward compatibility only. Pointing at a relay you run is *not* a
reason to skip it: the stock `relay` binary verifies nothing. It passes `nil` for
`NewBroadcastClients`' `addrVerifier` because it has no L1 connection to resolve batch
posters with, and `DefaultFeedVerifierConfig` ships `AcceptMissing: true`, so
`verifier.go` short-circuits every signature to accepted. The relay copies `signatureV2`
through verbatim and refuses to re-sign, which is what makes end-to-end verification
work — but it means the checking has to happen here.

**Silence.** A relay that is not running, a relay whose own upstream connection is
down, a relay still replaying its backlog, and a chain that happens to be quiet all
look identical from here: no messages. So this module says which one it is, through
`logging` — warnings for a connection that will not open and for one that has gone
quiet, info for connecting, draining and going live. Warnings reach stderr with no
setup; `logging.getLogger("rhfeed").setLevel(...)` mutes or unmutes the rest.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator

import websockets

from .codec import FeedMessage, parse_frame
from .verify import Verifier

_log = logging.getLogger(__name__)

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
        verify: Verifier | None = None,
        live_threshold: float = 5.0,
        max_backlog_seconds: float = 120.0,
        reconnect_delay: float = 0.5,
        max_reconnect_delay: float = 30.0,
        stall_warning: float = 30.0,
        reorg_window: int = 1024,
    ) -> None:
        self.url = url
        #: Drop messages that do not carry a good signature from an allowed signer.
        #: Off by default for backward compatibility, not because it is unnecessary —
        #: a relay you run verifies nothing of its own (see the module docstring), so
        #: passing `MAINNET_VERIFIER` is the right call against any feed, local or not.
        #: Costs one ECDSA recovery per message, ~0.1 ms.
        self.verify = verify
        self.live_threshold = live_threshold
        self.max_backlog_seconds = max_backlog_seconds
        self.reconnect_delay = reconnect_delay
        self.max_reconnect_delay = max_reconnect_delay
        #: Warn after this long with an open connection and no frames. The chain
        #: produces several blocks a second, so silence this long is a fault
        #: somewhere, usually the relay's own upstream. 0 disables the check.
        self.stall_warning = stall_warning
        #: How many recent block hashes to keep for reorg detection. At ~115 ms a block
        #: the default covers about two minutes, comfortably past the 10-second window
        #: within which a relay would have swallowed the replay anyway.
        self.reorg_window = reorg_window

        self.stats = {
            "frames": 0,
            "backlog_messages": 0,
            "live_messages": 0,
            "duplicate_messages": 0,
            "unverified_messages": 0,
            "reconnects": 0,
            "reorgs": 0,
        }
        self._live = False
        self._highest_seq = -1
        self._last_frame_at = 0.0
        self._warned_unverified = False
        self._hashes: dict[int, str] = {}

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
        if self._live:
            _log.info(
                "live — %d backlog messages skipped, at seq %d",
                self.stats["backlog_messages"],
                self._highest_seq,
            )
        return self._live

    async def _monitor(self) -> None:
        """Narrate an open connection that has not started producing yet.

        Two silences look identical from outside and mean different things. A relay
        whose own upstream is down holds the socket open and never sends anything —
        that is a fault, and it warns. A relay still replaying its backlog is sending
        plenty, none of it live yet — that is normal, and it just reports progress.
        """
        # Poll well inside the threshold, or a stall that starts just after a tick
        # goes unreported for twice as long as advertised.
        interval = max(self.stall_warning / 4, 0.1)
        warned = False
        while True:
            await asyncio.sleep(interval)
            if not self._last_frame_at:
                # No connection to be stalled on; the reconnect loop is already saying so.
                warned = False
                continue

            idle = time.monotonic() - self._last_frame_at
            if idle >= self.stall_warning:
                if not warned:
                    warned = True  # once per stall, not once per poll
                    _log.warning(
                        "no frames from %s for %.0fs — connected, but nothing is arriving. "
                        "Usually the relay's own upstream is down "
                        "(check: docker compose logs relay)",
                        self.url,
                        idle,
                    )
                continue

            warned = False
            if not self._live:
                # Frames are flowing but none are current yet. Common on a relay that
                # has just started, because it is replaying its own upstream backlog.
                _log.info(
                    "draining backlog — %d messages so far, none current yet",
                    self.stats["backlog_messages"],
                )

    async def messages(self, decode_backlog: bool = True) -> AsyncIterator[FeedMessage]:
        """Yield every message, backlog first, reconnecting until cancelled.

        Each message carries `.live`. With `decode_backlog=False`, messages from
        before the drain arrive with an empty `.txs` — the frame is still parsed for
        its sequence number, but no transaction is decoded.
        """
        delay = self.reconnect_delay
        failures = 0
        monitor = asyncio.create_task(self._monitor()) if self.stall_warning else None
        try:
            while True:
                try:
                    _log.info("connecting to %s", self.url)
                    async with websockets.connect(
                        self.url, additional_headers=self._headers(), max_size=2**24
                    ) as ws:
                        if failures:
                            _log.warning("%s is reachable again", self.url)
                        failures = 0
                        delay = self.reconnect_delay
                        started = time.time()
                        self._live = False
                        self._last_frame_at = time.monotonic()
                        async for raw in ws:
                            self.stats["frames"] += 1
                            received = time.time()
                            mono = self._last_frame_at = time.monotonic()
                            for msg in self._frame(_loads(raw), started, decode_backlog):
                                msg.received_at = received
                                msg.received_monotonic = mono
                                yield msg
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    failures += 1
                    self.stats["reconnects"] += 1
                    # Never fail silently here. A relay that was never started is the
                    # single most common way to end up staring at an empty terminal,
                    # and the retry loop would otherwise hide it forever.
                    _log.warning(
                        "cannot read %s (%s: %s) — retrying in %.1fs%s",
                        self.url,
                        type(exc).__name__,
                        exc,
                        delay,
                        "; is the relay running? (docker compose up -d relay)"
                        if failures == 1
                        else "",
                    )
                    self._last_frame_at = 0.0
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, self.max_reconnect_delay)
        finally:
            if monitor is not None:
                # Cancel without awaiting: this runs during aclose(), which may itself
                # be unwinding a cancellation.
                monitor.cancel()

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
        messages = parse_frame(frame, decode_txs=live or decode_backlog)
        # parse_frame returns one message per entry, in order. strict=True turns any
        # future drift in that contract into an error rather than a signature checked
        # against the wrong message.
        for entry, msg in zip(entries, messages, strict=True):
            # Verify before the watermark is consulted, not after. Both branches below
            # can move the watermark — forward for a new message, *backward* for a
            # reorg — so a message that cannot be trusted must not reach either.
            if self.verify is not None and not self.verify.accepts(entry):
                self._reject(entry, msg.seq)
                continue
            if msg.seq <= self._highest_seq:
                if not self._is_reorg(msg):
                    self.stats["duplicate_messages"] += 1
                    continue
                # A reorg replaces this sequence number and everything after it, so the
                # watermark rewinds to just below it and the replacements that follow
                # are delivered as ordinary new messages.
                self.stats["reorgs"] += 1
                msg.reorg = True
                self._highest_seq = msg.seq - 1
                _log.warning(
                    "feed reorg at seq %d: block hash changed from %s to %s. "
                    "Anything you derived from the old block is now stale",
                    msg.seq,
                    self._hashes.get(msg.seq),
                    msg.block_hash,
                )
            self._highest_seq = msg.seq
            self._remember(msg)
            msg.live = live
            self.stats["live_messages" if live else "backlog_messages"] += 1
            yield msg

    def _is_reorg(self, msg: FeedMessage) -> bool:
        """Whether a re-sent sequence number is a replacement rather than a duplicate.

        Nitro has no reorg message type. `addMessagesAndReorg` simply re-broadcasts from
        the reorg point, so the same sequence number arrives again carrying a different
        block hash — that difference is the entire signal, which is why `block_hash` has
        to be kept rather than decoded and dropped.

        Unknown hash on either side means unknown, so this says no. Better to treat a
        reorg as a duplicate than to rewind the watermark on a message we cannot compare.
        """
        previous = self._hashes.get(msg.seq)
        return bool(previous and msg.block_hash) and previous != msg.block_hash

    def _remember(self, msg: FeedMessage) -> None:
        """Record seq -> block hash, keeping the window bounded."""
        if not msg.block_hash:
            return
        self._hashes[msg.seq] = msg.block_hash
        if len(self._hashes) > self.reorg_window * 2:
            cutoff = self._highest_seq - self.reorg_window
            self._hashes = {s: h for s, h in self._hashes.items() if s > cutoff}

    def _reject(self, entry: dict, seq: int) -> None:
        """Drop a message that failed verification, without advancing the watermark.

        Not advancing matters: the watermark is what a reconnect re-requests from, so
        honouring a rejected sequence number would let anything that can inject one
        frame make us skip the real messages behind it.
        """
        self.stats["unverified_messages"] += 1
        if self._warned_unverified:
            return
        # Once, then counted. A wrong chain id or a stale signer set rejects *every*
        # message, and a warning per message would bury the reason it started.
        self._warned_unverified = True
        signer = self.verify.signer_of(entry)
        _log.warning(
            "dropping unverified message at seq %d: %s, expected one of %s. "
            "Further ones are counted in stats['unverified_messages'] but not logged. "
            "Check the verifier's chain id (%d) and signer set before suspecting the feed",
            seq,
            "signed by 0x" + signer.hex() if signer else "no usable signature",
            ", ".join(sorted("0x" + s.hex() for s in self.verify.signers)),
            self.verify.chain_id,
        )

    async def live(self) -> AsyncIterator[FeedMessage]:
        """Only messages from after the backlog drained, and only those decoded."""
        async for msg in self.messages(decode_backlog=False):
            if msg.live:
                yield msg
