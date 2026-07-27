"""Decode a capture file instead of a live socket — the basis of a backtest.

    rhfeed capture --seconds 600 session.jsonl
    uv run python examples/replay_capture.py session.jsonl

Same decoder, no network. Whatever strategy you write against `FeedConsumer` should
run unchanged here, which is how you test it against known traffic before pointing
it at a live relay. This prints a profile of what the capture contained.
"""

from __future__ import annotations

import json
import sys
from collections import Counter

from rhfeed import parse_frame


def main(path: str) -> None:
    selectors: Counter[str] = Counter()
    recipients: Counter[str] = Counter()
    types: Counter[int] = Counter()
    blocks = txs = deploys = 0
    from_parent = 0

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


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(f"usage: {sys.argv[0]} <capture.jsonl>")
    main(sys.argv[1])
