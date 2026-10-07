"""Compare what a transaction's calldata says with what its receipt says.

    RH_RPC_URL=https://... uv run python examples/receipt_check.py <tx hash> [<tx hash> ...]

Prints every ERC-20 Transfer in the receipt (token, from, to, amount) next to the
transaction's selector and `to`. Used to confirm field roles that an ABI does not name:
which slot of a step is the input token, which array of a sweep names the delivered
token, which address a fill is delivered to. The feed carries the calldata; only a
node carries the outcome, which is why this reads from one.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

from rhfeed.codec import checksum

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def rpc(url: str, method: str, params: list) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, body, {"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        reply = json.load(resp)
    if "error" in reply:
        sys.exit(f"{method}: {reply['error']}")
    return reply["result"]


def main(hashes: list[str]) -> None:
    url = os.environ.get("RH_RPC_URL")
    if not url:
        sys.exit("set RH_RPC_URL to a Robinhood Chain JSON-RPC endpoint")
    for tx_hash in hashes:
        tx = rpc(url, "eth_getTransactionByHash", [tx_hash])
        receipt = rpc(url, "eth_getTransactionReceipt", [tx_hash])
        if tx is None or receipt is None:
            print(f"{tx_hash}: not found")
            continue
        status = "ok" if receipt["status"] == "0x1" else "REVERTED"
        print(f"\n{tx_hash}  {status}")
        value = int(tx["value"], 16)
        print(f"  from {tx['from']}  to {tx['to']}  selector {tx['input'][:10]}  value {value}")
        for log in receipt["logs"]:
            topics = log["topics"]
            if len(topics) == 3 and topics[0] == TRANSFER_TOPIC:
                amount = int(log["data"], 16) if log["data"] != "0x" else 0
                token, sender, receiver = (
                    checksum(bytes.fromhex(h[-40:])) for h in (log["address"], topics[1], topics[2])
                )
                index = int(log["logIndex"], 16)
                print(f"  #{index:<3} transfer {token}  {sender} -> {receiver}  {amount}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(sys.argv[1:])
