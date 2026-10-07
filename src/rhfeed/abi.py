"""Read ABI-encoded arguments slot by slot, without a type interpreter.

Calldata after the selector is a sequence of 32-byte slots. A static value sits in its
slot; a dynamic value (bytes, arrays, tuples with dynamic members) leaves an offset in
its slot and its body further on. These readers do one slice each, so a decoder that
wants two fields pays for two slices and nothing else — the same bargain `codec.py`
makes with RLP.

Every reader raises `Malformed` when the payload cannot hold what was asked. Decoders
let it propagate; `intents.decode_call` turns it into "no intent".
"""

from __future__ import annotations

WORD = 32
ZERO = bytes(20)
#: What Uniswap, 0x and others use for native ETH where an ERC-20 address is expected.
NATIVE = b"\xee" * 20


class Malformed(ValueError):
    """The payload does not hold what the ABI layout says it should."""


def word(data: bytes, i: int) -> bytes:
    start = i * WORD
    end = start + WORD
    if end > len(data):
        raise Malformed(f"slot {i} is past the end of {len(data)} bytes")
    return data[start:end]


def uint(data: bytes, i: int) -> int:
    return int.from_bytes(word(data, i), "big")


def address(data: bytes, i: int) -> bytes:
    w = word(data, i)
    if w[:12] != bytes(12):
        raise Malformed(f"slot {i} is not an address")
    return w[12:]


def token(data: bytes, i: int) -> bytes | None:
    """An address slot where the zero address or 0xeeee…eeee means native ETH."""
    a = address(data, i)
    return None if a in (ZERO, NATIVE) else a


def _offset(data: bytes, i: int) -> int:
    off = uint(data, i)
    if off % WORD or off + WORD > len(data):
        raise Malformed(f"slot {i} holds a bad offset {off}")
    return off


def dynamic(data: bytes, i: int) -> bytes:
    """The `bytes` (or `string`) whose offset is in slot `i`."""
    off = _offset(data, i)
    n = int.from_bytes(data[off : off + WORD], "big")
    start = off + WORD
    if start + n > len(data):
        raise Malformed(f"bytes at {off} claim {n} bytes past the end")
    return data[start : start + n]


def tuple_at(data: bytes, i: int) -> bytes:
    """Body of the dynamic tuple whose offset is in slot `i`. Slots inside are relative."""
    return data[_offset(data, i) :]


def _elements(data: bytes, i: int, width: int) -> tuple[bytes, int]:
    """(element area, count) of the array whose offset is in slot `i`."""
    body = tuple_at(data, i)
    n = uint(body, 0)
    area = body[WORD:]
    if n > len(area) // (width * WORD):
        raise Malformed(f"array claims {n} elements in {len(area)} bytes")
    return area, n


def static_array(data: bytes, i: int, width: int = 1) -> list[bytes]:
    """Array of static elements, each `width` slots wide. One body per element."""
    area, n = _elements(data, i, width)
    size = width * WORD
    return [area[k * size : (k + 1) * size] for k in range(n)]


def tuple_array(data: bytes, i: int) -> list[bytes]:
    """Array of dynamic tuples. Each element's offset is relative to the element area."""
    area, n = _elements(data, i, 1)
    return [area[_offset(area, k) :] for k in range(n)]


def bytes_array(data: bytes, i: int) -> list[bytes]:
    """`bytes[]`: each element is a length-prefixed blob at its own offset."""
    area, n = _elements(data, i, 1)
    out = []
    for k in range(n):
        off = _offset(area, k)
        size = int.from_bytes(area[off : off + WORD], "big")
        start = off + WORD
        if start + size > len(area):
            raise Malformed(f"bytes[{k}] claims {size} bytes past the end")
        out.append(area[start : start + size])
    return out
