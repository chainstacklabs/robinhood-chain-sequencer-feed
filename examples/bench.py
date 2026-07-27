"""Reproduce the Speed table in the README on your own hardware.

    uv run python examples/bench.py                  # against tests/frames.jsonl
    uv run python examples/bench.py session.jsonl    # against your own capture

Each row decodes every transaction in the capture and reads one more field than the
row above it, so the differences are what that field costs. The absolute numbers are
your machine's; the ratios are the point, and they are why `hash`, `to` and `sender`
are lazy instead of computed up front.

Add --reference (needs the dev extra) to time the libraries the fast paths replace.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from rhfeed import parse_frame
from rhfeed.codec import decode_transaction

DEFAULT_CAPTURE = Path(__file__).resolve().parent.parent / "tests" / "frames.jsonl"


def load(path: Path) -> list[bytes]:
    """Every transaction envelope in a capture, as raw bytes."""
    raws: list[bytes] = []
    for line in path.read_text().splitlines():
        if line.strip():
            for msg in parse_frame(json.loads(line)):
                raws.extend(tx.raw for tx in msg.txs)
    return raws


def best_of(fn, raws: list[bytes], rounds: int) -> float:
    """Microseconds per transaction, best round.

    Best rather than mean: we are measuring how long the work takes, and every
    source of noise on a shared machine only ever adds time.
    """
    best = float("inf")
    for _ in range(rounds):
        started = time.perf_counter()
        fn(raws)
        best = min(best, (time.perf_counter() - started) / len(raws))
    return best * 1e6


def cheap(raws):
    for tx in map(decode_transaction, raws):
        _ = tx.to_bytes, tx.selector, tx.value, tx.nonce, tx.gas


def with_hash(raws):
    for tx in map(decode_transaction, raws):
        _ = tx.to_bytes, tx.selector, tx.hash


def with_to(raws):
    for tx in map(decode_transaction, raws):
        _ = tx.to_bytes, tx.selector, tx.hash, tx.to


def with_sender(raws):
    for tx in map(decode_transaction, raws):
        _ = tx.to_bytes, tx.selector, tx.hash, tx.to, tx.sender


ROWS = [
    ("to_bytes, selector, value, nonce, gas", cheap, 20),
    ("+ hash", with_hash, 20),
    ("+ to (checksummed)", with_to, 20),
    ("+ sender", with_sender, 5),
]


def reference(raws: list[bytes]) -> None:
    """The libraries the hand-written paths replace, for the 2x / 5x claims."""
    try:
        import rlp
        from eth_account import Account
    except ImportError:
        sys.exit("rhfeed: --reference needs the dev extra:  uv run --extra dev python ...")

    legacy = [r for r in raws if r and r[0] >= 0x80]
    print(f"\nreference implementations ({len(raws)} tx, {len(legacy)} of them legacy)")
    print(
        f"  eth_account.recover_transaction      "
        f"{best_of(lambda rs: [Account.recover_transaction(r) for r in rs], raws, 3):8.1f} us/tx"
    )
    if legacy:
        print(
            f"  rlp.decode (legacy envelopes only)   "
            f"{best_of(lambda rs: [rlp.decode(r) for r in rs], legacy, 20):8.1f} us/tx"
        )
        print(f"  rhfeed scan (legacy envelopes only)  {best_of(cheap, legacy, 20):8.1f} us/tx")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("capture", nargs="?", type=Path, default=DEFAULT_CAPTURE)
    ap.add_argument("--reference", action="store_true", help="also time eth-account and rlp")
    args = ap.parse_args()

    if not args.capture.exists():
        sys.exit(f"rhfeed: no such capture: {args.capture}")
    raws = load(args.capture)
    if not raws:
        sys.exit(f"rhfeed: {args.capture} contained no transactions")

    print(f"{len(raws)} transactions from {args.capture}\n")
    print(f"{'what you read':<40}{'per tx':>10}")
    for label, fn, rounds in ROWS:
        print(f"{label:<40}{best_of(fn, raws, rounds):>7.1f} us")

    per_core = 1e6 / best_of(with_sender, raws, 5)
    print(f"\none core, full decode including sender: ~{per_core:,.0f} tx/s")

    if args.reference:
        reference(raws)


if __name__ == "__main__":
    main()
