from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from cmx_plugin_testlib import AGENT_TOKEN, FakeGuard

from cashmaxx import plugin
from cashmaxx.config import CashmaxxAgentConfig
from cashmaxx.plugin import tools as t
from nanobot.agent.tools.base import ToolResult
from nanobot.agent.tools.context import RequestContext, request_context

PAY_ARGS = {"to": "0xDEF", "amount_usd": "1.5", "purpose": "buy a dataset", "category": "payment_out"}


def _is_error(result: object) -> bool:
    return isinstance(result, ToolResult) and result.is_error


# --- idempotency ------------------------------------------------------------------------------


def test_idempotency_key_is_stable_and_normalized() -> None:
    now = datetime(2026, 9, 26, 10, tzinfo=timezone.utc)
    a = t.idempotency_key("s1", "0xDEF", Decimal("1.5"), "buy  a dataset", "payment_out", now=now)
    b = t.idempotency_key("s1", "0xdef ", Decimal("1.50"), "buy a dataset", "payment_out",
                          now=now.replace(hour=23))
    assert a == b
    assert a.startswith("cmx-")


@pytest.mark.parametrize("change", [
    {"session": "s2"}, {"to": "0x999"}, {"amount": Decimal("2")}, {"purpose": "other"},
    {"category": "fees"}, {"day": 27},
])
def test_idempotency_key_changes_with_inputs(change: dict) -> None:
    base = dict(session="s1", to="0xdef", amount=Decimal("1.5"), purpose="p", category="payment_out",
                day=26)
    base.update(change)
    key = t.idempotency_key(base["session"], base["to"], base["amount"], base["purpose"],
                            base["category"], now=datetime(2026, 9, base["day"], tzinfo=timezone.utc))
    ref = t.idempotency_key("s1", "0xdef", Decimal("1.5"), "p", "payment_out",
                            now=datetime(2026, 9, 26, tzinfo=timezone.utc))
    assert key != ref


async def test_retried_pay_sends_same_key(fake_guard: FakeGuard) -> None:
    fake_guard.set("POST", "/spend", {"status": "paid", "payment_id": "p1", "tx_hash": "0xtx"})
    tool = t.PayTool()
    ctx = RequestContext(channel="telegram", chat_id="42", session_key="telegram:42")
    with request_context(ctx):
        await tool.execute(**PAY_ARGS)
        first = fake_guard.last_json()["idempotency_key"]
        await tool.execute(**PAY_ARGS)
        second = fake_guard.last_json()["idempotency_key"]
    assert first == second


# --- pay ----------------------------------------------------------------------------------------


async def test_pay_paid(fake_guard: FakeGuard) -> None:
    fake_guard.set("POST", "/spend", {"status": "paid", "payment_id": "p1", "tx_hash": "0xtx"})
    result = await t.PayTool().execute(**PAY_ARGS)
    assert not _is_error(result)
    assert "Paid 1.50 USDC to 0xDEF" in result and "0xtx" in result
    body = fake_guard.last_json()
    assert body["amount_usd"] == "1.50" and body["kind"] == "transfer"
    assert fake_guard.last.headers["authorization"] == f"Bearer {AGENT_TOKEN}"


async def test_pay_pending_says_wait_and_never_resend(fake_guard: FakeGuard) -> None:
    fake_guard.set("POST", "/spend", {"status": "pending", "payment_id": "p2", "approval_id": "ap9"})
    result = await t.PayTool().execute(**PAY_ARGS)
    assert not _is_error(result)
    assert "ap9" in result and "cashmaxx_payment_status" in result
    assert "Never send this payment again" in result


async def test_pay_denied_is_error_with_hint(fake_guard: FakeGuard) -> None:
    fake_guard.set("POST", "/spend", {"status": "denied", "payment_id": "p3", "reason": "daily_cap"})
    result = await t.PayTool().execute(**PAY_ARGS)
    assert _is_error(result)
    assert "daily_cap" in result and "cap" in result


async def test_pay_guard_down_fails_closed(fake_guard: FakeGuard) -> None:
    fake_guard.down = True
    result = await t.PayTool().execute(**PAY_ARGS)
    assert _is_error(result)
    assert result == t.GUARD_DOWN


async def test_pay_invalid_amount_never_calls_guard(fake_guard: FakeGuard) -> None:
    result = await t.PayTool().execute(**{**PAY_ARGS, "amount_usd": "-1"})
    assert _is_error(result)
    assert fake_guard.requests == []


async def test_guard_http_error_is_reported(fake_guard: FakeGuard) -> None:
    fake_guard.set("POST", "/spend", {"error": "unauthorized", "message": "bad token"}, status=401)
    result = await t.PayTool().execute(**PAY_ARGS)
    assert _is_error(result) and "unauthorized" in result


async def test_not_configured_fails_closed(fake_guard: FakeGuard) -> None:
    plugin.configure(CashmaxxAgentConfig(guard_url="http://guard.test", agent_token=""))
    result = await t.PayTool().execute(**PAY_ARGS)
    assert result == t.NOT_CONFIGURED and _is_error(result)
    assert fake_guard.requests == []


# --- the other tools: success / pending / denied / down ----------------------------------------


async def test_wallet(fake_guard: FakeGuard) -> None:
    result = await t.WalletTool().execute()
    assert "0xabc" in result and "12.50" in result and "40.00" in result
    assert "This is your own wallet" in result and "Never create or import another wallet" in result
    assert result.startswith("Now: ") and " UTC (" in result.splitlines()[0]
    fake_guard.down = True
    assert await t.WalletTool().execute() == t.GUARD_DOWN


async def test_payment_status(fake_guard: FakeGuard) -> None:
    fake_guard.set("GET", "/spend/p1", {"payment_id": "p1", "status": "pending"})
    result = await t.PaymentStatusTool().execute(payment_id="p1")
    assert "pending" in result and "Do not resend" in result
    fake_guard.set("GET", "/spend/p1", {"payment_id": "p1", "status": "denied", "reason": "frozen"})
    assert "denied (frozen)" in await t.PaymentStatusTool().execute(payment_id="p1")
    fake_guard.set("GET", "/spend/p1", {"payment_id": "p1", "status": "paid", "tx_hash": "0x1"})
    assert "paid" in await t.PaymentStatusTool().execute(payment_id="p1")
    fake_guard.down = True
    assert await t.PaymentStatusTool().execute(payment_id="p1") == t.GUARD_DOWN


@pytest.fixture
def allow_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    import nanobot.security.network as network

    monkeypatch.setattr(network, "validate_url_target", lambda url, **_: (True, ""))


X402_ARGS = {"url": "https://api.example.com/data", "max_usd": "0.05", "purpose": "market data"}


async def test_x402_paid(fake_guard: FakeGuard, allow_urls: None) -> None:
    fake_guard.set("POST", "/x402/fetch", {
        "status": "paid", "http_status": 200, "body_text": '{"ok":1}', "paid_usd": "0.01",
        "payment_id": "p5",
    })
    result = await t.X402FetchTool().execute(**X402_ARGS)
    assert "HTTP 200" in result and '{"ok":1}' in result and "0.01" in result
    assert fake_guard.last_json()["max_usd"] == "0.05"


async def test_x402_pending_denied_down(fake_guard: FakeGuard, allow_urls: None) -> None:
    fake_guard.set("POST", "/x402/fetch", {"status": "pending", "payment_id": "p6",
                                           "approval_id": "ap6"})
    assert "ap6" in await t.X402FetchTool().execute(**X402_ARGS)
    fake_guard.set("POST", "/x402/fetch", {"status": "denied", "payment_id": "p7",
                                           "reason": "over_budget"})
    denied = await t.X402FetchTool().execute(**X402_ARGS)
    assert _is_error(denied) and "over_budget" in denied
    fake_guard.down = True
    assert await t.X402FetchTool().execute(**X402_ARGS) == t.GUARD_DOWN


async def test_x402_blocks_private_urls(fake_guard: FakeGuard) -> None:
    result = await t.X402FetchTool().execute(url="http://127.0.0.1:18799/wallet", max_usd="1",
                                             purpose="sneaky")
    assert _is_error(result) and "not allowed" in result
    assert fake_guard.requests == []


async def test_ledger(fake_guard: FakeGuard) -> None:
    result = await t.LedgerTool().execute(window="7d")
    assert '"net":"1.75"' in result
    assert fake_guard.last.url.params["window"] == "7d"
    fake_guard.down = True
    assert await t.LedgerTool().execute() == t.GUARD_DOWN


async def test_record_cost(fake_guard: FakeGuard) -> None:
    fake_guard.set("POST", "/ledger/cost", {"id": 1})
    result = await t.RecordCostTool().execute(amount_usd="12", category="other", note="domain")
    assert "12.00" in result
    assert fake_guard.last_json() == {"amount_usd": "12.00", "category": "other", "note": "domain"}
    fake_guard.set("POST", "/ledger/cost", {"error": "invalid", "message": "no"}, status=422)
    assert _is_error(await t.RecordCostTool().execute(amount_usd="1", category="other", note="x y"))
    fake_guard.down = True
    assert await t.RecordCostTool().execute(amount_usd="1", category="other", note="abc") == t.GUARD_DOWN


async def test_sell_product(fake_guard: FakeGuard) -> None:
    fake_guard.set("POST", "/stripe/product", {
        "product_id": "prod_1", "price_id": "price_1", "payment_link_url": "https://buy.stripe.com/x",
    })
    result = await t.SellProductTool().execute(name="Guide", description="A useful guide",
                                               price_usd="9")
    assert "https://buy.stripe.com/x" in result
    fake_guard.set("POST", "/stripe/product", {"error": "stripe_not_configured", "message": "no"},
                   status=409)
    denied = await t.SellProductTool().execute(name="Guide", description="A useful guide",
                                               price_usd="9")
    assert _is_error(denied) and "stripe_not_configured" in denied
    fake_guard.down = True
    assert await t.SellProductTool().execute(name="G", description="0123456789",
                                             price_usd="9") == t.GUARD_DOWN


async def test_settings(fake_guard: FakeGuard) -> None:
    assert "base-sepolia" in await t.SettingsTool().execute()
    fake_guard.down = True
    assert await t.SettingsTool().execute() == t.GUARD_DOWN


def test_agent_view_hides_fields_of_other_compute_modes() -> None:
    base = {"network": "fake", "ownerWallet": None, "reimburseIntervalHours": 24,
            "ownerTopupThresholdUsd": "2", "x402GatewayUrl": None}
    virtual = t.agent_view_of_settings({"settings": {**base, "computePaymentMode": "virtual"}})
    assert set(virtual["settings"]) == {"network", "computePaymentMode"}
    reimburse = t.agent_view_of_settings(
        {"settings": {**base, "computePaymentMode": "reimburse", "ownerWallet": "0xabc"}})
    assert reimburse["settings"]["ownerWallet"] == "0xabc"
    assert "x402GatewayUrl" not in reimburse["settings"]
    gateway = t.agent_view_of_settings({"settings": {**base, "computePaymentMode": "x402_gateway"}})
    assert "x402GatewayUrl" in gateway["settings"] and "ownerWallet" not in gateway["settings"]


async def test_freeze(fake_guard: FakeGuard) -> None:
    assert "frozen" in await t.FreezeTool().execute(reason="odd payment")
    assert fake_guard.last_json() == {"reason": "odd payment"}
    fake_guard.down = True
    assert await t.FreezeTool().execute(reason="odd") == t.GUARD_DOWN


# --- registration -----------------------------------------------------------------------------


def test_enabled_follows_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import nanobot.config.loader as loader

    ctx = SimpleNamespace(config=SimpleNamespace())
    assert t.WalletTool.enabled(ctx)  # pinned by the fixture
    plugin.reset()
    cfg_path = tmp_path / "config.json"
    monkeypatch.setattr(loader, "_current_config_path", cfg_path)
    assert not t.WalletTool.enabled(ctx)  # no file
    cfg_path.write_text('{"agents": {}}', encoding="utf-8")
    assert not t.WalletTool.enabled(ctx)  # no cashmaxx section
    cfg_path.write_text('{"cashmaxx": {"guardUrl": "http://g", "agentToken": "tok"}}',
                        encoding="utf-8")
    import os

    os.utime(cfg_path, (1, 1))  # new mtime for the cache
    assert t.WalletTool.enabled(ctx)
    assert plugin.resolve_agent_config().agent_token == "tok"


def test_entry_points_resolve_to_tools() -> None:
    from importlib.metadata import entry_points

    eps = {ep.name: ep.load() for ep in entry_points(group="nanobot.tools")
           if ep.name.startswith("cashmaxx_")}
    assert len(eps) == 15
    for name, cls in eps.items():
        tool = cls()
        assert tool.name == name and tool.description
        schema = json.loads(json.dumps(tool.to_schema()))  # must be sent to the LLM as JSON
        params = schema["function"]["parameters"]
        assert params["type"] == "object" and params.get("additionalProperties") is False
        assert set(params.get("required", [])) <= set(params["properties"])
    sell = eps["cashmaxx_sell_product"]().parameters
    assert set(sell["properties"]) == {"name", "description", "price_usd"}
    assert "description" not in sell or isinstance(sell.get("description"), str)
