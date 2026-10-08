"""Turn calldata into trade intents.

    from rhfeed import decode_intents
    for intent in decode_intents(tx):
        if intent.kind == "swap" and intent.token_out in WATCH: ...

One table, keyed by selector. A selector in the table is decoded by a function that reads
fixed slots; one that is not yields nothing. Containers — a 4337 bundle, a router's
`multicall`, a Relay fill — build inner `Call`s and dispatch them through the same table,
so one transaction can yield several intents, each carrying the path of contracts it was
found under (`via`) and the party it is for (`actor`).

`actor` is the one field a consumer must understand: it is *not* the transaction sender.
In a `handleOps` bundle the sender is a bundler and the user is `op.sender`; in a Relay
fill the sender is a solver and the wallet delivered to is the actor. `None` means "the
transaction sender", left unresolved because recovering it costs ~50 µs and the caller
may not need it.

Adding a function is one decorated function in `decoders/`. Adding a protocol is one
module there, imported from `decoders/__init__.py`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from .abi import Malformed
from .codec import Tx, checksum, sel, selector_of

#: Containers nest: handleOps -> executeBatch -> Relay router -> AllowanceHolder -> Settler
#: is five deep on mainnet today. Eight leaves room and still stops a payload that nests
#: itself forever.
MAX_DEPTH = 8


@dataclass(slots=True)
class Call:
    """One call to decode: the top-level transaction, or a call nested inside one."""

    to: bytes | None
    value: int
    data: bytes
    #: The party this call is for. None = the transaction sender.
    actor: bytes | None
    #: Contracts walked to reach this call, outermost first.
    via: tuple[bytes, ...]


@dataclass(slots=True)
class Intent:
    """What a call asks for. Not what it got — the feed carries no outcome.

    Field meaning by kind:
      swap        token_in/amount_in sold, token_out/amount_out bought, recipient gets output
      transfer    token_in/amount_in moved to recipient; payer set when it is not the actor.
                  A plain ETH transfer (empty calldata, value > 0) is a transfer with token_in=None.
      approve     token_in approved to recipient for amount_in
      relay_fill  solver delivers token_out to recipient (= actor); payer is the solver treasury
      relay_sell  actor sells token_in; token_out goes to Relay as credit, paid out elsewhere

    `actor` is what the calldata claims; a receipt confirms it.

    `amount_in` is exact and `amount_out` a minimum when `exact_in`; the other way round
    otherwise. A token of None is native ETH.
    """

    kind: str
    actor: bytes | None
    via: tuple[bytes, ...]
    payer: bytes | None = None
    token_in: bytes | None = None
    token_out: bytes | None = None
    amount_in: int | None = None
    amount_out: int | None = None
    exact_in: bool = True
    recipient: bytes | None = None
    order_id: bytes | None = field(default=None, repr=False)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for key in ("actor", "payer", "token_in", "token_out", "recipient"):
            d[key] = checksum(d[key]) if d[key] else None
        d["via"] = [checksum(v) for v in self.via]
        d["order_id"] = "0x" + self.order_id.hex() if self.order_id else None
        return d


RAW_SELECTOR = re.compile(r"0x[0-9a-fA-F]{8}")

Decoder = Callable[[Call, int], list[Intent]]

#: selector -> decoder. Populated by `decodes` when `rhfeed.decoders` is imported.
DECODERS: dict[bytes, Decoder] = {}


def decodes(*signatures: str) -> Callable[[Decoder], Decoder]:
    """Register a decoder under each signature. Signatures, not hex, so a typo fails loudly.

    The exception is a raw selector (`0x` and 8 hex digits), for functions whose ABI is
    unknown and whose layout was read from receipts. Such a decoder must check the shape
    of its own calldata, because the selector alone could belong to any contract.
    """

    def register(fn: Decoder) -> Decoder:
        for signature in signatures:
            if RAW_SELECTOR.fullmatch(signature):
                selector = sel(signature)
            elif signature[:2] in ("0x", "0X") and "(" not in signature:
                raise ValueError(f"{signature!r} is neither a signature nor a 4-byte selector")
            else:
                selector = selector_of(signature)
            if DECODERS.get(selector, fn) is not fn:
                raise ValueError(f"{signature!r} is already registered")
            DECODERS[selector] = fn
        return fn

    return register


def inner(
    call: Call, to: bytes | None, value: int, data: bytes, actor: bytes | None = None
) -> Call:
    """A call nested inside `call`: same actor unless given, `via` extended by `to`."""
    via = (*call.via, to) if to is not None else call.via
    return Call(to, value, data, call.actor if actor is None else actor, via)


def decode_call(call: Call, depth: int = 0) -> list[Intent]:
    if depth > MAX_DEPTH or len(call.data) < 4:
        return []
    decoder = DECODERS.get(call.data[:4])
    if decoder is None:
        return []
    try:
        return decoder(call, depth)
    except Malformed:
        # A payload that does not fit its own layout is not a trade. Same rule as the
        # codec: say nothing rather than something wrong.
        return []


def decode_intents(tx: Tx) -> list[Intent]:
    """Every intent in one transaction. Cheap to call on a transaction with no match."""
    if tx.to_bytes is not None and not tx.data and tx.value > 0:
        # No calldata, so no selector to dispatch on: a plain ETH transfer.
        return [
            Intent(
                "transfer",
                None,
                (tx.to_bytes,),
                token_in=None,
                amount_in=tx.value,
                recipient=tx.to_bytes,
            )
        ]
    via = (tx.to_bytes,) if tx.to_bytes is not None else ()
    return decode_call(Call(tx.to_bytes, tx.value, tx.data, None, via))
