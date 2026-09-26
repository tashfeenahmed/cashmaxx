"""CDP / x402 backend: pure parts and an offline x402 round trip. No network.

Set CASHMAXX_LIVE_CDP=1 (plus CDP_API_KEY_ID / CDP_API_KEY_SECRET / CDP_WALLET_SECRET) to run the
live Base Sepolia smoke test.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from typing import Any

import httpx
import pytest
from eth_account import Account
from x402.http import decode_payment_signature_header, encode_payment_required_header
from x402.schemas.payments import PaymentRequired, PaymentRequirements

from cashmaxx.config import CdpConfig
from cashmaxx.guard.wallet import USDC_ADDRESSES
from cashmaxx.guard.wallet.cdp import (
    TRANSFER_TOPIC,
    BaseRpc,
    CdpWallet,
    address_topic,
    parse_transfer_log,
    x402_paid_fetch,
)
from cashmaxx.money import from_usdc_units, to_usdc_units

OURS = "0x" + "c" * 40
SENDER = "0x" + "d" * 40
PAY_TO = "0x1111111111111111111111111111111111111111"


def test_usdc_amount_conversion() -> None:
    assert to_usdc_units(Decimal("1.5")) == 1_500_000
    assert to_usdc_units(Decimal("0.0000019")) == 1  # rounds down, never up
    assert from_usdc_units(10_000) == Decimal("0.01")


def test_parse_transfer_log() -> None:
    log = {
        "address": USDC_ADDRESSES["base-sepolia"],
        "topics": [TRANSFER_TOPIC, address_topic(SENDER), address_topic(OURS)],
        "data": hex(2_500_000),
        "blockNumber": hex(123),
        "transactionHash": "0xabc",
        "logIndex": "0x2",
    }
    item = parse_transfer_log(log)
    assert item.from_addr == SENDER and item.amount_usd == Decimal("2.5")
    assert (item.block, item.log_index, item.ref) == (123, 2, "0xabc:2")
    assert address_topic(OURS) == "0x" + "0" * 24 + "c" * 40
    with pytest.raises(ValueError):
        parse_transfer_log({**log, "topics": ["0xdead"]})


def test_cdp_wallet_requires_credentials() -> None:
    with pytest.raises(ValueError):
        CdpWallet(CdpConfig(), "base-sepolia")
    with pytest.raises(ValueError):
        creds = CdpConfig(api_key_id="a", api_key_secret="b", wallet_secret="c")
        CdpWallet(creds, "fake")  # type: ignore[arg-type]


async def test_incoming_transfers_scans_logs_with_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[dict[str, Any]] = []
    head = {"n": 5000}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if body["method"] == "eth_blockNumber":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                             "result": hex(head["n"])})
        flt = body["params"][0]
        assert flt["topics"][2] == address_topic(OURS)
        logs = []
        if int(flt["fromBlock"], 16) <= 4500 <= int(flt["toBlock"], 16):
            logs.append({"topics": [TRANSFER_TOPIC, address_topic(SENDER), address_topic(OURS)],
                         "data": hex(1_000_000), "blockNumber": hex(4500),
                         "transactionHash": "0x01", "logIndex": "0x0"})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": logs})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    wallet = CdpWallet(CdpConfig(api_key_id="a", api_key_secret="b", wallet_secret="c"),
                       "base-sepolia", rpc_url="https://rpc.test", http=http)

    async def fake_address() -> str:
        return OURS

    monkeypatch.setattr(wallet, "address", fake_address)
    items, cursor = await wallet.incoming_transfers(None)
    assert items == [] and cursor == "4998"  # first call starts at head - confirmations
    head["n"] = 6200
    items, cursor = await wallet.incoming_transfers("3000")
    assert [i.amount_usd for i in items] == [Decimal("1")]
    assert cursor == "6198"
    ranges = [(int(r["params"][0]["fromBlock"], 16), int(r["params"][0]["toBlock"], 16))
              for r in requests if r["method"] == "eth_getLogs"]
    assert ranges == [(3001, 4000), (4001, 5000), (5001, 6000), (6001, 6198)]
    await wallet.aclose()


async def test_rpc_error_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                                         "error": {"code": -32000, "message": "range"}})

    rpc = BaseRpc("https://rpc.test", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(Exception, match="range"):
        await rpc.block_number()


def _x402_server(amount: str, network: str = "eip155:84532",
                 asset: str = USDC_ADDRESSES["base-sepolia"]) -> tuple[Any, list[Any]]:
    required = PaymentRequired(accepts=[PaymentRequirements(
        scheme="exact", network=network, asset=asset, amount=amount, pay_to=PAY_TO,
        max_timeout_seconds=60, extra={"name": "USDC", "version": "2"})])
    payloads: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/free":
            return httpx.Response(200, text="free")
        sig = request.headers.get("PAYMENT-SIGNATURE")
        if sig:
            payloads.append(decode_payment_signature_header(sig))
            return httpx.Response(200, text="paid content")
        header = encode_payment_required_header(required)
        return httpx.Response(402, json={}, headers={"PAYMENT-REQUIRED": header})

    return handler, payloads


async def test_x402_round_trip_with_local_signer() -> None:
    handler, payloads = _x402_server("10000")
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    signer = Account.create()
    status, body, paid = await x402_paid_fetch(http, signer, "base-sepolia",
                                               "https://api.test/data", "GET", None,
                                               Decimal("0.05"))
    assert (status, body, paid) == (200, "paid content", Decimal("0.01"))
    payload = payloads[0]
    assert payload.accepted.pay_to == PAY_TO
    assert payload.payload["authorization"]["from"].lower() == signer.address.lower()

    assert await x402_paid_fetch(http, signer, "base-sepolia", "https://api.test/free", "GET",
                                 None, Decimal("0.05")) == (200, "free", Decimal("0"))


@pytest.mark.parametrize(
    ("amount", "network", "wallet_network", "max_usd"),
    [
        ("10000", "eip155:84532", "base-sepolia", "0.005"),  # over max_usd
        ("10000", "eip155:84532", "base", "1"),  # wrong network for this guard
    ],
)
async def test_x402_refuses_to_overpay(amount: str, network: str, wallet_network: str,
                                       max_usd: str) -> None:
    handler, payloads = _x402_server(amount, network=network)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    status, _, paid = await x402_paid_fetch(http, Account.create(), wallet_network,
                                            "https://api.test/data", "GET", None,
                                            Decimal(max_usd))
    assert status == 402 and paid == 0 and payloads == []


async def test_x402_refuses_non_usdc_asset() -> None:
    handler, payloads = _x402_server("1", asset="0x" + "9" * 40)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    status, _, paid = await x402_paid_fetch(http, Account.create(), "base-sepolia",
                                            "https://api.test/data", "GET", None, Decimal("1"))
    assert status == 402 and paid == 0 and payloads == []


@pytest.mark.skipif(os.environ.get("CASHMAXX_LIVE_CDP") != "1", reason="live CDP test")
async def test_live_cdp_balance_and_address() -> None:  # pragma: no cover - needs network
    cfg = CdpConfig(
        api_key_id=os.environ["CDP_API_KEY_ID"], api_key_secret=os.environ["CDP_API_KEY_SECRET"],
        wallet_secret=os.environ["CDP_WALLET_SECRET"], account_name="cashmaxx-live-test",
    )
    wallet = CdpWallet(cfg, "base-sepolia")
    try:
        address = await wallet.address()
        assert address.startswith("0x")
        assert await wallet.balance_usdc() >= 0
        _, cursor = await wallet.incoming_transfers(None)
        assert cursor is not None
    finally:
        await wallet.aclose()
