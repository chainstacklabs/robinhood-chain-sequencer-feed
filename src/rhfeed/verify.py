"""Check that a feed message really came from the chain's sequencer key.

Robinhood's mainnet feed signs every message — a 65-byte ECDSA signature in
`signatureV2`. Arbitrum One's public feed does not, which makes this one of the two
places the two feeds actually differ. Nothing else in this package needs the network,
and neither does this: verification is a keccak over the message plus one public-key
recovery, both local.

Why bother, given the transport is already `wss`? TLS tells you that you are talking
to whatever is in front of the endpoint. The signature tells you the message was
produced by the key that posts this chain's batches to Ethereum — a claim that
survives a compromised CDN edge, a proxy of your own, any relay including one you run
yourself, and a `.jsonl` capture someone hands you. It is also the check a stock Nitro
*node* makes by default (`--feed.input.verify.accept-sequencer`), so declining to make
it puts you strictly behind the software everyone else on the chain is running.

Note the emphasis on node. A stock **relay** makes no such check: it has no L1
connection, so it passes `nil` for `NewBroadcastClients`' `addrVerifier`, and
`DefaultFeedVerifierConfig` ships `Dangerous.AcceptMissing: true`, which together
short-circuit every signature to accepted. `accept-sequencer` is inert in the relay
binary.

    from rhfeed import MAINNET_VERIFIER, FeedConsumer

    async for msg in FeedConsumer(MAINNET_FEED, verify=MAINNET_VERIFIER).live():
        ...   # anything that arrives here carried a good signature

Cost is one ECDSA recovery per *message*, which is the same order as recovering one
transaction sender — call it ~0.1 ms, against a feed that delivers tens of messages a
second. Cheap, but not free, which is the only reason it is opt-in rather than the
default. There is no feed this is pointless against.

**The preimage.** `BroadcastFeedMessage.SignatureHash` in Nitro's
`broadcaster/message/message.go`. Field order matters, and two details are easy to get
wrong because the JSON does not show them:

* `requestId` and `baseFeeL1` are *skipped entirely* when null, not written as zeros.
* `baseFeeL1` is Go's `big.Int.Bytes()` — minimal-length big-endian. A zero base fee,
  which is what this chain's sequencer messages carry, therefore contributes **no
  bytes at all**, not 32.

Get either wrong and every recovery returns a different plausible-looking address
rather than failing outright. If you port this: recompute with a deliberately wrong
`chain_id` and check that the recovered addresses scatter across messages. If they
stay consistent, the preimage is not binding what you think it is.
"""

from __future__ import annotations

import base64
from collections.abc import Iterable
from typing import Any

from coincurve import PublicKey
from eth_hash.auto import keccak

#: Domain separator, so a feed signature cannot be replayed as a signature over
#: anything else. Part of the preimage, not of the transport.
FEED_PREFIX = b"Arbitrum Nitro Feed:"

MAINNET_CHAIN_ID = 4663

#: The key that signs mainnet feed messages. Not a name Robinhood publishes — it is
#: recovered from the signatures, and then confirmed as this chain's batch poster by
#: `isBatchPoster()` on the L1 SequencerInbox. So the same key signs the feed and
#: posts batches to Ethereum, which is what makes the check meaningful: the identity
#: is anchored in L1 state rather than in a value Robinhood could quietly change.
MAINNET_SIGNER = bytes.fromhex("daa526086787d9debe1d7f3ffdb1fe50cf8687f4")


def _minimal(value: int) -> bytes:
    """Go's `big.Int.Bytes()`: big-endian, no leading zero byte, empty for zero."""
    return value.to_bytes((value.bit_length() + 7) // 8, "big")


def signature_payload(entry: dict[str, Any], chain_id: int) -> bytes:
    """The exact bytes the sequencer hashed, rebuilt from one raw envelope.

    `entry` is a single element of a frame's `messages` array, undecoded — the
    signature covers fields that `FeedMessage` deliberately drops, so this takes the
    JSON rather than the decoded message.
    """
    wrapper = entry.get("message") or {}
    incoming = wrapper.get("message") or {}
    header = incoming.get("header") or {}

    out = bytearray(FEED_PREFIX)
    out += chain_id.to_bytes(8, "big")
    out += int(entry.get("sequenceNumber") or 0).to_bytes(8, "big")

    # Present in practice on this chain: the sequencer executes the block before it
    # broadcasts, so it already knows the hash. Skipped when absent, per Nitro.
    block_hash = entry.get("blockHash")
    if block_hash:
        out += bytes.fromhex(block_hash.removeprefix("0x"))

    # Timeboost's express-lane bitmap. Always absent on this chain, always present on
    # Arbitrum One. Signed either way, so it has to be handled either way.
    metadata = entry.get("blockMetadata")
    if metadata:
        out += base64.b64decode(metadata)

    out += int(wrapper.get("delayedMessagesRead") or 0).to_bytes(8, "big")

    out += bytes((header.get("kind") or 0,))
    out += bytes.fromhex((header.get("sender") or "").removeprefix("0x"))
    out += int(header.get("blockNumber") or 0).to_bytes(8, "big")
    out += int(header.get("timestamp") or 0).to_bytes(8, "big")

    # Both omitted when null rather than zero-padded — see the module docstring.
    request_id = header.get("requestId")
    if request_id is not None:
        out += bytes.fromhex(request_id.removeprefix("0x"))
    base_fee = header.get("baseFeeL1")
    if base_fee is not None:
        out += _minimal(int(base_fee))

    l2_msg = incoming.get("l2Msg")
    if l2_msg:
        out += base64.b64decode(l2_msg)
    return bytes(out)


def signature_hash(entry: dict[str, Any], chain_id: int) -> bytes:
    """keccak of `signature_payload` — what the 65-byte signature is over."""
    return keccak(signature_payload(entry, chain_id))


def recover_signer(entry: dict[str, Any], chain_id: int) -> bytes | None:
    """The 20-byte address that signed this message, or None if that cannot be had.

    None means unusable, not forged: no signature, a malformed one, or a recovery
    that failed. A *forged* message recovers some address perfectly well — it just
    will not be one you accept, which is why callers compare rather than truth-test.
    """
    encoded = entry.get("signatureV2")
    if not encoded:
        return None
    try:
        sig = base64.b64decode(encoded)
    except Exception:
        return None
    if len(sig) != 65:
        return None

    v = sig[64]
    if 27 <= v <= 30:  # a signer that normalised v the Ethereum way
        sig = sig[:64] + bytes((v - 27,))
    elif v > 3:
        return None

    try:
        key = PublicKey.from_signature_and_message(
            sig, signature_hash(entry, chain_id), hasher=None
        )
    except Exception:
        return None
    return keccak(key.format(compressed=False)[1:])[-20:]


class Verifier:
    """A chain id and the signers you will accept for it.

    Nitro resolves the allowed set at runtime by asking the L1 SequencerInbox whether
    a recovered address is a batch poster. This does not — it holds a fixed set, so
    verification stays local and offline. The trade is that a key rotation on L1 needs
    a new set here; `MAINNET_SIGNER` was confirmed against the SequencerInbox on
    2026-07-27, and re-checking it is one `eth_call`.
    """

    __slots__ = ("chain_id", "signers")

    def __init__(self, chain_id: int, signers: Iterable[bytes]) -> None:
        self.chain_id = chain_id
        self.signers = frozenset(signers)
        for signer in self.signers:
            if len(signer) != 20:
                raise ValueError(f"not a 20-byte address: {signer.hex()}")

    def signer_of(self, entry: dict[str, Any]) -> bytes | None:
        """Who signed this message, whether or not you accept them."""
        return recover_signer(entry, self.chain_id)

    def accepts(self, entry: dict[str, Any]) -> bool:
        """Whether this message carries a good signature from an allowed signer.

        False for an unsigned message too. A feed that stops signing is a change you
        want to notice, not one to wave through.
        """
        return self.signer_of(entry) in self.signers

    def __repr__(self) -> str:
        who = ", ".join(sorted("0x" + s.hex()[:8] for s in self.signers))
        return f"<Verifier chain={self.chain_id} signers=[{who}]>"


#: Ready-made: Robinhood Chain mainnet, signed by its batch poster.
MAINNET_VERIFIER = Verifier(MAINNET_CHAIN_ID, [MAINNET_SIGNER])
