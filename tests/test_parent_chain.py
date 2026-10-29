"""The transactions arbos builds from SubmitRetryable and EthDeposit messages.

`parent_chain.jsonl` holds four feed entries captured on 2026-10-29: a SubmitRetryable
and an EthDeposit from Robinhood Chain mainnet (4663), and the same two kinds from
Elysium testnet (99801). Every field asserted here was read back from each chain's
`eth_getTransactionByHash`.
"""

from __future__ import annotations

import base64
import copy
import json
from pathlib import Path

import pytest

from rhfeed import FeedConsumer, parse_frame
from rhfeed.consume import KNOWN_CHAIN_IDS, MAINNET_FEED
from rhfeed.verify import MAINNET_VERIFIER

ENTRIES = {
    e["sequenceNumber"]: e
    for e in map(json.loads, (Path(__file__).parent / "parent_chain.jsonl").open())
}

# seq: (chain id, type, hash, to, value, gas, input length)
NODE = {
    100864538: (
        4663,
        0x69,
        "0x793b9e3eca7c17ae394bf9ca8c4840ce97bc9a274d5e4fadf5c376cf5d957b6d",
        "0x000000000000000000000000000000000000006E",
        0,
        None,
        1156,
    ),
    100868052: (
        4663,
        0x64,
        "0xcb29b668d7f387ca6fa429853959d4354f7f918458bf4492846bfdeea759a313",
        None,
        None,
        0,
        0,
    ),
    18382979: (
        99801,
        0x64,
        "0x70532e51db367a70a14937be531cd5267fa23bec628eeb4199d4abdc01cc8a99",
        None,
        None,
        0,
        0,
    ),
    18382981: (
        99801,
        0x69,
        "0x30a61b90f62a26ecf83aa62e8ddb4ca87b15391b6b36f54c8354713178745bf9",
        "0x000000000000000000000000000000000000006E",
        0,
        None,
        612,
    ),
}


def one(entry: dict, chain_id: int | None):
    return parse_frame({"messages": [entry]}, chain_id=chain_id)[0]


@pytest.mark.parametrize("seq", sorted(NODE))
def test_the_built_transaction_is_the_one_in_the_block(seq):
    chain_id, tx_type, tx_hash, to, value, gas, input_len = NODE[seq]
    (tx,) = one(ENTRIES[seq], chain_id).txs
    assert tx.tx_type == tx_type
    assert tx.hash == tx_hash
    assert tx.sender == _checksummed(ENTRIES[seq]["message"]["message"]["header"]["sender"])
    assert tx.data_len == input_len
    if to is not None:
        assert tx.to == to
    if value is not None:
        assert tx.value == value
    if gas is not None:
        assert tx.gas == gas


def _checksummed(address: str) -> str:
    from rhfeed.codec import addr, checksum

    return checksum(addr(address))


def test_a_submit_retryable_calls_the_precompile_with_its_fields():
    (tx,) = one(ENTRIES[18382981], 99801).txs
    assert tx.selector_hex == "0xc9f95d32"
    assert tx.kind == "call"
    # The request id is the first argument.
    header = ENTRIES[18382981]["message"]["message"]["header"]
    assert tx.data[4:36].hex() == header["requestId"][2:]


def test_without_a_chain_id_these_messages_carry_no_transactions():
    for entry in ENTRIES.values():
        assert one(entry, None).txs == []


def test_a_different_chain_id_gives_a_different_hash():
    assert one(ENTRIES[18382981], 4663).txs[0].hash != NODE[18382981][2]


def _with_payload(seq: int, payload: bytes) -> dict:
    entry = copy.deepcopy(ENTRIES[seq])
    entry["message"]["message"]["l2Msg"] = base64.b64encode(payload).decode()
    return entry


def _payload(seq: int) -> bytes:
    return base64.b64decode(ENTRIES[seq]["message"]["message"]["l2Msg"])


@pytest.mark.parametrize("cut", [0, 51])
def test_a_short_deposit_is_refused(cut):
    """parseEthDepositMessage reads 20 + 32 bytes with io.ReadFull."""
    assert one(_with_payload(18382979, _payload(18382979)[:cut]), 99801).txs == []


def test_a_retryable_short_of_its_nine_words_is_refused():
    assert one(_with_payload(18382981, _payload(18382981)[:287]), 99801).txs == []


def test_a_retryable_with_no_data_left_is_refused():
    """The data is read with a plain Read: only an empty tail fails."""
    assert one(_with_payload(18382981, _payload(18382981)[:288]), 99801).txs == []


def test_a_short_retryable_tail_is_zero_padded():
    full = _payload(18382981)
    (tx,) = one(_with_payload(18382981, full[:-1]), 99801).txs
    (padded,) = one(_with_payload(18382981, full[:-1] + b"\x00"), 99801).txs
    assert tx.hash == padded.hash


def test_a_retryable_gas_limit_past_uint64_is_refused():
    payload = bytearray(_payload(18382981))
    payload[6 * 32 + 23] = 1  # bit 64 of the gas-limit word
    assert one(_with_payload(18382981, bytes(payload)), 99801).txs == []


def test_a_message_without_a_request_id_is_refused():
    entry = copy.deepcopy(ENTRIES[18382979])
    del entry["message"]["message"]["header"]["requestId"]
    assert one(entry, 99801).txs == []


# --------------------------------------------------------------------------- #
# where the consumer gets the chain id
# --------------------------------------------------------------------------- #


class _Conn:
    def __init__(self, headers: dict[str, str]):
        from websockets.datastructures import Headers

        self.response = type("Response", (), {"headers": Headers(headers)})()


@pytest.mark.parametrize(
    ("url", "headers", "verify", "want"),
    [
        ("ws://relay", {"Arbitrum-Chain-Id": "99801"}, MAINNET_VERIFIER, 99801),
        (MAINNET_FEED, {}, None, 4663),
        ("ws://relay", {}, MAINNET_VERIFIER, 4663),
        ("ws://relay", {}, None, None),
    ],
)
def test_the_chain_id_comes_from_the_feed_then_the_url_then_the_verifier(
    url, headers, verify, want
):
    assert FeedConsumer(url, verify=verify)._find_chain_id(_Conn(headers)) == want


def test_the_known_testnet_chain_id_is_testnet():
    assert KNOWN_CHAIN_IDS["wss://feed.testnet.chain.robinhood.com"] == 46630


def test_the_consumer_builds_them_with_its_chain_id():
    consumer = FeedConsumer(chain_id=99801)
    consumer._live = True
    (msg,) = consumer._frame({"messages": [ENTRIES[18382981]]}, 0.0, False)
    assert msg.txs[0].hash == NODE[18382981][2]
