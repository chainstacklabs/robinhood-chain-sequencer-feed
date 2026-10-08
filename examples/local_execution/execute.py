"""Execute Robinhood Chain feed blocks on your own machine, with no RPC on the execution path.

    RH_RPC_URL=https://... uv run --extra exec python examples/local_execution/execute.py --blocks 3

Four steps, one run:

  record    take the next blocks off the public feed, with each block's real receipts (for
            scoring only — the executor never sees them)
  forked    execute them in an EVM forked from the node at the block before the first one. State
            is read from the node as needed; the opcode trace records every storage slot and
            account the blocks touch
  snapshot  read exactly those slots and accounts, as they stood at that cut block, from a fresh
            fork. This is the flat state the blocks need, and nothing else
  offline   start a new EVM from the snapshot alone and execute the same blocks again. No RPC;
            each block's writes carry into the next

Every transaction's status and logs (address, topics, data) are compared with the real receipt
(`exact`, `differs`, `missing`) and, offline, with the forked run (`agrees_with_forked`).
Gas, fees, ArbOS precompiles, deposits and the per-block internal transaction are not modelled;
see the README next to this file.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
import urllib.request
from collections import Counter
from pathlib import Path

import pyrevm
from eth_hash.auto import keccak

from rhfeed import MAINNET_FEED, FeedConsumer
from rhfeed.codec import FeedMessage

CHAIN_ID = 4663
#: Arbitrum blocks name this address as coinbase.
SEQUENCER = "0xA4b000000000000000000073657175656e636572"
CALL_OPS = {"CALL", "STATICCALL"}
CONTEXT_KEEPING_OPS = {"DELEGATECALL", "CALLCODE"}
#: Opcodes that read another account without calling it.
ACCOUNT_PROBES = {"BALANCE", "EXTCODESIZE", "EXTCODEHASH", "EXTCODECOPY"}
#: A runaway on synthetic or stale state can loop to the block gas limit; cap each call.
GAS_CAP = 30_000_000
HERE = Path(__file__).parent


# ----------------------------------------------------------------------------- rpc (scoring only)


def rpc(method: str, params: list):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    headers = {"content-type": "application/json"}
    req = urllib.request.Request(os.environ["RH_RPC_URL"], body, headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        reply = json.load(resp)
    if "error" in reply:
        raise RuntimeError(f"{method}: {reply['error']}")
    return reply["result"]


def receipts_for(block_hash: str) -> list[dict]:
    """The node lags the feed by a few hundred ms; retry until it has the block."""
    for _ in range(20):
        try:
            got = rpc("eth_getBlockReceipts", [block_hash])
        except RuntimeError:
            got = None
        if got:
            return got
        time.sleep(0.25)
    raise RuntimeError(f"no receipts for {block_hash}")


# ----------------------------------------------------------------------------- phase 1: record


async def record(count: int) -> list[tuple[FeedMessage, list[dict]]]:
    out: list[tuple[FeedMessage, list[dict]]] = []
    async for msg in FeedConsumer(MAINNET_FEED).live():
        if not msg.txs or not msg.block_hash:
            continue
        out.append((msg, receipts_for(msg.block_hash)))
        print(f"  recorded block {msg.seq} ({len(msg.txs)} txs)", file=sys.stderr)
        if len(out) >= count:
            return out
    return out


# ----------------------------------------------------------------------------- execution


def block_env(msg: FeedMessage) -> pyrevm.BlockEnv:
    # On Arbitrum the NUMBER opcode returns the parent-chain block number, not the L2 one.
    return pyrevm.BlockEnv(
        number=msg.l1_block_number,
        timestamp=msg.timestamp,
        coinbase=SEQUENCER,
        basefee=0,
        gas_limit=1 << 50,
    )


def checksum_hex(raw) -> str:
    return raw.lower() if isinstance(raw, str) else "0x" + bytes(raw).hex()


def execute(evm: pyrevm.EVM, tx) -> tuple[str, list[tuple]]:
    """Run one transaction; return status and the logs it emitted as (address, topics, data)."""
    try:
        evm.message_call(
            caller=tx.sender,
            to=tx.to,
            calldata=tx.data,
            value=tx.value,
            gas=min(tx.gas, GAS_CAP),
            gas_price=0,
        )
    except RuntimeError as exc:
        return str(exc).split("{")[0].strip().lower() or "error", []
    result = evm.result
    if not result.is_success:
        return "revert", []
    logs = [
        (
            checksum_hex(log.address),
            tuple(checksum_hex(t) for t in log.topics),
            # pyrevm gives Log.data as (topics, data); the data half is what a receipt calls data.
            "0x" + bytes(log.data[1] if isinstance(log.data, tuple) else log.data or b"").hex(),
        )
        for log in result.logs
    ]
    return "success", logs


def traced(evm: pyrevm.EVM, tx) -> tuple[str, list[tuple], set[tuple[str, int]], set[str]]:
    """`execute` with the EIP-3155 tracer on; also return every slot and account touched.

    The trace names no contract per step, so it is tracked by call depth: CALL/STATICCALL run the
    callee's storage, DELEGATECALL/CALLCODE keep the caller's.
    """
    fd = sys.stdout.fileno()
    saved = os.dup(fd)
    sys.stdout.flush()
    with tempfile.TemporaryFile("w+b") as tmp:
        os.dup2(tmp.fileno(), fd)
        try:
            status, logs = execute(evm, tx)
        finally:
            sys.stdout.flush()
            os.dup2(saved, fd)
            os.close(saved)
        tmp.seek(0)
        slots: set[tuple[str, int]] = set()
        reached: set[str] = {tx.to.lower(), tx.sender.lower()}
        context = {1: tx.to.lower()}
        pending: tuple[int, str] | None = None
        for line in tmp:
            if b'"opName"' not in line:
                continue
            step = json.loads(line)
            depth = step.get("depth", 1)
            if pending and depth == pending[0] + 1:
                context[depth] = pending[1]
                pending = None
            name = step.get("opName")
            stack = step.get("stack") or []
            here = context.get(depth, tx.to.lower())
            if name in ("SLOAD", "SSTORE") and stack:
                slots.add((here, int(stack[-1], 16)))
            elif name in CALL_OPS and len(stack) >= 2:
                target = "0x" + int(stack[-2], 16).to_bytes(32, "big")[12:].hex()
                reached.add(target)
                pending = (depth, target)
            elif name in ACCOUNT_PROBES and stack:
                reached.add("0x" + int(stack[-1], 16).to_bytes(32, "big")[12:].hex())
            elif name in CONTEXT_KEEPING_OPS:
                if len(stack) >= 2:
                    reached.add("0x" + int(stack[-2], 16).to_bytes(32, "big")[12:].hex())
                pending = (depth, here)
    return status, logs, slots, reached


def real_logs(receipt: dict) -> list[tuple]:
    return [
        (log["address"].lower(), tuple(t.lower() for t in log["topics"]), log["data"].lower())
        for log in receipt["logs"]
    ]


def score(ours: list[tuple], theirs: list[tuple]) -> str:
    if ours == theirs:
        return "exact"
    return "missing" if not ours else "differs"


def run_blocks(evm, blocks, stats: Counter, label: str, reads=None, outcomes=None, detail=None):
    """Execute blocks in order on one EVM, score each transaction, return ms per block.

    `reads` (a pair of sets) collects every storage slot and account touched, from the trace.
    `outcomes` maps tx hash -> (status, logs); filled by the forked pass, compared by the offline
    pass, so "offline agrees with forked" is measured directly, not inferred.
    """
    timings = []
    for msg, receipts in blocks:
        by_hash = {r["transactionHash"].lower(): r for r in receipts}
        evm.set_block_env(block_env(msg))
        started = time.perf_counter()
        for tx in msg.txs:
            if tx.to_bytes is None:
                stats["deploy_skipped"] += 1
                continue
            if reads is not None:
                status, logs, slots, accounts = traced(evm, tx)
                reads[0].update(slots)
                reads[1].update(accounts)
            else:
                status, logs = execute(evm, tx)
            if outcomes is not None:
                if tx.hash in outcomes:
                    agree = outcomes[tx.hash] == (status, logs)
                    stats["agrees_with_forked" if agree else "differs_from_forked"] += 1
                else:
                    outcomes[tx.hash] = (status, logs)
            truth = by_hash.get(tx.hash.lower())
            if truth is None:
                stats["no_receipt"] += 1
                continue
            chain_ok = truth["status"] == "0x1"
            stats["txs"] += 1
            stats["real_logs"] += len(truth["logs"])
            stats[f"status_{'match' if (status == 'success') == chain_ok else 'mismatch'}"] += 1
            verdict = score(logs, real_logs(truth))
            stats[verdict] += 1
            if truth["logs"]:
                stats["txs_with_logs"] += 1
                stats[f"with_logs_{verdict}"] += 1
            if detail is not None:
                first_diff = next(
                    ([o, t] for o, t in zip(logs, real_logs(truth), strict=False) if o != t),
                    None,
                )
                row = {
                    "phase": label,
                    "seq": msg.seq,
                    "hash": tx.hash,
                    "to": tx.to,
                    "selector": tx.selector_hex,
                    "ours": status,
                    "chain_ok": chain_ok,
                    "verdict": verdict,
                    "our_logs": len(logs),
                    "real_logs": len(truth["logs"]),
                    "first_diff": first_diff,
                }
                detail.write(json.dumps(row) + "\n")
        timings.append((time.perf_counter() - started) * 1000)
        if msg.from_parent_chain:
            stats["parent_chain_messages"] += 1
        print(f"  [{label}] block {msg.seq}: {timings[-1]:.0f} ms  {dict(stats)}", file=sys.stderr)
    return timings


# ----------------------------------------------------------------------------- snapshot


def snapshot_at(block: int, slots: set[tuple[str, int]], accounts: set[str]) -> dict:
    """Read the given accounts and slots as they stood after `block`, from a fresh fork.

    A fresh fork, not the one the blocks ran on: that one holds post-execution values, and the
    offline pass has to start from the state before the first block.
    """
    evm = pyrevm.EVM(
        env=pyrevm.Env(cfg=pyrevm.CfgEnv(chain_id=CHAIN_ID)),
        fork_url=os.environ["RH_RPC_URL"],
        fork_block=str(block),
    )
    out_accounts = {}
    for address in sorted(accounts | {a for a, _ in slots}):
        info = evm.basic(address)
        code = bytes(evm.get_code(address) or b"")
        out_accounts[address] = {
            "balance": str(info.balance if info else 0),
            "nonce": info.nonce if info else 0,
            "code": code.hex(),
        }
    storage: dict[str, dict[str, str]] = {}
    for address, slot in slots:
        storage.setdefault(address, {})[hex(slot)] = hex(evm.storage(address, slot))
    return {"block": block, "accounts": out_accounts, "storage": storage}


def evm_from_snapshot(snap: dict) -> pyrevm.EVM:
    """An EVM holding only the snapshot. Nothing else exists; nothing is fetched."""
    evm = pyrevm.EVM(env=pyrevm.Env(cfg=pyrevm.CfgEnv(chain_id=CHAIN_ID)))
    for address, a in snap["accounts"].items():
        code = bytes.fromhex(a["code"])
        evm.insert_account_info(
            address,
            pyrevm.AccountInfo(
                balance=int(a["balance"]),
                nonce=a["nonce"],
                code=code or None,
                code_hash=bytes(keccak(code)) if code else None,
            ),
        )
    for address, slots in snap["storage"].items():
        for slot, value in slots.items():
            evm.insert_account_storage(address, int(slot, 16), int(value, 16))
    return evm


# ----------------------------------------------------------------------------- main


def summary(name: str, stats: Counter, timings: list[float]) -> dict:
    timings = sorted(timings) or [0.0]
    txs = max(stats["txs"], 1)
    with_logs = stats["txs_with_logs"] or 1
    return {
        "phase": name,
        "blocks": len(timings),
        "txs": stats["txs"],
        "exact_all_txs": f"{100 * stats['exact'] / txs:.1f}%",
        "exact_txs_with_logs": f"{100 * stats['with_logs_exact'] / with_logs:.1f}%",
        "status_match": f"{100 * stats['status_match'] / txs:.1f}%",
        "ms_per_block_p50": round(timings[len(timings) // 2], 2),
        "ms_per_block_max": round(timings[-1], 2),
        **{k: v for k, v in stats.items() if k != "txs"},
    }


def main(count: int) -> None:
    if not os.environ.get("RH_RPC_URL"):
        sys.exit("set RH_RPC_URL to a Robinhood Chain JSON-RPC endpoint")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    detail = (HERE / f"detail-{stamp}.local.jsonl").open("w", buffering=1)

    print(f"record {count} blocks", file=sys.stderr)
    blocks = asyncio.run(record(count))
    cut = blocks[0][0].seq - 1

    # 1. Forked: state comes from the node as needed. Also learns what the blocks read.
    forked = pyrevm.EVM(
        env=pyrevm.Env(cfg=pyrevm.CfgEnv(chain_id=CHAIN_ID)),
        fork_url=os.environ["RH_RPC_URL"],
        fork_block=str(cut),
        tracing=True,
    )
    fork_stats, reads, outcomes = Counter(), (set(), set()), {}
    print(f"forked: from block {cut}, execute {count} blocks", file=sys.stderr)
    fork_t = run_blocks(forked, blocks, fork_stats, "forked", reads, outcomes, detail)

    # 2. Snapshot: those accounts and slots, as they stood at the cut.
    started = time.perf_counter()
    snap = snapshot_at(cut, *reads)
    path = HERE / "snapshot.local.json"
    path.write_text(json.dumps(snap))
    kb = path.stat().st_size / 1024
    build_s = time.perf_counter() - started
    n_acc, n_slots = len(snap["accounts"]), len(reads[0])
    print(
        f"snapshot: {n_acc} accounts, {n_slots} slots, {kb:.0f} KB, {build_s:.0f} s",
        file=sys.stderr,
    )

    # 3. Offline: the same blocks from the snapshot alone. No RPC.
    offline = evm_from_snapshot(json.loads(path.read_text()))
    off_stats = Counter()
    print(f"offline: no RPC, execute the same {count} blocks", file=sys.stderr)
    off_t = run_blocks(offline, blocks, off_stats, "offline", None, outcomes, detail)
    detail.close()

    report = {
        "forked": summary("forked from the node", fork_stats, fork_t),
        "snapshot": {
            "accounts": n_acc,
            "slots": n_slots,
            "kb": round(kb),
            "build_s": round(build_s),
        },
        "offline": summary("offline, snapshot only, no RPC", off_stats, off_t),
    }
    (HERE / f"result-{stamp}.local.json").write_text(json.dumps(report, indent=1))
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--blocks", type=int, default=5, help="blocks to record and execute")
    main(ap.parse_args().blocks)
