"""Command line: watch a relay, or capture its frames for replay.

    rhfeed watch                          # decoded transactions from a local relay
    rhfeed capture --seconds 300 out.jsonl
    rhfeed watch --feed mainnet           # straight at the public feed, for a look

Both default to `ws://127.0.0.1:9642`, which is where `docker compose up -d relay`
puts the official Nitro relay. Point `--feed` at a public endpoint only for a quick
look — Robinhood rate-limits those per client, not per connection.

Progress and problems go to stderr, transactions to stdout, so `rhfeed watch --json
| jq` keeps working while you can still see whether anything is connected.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import sys
import time

from .codec import Tx, addr, sel
from .consume import DEFAULT_RELAY, MAINNET_FEED, TESTNET_FEED, FeedConsumer

FEEDS = {"mainnet": MAINNET_FEED, "testnet": TESTNET_FEED, "relay": DEFAULT_RELAY}

#: How much of an address to print. Full hashes are worth their width because you
#: paste them into an explorer; four addresses per line at 42 characters each are
#: not. The ellipsis is deliberate — see `--json` for values you can filter on.
ADDR_WIDTH = 10


def resolve_feed(value: str) -> str:
    return FEEDS.get(value, value)


def short(address: str | None, placeholder: str = "deploy") -> str:
    if address is None:
        return placeholder.ljust(ADDR_WIDTH + 3)
    return f"{address[: ADDR_WIDTH + 2]}…"


@contextlib.asynccontextmanager
async def _forever():
    """Stand-in for `asyncio.timeout` when --seconds was not given."""
    yield


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
    try:
        keep = Filter(args)
    except ValueError as exc:
        # A shortened address pasted out of the terminal lands here. Say so in one
        # line instead of unwinding a traceback through bytes.fromhex.
        sys.exit(f"rhfeed: {exc}")
    # Printing a sender costs a recovery per transaction shown. That is fine for a
    # handful of matches and wasteful for an unfiltered stream, so it is opt-in
    # unless a sender filter already forced the work.
    show_sender = args.sender_column or keep.wants_sender
    shown = 0

    # aclosing, not a bare `async for` + break: the generator is suspended inside the
    # websocket context manager, and letting the GC close it later unwinds that from
    # the wrong task. The timeout goes inside it so it unwinds first, leaving a live
    # generator for aclosing to shut down.
    async with contextlib.aclosing(consumer.live()) as stream:
        # asyncio.timeout, not a deadline checked per message: the whole point of
        # --seconds is to bound a run that might see no messages at all.
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(args.seconds) if args.seconds else _forever():
                async for msg in stream:
                    txs = [t for t in msg.txs if keep.keep(t)] if keep.active else msg.txs
                    if not txs and keep.active:
                        continue
                    shown += len(txs)

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
                            # Full hash: a truncated one is only good for eyeballing,
                            # and you generally want to paste this into an explorer or
                            # a node call. Addresses are elided — use --json for ones
                            # you can feed back into --to / --sender.
                            who = f"{short(t.sender, '?')} -> " if show_sender else ""
                            print(
                                f"    {t.hash}  {t.kind:<8} {who}"
                                f"{short(t.to)}  {t.selector_hex or ''}",
                                flush=True,
                            )

    s = consumer.stats
    counted = "matched" if keep.active else "seen"
    print(
        f"# {shown} transactions {counted} | {s['live_messages']} live messages, "
        f"{s['backlog_messages']} backlog skipped, {s['reconnects']} failed connections",
        file=sys.stderr,
    )


async def cmd_capture(args: argparse.Namespace) -> None:
    """Save raw frames for replay — the basis of a backtest or a decoder test."""
    import websockets

    from .consume import FEED_CLIENT_VERSION

    url = resolve_feed(args.feed)
    written = 0
    deadline = time.monotonic() + args.seconds
    logging.getLogger(__name__).info("capturing from %s for %gs", url, args.seconds)
    # Blocking file writes on the event loop, deliberately: this is a one-shot CLI
    # writing ~15 lines a second, and a thread pool would only add moving parts.
    with open(args.out, "w") as fh:  # noqa: ASYNC230
        # One shot, no reconnect loop. If the relay is not there, say which relay and
        # what to do about it, rather than unwinding a ConnectionRefusedError.
        try:
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
        except OSError as exc:
            sys.exit(
                f"rhfeed: cannot reach {url} ({type(exc).__name__}: {exc})\n"
                f"        is the relay running?  docker compose up -d relay"
            )
    if not written:
        sys.exit(
            f"rhfeed: {url} sent nothing in {args.seconds:g}s — check:  docker compose logs relay"
        )
    print(f"wrote {written} frames to {args.out}", file=sys.stderr)


FEED_HELP = (
    f"relay URL (default {DEFAULT_RELAY}), or 'mainnet' / 'testnet' for Robinhood's "
    "public feed. Accepted before or after the subcommand"
)
QUIET_HELP = "only report problems on stderr, not connection and backlog progress"


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="rhfeed",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--feed", default=DEFAULT_RELAY, help=FEED_HELP)
    ap.add_argument("--quiet", action="store_true", help=QUIET_HELP)

    # The same options again on every subcommand, so that `rhfeed watch --feed
    # mainnet` works as well as `rhfeed --feed mainnet watch`. SUPPRESS is what makes
    # that safe: without it the subparser's default would overwrite whatever was
    # given before the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--feed", default=argparse.SUPPRESS, help=FEED_HELP)
    common.add_argument(
        "--quiet", action="store_true", default=argparse.SUPPRESS, help=QUIET_HELP
    )

    sub = ap.add_subparsers(dest="command", required=True)

    watch = sub.add_parser("watch", help="stream decoded transactions", parents=[common])
    watch.add_argument(
        "--seconds", type=float, help="stop after this long, whether or not anything arrives"
    )
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

    cap = sub.add_parser(
        "capture", help="append raw frames to a .jsonl file for replay", parents=[common]
    )
    cap.add_argument("out")
    cap.add_argument("--seconds", type=float, default=60.0)
    cap.add_argument("--frames", type=int, default=100_000)
    cap.set_defaults(func=cmd_capture)

    return ap


def main() -> None:
    args = build_parser().parse_args()
    # Connection notes and warnings are the difference between "nothing is happening"
    # and "nothing is happening *because*". They go to stderr, prefixed like the
    # summary line, so redirecting stdout still leaves them visible.
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="# %(message)s",
        stream=sys.stderr,
    )
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
