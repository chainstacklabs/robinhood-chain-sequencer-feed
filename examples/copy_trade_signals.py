"""Follow a set of wallets and emit a signal when the sequencer orders a trade we can decode.

    uv run python examples/copy_trade_signals.py 0xabc... 0xdef...

This is the decode half of a copy-trading bot and nothing more: it turns the feed
into structured signals on stdout. What you do with a signal — mirror the trade,
size it, alert on it, ignore it — is yours to write. Two things it will not do for
you are pretending a signal is settled and pretending it cannot be censored; both
are explained at the bottom of this file and in the README. A followed wallet's
transaction that decodes to no intent emits nothing, and costs no recovery.

**The cost of following a wallet.** Matching on `intent.actor` is a set lookup for
bundled and Relay trades: the decoder has already pulled the wallet out of the
calldata. Recovery happens only for recognised trades whose intent names no actor (a
plain EOA trade), and recovery is ~95% of what decoding a transaction costs (~46 us against
~2 us for the fields). At Robinhood Chain's current rate that is a few percent of one
core, so it is affordable. Emitting `sender` in the signal costs one recovery per emitted
signal, whatever the actor. WATCH_CONTRACTS below filters on the transaction's `to`: right
for plain EOA trades through a known router, wrong for 4337 bundles and Relay fills,
where `to` is the EntryPoint or the Relay router, never the token.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

from rhfeed import FeedConsumer, addr, decode_intents

# Optional prefilter on the transaction's `to`. Right for plain EOA trades through a known
# router; wrong for 4337 bundles and Relay fills, where `to` is the EntryPoint or the
# Relay router. Leave it empty to follow FOMO wallets.
WATCH_CONTRACTS: set[bytes] = set()
# e.g. {addr("0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec")}   # NVDA


async def main(follow: set[bytes]) -> None:
    consumer = FeedConsumer()
    seen = 0
    started = time.time()

    async for msg in consumer.live():
        for tx in msg.txs:
            # Cheap gate first: raw bytes the decoder already sliced out.
            if WATCH_CONTRACTS and tx.to_bytes not in WATCH_CONTRACTS:
                continue
            for intent in decode_intents(tx):
                # The wallet the trade is for. Only when calldata names none do we
                # pay for sender recovery.
                actor = intent.actor if intent.actor is not None else tx.sender_bytes
                if actor not in follow:
                    continue

                seen += 1
                print(
                    json.dumps(
                        {
                            "signal": "followed_wallet_tx",
                            "block": msg.seq,  # sequence number == L2 block number
                            "sequencer_time": msg.timestamp,
                            "seen_at": round(msg.received_at, 3),
                            "hash": tx.hash,
                            "intent": intent.as_dict(),
                            # Signer, so you can see when the claimed actor differs.
                            "sender": tx.sender,
                            # Soft confirmation. Not a fill, not a receipt, not final.
                            "status": "soft_confirmed",
                        }
                    ),
                    flush=True,
                )

    print(f"# {seen} signals in {time.time() - started:.0f}s", file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(f"usage: {sys.argv[0]} <address> [address ...]")
    asyncio.run(main({addr(a) for a in sys.argv[1:]}))

# ---------------------------------------------------------------------------
# Before you trade on this
#
# 1. A feed message is a soft confirmation. The sequencer has committed to an
#    ordering and has already built the block, but the message carries no outcome
#    and the batch has not been posted to Ethereum. Confirm against a node before
#    you treat anything as settled.
#
# 2. Robinhood Chain filters transactions at the protocol level (ArbOS 61). An
#    authorised filterer can register a hash and the chain voids it — included in a
#    block, status 0x0, gas burned — including transactions that arrive through
#    Ethereum's force-inclusion path. A transaction can appear in this feed and
#    never take effect.
#    `rhfeed.is_filtered_call(tx_hash)` builds the eth_call that answers whether a
#    hash is registered; send it to an RPC node.
#
# 3. You are reading, not racing. The feed tells you what the sequencer already
#    decided. There is no mempool on this chain to front-run.
#
# 4. `actor` is read from calldata — a Relay fill's recipient or a 4337 operation's
#    sender — and the feed does not verify it. Anyone can send a transaction naming a
#    wallet you follow. The receipt is what confirms it.
# ---------------------------------------------------------------------------
