"""Decode saved frames instead of a live socket — the basis of a backtest.

    uv run python examples/replay_capture.py                 # the bundled frames
    uv run python examples/replay_capture.py session.jsonl   # your own

Same decoder, no network, no relay. Whatever you write against `FeedConsumer` runs
unchanged here, which is how you test it against known traffic first. This prints a
profile of what the frames contained — the quickest way to find out which contracts,
selectors and wallets are worth filtering on.

Note which tallies are cheap and which is not. Contracts and selectors come from
bytes the decoder already sliced out. Senders do not exist in a transaction and have
to be recovered from the signature, one at a time, at roughly fifteen times the cost
of everything else together. Offline that only means waiting; on a live stream it is
the thing you arrange not to do, which is why `Tx.sender` is lazy.

Saving your own is four lines of `websockets`: connect to the relay and write each
raw message to a file, one per line. That is exactly the format read here, and how
`tests/frames.jsonl` was made.
"""

from __future__ import annotations

import json
import sys
import time
from collections import Counter
from pathlib import Path

from rhfeed import parse_frame

BUNDLED = Path(__file__).resolve().parent.parent / "tests" / "frames.jsonl"


def main(path: str) -> None:
    selectors: Counter[str] = Counter()
    recipients: Counter[str] = Counter()
    senders: Counter[str] = Counter()
    types: Counter[int] = Counter()
    blocks = txs = deploys = 0
    from_parent = 0
    recovery_seconds = 0.0
    recovered = 0
    # coincurve builds its signing context on the first call, which costs ~16 ms
    # once. Averaged over a small capture that swamps the real per-transaction cost,
    # so the first recovery is excluded from the timing rather than the tally.
    warmed = False

    with open(path) as fh:
        for line in fh:
            if not line.strip():
                continue
            for msg in parse_frame(json.loads(line)):
                blocks += 1
                from_parent += msg.from_parent_chain
                for tx in msg.txs:
                    txs += 1
                    types[tx.tx_type] += 1

                    # The expensive one, timed so the cost is visible rather than
                    # asserted. Live code recovers a sender only for the few
                    # transactions that survive a cheap filter.
                    started = time.perf_counter()
                    sender = tx.sender_bytes
                    if warmed:
                        recovery_seconds += time.perf_counter() - started
                        recovered += 1
                    warmed = True
                    if sender:
                        senders[sender.hex()] += 1

                    if tx.to_bytes is None:
                        deploys += 1
                        continue
                    # to_bytes.hex() avoids the checksum keccak; this is a tally,
                    # not a display path.
                    recipients[tx.to_bytes.hex()] += 1
                    if tx.selector:
                        selectors[tx.selector.hex()] += 1

    print(f"{blocks} blocks, {txs} transactions, {deploys} deploys")
    print(f"{from_parent} messages entered through Ethereum rather than the sequencer")
    print(f"envelope types: {dict(types)}\n")

    print("most-called contracts")
    for address, n in recipients.most_common(10):
        print(f"  0x{address}  {n:>6}  {n / max(txs, 1):.1%}")

    print("\nmost-used selectors")
    for selector, n in selectors.most_common(10):
        print(f"  0x{selector}  {n:>6}  {n / max(txs, 1):.1%}")

    print("\nbusiest wallets — pass one of these to `rhfeed --sender`")
    for address, n in senders.most_common(10):
        print(f"  0x{address}  {n:>6}  {n / max(txs, 1):.1%}")
    if recovered:
        print(
            f"\nrecovering {recovered} senders took {recovery_seconds * 1e3:.0f} ms, "
            f"{recovery_seconds / recovered * 1e6:.0f} us each — the reason a live "
            f"filter matches on `to` and `selector` first"
        )


if __name__ == "__main__":
    if len(sys.argv) > 2:
        sys.exit(f"usage: {sys.argv[0]} [capture.jsonl]")
    main(sys.argv[1] if len(sys.argv) == 2 else str(BUNDLED))
