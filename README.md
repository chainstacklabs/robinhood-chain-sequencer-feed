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

# Robinhood Chain sequencer feed decoder

> **Experimental.** A demo. Do not use it in production.

Robinhood Chain has no public mempool. The sequencer publishes each block on a WebSocket feed.
A feed message contains the transaction order and the calldata. It does not contain receipts.

This repo shows how to:

1. Connect to the feed.
2. Decode the transactions and the calldata of known trade functions.
3. Execute feed blocks in a local EVM. See [`examples/local_execution/`](examples/local_execution/).

## Quick start

Requirements: Docker and [uv](https://docs.astral.sh/uv/getting-started/installation/).

```bash
docker compose up -d --wait relay   # start the relay
uv sync
uv run rhfeed                       # Ctrl-C to stop
```

```
seq 20543500  2 tx
    0xd438f08d61ca9e1cd2cecabfeb9471cb79922940d7c5f61173d1340d41c08f3d  call     0x73991a25C8…  0xac9650d8
    0xd23ff04ac94df64183ed3546b70201ab15c8d0f0a19f5c673e91de592d67b8c4  call     0x65050A9b7E…  0x4d819a2a
        ↳ swap          in WETH 10000000000000000  out 0xaebf7814… min 160321143103458484898880  via steps_router
```

Each transaction line shows `hash · kind · to · selector`. A `↳` line shows a decoded intent.
Text output shortens addresses. For full addresses, use `--json`.

`rhfeed` writes transactions to stdout and status to stderr. If stderr shows `no frames`, run
`docker compose logs relay` to check the relay connection to the feed.

To stop the relay, run `docker compose down`.

### The relay

The relay is the Offchain Labs Nitro `relay` binary. It keeps one connection to the feed and
serves all local clients. The feed accepts only clients that offer permessage-deflate. The
relay serves uncompressed JSON.

The relay does not verify signatures. Use `--verify`.

## Filter

| Flag | Function |
|---|---|
| `--to ADDR` | Keep transactions to this contract. |
| `--selector 0x…` | Keep calls with this 4-byte selector. |
| `--sender ADDR` | Keep transactions signed by this address. |
| `--actor ADDR` | Keep the intents of this wallet. See [Decode](#decode). |
| `--json` | Write one JSON object per block. |
| `--seconds N` | Stop after N seconds. |
| `--feed URL` | Read from this URL. `mainnet` and `testnet` are aliases. The default is the local relay. |
| `--verify` | Drop messages without a valid sequencer signature. Mainnet only. |

You can use `--to`, `--selector`, `--sender` and `--actor` more than one time. Values of one
flag are alternatives. All different flags must match.

```bash
uv run rhfeed --selector 0x095ea7b3                            # ERC-20 approve calls
uv run rhfeed --to 0xcaf681a66d020601342297493863e78c959e5cb2  # one contract
```

## Decode

`rhfeed` decodes the arguments of known trade functions into intents:

| Decoded | Intent |
|---|---|
| ERC-20 `transfer`, `transferFrom`, `approve`; Permit2 `approve`; ETH transfer | transfer, approve |
| Uniswap V3 router `exactInput*`, `exactOutput*`; V2 router `swap*`; `multicall` | swap |
| Universal Router `execute`: V2, V3 and V4 swap commands | swap |
| `swap(steps[])` aggregator | swap |
| Pons launchpad buy | swap |
| 0x AllowanceHolder `exec` | swap |
| ERC-4337 v0.7/v0.8 `handleOps` with `execute` / `executeBatch` | the intents of the inner calls |
| Relay router `permit2TransferAndMulticall`, `transferAndMulticall` | relay_fill, relay_sell |

Other calls give no intent. Amounts are raw integers.

In an ERC-4337 bundle, a bundler sends the transaction. The wallet is in the calldata. The
decoder writes this wallet to the `actor` field. In a Relay fill, `actor` is the wallet that
receives the tokens. An intent without an `actor` belongs to the sender.

```bash
uv run rhfeed --actor 0x9b5e82e3bcde529bbfba26e0b9e7044cef866a79
```

```
seq 20543508  1 tx
    0x28e8a4cc79ed77552e14e8e987ad36bd49c3f40d36409f653377fa2c1576e834  call     0x4337026D73… -> 0x4337084D9E…  0x765e827f
        ↳ relay_sell    in 0x69984ad3… 1588651804434692220595  out USDG  via entrypoint_v08 > 0x9b5e82e3… > relay_router
```

`actor` comes from the calldata. Only the receipt confirms it.

## Use from Python

```bash
uv add git+https://github.com/chainstacklabs/robinhood-chain-sequencer-feed
```

```python
import asyncio
from rhfeed import FeedConsumer, addr, selector_of

TOKENS = {addr("0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec")}  # NVDA
TRANSFER = selector_of("transfer(address,uint256)")


async def main():
    async for msg in FeedConsumer().live():  # local relay; or FeedConsumer(url)
        for tx in msg.txs:
            if tx.to_bytes in TOKENS and tx.selector == TRANSFER:
                print(msg.seq, tx.hash, "from", tx.sender)


asyncio.run(main())
```

| Example | Function |
|---|---|
| [`token_flow.py`](examples/token_flow.py) | Watches tokens. Filters on the cheap fields only. |
| [`copy_trade_signals.py`](examples/copy_trade_signals.py) | Writes a JSON signal for each trade of the given wallets. |
| [`replay_capture.py`](examples/replay_capture.py) | Decodes saved frames. Needs no network. |
| [`bench.py`](examples/bench.py) | Measures the decode costs on your machine. |
| [`local_execution/`](examples/local_execution/) | Executes feed blocks in a local EVM. |

### Decode cost

The decoder reads a field from the raw bytes only when you ask for it:

| Fields read | Time per transaction |
|---|---|
| `to_bytes`, `selector`, `value`, `nonce`, `gas` | ~2 µs |
| + `hash` | ~10 µs |
| + `to` (checksummed) | ~15 µs |
| + `sender` (signature recovery) | ~70 µs |

Filter on the first-row fields before you read `sender`.

## Verify the signature

```python
from rhfeed import MAINNET_FEED, MAINNET_VERIFIER, FeedConsumer

async for msg in FeedConsumer(MAINNET_FEED, verify=MAINNET_VERIFIER).live():
    ...  # each message here has a valid sequencer signature
```

Each mainnet message has an ECDSA signature in `signatureV2`. The signer is
`0xDaa526086787d9DEbE1D7F3FFdb1fE50cf8687F4`, the batch poster on the L1 SequencerInbox. A
message that fails is dropped and counted in `consumer.stats["unverified_messages"]`.

## Before you trade

- **A feed message is a soft confirmation.** It does not contain the result. A transaction can
  revert. Confirm the result on a node.
- **The chain can void a transaction.** Robinhood Chain uses ArbOS compliance filtering. A
  voided transaction is in a block with status `0x0` and no logs.
  `rhfeed.is_filtered_call(tx_hash)` makes the `eth_call` that checks a hash. Send it to a node.
- **A message can be reorged.** `FeedConsumer` sets `msg.reorg` when a known sequence number
  comes again with a different `blockHash`. The relay can drop a reorg. To see all reorgs, use
  `--feed mainnet`.

## Endpoints

| Endpoint | Mainnet | Testnet |
|---|---|---|
| Sequencer feed | `wss://feed.mainnet.chain.robinhood.com` | `wss://feed.testnet.chain.robinhood.com` |

Robinhood [documents](https://docs.robinhood.com/chain/connecting) the public endpoints as
rate-limited and not for production. For receipts, logs and state, use a node. Chainstack has
[Robinhood Chain nodes](https://docs.chainstack.com/reference/robinhood-getting-started).
