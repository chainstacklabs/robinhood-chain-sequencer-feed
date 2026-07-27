"""Command line: watch a relay, or capture its frames for replay.

    rhfeed watch                          # decoded transactions from a local relay
    rhfeed capture --seconds 300 out.jsonl

Both default to `ws://127.0.0.1:9642`, which is where `docker compose up relay`
puts the official Nitro relay. Point `--feed` at a public endpoint only for a quick
look — Robinhood rate-limits those per client, not per connection.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
import time

from .codec import Tx, addr, sel
from .consume import DEFAULT_RELAY, MAINNET_FEED, TESTNET_FEED, FeedConsumer

FEEDS = {"mainnet": MAINNET_FEED, "testnet": TESTNET_FEED, "relay": DEFAULT_RELAY}


def resolve_feed(value: str) -> str:
    return FEEDS.get(value, value)


class Filter:
    """The filters, applied cheapest first.

    `to` and `selector` are compared as raw bytes against fields the decoder has
    already sliced out, so they cost a set lookup. `sender` triggers ECDSA recovery
    and is therefore checked last, on whatever survived the others.
    """

    def __init__(self, args: argparse.Namespace) -> None:
        self.to = {addr(a) for a in args.to} if args.to else None
        self.selector = {sel(s) for s in args.selector} if args.selector else None
        self.sender = {addr(a) for a in args.sender} if args.sender else None
        self.min_value = args.min_value

    @property
    def wants_sender(self) -> bool:
        return self.sender is not None

    @property
    def active(self) -> bool:
        return bool(self.to or self.selector or self.sender or self.min_value)

    def keep(self, tx: Tx) -> bool:
        if self.to is not None and tx.to_bytes not in self.to:
            return False
        if self.selector is not None and tx.selector not in self.selector:
            return False
        if self.min_value and tx.value < self.min_value:
            return False
        return not (self.sender is not None and tx.sender_bytes not in self.sender)


async def cmd_watch(args: argparse.Namespace) -> None:
    consumer = FeedConsumer(resolve_feed(args.feed))
    keep = Filter(args)
    # Printing a sender costs a recovery per transaction shown. That is fine for a
    # handful of matches and wasteful for an unfiltered stream, so it is opt-in
    # unless a sender filter already forced the work.
    show_sender = args.sender_column or keep.wants_sender
    deadline = time.monotonic() + args.seconds if args.seconds else None
    matched = 0

    # aclosing, not a bare `async for` + break: the generator is suspended inside the
    # websocket context manager, and letting the GC close it later unwinds that from
    # the wrong task.
    async with contextlib.aclosing(consumer.live()) as stream:
        async for msg in stream:
            if deadline and time.monotonic() > deadline:
                break

            txs = [t for t in msg.txs if keep.keep(t)] if keep.active else msg.txs
            if not txs and keep.active:
                continue
            matched += len(txs)

            if args.json:
                print(
                    json.dumps(
                        {
                            "seq": msg.seq,
                            "timestamp": msg.timestamp,
                            "received_at": round(msg.received_at, 6),
                            "kind": msg.l1_kind_name,
                            "from_parent_chain": msg.from_parent_chain,
                            "txs": [t.as_dict(sender=show_sender) for t in txs],
                        }
                    ),
                    flush=True,
                )
            else:
                tag = "" if not msg.from_parent_chain else f" [{msg.l1_kind_name}]"
                print(f"seq {msg.seq}{tag}  {len(txs)} tx", flush=True)
                for t in txs:
                    # Full hash: a truncated one is only good for eyeballing, and you
                    # generally want to paste this into an explorer or a node call.
                    who = f"{(t.sender or '?')[:12]} -> " if show_sender else ""
                    print(
                        f"    {t.hash}  {t.kind:<8} {who}"
                        f"{(t.to or 'deploy')[:12]}  {t.selector_hex or ''}",
                        flush=True,
                    )

    s = consumer.stats
    print(
        f"# {matched} transactions matched | {s['live_messages']} live messages, "
        f"{s['backlog_messages']} backlog skipped, {s['reconnects']} reconnects",
        file=sys.stderr,
    )


async def cmd_capture(args: argparse.Namespace) -> None:
    """Save raw frames for replay — the basis of a backtest or a decoder test."""
    import websockets

    from .consume import FEED_CLIENT_VERSION

    url = resolve_feed(args.feed)
    written = 0
    deadline = time.monotonic() + args.seconds
    # Blocking file writes on the event loop, deliberately: this is a one-shot CLI
    # writing ~15 lines a second, and a thread pool would only add moving parts.
    with open(args.out, "w") as fh:  # noqa: ASYNC230
        async with websockets.connect(
            url, additional_headers={FEED_CLIENT_VERSION: "2"}, max_size=2**24
        ) as ws:
            while time.monotonic() < deadline and written < args.frames:
                remaining = deadline - time.monotonic()
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(remaining, 0.1))
                except TimeoutError:
                    break
                fh.write(raw if isinstance(raw, str) else raw.decode())
                fh.write("\n")
                written += 1
    print(f"wrote {written} frames to {args.out}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="rhfeed", description=__doc__)
    ap.add_argument(
        "--feed",
        default=DEFAULT_RELAY,
        help="relay URL (default), or 'mainnet' / 'testnet' for the public feed",
    )
    sub = ap.add_subparsers(dest="command", required=True)

    watch = sub.add_parser("watch", help="stream decoded transactions")
    watch.add_argument("--seconds", type=float, help="stop after this long")
    watch.add_argument("--json", action="store_true", help="one JSON object per message")
    watch.add_argument("--to", action="append", help="only transactions to this address")
    watch.add_argument("--selector", action="append", help="only calls with this 4-byte selector")
    watch.add_argument(
        "--sender", action="append", help="only transactions from this address (forces recovery)"
    )
    watch.add_argument("--min-value", type=int, default=0, help="only transfers of at least N wei")
    watch.add_argument(
        "--sender-column", action="store_true", help="resolve and print senders (costs a recovery)"
    )
    watch.set_defaults(func=cmd_watch)

    cap = sub.add_parser("capture", help="append raw frames to a .jsonl file for replay")
    cap.add_argument("out")
    cap.add_argument("--seconds", type=float, default=60.0)
    cap.add_argument("--frames", type=int, default=100_000)
    cap.set_defaults(func=cmd_capture)

    return ap


def main() -> None:
    args = build_parser().parse_args()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
