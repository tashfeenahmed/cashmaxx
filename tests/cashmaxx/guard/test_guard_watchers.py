from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from guard_testlib import ALICE, OWNER_WALLET, EnvFactory

from cashmaxx.config import load_guard_config
from cashmaxx.guard import ledger
from cashmaxx.guard.stripe_client import StripeClient
from cashmaxx.guard.wallet.fake import FakeWallet


@dataclass
class StubStripe:
    """Mimics the parts of ``stripe.StripeClient`` the guard uses."""

    sessions: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    calls: list[tuple[str, dict[str, Any]]] = field(
        default_factory=list[tuple[str, dict[str, Any]]]
    )

    def __post_init__(self) -> None:
        def creator(kind: str, obj: dict[str, Any]) -> SimpleNamespace:
            def create(params: dict[str, Any]) -> dict[str, Any]:
                self.calls.append((kind, params))
                return obj
            return SimpleNamespace(create=create)

        def list_sessions(params: dict[str, Any]) -> SimpleNamespace:
            self.calls.append(("sessions.list", params))
            since = params.get("created", {}).get("gte", 0)
            items = [s for s in self.sessions if s["created"] >= since]
            return SimpleNamespace(auto_paging_iter=lambda: iter(items))

        self.v1 = SimpleNamespace(
            products=creator("products", {"id": "prod_1"}),
            prices=creator("prices", {"id": "price_1"}),
            payment_links=creator("payment_links", {"id": "plink_1",
                                                    "url": "https://buy.stripe.com/x"}),
            checkout=SimpleNamespace(sessions=SimpleNamespace(list=list_sessions)),
        )

    def client(self) -> StripeClient:
        return StripeClient("rk_test_x", client_factory=lambda _key: self)


def session(sid: str, created: int, cents: int, *, paid: bool = True,
            currency: str = "usd") -> dict[str, Any]:
    return {"id": sid, "created": created, "amount_total": cents, "currency": currency,
            "payment_status": "paid" if paid else "unpaid", "payment_link": "plink_1"}


async def test_stripe_product_endpoint(make_env: EnvFactory) -> None:
    stub = StubStripe()
    env = await make_env(stripe=stub.client())
    resp = await env.client.post("/stripe/product", headers=env.agent, json={
        "name": "Guide", "description": "A PDF", "price_usd": "9.99"})
    body = await resp.json()
    assert resp.status == 200, body
    assert body == {"product_id": "prod_1", "price_id": "price_1",
                    "payment_link_url": "https://buy.stripe.com/x", "price_usd": "9.99"}
    assert ("prices", {"product": "prod_1", "unit_amount": 999, "currency": "usd"}) in stub.calls
    resp = await env.client.post("/stripe/product", headers=env.agent, json={
        "name": "Cheap", "price_usd": "0.10"})
    assert resp.status == 422


async def test_stripe_product_requires_config_and_method(make_env: EnvFactory) -> None:
    env = await make_env()
    resp = await env.client.post("/stripe/product", headers=env.agent,
                                 json={"name": "x", "price_usd": "5"})
    assert resp.status == 409 and (await resp.json())["error"] == "stripe_not_configured"
    env2 = await make_env(stripe=StubStripe().client(), settings={"earning_methods": ["bounties"]})
    resp = await env2.client.post("/stripe/product", headers=env2.agent,
                                  json={"name": "x", "price_usd": "5"})
    assert resp.status == 403


async def test_stripe_revenue_recorded_once(make_env: EnvFactory) -> None:
    stub = StubStripe(sessions=[session("cs_1", 1000, 1500), session("cs_2", 1010, 700, paid=False),
                                session("cs_3", 1020, 900, currency="eur")])
    env = await make_env(stripe=stub.client())
    assert await env.watchers.stripe_sales() == 1
    stub.sessions.append(session("cs_4", 1020, 250))
    assert await env.watchers.stripe_sales() == 1  # cs_1 is outside the cursor; cs_4 is new
    assert await env.watchers.stripe_sales() == 0  # overlap at the cursor second is deduped
    assert stub.calls[-1][1]["created"] == {"gte": 1020}
    pnl = await ledger.pnl(env.guard.store, "all", env.clock())
    assert pnl["income"] == "17.50" and pnl["by_category"] == {"sale_stripe": "17.50"}
    assert all(e["verified"] for e in pnl["entries"])
    assert any("Stripe sale: $15.00" in t for t in env.notifier.texts())


async def test_incoming_usdc_is_income_except_owner_funding(make_env: EnvFactory) -> None:
    wallet = FakeWallet()
    env = await make_env(wallet=wallet, settings={"owner_wallet": OWNER_WALLET})
    wallet.inject_incoming(ALICE, Decimal("3"))
    wallet.inject_incoming(OWNER_WALLET, Decimal("50"))
    assert await env.watchers.incoming_usdc() == 1
    assert await env.watchers.incoming_usdc() == 0
    pnl = await ledger.pnl(env.guard.store, "all", env.clock())
    assert pnl["by_category"] == {"transfer_in": "3.00"}
    events = [e.type for e in await env.guard.store.events()]
    assert "owner_funding" in events


async def test_pending_approvals_expire_after_24h(make_env: EnvFactory) -> None:
    env = await make_env()
    _, body = await env.spend(amount="7", key="big")
    env.clock.advance(hours=23)
    assert await env.watchers.expire_approvals() == 0
    env.clock.advance(hours=2)
    assert await env.watchers.expire_approvals() == 1
    payment = await env.guard.store.get_payment(body["payment_id"])
    assert payment is not None and payment.status == "denied"
    assert payment.reason == "approval_expired"
    approval = await env.guard.store.get_approval(body["approval_id"])
    assert approval is not None and approval.status == "expired"


async def test_loss_stop_freezes(make_env: EnvFactory) -> None:
    env = await make_env(settings={"loss_stop_usd": "5", "per_tx_approval_usd": "10",
                                   "daily_cap_usd": "20"})
    await env.spend(amount="4", key="a")
    assert await env.watchers.loss_stop() is False
    await env.spend(amount="1.5", key="b")
    result = await env.watchers.run_once()
    assert result["results"]["loss_stop"] is True and not result["errors"]
    assert env.guard.settings.frozen is True
    assert "loss stop" in (env.guard.settings.frozen_reason or "")
    assert load_guard_config(env.config_path).settings.frozen is True
    assert any("FROZEN" in t for t in env.notifier.texts())
    types = [e.type for e in await env.guard.store.events()]
    assert "loss_stop" in types and "watcher_run" in types


async def test_loss_stop_respects_rule_toggle(make_env: EnvFactory) -> None:
    env = await make_env(settings={"loss_stop_usd": "1", "rules": ["no_trading"]})
    await env.spend(amount="3", key="a")
    assert await env.watchers.loss_stop() is False
    assert env.guard.settings.frozen is False


async def test_watcher_errors_are_isolated_and_summary_daily(make_env: EnvFactory) -> None:
    class BrokenWallet(FakeWallet):
        async def incoming_transfers(self, since_cursor: str | None) -> Any:
            raise RuntimeError("rpc down")

    env = await make_env(wallet=BrokenWallet())
    result = await env.watchers.run_once()
    assert "incoming_usdc" in result["errors"]
    assert result["results"]["daily_summary"] is True  # 12:00 UTC
    assert any(t.startswith("Daily summary") for t in env.notifier.texts())
    assert (await env.watchers.run_once())["results"]["daily_summary"] is False
