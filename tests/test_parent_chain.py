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

# seq: (chain id, type, hash, to, value, gas, input length), from eth_getTransactionByHash
PRECOMPILE = "0x000000000000000000000000000000000000006e"
NODE = {
    100864538: (
        4663,
        0x69,
        "0x793b9e3eca7c17ae394bf9ca8c4840ce97bc9a274d5e4fadf5c376cf5d957b6d",
        PRECOMPILE,
        0,
        126025,
        1156,
    ),
    100868052: (
        4663,
        0x64,
        "0xcb29b668d7f387ca6fa429853959d4354f7f918458bf4492846bfdeea759a313",
        "0x48e2a5ebd991e240a1e0455e89a6104c20663b7f",
        99845787124619670,
        0,
        0,
    ),
    18382979: (
        99801,
        0x64,
        "0x70532e51db367a70a14937be531cd5267fa23bec628eeb4199d4abdc01cc8a99",
        "0xe12257ee1580a015ddd85ad92fef28c62000725c",
        15000000000000000,
        0,
        0,
    ),
    18382981: (
        99801,
        0x69,
        "0x30a61b90f62a26ecf83aa62e8ddb4ca87b15391b6b36f54c8354713178745bf9",
        PRECOMPILE,
        0,
        300000,
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
    assert tx.to.lower() == to
    assert tx.value == value
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


# --------------------------------------------------------------------------- #
# malformed input: arbos refuses the message, and parse_frame must not raise
# --------------------------------------------------------------------------- #


def _with_header(seq: int, **fields) -> dict:
    entry = copy.deepcopy(ENTRIES[seq])
    entry["message"]["message"]["header"].update(fields)
    return entry


@pytest.mark.parametrize("seq", [18382979, 18382981])
@pytest.mark.parametrize(
    "fields", [{"requestId": None}, {"requestId": 7}, {"sender": None}, {"sender": ["0x"]}]
)
def test_a_missing_or_malformed_request_id_or_poster_is_refused(seq, fields):
    """A nil RequestId is JSON null; parse_l2.go refuses the message without one."""
    assert one(_with_header(seq, **fields), NODE[seq][0]).txs == []


@pytest.mark.parametrize("fee", ["0x10", "16", -1, 2**256, [1], 1.5, True])
def test_a_malformed_l1_base_fee_is_refused(fee):
    """L1BaseFee is a *big.Int: a JSON integer or null, nothing else decodes into one."""
    assert one(_with_header(18382981, baseFeeL1=fee), 99801).txs == []


def test_a_null_l1_base_fee_is_zero():
    zero = one(_with_header(18382981, baseFeeL1=0), 99801).txs[0].hash
    assert one(_with_header(18382981, baseFeeL1=None), 99801).txs[0].hash == zero


@pytest.mark.parametrize("seq", [18382979, 18382981])
def test_a_payload_past_the_l2_message_limit_is_refused(seq):
    """ParseL2Transactions refuses any l2Msg over MaxL2MessageSize before its kind."""
    padded = _payload(seq) + bytes(256 * 1024)
    assert one(_with_payload(seq, padded), NODE[seq][0]).txs == []


def test_an_l2msg_that_is_not_base64_is_refused():
    entry = copy.deepcopy(ENTRIES[18382981])
    entry["message"]["message"]["l2Msg"] = "abc"  # bad padding
    assert one(entry, 99801).txs == []
