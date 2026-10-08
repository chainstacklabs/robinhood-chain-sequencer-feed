# Execute feed blocks locally

> **Experimental.** A proof of concept. Do not use it in production.

The feed does not contain transaction results. This example executes feed blocks in a local EVM
(revm through pyrevm) and compares the logs with the chain receipts.

```bash
uv sync --extra exec
RH_RPC_URL=https://your-robinhood-chain-endpoint uv run --extra exec \
    python examples/local_execution/execute.py --blocks 3
```

## Procedure

1. **Record.** Read the next N blocks from the public feed. Get their receipts from the RPC. The
   script uses the receipts only to compare results.
2. **Forked run.** Execute the blocks in an EVM forked from the RPC at the block before the
   first block. The trace records each storage slot and account that the blocks use.
3. **Snapshot.** Read these slots and accounts at the fork block and write them to a file.
4. **Offline run.** Execute the same blocks again from the snapshot only, with no RPC. Compare
   each transaction with the receipt and with the forked run.

## Result

Three consecutive mainnet blocks, 7 transactions:

| | Forked run | Offline run |
|---|---|---|
| State | read from the RPC | snapshot: 22 accounts, 85 slots, 405 KB |
| Median time per block | 7.2 s | 0.22 ms |
| Same status and logs as the forked run | — | 7 of 7 |
| Same status and logs as the receipt | 4 of 7 | 4 of 7 |

The offline run needs only the state that the blocks use. The 3 differences are calls with
selector `0xb6621842` to three contracts. They revert locally and succeed on chain. The cause is
not known.

## Limits

- **No ArbOS precompiles** (`0x64`–`0x74`). A call to one succeeds with empty return data.
- **No fees.** Gas price and base fee are zero. Coinbase is the Arbitrum sequencer address.
- **No deposits and no ArbOS internal transaction.** Contract deployments are skipped.
- **`NUMBER`** returns the L1 block number from the feed message, as on Arbitrum.
- **State in memory.** pyrevm has no on-disk state database.
- **The snapshot comes after the blocks.** The script builds the snapshot from the blocks that it
  then executes. A live client must have the state before each block arrives.
