"""Shared test plumbing: a reference encoder and the captured transactions."""

from __future__ import annotations

import json
from pathlib import Path

from eth_abi import encode as abi_encode

from rhfeed.codec import Tx, parse_frame, selector_of

FRAMES = Path(__file__).parent / "frames.jsonl"


def encode(signature: str, types: list[str], args: list) -> bytes:
    """selector || abi.encode(args). `types` is spelled out because signatures nest tuples."""
    return selector_of(signature) + abi_encode(types, args)


def captured_txs() -> list[Tx]:
    out: list[Tx] = []
    with FRAMES.open() as fh:
        for line in fh:
            for msg in parse_frame(json.loads(line)):
                out.extend(msg.txs)
    return out


def by_selector(selector_hex: str) -> list[Tx]:
    return [t for t in captured_txs() if t.selector_hex == selector_hex]


def a(hex_address: str) -> bytes:
    return bytes.fromhex(hex_address.removeprefix("0x"))
