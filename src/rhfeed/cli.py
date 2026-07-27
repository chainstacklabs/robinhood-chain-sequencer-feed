"""Command line: stream decoded transactions from a Nitro relay.

    rhfeed                                    # everything the sequencer orders
    rhfeed --to 0xcaf6...                     # one contract
    rhfeed --selector 0x095ea7b3 --seconds 30 # ERC-20 approvals, for half a minute
    rhfeed --json | jq                        # full addresses, machine-readable

Defaults to `ws://127.0.0.1:9642`, which is where `docker compose up -d relay` puts
the official Nitro relay. `--feed mainnet` goes straight at Robinhood's public
endpoint instead — fine for a look, but it rate-limits per client rather than per
connection, which is the whole reason for running a relay.

Progress and problems go to stderr, transactions to stdout, so a pipe keeps working
while you can still see whether anything is connected.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import sys

from .codec import Tx, addr, sel
from .consume import DEFAULT_RELAY, MAINNET_FEED, TESTNET_FEED, FeedConsumer

FEEDS = {"mainnet": MAINNET_FEED, "testnet": TESTNET_FEED, "relay": DEFAULT_RELAY}

#: How much of an address to print. Full hashes are worth their width because you
#: paste them into an explorer; several addresses per line at 42 characters each are
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

    @property
    def wants_sender(self) -> bool:
        return self.sender is not None

    @property
    def active(self) -> bool:
        return bool(self.to or self.selector or self.sender)

    def keep(self, tx: Tx) -> bool:
        if self.to is not None and tx.to_bytes not in self.to:
            return False
        if self.selector is not None and tx.selector not in self.selector:
            return False
        return not (self.sender is not None and tx.sender_bytes not in self.sender)


async def watch(args: argparse.Namespace) -> None:
    consumer = FeedConsumer(resolve_feed(args.feed))
    try:
        keep = Filter(args)
    except ValueError as exc:
        # A shortened address pasted out of the terminal lands here. Say so in one
        # line instead of unwinding a traceback through bytes.fromhex.
        sys.exit(f"rhfeed: {exc}")
    # Recovering a sender is ~15x the cost of every other field put together, so it
    # happens only when a --sender filter already forced the work.
    show_sender = keep.wants_sender
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


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="rhfeed",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--feed",
        default=DEFAULT_RELAY,
        help=f"relay URL (default {DEFAULT_RELAY}), or 'mainnet' / 'testnet' for "
        "Robinhood's public feed",
    )
    ap.add_argument(
        "--seconds", type=float, help="stop after this long, whether or not anything arrives"
    )
    ap.add_argument("--json", action="store_true", help="one JSON object per message")
    ap.add_argument("--to", action="append", help="only transactions to this address")
    ap.add_argument("--selector", action="append", help="only calls with this 4-byte selector")
    ap.add_argument(
        "--sender",
        action="append",
        help="only transactions from this address, and show who sent each one. "
        "Forces a signature recovery per transaction",
    )
    return ap


def main() -> None:
    args = build_parser().parse_args()
    # Connection notes and warnings are the difference between "nothing is happening"
    # and "nothing is happening *because*". They go to stderr, prefixed like the
    # summary line, so redirecting stdout still leaves them visible.
    logging.basicConfig(level=logging.INFO, format="# %(message)s", stream=sys.stderr)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(watch(args))


if __name__ == "__main__":
    main()
