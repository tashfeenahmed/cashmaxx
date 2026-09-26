from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import httpx
from guard_testlib import OWNER_WALLET, EnvFactory

from cashmaxx.guard import ledger
from cashmaxx.guard.compute import OpenRouterClient


@dataclass
class FakeOpenRouter:
    usage: str = "1.00"
    total_credits: str | None = "20"

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer or-key"
        if request.url.path.endswith("/key"):
            return httpx.Response(200, json={"data": {"usage": float(self.usage)}})
        if request.url.path.endswith("/credits"):
            if self.total_credits is None:
                return httpx.Response(403, json={"error": {"message": "forbidden"}})
            return httpx.Response(200, json={"data": {"total_credits": float(self.total_credits),
                                                      "total_usage": float(self.usage)}})
        return httpx.Response(404)

    def client(self) -> OpenRouterClient:
        return OpenRouterClient("or-key",
                                http=httpx.AsyncClient(transport=httpx.MockTransport(self.handler)))


async def test_virtual_records_usage_deltas(make_env: EnvFactory) -> None:
    fake = FakeOpenRouter()
    env = await make_env(openrouter=fake.client())
    await env.guard.compute.run_once()  # baseline only
    assert (await ledger.pnl(env.guard.store, "all", env.clock()))["costs"] == "0.00"
    fake.usage = "1.75"
    result = await env.guard.compute.run_once()
    assert result["recorded_usd"] == "0.75"
    fake.usage = "1.75"
    assert (await env.guard.compute.run_once())["recorded_usd"] == "0.00"
    pnl = await ledger.pnl(env.guard.store, "all", env.clock())
    assert pnl["by_category"] == {"compute": "0.75"}
    assert await ledger.available_budget(env.guard.store, env.guard.settings) == Decimal("49.25")
    assert env.wallet.transfers == []  # virtual: no money moves
    fake.usage = "0.10"  # counter reset: re-baseline, record nothing
    assert (await env.guard.compute.run_once())["recorded_usd"] == "0.00"


async def test_reimburse_cycle_pays_owner_wallet(make_env: EnvFactory) -> None:
    fake = FakeOpenRouter()
    env = await make_env(openrouter=fake.client(), settings={
        "compute_payment_mode": "reimburse", "owner_wallet": OWNER_WALLET,
        "reimburse_interval_hours": 24})
    await env.guard.compute.run_once()  # usage + reimbursement baselines
    fake.usage = "3.00"
    env.clock.advance(hours=1)
    result = await env.guard.compute.run_once()
    assert result["recorded_usd"] == "2.00" and result["reimbursement"] is None  # not due yet

    env.clock.advance(hours=24)
    result = await env.guard.compute.run_once()
    reimb = result["reimbursement"]
    # owner wallet counts as allowlisted: no new_recipient approval
    assert reimb["status"] == "paid" and reimb["category"] == "reimbursement"
    assert env.wallet.transfers[-1][:2] == (OWNER_WALLET, Decimal("2.00"))

    pnl = await ledger.pnl(env.guard.store, "all", env.clock())
    assert pnl["costs"] == "2.00"  # compute counted once, reimbursement is a settlement
    assert pnl["by_category"] == {"compute": "2.00", "reimbursement": "2.00"}
    assert await ledger.unreimbursed_compute(env.guard.store) == 0

    # next cycle: nothing owed, nothing paid
    env.clock.advance(hours=25)
    assert (await env.guard.compute.run_once())["reimbursement"] is None
    assert len(env.wallet.transfers) == 1


async def test_reimburse_goes_through_policy(make_env: EnvFactory) -> None:
    fake = FakeOpenRouter()
    env = await make_env(openrouter=fake.client(), settings={
        "compute_payment_mode": "reimburse", "owner_wallet": OWNER_WALLET,
        "per_tx_approval_usd": "1"})
    await env.guard.compute.run_once()
    fake.usage = "4.00"
    env.clock.advance(hours=25)
    reimb = (await env.guard.compute.run_once())["reimbursement"]
    assert reimb["status"] == "pending" and reimb["reason"] == "over_threshold"
    # a pending reimbursement is not requested twice
    env.clock.advance(hours=25)
    assert (await env.guard.compute.run_once())["reimbursement"] is None

    await env.guard.freeze("stop", actor="owner")
    await env.guard.unfreeze()
    fake.usage = "4.50"
    await env.guard.freeze("again", actor="agent")
    env.clock.advance(hours=25)
    frozen = (await env.guard.compute.run_once())["reimbursement"]
    assert frozen["status"] == "denied" and frozen["reason"] == "frozen"


async def test_owner_topup_notifies_once_a_day(make_env: EnvFactory) -> None:
    fake = FakeOpenRouter(usage="5", total_credits="10")
    env = await make_env(openrouter=fake.client(), settings={
        "compute_payment_mode": "owner_topup", "owner_topup_threshold_usd": "2"})
    assert (await env.guard.compute.run_once())["topup_notice"] is False
    fake.usage = "8.5"
    result = await env.guard.compute.run_once()
    assert result["topup_notice"] is True and result["recorded_usd"] == "3.50"
    assert "OpenRouter credit is low: $1.50" in env.notifier.texts()[-1]
    assert (await env.guard.compute.run_once())["topup_notice"] is False
    env.clock.advance(hours=25)
    assert (await env.guard.compute.run_once())["topup_notice"] is True


async def test_owner_topup_without_credit_permission_records_usage_only(
    make_env: EnvFactory,
) -> None:
    fake = FakeOpenRouter(usage="1", total_credits=None)
    env = await make_env(openrouter=fake.client(), settings={
        "compute_payment_mode": "owner_topup"})
    await env.guard.compute.run_once()
    fake.usage = "2"
    result = await env.guard.compute.run_once()
    assert result == {"mode": "owner_topup", "recorded_usd": "1.00", "topup_notice": False}


async def test_x402_gateway_polls_nothing(make_env: EnvFactory) -> None:
    fake = FakeOpenRouter()
    env = await make_env(openrouter=fake.client(), settings={
        "compute_payment_mode": "x402_gateway", "x402_gateway_url": "https://gw.example.com"})
    assert await env.guard.compute.run_once() == {"mode": "x402_gateway"}
    assert await env.guard.store.kv_get("openrouter_usage") is None
