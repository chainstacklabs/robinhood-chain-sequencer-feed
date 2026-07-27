<img width="1200" alt="Labs" src="https://user-images.githubusercontent.com/99700157/213291931-5a822628-5b8a-4768-980d-65f324985d32.png">

<p>
 <h3 align="center">Chainstack is the leading suite of services connecting developers with Web3 infrastructure</h3>
</p>

<p align="center">
  • <a target="_blank" href="https://chainstack.com/">Homepage</a> •
  <a target="_blank" href="https://chainstack.com/protocols/">Supported protocols</a> •
  <a target="_blank" href="https://chainstack.com/blog/">Chainstack blog</a> •
  <a target="_blank" href="https://docs.chainstack.com/quickstart/">Blockchain API reference</a> • <br> 
  • <a target="_blank" href="https://console.chainstack.com/user/account/create">Start for free</a> •
</p>

# See Robinhood Chain transactions before they execute

Robinhood Chain has **no public mempool**. A transaction is invisible until
Robinhood's sequencer decides its order — and the sequencer announces that decision
on one WebSocket, before the transaction runs. Everything else (RPC, explorers,
indexers) finds out later.

This repo turns that WebSocket into structured data:

```
seq 20555512  2 tx
    0xb7ef877253db5328f1e53b12afd6ca144186e69b7e7ab28b7886bbbf36870aeb  call  0xb02aD7d2C0  0xb6621842
    0xe8867c10f4d1deb07aa266220c39705969008c8caa4b04b2627a8051385ac8cc  call  0x50B98EcdE3  0x00000000
```

Copy-trading, liquidation alerts, flow analytics — that's yours to build. This is
the part underneath it: get the data, decode it fast, hand it over.

## Try it

You need Docker and Python 3.11+.

```bash
docker compose up -d relay    # give it ~20s to connect
uv sync
uv run rhfeed watch           # Ctrl-C to stop
```

That's it. Columns are `hash · kind · to · selector`, grouped by block.

**Why the Docker step?** That's Offchain Labs' official relay — one connection to
Robinhood's feed, re-served to as many local consumers as you like. You want it
because Robinhood rate-limits **per client, not per connection**, so opening five
sockets yourself splits one client's budget five ways. It's also cheap: 23 MB of
memory and ~1.6% of a core, nothing written to disk. It is *not* a full node.

## Filter it

```bash
uv run rhfeed watch --selector 0x095ea7b3     # ERC-20 approvals only
uv run rhfeed watch --to 0xcaf681a6...        # one contract
uv run rhfeed watch --sender 0xabc...         # one wallet
uv run rhfeed watch --min-value 1000000000000000000   # ≥ 1 ETH
uv run rhfeed watch --sender-column           # show who sent each tx
uv run rhfeed watch --json                    # machine-readable
```

Not sure what to filter on? Record a minute and see what's actually busy:

```bash
uv run rhfeed capture --seconds 60 session.jsonl
uv run python examples/replay_capture.py session.jsonl
```

## Use it from Python

```python
import asyncio
from rhfeed import FeedConsumer, addr, selector_of

TOKENS = {addr("0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec")}  # NVDA
TRANSFER = selector_of("transfer(address,uint256)")


async def main():
    async for msg in FeedConsumer().live():
        for tx in msg.txs:
            if tx.to_bytes in TOKENS and tx.selector == TRANSFER:
                print(msg.seq, tx.hash, "from", tx.sender)


asyncio.run(main())
```

Three worked examples:

| | |
|---|---|
| [`token_flow.py`](examples/token_flow.py) | watch specific tokens — the cheap-filter pattern, start here |
| [`copy_trade_signals.py`](examples/copy_trade_signals.py) | follow wallets, emit JSON signals |
| [`replay_capture.py`](examples/replay_capture.py) | run the same code offline against a recording |

## Read this before you trade on it

**These are pre-confirmations, not settled transactions.** The sequencer has
promised an ordering. Nothing has executed and nothing has been posted to Ethereum
yet. Confirm against a node before you treat anything as final.

**Transactions here can be censored at the protocol level.** Robinhood Chain runs
ArbOS 61 compliance filtering: an authorised party registers a transaction hash and
the chain refuses to execute it — even if it arrived through Ethereum's
force-inclusion path. **A transaction can appear in this feed and never take
effect.** `rhfeed.is_filtered_call(tx_hash)` builds the `eth_call` that tells you
whether a hash has been registered; send it to a node.

**There is nothing to front-run.** The feed reports what the sequencer already
decided. You're reading, not racing.

## Speed

Your head start over an RPC node is single-digit milliseconds, so decoding has to be
cheap. The decoder is built around that: filter on the fields that are free, and
never compute the expensive ones for a transaction you are going to discard.

`to_bytes` and `selector` are raw bytes, sliced straight out of the envelope.
`hash`, `to` and `sender` are computed on first access, then cached.

| What you touch | Per transaction |
|---|---|
| `to_bytes`, `selector`, `value`, `nonce`, `gas` | ~4 µs |
| `+ hash` | ~10 µs |
| `+ to` (checksummed) | ~18 µs |
| `+ sender` (ECDSA recovery) | ~70 µs |

A transaction you filter out therefore costs about 5% of a full decode. For scale:
the chain runs ~14.5 blocks/s and ~71 tx/s, and one core handles roughly 14,000
fully decoded transactions a second — decoding everything is ~0.5% of a core.

Two things account for most of that. RLP is walked by an offset scanner that copies
nothing, instead of building an object tree per transaction: ~2x faster than `rlp`.
Sender recovery reconstructs the signing payload from offsets already scanned and
calls coincurve directly, instead of handing the envelope to a library that decodes
it a second time: ~5.3x faster than `eth_account`, 44 µs against 233 µs.

Hand-rolling RLP, EIP-55 checksumming and signature recovery is only worth doing if
they are exactly right, so `tests/` compares each against `rlp`, `eth_utils` and
`eth_account` — on synthetic envelopes of every type, and on 143 real mainnet
transactions:

```bash
uv run --extra dev pytest
```

## Endpoints

| What | Endpoint |
|---|---|
| Sequencer feed | `wss://feed.mainnet.chain.robinhood.com` |
| Testnet feed | `wss://feed.testnet.chain.robinhood.com` |
| Public RPC | `https://rpc.mainnet.chain.robinhood.com` |

Point the relay at the feed; point everything else at a node. Robinhood documents
both public endpoints as rate-limited and not for production. Chainstack has
[Robinhood Chain nodes](https://docs.chainstack.com/reference/robinhood-getting-started)
and you can [start for free](https://console.chainstack.com/user/account/create) — a
node answers for the receipts, logs and state the feed deliberately leaves out, and
for the compliance-filter check above.

---

<details>
<summary>Details worth knowing once you're past the basics</summary>

**One message = one block.** The sequence number *is* the L2 block number. Every
block also contains one `ArbitrumInternalTx` the feed never carries, because the
chain generates it rather than receiving it.

**The backlog.** Every new client — of the public feed *and* of your own relay — is
replayed history before live messages start. Measured against a local relay:

| Connecting with | First message age | Messages before live |
|---|---|---|
| nothing | 124 s | 1,203 |
| `Arbitrum-Requested-Sequence-Number: <last seen>` | 1 s | 1 |
| `Arbitrum-Requested-Sequence-Number: <absurdly high>` | 131 s | **1,278** |

That last row is a trap: a sequence number past the relay's tail isn't in its lookup
table, and Nitro's fallback for a failed lookup is to send *the entire backlog*
([`clientconnection.go`](https://github.com/OffchainLabs/nitro/blob/master/wsbroadcastserver/clientconnection.go)).
`FeedConsumer` handles it by re-requesting the highest sequence number it has already
seen — always in range, one duplicate, dropped. On a first connection it just drains,
which locally takes ~50 ms, and it skips decoding those transactions entirely.

**Any Orbit chain.** This is Nitro's standard broadcaster protocol, so it works
against any Arbitrum Orbit chain that exposes a feed — point `--node.feed.input.url`
and `--chain.id` somewhere else.

**The timings above** were measured on one developer machine. The ratios between
them hold generally; the absolute microseconds depend on your hardware.

</details>
