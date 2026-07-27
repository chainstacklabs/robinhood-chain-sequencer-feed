"""Decode Nitro sequencer-feed frames into transactions.

A relay hands you JSON frames of the form ``{"version":1,"messages":[...]}``. Each
message carries an ``l2Msg`` in base64; inside it is either one signed transaction
or a length-prefixed batch of them, in the order the sequencer chose. No receipts,
no logs, no state — the outcome is not in the message, so a transaction here can
still revert or be voided by the compliance filter.

Everything here is a pure function over bytes. Nothing touches the network.

The design goal is that decoding a transaction you end up discarding costs almost
nothing, because a filter keeps a small fraction of what the feed carries. So:

* RLP is walked with a scanner that records field offsets and copies nothing. The
  ``rlp`` package builds a full object tree per transaction; this does not.
* The three expensive fields are lazy and cached — ``hash`` (keccak of the
  envelope), ``to`` (EIP-55 checksumming is another keccak), and ``sender`` (ECDSA
  recovery, the big one). Filter on ``to_bytes`` and ``selector``, which are raw
  bytes already sliced out, and you never pay for the rest.
* Sender recovery reconstructs the signing payload from offsets already scanned and
  calls coincurve directly, instead of handing the envelope back to a library that
  would decode it a second time.

See ``tests/`` for the cross-check against ``eth-account``, ``eth_utils`` and ``rlp``
that keeps the hand-rolled paths honest.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from typing import Any

from coincurve import PublicKey
from eth_hash.auto import keccak

# arbostypes: L1 message kinds. Anything that is not L2Message reached the chain
# through Ethereum rather than through the sequencer.
L1_KIND = {
    3: "L2Message",
    6: "EndOfBlock",
    7: "L2FundedByL1",
    8: "RollupEvent",
    9: "SubmitRetryable",
    10: "BatchForGasEstimation",
    11: "Initialize",
    12: "EthDeposit",
    13: "BatchPostingReport",
    0xFF: "Invalid",
}

# arbos/parse_l2.go: L2 message kinds. Only 3 (Batch) and 4 (SignedTx) carry the
# user transactions a consumer cares about.
L2_BATCH = 3
L2_SIGNED_TX = 4

FILTER_PRECOMPILE = "0x0000000000000000000000000000000000000074"
IS_FILTERED_SELECTOR = "0x85c733a4"

_UNSET = object()


# --------------------------------------------------------------------------- #
# helpers for building filters
# --------------------------------------------------------------------------- #


def selector_of(signature: str) -> bytes:
    """4-byte selector for a function signature.

    selector_of("transfer(address,uint256)")  ->  b"\\xa9\\x05\\x9c\\xbb"
    """
    return keccak(signature.encode())[:4]


def _unhex(value: str, what: str) -> bytes:
    # bytes.fromhex raises before any length check, and its message ("non-hexadecimal
    # number found in fromhex() arg at position 8") says nothing useful to someone who
    # pasted a shortened address off the terminal. Say what was wrong with what.
    try:
        return bytes.fromhex(value.removeprefix("0x"))
    except ValueError as exc:
        raise ValueError(f"not a hex {what}: {value!r}") from exc


def sel(hex_selector: str) -> bytes:
    """4-byte selector from its hex form, with or without the 0x."""
    raw = _unhex(hex_selector, "selector")
    if len(raw) != 4:
        raise ValueError(f"not a 4-byte selector: {hex_selector!r} is {len(raw)} bytes")
    return raw


def addr(hex_address: str) -> bytes:
    """20 raw bytes from a hex address, for membership tests against `to_bytes`."""
    raw = _unhex(hex_address, "address")
    if len(raw) != 20:
        raise ValueError(f"not a 20-byte address: {hex_address!r} is {len(raw)} bytes")
    return raw


def checksum(address: bytes) -> str:
    """EIP-55 checksummed hex. One keccak — which is why `Tx.to` is lazy."""
    lower = address.hex()
    digest = keccak(lower.encode()).hex()
    return "0x" + "".join(
        c.upper() if c > "9" and digest[i] > "7" else c for i, c in enumerate(lower)
    )


# --------------------------------------------------------------------------- #
# RLP: a scanner, not a parser
# --------------------------------------------------------------------------- #


def _scan_list(buf: bytes) -> list[tuple[int, int, int]]:
    """Offsets of every item in the RLP list at the head of `buf`.

    Returns (item_start, payload_start, payload_end) per item. `item_start` keeps
    the length prefix, so a run of items can be re-sliced verbatim when rebuilding
    the signing payload. Nested items (an access list) are skipped over by length
    rather than descended into — nothing here needs their contents.
    """
    if not buf:
        raise ValueError("empty")
    head = buf[0]
    if head < 0xC0:
        raise ValueError("not an RLP list")
    if head < 0xF8:
        i = 1
        end = 1 + (head - 0xC0)
    else:
        n = head - 0xF7
        i = 1 + n
        end = i + int.from_bytes(buf[1 : 1 + n], "big")
    if end > len(buf):
        raise ValueError("truncated RLP list")

    out: list[tuple[int, int, int]] = []
    append = out.append
    while i < end:
        c = buf[i]
        if c < 0x80:  # the byte is its own payload
            append((i, i, i + 1))
            i += 1
        elif c < 0xB8:
            s = i + 1
            e = s + (c - 0x80)
            append((i, s, e))
            i = e
        elif c < 0xC0:
            n = c - 0xB7
            s = i + 1 + n
            e = s + int.from_bytes(buf[i + 1 : s], "big")
            append((i, s, e))
            i = e
        elif c < 0xF8:
            s = i + 1
            e = s + (c - 0xC0)
            append((i, s, e))
            i = e
        else:
            n = c - 0xF7
            s = i + 1 + n
            e = s + int.from_bytes(buf[i + 1 : s], "big")
            append((i, s, e))
            i = e
    if i != end:
        raise ValueError("malformed RLP list")
    return out


def _rlp_header(length: int, offset: int) -> bytes:
    if length < 56:
        return bytes((offset + length,))
    n = (length.bit_length() + 7) // 8
    return bytes((offset + 55 + n,)) + length.to_bytes(n, "big")


def _rlp_list(content: bytes) -> bytes:
    return _rlp_header(len(content), 0xC0) + content


def _rlp_uint(value: int) -> bytes:
    if value == 0:
        return b"\x80"
    body = value.to_bytes((value.bit_length() + 7) // 8, "big")
    if len(body) == 1 and body[0] < 0x80:
        return body
    return _rlp_header(len(body), 0x80) + body


# --------------------------------------------------------------------------- #
# transactions
# --------------------------------------------------------------------------- #

# Field positions per envelope type: (nonce, gas, to, value, data). The signature
# is always the final three fields whatever the type, so it is found by counting
# back from the end rather than by position.
_LAYOUT = {
    0: (0, 2, 3, 4, 5),  # legacy:  nonce gasPrice gas to value data v r s
    1: (1, 3, 4, 5, 6),  # 2930:    +chainId, +accessList
    2: (1, 4, 5, 6, 7),  # 1559:    chainId nonce maxPrio maxFee gas to value data ...
    3: (1, 4, 5, 6, 7),  # blob:    same first eight fields
    4: (1, 4, 5, 6, 7),  # 7702:    same first eight fields
}


class Tx:
    """One signed transaction as the sequencer ordered it, without its outcome.

    Cheap and eager: `tx_type`, `nonce`, `gas`, `value`, `to_bytes`, `selector`.
    Expensive and lazy: `hash`, `to`, `sender`, `sender_bytes`.

    Filter on the eager fields. `to_bytes` and `selector` are raw bytes, so a
    membership test against a set of `addr(...)` / `selector_of(...)` values costs
    a hash lookup and nothing else.
    """

    __slots__ = (
        "_body",
        "_f",
        "_hash",
        "_sender",
        "_to",
        "data_len",
        "gas",
        "nonce",
        "raw",
        "selector",
        "to_bytes",
        "tx_type",
        "value",
    )

    def __init__(
        self,
        raw: bytes,
        tx_type: int,
        body: bytes | None,
        fields: list[tuple[int, int, int]] | None,
        layout: tuple[int, int, int, int, int] | None,
    ) -> None:
        self.raw = raw
        self.tx_type = tx_type
        self._body = body
        self._f = fields
        self._hash: str | None = None
        self._to: str | None = None
        self._sender: Any = _UNSET

        if body is None or fields is None or layout is None:
            self.nonce = self.gas = self.value = self.data_len = 0
            self.to_bytes = None
            self.selector = None
            return

        ni, gi, ti, vi, di = layout
        self.nonce = _uint(body, fields[ni])
        self.gas = _uint(body, fields[gi])
        self.value = _uint(body, fields[vi])

        _, ts, te = fields[ti]
        self.to_bytes: bytes | None = body[ts:te] if te > ts else None

        _, ds, de = fields[di]
        self.data_len = de - ds
        self.selector: bytes | None = body[ds : ds + 4] if de - ds >= 4 else None

    # -- lazy fields --------------------------------------------------------- #

    @property
    def hash(self) -> str:
        """keccak of the envelope. One hash over the whole transaction."""
        h = self._hash
        if h is None:
            h = self._hash = "0x" + keccak(self.raw).hex()
        return h

    @property
    def to(self) -> str | None:
        """Checksummed recipient. Costs a keccak; `to_bytes` does not."""
        if self.to_bytes is None:
            return None
        t = self._to
        if t is None:
            t = self._to = checksum(self.to_bytes)
        return t

    @property
    def sender_bytes(self) -> bytes | None:
        """ECDSA recovery — by far the most expensive thing here. Cached."""
        s = self._sender
        if s is _UNSET:
            s = self._sender = self._recover()
        return s

    @property
    def sender(self) -> str | None:
        raw = self.sender_bytes
        return checksum(raw) if raw else None

    # -- derived ------------------------------------------------------------- #

    @property
    def selector_hex(self) -> str | None:
        return "0x" + self.selector.hex() if self.selector else None

    @property
    def kind(self) -> str:
        if self.to_bytes is None:
            return "deploy"
        return "call" if self.selector else "transfer"

    def as_dict(self, sender: bool = False) -> dict[str, Any]:
        """Everything, resolved. Realises the lazy fields — for printing, not hot paths."""
        out = {
            "hash": self.hash,
            "tx_type": self.tx_type,
            "to": self.to,
            "value": self.value,
            "nonce": self.nonce,
            "gas": self.gas,
            "selector": self.selector_hex,
            "kind": self.kind,
            "raw_len": len(self.raw),
        }
        if sender:
            out["sender"] = self.sender
        return out

    def __repr__(self) -> str:
        return f"<Tx {self.hash[:12]} type={self.tx_type} {self.kind}>"

    # -- recovery ------------------------------------------------------------ #

    def _recover(self) -> bytes | None:
        body, f = self._body, self._f
        if body is None or f is None or len(f) < 4:
            return None
        n = len(f)
        v = _uint(body, f[n - 3])
        r = body[f[n - 2][1] : f[n - 2][2]]
        s = body[f[n - 1][1] : f[n - 1][2]]
        if not r or not s or len(r) > 32 or len(s) > 32:
            return None

        if self.tx_type == 0:
            # EIP-155 folds the chain id into v; pre-155 signatures use 27/28 and
            # sign only the first six fields.
            if v >= 35:
                parity = (v - 35) & 1
                unsigned = body[f[0][0] : f[5][2]] + _rlp_uint((v - 35) >> 1) + b"\x80\x80"
            elif v in (27, 28):
                parity = v - 27
                unsigned = body[f[0][0] : f[5][2]]
            else:
                return None
            payload = _rlp_list(unsigned)
        else:
            # Typed envelopes sign type || rlp(everything but the signature).
            parity = v
            if parity > 1:
                return None
            payload = bytes((self.tx_type,)) + _rlp_list(body[f[0][0] : f[n - 4][2]])

        sig = r.rjust(32, b"\x00") + s.rjust(32, b"\x00") + bytes((parity,))
        try:
            pk = PublicKey.from_signature_and_message(sig, keccak(payload), hasher=None)
        except Exception:
            return None
        return keccak(pk.format(compressed=False)[1:])[-20:]


def _uint(buf: bytes, item: tuple[int, int, int]) -> int:
    return int.from_bytes(buf[item[1] : item[2]], "big")


def decode_transaction(raw: bytes) -> Tx | None:
    """One signed transaction envelope: legacy, 2930, 1559, blob or 7702."""
    if not raw:
        return None
    head = raw[0]
    typed = head < 0x80
    tx_type = head if typed else 0
    layout = _LAYOUT.get(tx_type)
    if layout is None:
        # An envelope type we do not model — keep the hash, drop the fields.
        return Tx(raw, tx_type, None, None, None)
    try:
        fields = _scan_list(raw[1:] if typed else raw)
    except ValueError:
        return Tx(raw, tx_type, None, None, None)
    if len(fields) < layout[4] + 4:  # payload fields plus v, r, s
        return Tx(raw, tx_type, None, None, None)
    return Tx(raw, tx_type, raw[1:] if typed else raw, fields, layout)


def _split_batch(payload: bytes) -> Iterator[bytes]:
    """A Batch is repeated [8-byte big-endian length][nested L2 message]."""
    offset = 0
    size = len(payload)
    while offset + 8 <= size:
        length = int.from_bytes(payload[offset : offset + 8], "big")
        offset += 8
        if length == 0 or offset + length > size:
            return
        yield payload[offset : offset + length]
        offset += length


def decode_l2_message(payload: bytes) -> list[Tx]:
    """Walk an l2Msg, flattening nested batches into the signed transactions inside."""
    if not payload:
        return []
    kind = payload[0]
    if kind == L2_SIGNED_TX:
        tx = decode_transaction(payload[1:])
        return [tx] if tx else []
    if kind == L2_BATCH:
        return [tx for nested in _split_batch(payload[1:]) for tx in decode_l2_message(nested)]
    return []


# --------------------------------------------------------------------------- #
# frames
# --------------------------------------------------------------------------- #


class FeedMessage:
    """One sequencer message. On this chain, `seq` equals the L2 block number."""

    __slots__ = ("l1_kind", "l1_sender", "live", "received_at", "seq", "timestamp", "txs")

    def __init__(
        self, seq: int, l1_kind: int, l1_sender: str | None, timestamp: int, txs: list[Tx]
    ) -> None:
        self.seq = seq
        self.l1_kind = l1_kind
        self.l1_sender = l1_sender
        self.timestamp = timestamp
        self.txs = txs
        # Set by FeedConsumer; meaningless for a frame decoded straight off disk.
        self.live = True
        self.received_at = 0.0

    @property
    def l1_kind_name(self) -> str:
        return L1_KIND.get(self.l1_kind, f"kind{self.l1_kind}")

    @property
    def from_parent_chain(self) -> bool:
        """Anything not an L2Message entered through Ethereum, not the sequencer."""
        return self.l1_kind != 3

    def __repr__(self) -> str:
        return f"<FeedMessage seq={self.seq} txs={len(self.txs)}>"


def parse_frame(frame: dict[str, Any], decode_txs: bool = True) -> list[FeedMessage]:
    """One WebSocket frame can carry several sequencer messages.

    With `decode_txs=False` the envelope is read but `txs` is left empty — how the
    consumer skips the relay's backlog replay without paying to decode it.
    """
    out: list[FeedMessage] = []
    for entry in frame.get("messages") or ():
        incoming = (entry.get("message") or {}).get("message") or {}
        header = incoming.get("header") or {}
        l2_msg = incoming.get("l2Msg")
        out.append(
            FeedMessage(
                seq=entry.get("sequenceNumber", -1),
                l1_kind=header.get("kind", -1),
                l1_sender=header.get("sender"),
                timestamp=header.get("timestamp", 0),
                txs=decode_l2_message(base64.b64decode(l2_msg)) if (l2_msg and decode_txs) else [],
            )
        )
    return out


# --------------------------------------------------------------------------- #
# compliance filtering
# --------------------------------------------------------------------------- #


def is_filtered_call(tx_hash: str) -> dict[str, Any]:
    """eth_call body asking the filter precompile whether a hash is blocked.

    A transaction can appear in the feed and still be voided — included in a block,
    status 0x0, no logs, gas burned. See the README. Send this to an RPC node; a
    non-zero result means the hash is registered and the chain will void it.
    """
    data = IS_FILTERED_SELECTOR + tx_hash.removeprefix("0x").rjust(64, "0")
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [{"to": FILTER_PRECOMPILE, "data": data}, "latest"],
    }
