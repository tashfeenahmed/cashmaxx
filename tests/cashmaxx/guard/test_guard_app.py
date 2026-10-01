from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from guard_testlib import ALICE, BOB, PIN, Env, EnvFactory, RecordingNotifier, make_config

from cashmaxx.config import load_guard_config
from cashmaxx.guard import ledger
from cashmaxx.guard.app import GUARD_KEY, build_wallet, create_app
from cashmaxx.guard.wallet.fake import FakeWallet, FakeX402Resource


async def test_health_needs_no_auth(env: Env) -> None:
    resp = await env.client.get("/health")
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True and body["network"] == "fake" and body["frozen"] is False
    assert "version" in body


async def test_auth_required_and_errors_are_json(env: Env) -> None:
    resp = await env.client.get("/wallet")
    assert resp.status == 401
    assert (await resp.json())["error"] == "unauthorized"
    resp = await env.client.get("/wallet", headers={"Authorization": "Bearer wrong"})
    assert resp.status == 401
    resp = await env.client.get("/nope", headers=env.agent)
    assert resp.status == 404 and (await resp.json())["error"] == "not_found"
    resp = await env.client.post("/spend", data="not json", headers=env.agent)
    assert resp.status == 422 and (await resp.json())["error"] == "invalid"


async def test_wallet(env: Env) -> None:
    resp = await env.client.get("/wallet", headers=env.agent)
    body = await resp.json()
    assert body["address"] == env.wallet.wallet_address
    assert body["balance_usdc"] == "100.00"
    assert body["available_budget_usd"] == "50.00"


async def test_auto_payment_records_ledger(env: Env) -> None:
    status, body = await env.spend(amount="2.5")
    assert status == 200
    assert body["status"] == "paid" and body["tx_hash"].startswith("0x")
    assert env.wallet.transfers[0][:2] == (ALICE, Decimal("2.5"))
    ledger = await (await env.client.get("/ledger?window=all", headers=env.agent)).json()
    assert ledger["costs"] == "2.50" and ledger["by_category"] == {"payment_out": "2.50"}
    assert ledger["available_budget_usd"] == "47.50"
    assert any("Paid $2.50" in t for t in env.notifier.texts())
    resp = await env.client.get(f"/spend/{body['payment_id']}", headers=env.agent)
    assert (await resp.json())["status"] == "paid"


async def test_idempotency_never_pays_twice(env: Env) -> None:
    _, first = await env.spend(amount="1", key="same")
    _, second = await env.spend(amount="1", key="same")
    _, third = await env.spend(amount="3", key="same", to=BOB)  # same key, different args
    assert first["payment_id"] == second["payment_id"] == third["payment_id"]
    assert len(env.wallet.transfers) == 1


async def test_denials_are_recorded(env: Env) -> None:
    status, body = await env.spend(amount="0", key="a")
    assert status == 200 and body["status"] == "denied" and body["reason"] == "invalid"
    status, body = await env.spend(amount="1", key="b", kind="swap")
    assert body["reason"] == "unsupported"
    status, body = await env.spend(amount="60", key="c")
    assert body["reason"] == "over_budget"
    resp = await env.client.post("/spend", headers=env.agent, json={
        "to": ALICE, "amount_usd": "1", "purpose": "x", "idempotency_key": "d",
        "category": "reimbursement"})
    assert resp.status == 422
    events = (await (await env.client.get("/events", headers=env.agent)).json())["events"]
    decisions = [e for e in events if e["type"] == "spend_decision"]
    assert [e["data"]["reason"] for e in decisions] == ["invalid", "unsupported", "over_budget"]


async def test_daily_cap_and_rate_limit(make_env: EnvFactory) -> None:
    env = await make_env(settings={"max_payments_per_hour": 3, "daily_cap_usd": "10"})
    for i in range(2):
        assert (await env.spend(amount="4", key=f"cap{i}"))[1]["status"] == "paid"
    assert (await env.spend(amount="4", key="cap2"))[1]["reason"] == "daily_cap"
    assert (await env.spend(amount="1", key="r1"))[1]["status"] == "paid"
    assert (await env.spend(amount="1", key="r2"))[1]["reason"] == "rate_limited"
    env.clock.advance(hours=1, seconds=1)
    assert (await env.spend(amount="1.5", key="r3"))[1]["reason"] == "daily_cap"
    env.clock.advance(days=1)
    assert (await env.spend(amount="1", key="r4"))[1]["status"] == "paid"


async def test_new_recipient_approval_flow(env: Env) -> None:
    status, body = await env.spend(to=BOB, amount="1", key="bob1")
    assert status == 200 and body["status"] == "pending" and body["reason"] == "new_recipient"
    approval_id = body["approval_id"]
    assert env.notifier.sent[-1][1] == approval_id  # card with buttons
    assert "first payment to this recipient" in env.notifier.sent[-1][0]

    listed = await (await env.client.get("/approvals?status=pending", headers=env.agent)).json()
    assert [a["id"] for a in listed["approvals"]] == [approval_id]
    assert listed["approvals"][0]["payment"]["to"] == BOB

    # the agent can read but never approve
    resp = await env.client.post(f"/approvals/{approval_id}/approve", headers=env.agent)
    assert resp.status == 403
    owner = await env.owner()
    resp = await env.client.post(f"/approvals/{approval_id}/approve", headers=owner)
    body = await resp.json()
    assert resp.status == 200 and body["status"] == "paid" and body["approval_status"] == "approved"
    # second decision conflicts
    resp = await env.client.post(f"/approvals/{approval_id}/deny", headers=owner)
    assert resp.status == 409
    # BOB is now known: small payments go straight through
    assert (await env.spend(to=BOB, amount="1", key="bob2"))[1]["status"] == "paid"


async def test_deny_and_over_threshold(env: Env) -> None:
    _, body = await env.spend(amount="6", key="big")
    assert body["status"] == "pending"
    owner = await env.owner()
    resp = await env.client.post(f"/approvals/{body['approval_id']}/deny", headers=owner)
    denied = await resp.json()
    assert denied["status"] == "denied" and denied["reason"] == "owner_denied"
    assert env.wallet.transfers == []
    resp = await env.client.post("/approvals/apr_missing/approve", headers=owner)
    assert resp.status == 404


async def test_approve_rechecks_policy(env: Env) -> None:
    _, body = await env.spend(amount="6", key="big")
    # frozen after the request: approval must not pay
    await env.client.post("/freeze", headers=env.agent, json={"reason": "test"})
    owner = await env.owner()
    resp = await env.client.post(f"/approvals/{body['approval_id']}/approve", headers=owner)
    result = await resp.json()
    assert resp.status == 409 and result["error"] == "frozen"
    assert result["status"] == "denied" and result["reason"] == "frozen"
    assert env.wallet.transfers == []


async def test_approve_rechecks_daily_cap(env: Env) -> None:
    _, pending = await env.spend(amount="6", key="big")
    for i in range(2):
        await env.spend(amount="2.5", key=f"s{i}")
    owner = await env.owner()
    resp = await env.client.post(f"/approvals/{pending['approval_id']}/approve", headers=owner)
    assert (await resp.json())["reason"] == "daily_cap"


async def test_freeze_anyone_unfreeze_owner_only(env: Env) -> None:
    resp = await env.client.post("/freeze", headers=env.agent, json={"reason": "agent panic"})
    assert resp.status == 200 and (await resp.json())["frozen"] is True
    assert (await (await env.client.get("/health")).json())["frozen"] is True
    status, body = await env.spend(key="while-frozen")
    assert status == 409 and body["error"] == "frozen" and body["status"] == "denied"
    # the idempotent replay gives the same answer
    assert (await env.spend(key="while-frozen"))[0] == 409

    resp = await env.client.post("/unfreeze", headers=env.agent)
    assert resp.status == 403
    resp = await env.client.post("/unfreeze")
    assert resp.status == 401
    owner = await env.owner()
    resp = await env.client.post("/unfreeze", headers=owner)
    assert resp.status == 200 and (await resp.json())["frozen"] is False
    # persisted to guard.json
    assert load_guard_config(env.config_path).settings.frozen is False
    assert any("FROZEN" in t for t in env.notifier.texts())


async def test_freeze_persists(env: Env) -> None:
    await env.client.post("/freeze", headers=env.agent, json={"reason": "r"})
    saved = load_guard_config(env.config_path)
    assert saved.settings.frozen is True and saved.settings.frozen_reason == "r"


async def test_owner_only_endpoints_reject_agent(env: Env) -> None:
    for method, path in [("POST", "/approvals/x/approve"), ("POST", "/approvals/x/deny"),
                         ("PATCH", "/settings"), ("POST", "/unfreeze")]:
        resp = await env.client.request(method, path, headers=env.agent, json={})
        assert resp.status == 403, path
        assert (await resp.json())["error"] == "forbidden"


async def test_agent_only_endpoints_reject_owner(env: Env) -> None:
    owner = await env.owner()
    resp = await env.client.post("/spend", headers=owner, json={})
    assert resp.status == 403


async def test_owner_session_and_pin_rate_limit(env: Env) -> None:
    resp = await env.client.post("/owner/session", json={"pin": "0000"})
    assert resp.status == 401 and (await resp.json())["error"] == "invalid_pin"
    resp = await env.client.post("/owner/session", json={"pin": PIN})
    body = await resp.json()
    assert resp.status == 200 and body["session"] and body["expires_at"]
    for _ in range(3):
        await env.client.post("/owner/session", json={"pin": "0000"})
    # 5 attempts used this minute: even the right PIN is refused now
    resp = await env.client.post("/owner/session", json={"pin": PIN})
    assert resp.status == 429 and (await resp.json())["error"] == "rate_limited"
    env.clock.advance(seconds=61)
    assert (await env.client.post("/owner/session", json={"pin": PIN})).status == 200
    events = (await (await env.client.get("/events", headers=env.agent)).json())["events"]
    assert any(e["type"] == "owner_login_failed" for e in events)
    assert all(PIN not in json.dumps(e) for e in events)


async def test_owner_session_expires(env: Env) -> None:
    owner = await env.owner()
    assert (await env.client.get("/settings", headers=owner)).status == 200
    env.clock.advance(hours=12, seconds=1)
    assert (await env.client.get("/settings", headers=owner)).status == 401


async def test_settings_get_patch(env: Env) -> None:
    agent_view = await (await env.client.get("/settings", headers=env.agent)).json()
    assert agent_view["settings"]["budgetUsd"] == "50"
    assert "integrations" not in agent_view
    raw = json.dumps(agent_view)
    assert "agentTokenHash" not in raw and "ownerPinHash" not in raw

    owner = await env.owner()
    resp = await env.client.patch("/settings", headers=owner,
                                  json={"budget_usd": "75", "dailyCapUsd": "20"})
    body = await resp.json()
    assert resp.status == 200, body
    assert body["settings"]["budgetUsd"] == "75" and body["settings"]["dailyCapUsd"] == "20"
    assert body["integrations"]["stripe"] is False and body["restart_required"] is False
    saved = load_guard_config(env.config_path)
    assert saved.settings.budget_usd == Decimal("75")

    for bad in ({"budgetUsd": "-1"}, {"nope": 1}, {"frozen": False},
                {"computePaymentMode": "reimburse"}):
        resp = await env.client.patch("/settings", headers=owner, json=bad)
        assert resp.status == 422, bad
        assert (await resp.json())["error"] == "invalid"
    assert env.guard.settings.budget_usd == Decimal("75")


async def test_record_cost(env: Env) -> None:
    resp = await env.client.post("/ledger/cost", headers=env.agent,
                                 json={"amount_usd": "12", "category": "other", "note": "domain"})
    assert resp.status == 200 and (await resp.json())["source"] == "agent_reported"
    resp = await env.client.post("/ledger/cost", headers=env.agent,
                                 json={"amount_usd": "1", "category": "sale_stripe", "note": ""})
    assert resp.status == 422
    ledger = await (await env.client.get("/ledger?window=7d", headers=env.agent)).json()
    assert ledger["costs"] == "12.00"
    assert ledger["entries"][0]["verified"] is False
    assert (await env.client.get("/ledger?window=2y", headers=env.agent)).status == 422


async def test_x402_fetch_fake(make_env: EnvFactory) -> None:
    wallet = FakeWallet()
    wallet.resources["https://api.example.com/data"] = FakeX402Resource(
        body="secret data", price_usd=Decimal("0.05"))
    wallet.resources["https://free.example.com/"] = FakeX402Resource(body="free")
    env = await make_env(wallet=wallet, settings={"allowlist": ["api.example.com",
                                                                "free.example.com"]})
    resp = await env.client.post("/x402/fetch", headers=env.agent, json={
        "url": "https://api.example.com/data", "method": "GET", "max_usd": "0.10",
        "purpose": "data"})
    body = await resp.json()
    assert resp.status == 200, body
    assert body["status"] == "paid" and body["http_status"] == 200
    assert body["body_text"] == "secret data" and body["paid_usd"] == "0.05"
    ledger = await (await env.client.get("/ledger?window=all", headers=env.agent)).json()
    assert ledger["costs"] == "0.05"

    resp = await env.client.post("/x402/fetch", headers=env.agent, json={
        "url": "https://free.example.com/", "max_usd": "0.10", "purpose": "free"})
    body = await resp.json()
    assert body["paid_usd"] == "0.00" and body["body_text"] == "free"

    # new host: needs approval, then the guard fetches on approve and keeps the result
    wallet.resources["https://new.example.com/x"] = FakeX402Resource(body="hi",
                                                                     price_usd=Decimal("0.01"))
    resp = await env.client.post("/x402/fetch", headers=env.agent, json={
        "url": "https://new.example.com/x", "max_usd": "0.02", "purpose": "try"})
    body = await resp.json()
    assert body["status"] == "pending" and body["http_status"] is None
    owner = await env.owner()
    await env.client.post(f"/approvals/{body['approval_id']}/approve", headers=owner)
    polled = await (await env.client.get(f"/spend/{body['payment_id']}",
                                         headers=env.agent)).json()
    assert polled["status"] == "paid" and polled["body_text"] == "hi"


async def test_x402_ssrf_blocked_on_real_networks(make_env: EnvFactory) -> None:
    env = await make_env(wallet=FakeWallet(network="base-sepolia"),
                         settings={"network": "base-sepolia"})
    for url in ("http://127.0.0.1:8080/x", "http://169.254.169.254/latest", "file:///etc/passwd"):
        resp = await env.client.post("/x402/fetch", headers=env.agent,
                                     json={"url": url, "max_usd": "0.1", "purpose": "x"})
        assert resp.status == 422, url


async def test_public_pnl(make_env: EnvFactory) -> None:
    env = await make_env()
    assert (await env.client.get("/public/pnl")).status == 404
    assert (await env.client.get("/public/pnl.json")).status == 404

    env2 = await make_env(settings={"public_pnl": True, "network": "base-sepolia"},
                          wallet=FakeWallet(network="base-sepolia"))
    await env2.spend(amount="1")
    # one verified income row (chain) and one unverified one (agent-reported)
    await env2.guard.store.add_ledger(ts=env2.clock(), direction="income",
                                      category="sale_stripe", amount=Decimal("2"),
                                      source="stripe", ref="cs_1", verified=True)
    await env2.guard.store.add_ledger(ts=env2.clock(), direction="income",
                                      category="bounty", amount=Decimal("0.5"),
                                      source="agent_reported", ref="b1")
    data = await (await env2.client.get("/public/pnl.json")).json()
    assert data["windows"]["all"]["costs"] == "1.00"
    assert data["windows"]["all"]["income"] == "2.50"
    assert data["windows"]["all"]["verified_income"] == "2.00"
    assert data["basescan_url"].startswith("https://sepolia.basescan.org/address/")
    assert "entries" not in data["windows"]["all"]
    # the marketing site fetches the JSON cross-origin from the browser
    assert (await env2.client.get("/public/pnl.json")).headers[
        "Access-Control-Allow-Origin"] == "*"
    resp = await env2.client.get("/public/pnl")
    assert resp.headers["Access-Control-Allow-Origin"] == "*"
    html = await resp.text()
    assert resp.status == 200 and resp.content_type == "text/html"
    assert "prefers-color-scheme: dark" in html and "sepolia.basescan.org" in html
    assert "payment_out" in html and "test" not in html.split("<main>")[1].split("By window")[0]
    assert "verified" in html and "Verified" in html


async def test_lifecycle_runs_watchers_and_closes(tmp_path: Path) -> None:
    import asyncio

    from aiohttp.test_utils import TestClient, TestServer

    wallet = FakeWallet()
    wallet.inject_incoming(BOB, Decimal("2"))
    app = create_app(make_config(), db_path=tmp_path / "g.sqlite3", wallet=wallet,
                     notifier=RecordingNotifier(), watch_interval_s=0.01)
    client = TestClient(TestServer(app))
    await client.start_server()
    guard = app[GUARD_KEY]
    for _ in range(100):
        if (await ledger.pnl(guard.store, "all", guard.now()))["income"] != "0.00":
            break
        await asyncio.sleep(0.01)
    assert (await ledger.pnl(guard.store, "all", guard.now()))["income"] == "2.00"
    await client.close()
    assert wallet.closed


def test_build_wallet_by_network() -> None:
    import pytest

    assert isinstance(build_wallet(make_config()), FakeWallet)
    with pytest.raises(ValueError, match="CDP credentials"):
        build_wallet(make_config(network="base-sepolia"))
