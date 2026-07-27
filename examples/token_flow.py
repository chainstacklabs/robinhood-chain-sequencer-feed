"""Watch tokenised-stock transfers as the sequencer orders them.

    uv run python examples/token_flow.py

The cheap-filter case, and the one to copy if you are building alerting or flow
analytics rather than following individual wallets. Both gates — the token contract
and the selector — compare raw bytes the decoder has already sliced out of the
envelope, so the ~95% of the stream that does not match never gets hashed,
checksummed or ECDSA-recovered. On captured traffic that is ~2 us per discarded
transaction against ~72 us for a full decode.

Sender is read only for the handful that match, which is exactly the point of
`tx.sender` being lazy.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter

from rhfeed import FeedConsumer, addr, selector_of

#: Print a scan line this often even when nothing matched, so an idle run is visibly
#: idle rather than indistinguishable from a broken one.
HEARTBEAT_SECONDS = 15.0

# Stock tokens are beacon proxies of one Stock implementation, so they share a
# selector set. Swap in whichever contracts you care about.
#
# Expect long silences on this default. The tokenised float is small — NVDA's whole
# on-chain supply is a few thousand tokens — while the chain's actual traffic is ETH
# and Uniswap. If you want to watch something busy while testing, point TOKENS at a
# router instead: `examples/replay_capture.py` prints which contracts are moving.
TOKENS = {
    addr("0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec"): "NVDA",
}

TRANSFERS = {
    selector_of("transfer(address,uint256)"),
    selector_of("transferFrom(address,address,uint256)"),
}


async def main() -> None:
    consumer = FeedConsumer()
    counts: Counter[str] = Counter()
    scanned = 0
    next_beat = time.monotonic() + HEARTBEAT_SECONDS

    print(f"watching {', '.join(TOKENS.values())} for transfers — quiet is normal", flush=True)

    async for msg in consumer.live():
        for tx in msg.txs:
            scanned += 1
            name = TOKENS.get(tx.to_bytes)
            if name is None or tx.selector not in TRANSFERS:
                continue
            counts[name] += 1
            # Only now is anything expensive computed.
            print(f"block {msg.seq}  {name:<6} {tx.hash}  from {tx.sender}", flush=True)

        # Unconditional, not gated on having matched something: a run with no matches
        # is the expected case here, and it still needs to look alive.
        if time.monotonic() >= next_beat:
            next_beat = time.monotonic() + HEARTBEAT_SECONDS
            print(
                f"# {consumer.stats['live_messages']} blocks, {scanned} tx scanned, "
                f"{sum(counts.values())} matched {dict(counts) or ''}".rstrip(),
                flush=True,
            )


if __name__ == "__main__":
    asyncio.run(main())
