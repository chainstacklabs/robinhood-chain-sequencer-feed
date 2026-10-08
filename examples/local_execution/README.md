# Execute the feed on your own machine

> **Experimental.** A proof of concept, not for production use.

The decoder tells you what a transaction asks for. This example executes it: blocks from the
sequencer feed run through an embedded EVM ([revm](https://github.com/bluealloy/revm), through
[pyrevm](https://github.com/paradigmxyz/pyrevm)) on your machine, and the logs that come out —
`Swap`, `Transfer`, the amounts actually filled — are compared with the chain's own receipts.

It needs no node. It needs the *state* the blocks read, and this example shows how little that
is: a few hundred kilobytes for a handful of blocks, after which execution runs with no RPC at
all.

```bash
uv sync --extra exec
RH_RPC_URL=https://your-robinhood-chain-endpoint uv run --extra exec \
    python examples/local_execution/execute.py --blocks 3
```

## How it works

```
feed ──► rhfeed: parse, recover sender ──► revm ──► status + logs ──► compare with the receipt
                                            ▲
     forked:   state read from the node as needed; the trace records every slot touched
     snapshot: exactly those slots and accounts, as they stood before the first block
     offline:  the same blocks from the snapshot alone — no RPC
```

1. **Record.** Take the next blocks off the public feed. Each block's receipts are fetched only
   to score the result; the executor never sees them.
2. **Forked.** Execute the blocks in an EVM forked from a node at the block before them. A value
   the EVM has not seen is read once from the node; everything a transaction writes stays local,
   so the next block sees it. The opcode trace records every storage slot and account touched.
3. **Snapshot.** Read exactly those slots and accounts as they stood at that block, from a fresh
   fork, and write them to a file. This is the flat state — key to value, no trie, no history —
   that the blocks need.
4. **Offline.** Start a new EVM from the snapshot alone and execute the same blocks again. Each
   transaction is compared with the receipt and with the forked run.

## Results

Three consecutive mainnet blocks, Oct 8, 2026:

| | forked | offline |
|---|---|---|
| state | read from the node | 22 accounts, 85 slots, 405 KB |
| time per block | 7.2 s (one RPC round trip per unseen slot) | **0.22 ms** |
| same status and logs as the forked run | — | **7 of 7** |
| logs identical to the receipt | 3 of 6 | 3 of 6 |

The three misses are one bot contract, selector `0xb6621842`, which reverts locally with
`Error(NP)` and succeeds on chain. It checks something this example sets to zero — gas price,
base fee or coinbase. Every other transaction emitted the chain's logs byte for byte, amounts
included, among them one with 15 logs.

So: with the right flat state, a laptop executes feed blocks exactly, with no RPC and in well
under a millisecond. The open question is not execution. It is having the state a block needs
before the block arrives.

## What this does not model

- **ArbOS precompiles** (`0x64`–`0x74`). Robinhood Chain is an Arbitrum chain; revm has no
  ArbOS. A call into one returns nothing. Rare for trades — they read ArbOS, they don't write it.
- **Fees.** Gas price, base fee and coinbase are zero, so ether balances drift by the fees.
  Token logs are unaffected; contracts that check the gas price are not.
- **Deposits and the per-block internal transaction.** Messages that arrive from Ethereum and
  the transaction ArbOS adds to every block are not executed.
- **`NUMBER`** returns the parent-chain block number on Arbitrum; it is set from the feed message.

## What would make it a client

- **The state, ahead of time.** Here the snapshot is built from the blocks it then executes. A
  client needs it before the block: a hot set of recently touched slots, or the chain's full flat
  state at one block, kept current by executing every block from the feed. A token or pool
  created after that point is created by a transaction in the feed, so it needs no download.
- **State on disk.** pyrevm keeps state in memory; a full flat state needs a revm database backed
  by an on-disk key-value store.
- **Trust.** Every value can be checked against a state root with `eth_getProof`, the way
  [paradigmxyz/stateless](https://github.com/paradigmxyz/stateless) checks an execution witness,
  so the node that served the snapshot need not be trusted.
