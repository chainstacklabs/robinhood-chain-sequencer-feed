"""Decode Nitro sequencer-feed frames into transactions.

A relay hands you JSON frames of the form ``{"version":1,"messages":[...]}``. Each
message carries an ``l2Msg`` in base64; inside it is either one signed transaction
or a length-prefixed batch of them, in the order the sequencer chose. No receipts,
no logs, no state — the outcome is not in the message, so a transaction here can
still revert or be voided by the compliance filter. One that came from the parent
chain (`FeedMessage.from_parent_chain`) may not execute at all.

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

# The only L1 kind whose l2Msg is an L2 message. arbos/parse_l2.go switches on the
# header kind before it reads a byte of the payload; every other kind has its own
# layout. An EthDeposit is a 20-byte address then a 32-byte amount, so an address
# starting 0x04 would read as an L2 SignedTx and decode into a transaction that
# does not exist.
L1_L2_MESSAGE = 3

# arbos/parse_l2.go: the parent-chain kinds that arbos turns into one transaction of a
# type of its own. Its hash covers the chain id, which the message does not carry, so
# `parse_frame` builds these only when it is given one.
L1_SUBMIT_RETRYABLE = 9
L1_ETH_DEPOSIT = 12
ARB_SUBMIT_RETRYABLE_TX = 0x69
ARB_DEPOSIT_TX = 0x64
# A submit-retryable transaction is addressed to the ArbRetryableTx precompile, with
# its own fields as `submitRetryable(...)` calldata (go-ethereum arb_types.go).
ARB_RETRYABLE_TX = bytes(19) + b"\x6e"
SUBMIT_RETRYABLE_SELECTOR = bytes.fromhex("c9f95d32")

# arbos/parse_l2.go: L2 message kinds. Only 3 (Batch) and 4 (SignedTx) carry the
# user transactions a consumer cares about. 0 and 1 are unsigned transactions an L1
# sender posts through the delayed inbox; arbos runs them, but they are not surfaced.
# Every other kind is one parseL2Message refuses.
L2_UNSIGNED_USER_TX = 0
L2_CONTRACT_TX = 1
L2_BATCH = 3
L2_SIGNED_TX = 4

# arbos/parse_l2.go caps batch nesting: parseL2Message rejects a batch at
# depth >= 16. A batch nested past that never reaches the state transition, so
# its transactions do not execute and must not be surfaced.
MAX_BATCH_DEPTH = 16

# arbostypes.MaxL2MessageSize. ParseL2Transactions ignores a longer l2Msg outright.
MAX_L2_MESSAGE_SIZE = 256 * 1024

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


def _rlp_bytes(value: bytes) -> bytes:
    if len(value) == 1 and value[0] < 0x80:
        return value
    return _rlp_header(len(value), 0x80) + value


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

# The envelopes arbos takes inside an l2Msg, with their exact field counts. geth's
# rlp.DecodeBytes refuses a list with a field too few or too many, and parseL2Message
# refuses a blob transaction even when it decodes. Keyed by type byte, so a typed
# envelope of type 0 is absent: geth does not read it as legacy.
_LEGACY_FIELDS = 9
_ARBOS_FIELDS = {1: 11, 2: 12, 4: 13}


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
        "_data",
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
            self._data = (0, 0)
            return

        ni, gi, ti, vi, di = layout
        self.nonce = _uint(body, fields[ni])
        self.gas = _uint(body, fields[gi])
        self.value = _uint(body, fields[vi])

        _, ts, te = fields[ti]
        self.to_bytes: bytes | None = body[ts:te] if te > ts else None

        _, ds, de = fields[di]
        self.data_len = de - ds
        self._data = (ds, de)
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
    def data(self) -> bytes:
        """The calldata. One slice, taken on demand — `selector` already covers filtering."""
        if self._body is None:
            return b""
        s, e = self._data
        return self._body[s:e]

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


class _Rejected(Exception):
    """arbos rejects this L2 message.

    parse_l2.go hands the error back up through every enclosing batch, and the block
    producer then runs the message with no transactions at all. One bad entry voids its
    siblings too, so none of them may be surfaced.
    """


def _split_batch(payload: bytes) -> Iterator[bytes]:
    """A Batch is repeated [8-byte big-endian length][nested L2 message].

    As in util.BytestringFromReader, a truncated length or body ends the batch and
    keeps what came before it. A zero length does not: it reads an empty nested
    message, which arbos rejects.
    """
    offset = 0
    size = len(payload)
    while offset + 8 <= size:
        length = int.from_bytes(payload[offset : offset + 8], "big")
        offset += 8
        if offset + length > size:
            return
        yield payload[offset : offset + length]
        offset += length


def _signed_tx(raw: bytes) -> Tx:
    """`decode_transaction`, held to what geth's UnmarshalBinary accepts.

    `decode_transaction` keeps an envelope it cannot read, for its hash. Inside an
    l2Msg that envelope fails parseL2Message and takes the whole message with it.
    """
    if not raw:
        raise _Rejected
    count = _ARBOS_FIELDS.get(raw[0]) if raw[0] < 0x80 else _LEGACY_FIELDS
    tx = decode_transaction(raw)
    if count is None or tx is None or tx._body is None or tx._f is None:
        raise _Rejected
    # The last field must end the envelope: rlp.DecodeBytes refuses trailing bytes.
    if len(tx._f) != count or tx._f[-1][2] != len(tx._body):
        raise _Rejected
    return tx


def _check_unsigned_tx(body: bytes, kind: int) -> None:
    """parseUnsignedTx: 32-byte words for gas limit, max fee, nonce (kind 0 only), to
    and value, then calldata. Gas limit and nonce must fit in a uint64.

    A kind 1 also needs a request id, which every delayed-inbox message has and the
    sequencer never builds one without.
    """
    words = 5 if kind == L2_UNSIGNED_USER_TX else 4
    if len(body) < 32 * words or any(body[:24]):
        raise _Rejected
    if kind == L2_UNSIGNED_USER_TX and any(body[64:88]):
        raise _Rejected


def _parse_l2_message(payload: bytes, depth: int) -> list[Tx]:
    if not payload:
        # No kind byte: parseL2Message fails its first read.
        raise _Rejected
    kind = payload[0]
    if kind == L2_SIGNED_TX:
        return [_signed_tx(payload[1:])]
    if kind == L2_BATCH:
        if depth >= MAX_BATCH_DEPTH:
            # Too deep for arbos to accept, so nothing in this message executes.
            raise _Rejected
        return [
            tx
            for nested in _split_batch(payload[1:])
            for tx in _parse_l2_message(nested, depth + 1)
        ]
    if kind in (L2_UNSIGNED_USER_TX, L2_CONTRACT_TX):
        _check_unsigned_tx(payload[1:], kind)
        return []
    # NonmutatingCall (2) and SignedCompressedTx (7) are unimplemented, Heartbeat (6)
    # has been refused since 2022-08-08, and any other kind is unknown.
    raise _Rejected


def decode_l2_message(payload: bytes, depth: int = 0) -> list[Tx]:
    """Walk an l2Msg, flattening nested batches into the signed transactions inside.

    Anything parseL2Message refuses, anywhere in the message, voids the whole l2Msg,
    exactly as `ParseL2Transactions` failing does in the block producer, so the
    result is then empty. That covers a batch past the depth cap, an empty entry, a
    kind arbos does not run and an envelope geth cannot decode.
    """
    if not payload or len(payload) > MAX_L2_MESSAGE_SIZE:
        return []
    try:
        return _parse_l2_message(payload, depth)
    except _Rejected:
        return []


def _parent_chain_tx(
    tx_type: int,
    fields: list[bytes],
    to: bytes,
    value: int,
    gas: int,
    data: bytes,
    sender: bytes,
) -> Tx:
    """A transaction arbos builds from a parent-chain message. It has no signature: the
    sender is the message's poster, so nothing is left to recover."""
    tx = Tx(bytes((tx_type,)) + _rlp_list(b"".join(fields)), tx_type, None, None, None)
    tx.gas = gas
    tx.value = value
    tx.to_bytes = to
    tx.data_len = len(data)
    tx.selector = data[:4] if len(data) >= 4 else None
    tx._body = data
    tx._data = (0, len(data))
    tx._sender = sender
    return tx


def _words(payload: bytes, count: int) -> list[bytes]:
    """`count` 32-byte words, as arbos reads them with io.ReadFull."""
    if len(payload) < 32 * count:
        raise _Rejected
    return [payload[i : i + 32] for i in range(0, 32 * count, 32)]


def decode_parent_chain_message(header: dict[str, Any], payload: bytes, chain_id: int) -> list[Tx]:
    """The transaction arbos builds from a SubmitRetryable or an EthDeposit message.

    Mirrors parseSubmitRetryableMessage and parseEthDepositMessage in arbos/parse_l2.go.
    A message arbos refuses gives no transaction. A submit-retryable's auto-redeem (type
    0x68) is not built: its fields depend on the block's base fee, which the feed does
    not carry.
    """
    try:
        request_id = _unhex(header["requestId"], "request id")
        poster = _unhex(header["sender"], "address")
    except (KeyError, TypeError, ValueError):
        return []
    if len(request_id) != 32 or len(poster) != 20:
        return []
    kind = header.get("kind")
    try:
        if kind == L1_ETH_DEPOSIT:
            if len(payload) < 52:
                raise _Rejected
            to, value = payload[:20], int.from_bytes(payload[20:52], "big")
            fields = [_rlp_uint(chain_id), _rlp_bytes(request_id), _rlp_bytes(poster)]
            fields += [_rlp_bytes(to), _rlp_uint(value)]
            return [_parent_chain_tx(ARB_DEPOSIT_TX, fields, to, value, 0, b"", poster)]
        if kind == L1_SUBMIT_RETRYABLE:
            return [_submit_retryable(payload, request_id, poster, header, chain_id)]
    except _Rejected:
        return []
    return []


def _submit_retryable(
    payload: bytes, request_id: bytes, poster: bytes, header: dict[str, Any], chain_id: int
) -> Tx:
    w = _words(payload, 9)
    retry_to, call_value, deposit, max_submission_fee = w[0], w[1], w[2], w[3]
    fee_refund, beneficiary, gas_limit, max_fee, data_length = w[4], w[5], w[6], w[7], w[8]
    gas = int.from_bytes(gas_limit, "big")
    length = int.from_bytes(data_length, "big")
    if gas >= 1 << 64 or length > MAX_L2_MESSAGE_SIZE:
        raise _Rejected
    # A plain Read, not ReadFull: a short tail is zero-padded, and only none at all fails.
    retry_data = payload[288 : 288 + length]
    if length and not retry_data:
        raise _Rejected
    retry_data = retry_data.ljust(length, b"\x00")
    l1_base_fee = int(header.get("baseFeeL1") or 0)

    to = retry_to[12:]
    fields = [
        _rlp_uint(chain_id),
        _rlp_bytes(request_id),
        _rlp_bytes(poster),
        _rlp_uint(l1_base_fee),
        _rlp_uint(int.from_bytes(deposit, "big")),
        _rlp_uint(int.from_bytes(max_fee, "big")),
        _rlp_uint(gas),
        _rlp_bytes(to if any(to) else b""),  # the zero address is encoded as nil
        _rlp_uint(int.from_bytes(call_value, "big")),
        _rlp_bytes(beneficiary[12:]),
        _rlp_uint(int.from_bytes(max_submission_fee, "big")),
        _rlp_bytes(fee_refund[12:]),
        _rlp_bytes(retry_data),
    ]
    # What eth_getTransactionByHash returns as `input`.
    data = (
        SUBMIT_RETRYABLE_SELECTOR
        + request_id
        + l1_base_fee.to_bytes(32, "big")
        + deposit
        + call_value
        + max_fee
        + gas_limit
        + max_submission_fee
        # Addresses are re-padded: arbos keeps only the low 20 bytes of each word.
        + bytes(12)
        + fee_refund[12:]
        + bytes(12)
        + beneficiary[12:]
        + bytes(12)
        + to
        + (32 * 11).to_bytes(32, "big")
        + data_length
        + retry_data
        + bytes(-length % 32)
    )
    return _parent_chain_tx(ARB_SUBMIT_RETRYABLE_TX, fields, ARB_RETRYABLE_TX, 0, gas, data, poster)


# --------------------------------------------------------------------------- #
# frames
# --------------------------------------------------------------------------- #


class FeedMessage:
    """One sequencer message. On this chain, `seq` equals the L2 block number.

    `block_hash` is the block the sequencer had already built when it broadcast this —
    it orders, executes, *then* announces. Two uses: ask a node for exactly this block
    (`eth_getBlockReceipts` takes a hash, so you need no polling by number), and tell a
    reorg from a duplicate, which is the only way the protocol offers. See
    `FeedConsumer`.

    `delayed_messages_read` is the running count of delayed-inbox messages the chain has
    consumed. A jump between consecutive messages means the sequencer just pulled that
    many out of the delayed inbox — L1 deposits, retryables, or a force inclusion
    routing around the sequencer entirely.

    `l1_block_number` is the parent-chain (Ethereum) block the sequencer had seen.

    `request_id` is set on a message read out of the delayed inbox (its index there)
    and is None on one the sequencer built.
    """

    __slots__ = (
        "block_hash",
        "delayed_messages_read",
        "l1_block_number",
        "l1_kind",
        "l1_sender",
        "live",
        "received_at",
        "received_monotonic",
        "reorg",
        "request_id",
        "seq",
        "timestamp",
        "txs",
    )

    def __init__(
        self,
        seq: int,
        l1_kind: int,
        l1_sender: str | None,
        timestamp: int,
        txs: list[Tx],
        block_hash: str | None = None,
        delayed_messages_read: int = 0,
        l1_block_number: int = 0,
        request_id: str | None = None,
    ) -> None:
        self.seq = seq
        self.l1_kind = l1_kind
        self.l1_sender = l1_sender
        self.timestamp = timestamp
        self.txs = txs
        self.block_hash = block_hash
        self.delayed_messages_read = delayed_messages_read
        self.l1_block_number = l1_block_number
        self.request_id = request_id
        # Set by FeedConsumer; meaningless for a frame decoded straight off disk.
        self.live = True
        self.received_at = 0.0
        # Wall clock is what you report; monotonic is what you subtract. An NTP step
        # between two `received_at` reads can produce a negative interval.
        self.received_monotonic = 0.0
        #: This sequence number arrived before, carrying a different block hash.
        self.reorg = False

    @property
    def l1_kind_name(self) -> str:
        return L1_KIND.get(self.l1_kind, f"kind{self.l1_kind}")

    @property
    def from_parent_chain(self) -> bool:
        """Entered through Ethereum, not the sequencer.

        That is every kind but L2Message, and an L2Message an L1 sender posted to the
        delayed inbox. The sequencer leaves out a transaction that fails to apply; a
        delayed message keeps whatever was posted, and the block producer skips, with
        no receipt, a transaction whose signature, nonce or balance does not hold.
        Treat these `txs` as claims, not as what will execute.
        """
        return self.request_id is not None or self.l1_kind != L1_L2_MESSAGE

    def __repr__(self) -> str:
        return f"<FeedMessage seq={self.seq} txs={len(self.txs)}>"


def parse_frame(
    frame: dict[str, Any], decode_txs: bool = True, chain_id: int | None = None
) -> list[FeedMessage]:
    """One WebSocket frame can carry several sequencer messages.

    With `decode_txs=False` the envelope is read but `txs` is left empty — how the
    consumer skips the relay's backlog replay without paying to decode it.

    With `chain_id`, a SubmitRetryable or EthDeposit message carries the transaction
    arbos builds from it. Without one, its `txs` is empty: the hash covers the chain id.
    """
    out: list[FeedMessage] = []
    for entry in frame.get("messages") or ():
        wrapper = entry.get("message") or {}
        incoming = wrapper.get("message") or {}
        header = incoming.get("header") or {}
        l1_kind = header.get("kind", -1)
        l2_msg = incoming.get("l2Msg")
        txs: list[Tx] = []
        if decode_txs and l2_msg:
            if l1_kind == L1_L2_MESSAGE:
                txs = decode_l2_message(base64.b64decode(l2_msg))
            elif chain_id is not None and l1_kind in (L1_SUBMIT_RETRYABLE, L1_ETH_DEPOSIT):
                txs = decode_parent_chain_message(header, base64.b64decode(l2_msg), chain_id)
        out.append(
            FeedMessage(
                seq=entry.get("sequenceNumber", -1),
                l1_kind=l1_kind,
                l1_sender=header.get("sender"),
                timestamp=header.get("timestamp", 0),
                txs=txs,
                block_hash=entry.get("blockHash"),
                delayed_messages_read=wrapper.get("delayedMessagesRead") or 0,
                l1_block_number=header.get("blockNumber") or 0,
                request_id=header.get("requestId"),
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

    The hash is checked here rather than left to the node: a short or malformed one
    still ABI-encodes into a perfectly well-formed `eth_call`, and the node would answer
    it about the wrong slot instead of refusing. A false "not filtered" is the one
    answer this function must never invent.
    """
    raw = _unhex(tx_hash, "transaction hash")
    if len(raw) != 32:
        raise ValueError(f"not a 32-byte transaction hash: {tx_hash!r} is {len(raw)} bytes")
    data = IS_FILTERED_SELECTOR + raw.hex()
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [{"to": FILTER_PRECOMPILE, "data": data}, "latest"],
    }
