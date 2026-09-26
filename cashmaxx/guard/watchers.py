"""Periodic guard tasks, run every ``interval_s`` seconds while the app is up.

Each step is isolated: one failing (RPC down, Stripe error) never stops the others.
Steps: incoming USDC -> ``transfer_in``; Stripe sessions -> ``sale_stripe``; compute polling;
expiring approvals (24h); loss stop; daily summary.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any, Protocol

from loguru import logger

from cashmaxx.config import CashmaxxSettings
from cashmaxx.guard import ledger
from cashmaxx.guard.compute import ComputeManager
from cashmaxx.guard.store import Actor, Store
from cashmaxx.guard.stripe_client import StripeClient
from cashmaxx.guard.wallet import WalletBackend
from cashmaxx.money import fmt_usd

APPROVAL_TTL = timedelta(hours=24)
DAILY_SUMMARY_HOUR_UTC = 8

KV_CHAIN = "chain_cursor"
KV_STRIPE = "stripe_cursor"
KV_SUMMARY = "daily_summary_date"


class WatchHost(Protocol):
    store: Store
    wallet: WalletBackend
    stripe: StripeClient | None
    compute: ComputeManager

    @property
    def settings(self) -> CashmaxxSettings: ...

    def now(self) -> datetime: ...

    async def notify(self, text: str, *, approval_id: str | None = None) -> None: ...

    async def event(self, actor: Actor, type: str, data: dict[str, Any] | None = None) -> None: ...

    async def freeze(self, reason: str, *, actor: Actor) -> bool: ...

    async def expire_approval(self, approval_id: str) -> None: ...

    async def daily_summary_text(self) -> str: ...


class Watchers:
    def __init__(self, host: WatchHost, *, interval_s: float = 60.0) -> None:
        self.host = host
        self.interval_s = interval_s
        self._task: asyncio.Task[None] | None = None

    # --- steps --------------------------------------------------------------------------------
    async def incoming_usdc(self) -> int:
        store = self.host.store
        cursor = await store.kv_get(KV_CHAIN)
        items, new_cursor = await self.host.wallet.incoming_transfers(cursor)
        owner = (self.host.settings.owner_wallet or "").lower()
        recorded = 0
        for item in items:
            if owner and item.from_addr.lower() == owner:
                # Owner funding is not earnings: log it, keep it out of income.
                await self.host.event("guard", "owner_funding", {
                    "amount_usd": fmt_usd(item.amount_usd), "tx_hash": item.tx_hash})
                continue
            row = await store.add_ledger(
                ts=self.host.now(), direction="income", category="transfer_in",
                amount=item.amount_usd, source="chain", ref=item.ref,
                note=f"USDC from {item.from_addr}", verified=True,
            )
            recorded += row is not None
        if new_cursor is not None:
            await store.kv_set(KV_CHAIN, new_cursor)
        return recorded

    async def stripe_sales(self) -> int:
        stripe = self.host.stripe
        if stripe is None:
            return 0
        store = self.host.store
        raw = await store.kv_get(KV_STRIPE)
        sales, cursor = await stripe.completed_sessions_since(int(raw) if raw else None)
        recorded = 0
        for sale in sales:
            if sale.amount_usd <= 0:
                continue
            row = await store.add_ledger(
                ts=self.host.now(), direction="income", category="sale_stripe",
                amount=sale.amount_usd, source="stripe", ref=sale.session_id, note=sale.note,
                verified=True,
            )
            if row is not None:
                recorded += 1
                await self.host.notify(f"Stripe sale: ${fmt_usd(sale.amount_usd)} ({sale.note})")
        if cursor is not None:
            await store.kv_set(KV_STRIPE, str(cursor))
        return recorded

    async def expire_approvals(self) -> int:
        stale = await self.host.store.pending_approvals_before(self.host.now() - APPROVAL_TTL)
        for approval in stale:
            await self.host.expire_approval(approval.id)
        return len(stale)

    async def loss_stop(self) -> bool:
        settings = self.host.settings
        if "loss_stop" not in settings.rules or settings.frozen:
            return False
        net = await ledger.net_since(self.host.store, self.host.now() - timedelta(days=7))
        if net >= -settings.loss_stop_usd:
            return False
        reason = f"loss stop: 7-day net ${fmt_usd(net)} < -${fmt_usd(settings.loss_stop_usd)}"
        await self.host.freeze(reason, actor="guard")
        await self.host.event("guard", "loss_stop", {"net_7d": fmt_usd(net)})
        return True

    async def daily_summary(self) -> bool:
        now = self.host.now()
        if now.hour < DAILY_SUMMARY_HOUR_UTC:
            return False
        today = now.date().isoformat()
        if await self.host.store.kv_get(KV_SUMMARY) == today:
            return False
        await self.host.store.kv_set(KV_SUMMARY, today)
        await self.host.notify(await self.host.daily_summary_text())
        return True

    # --- loop ---------------------------------------------------------------------------------
    async def run_once(self) -> dict[str, Any]:
        steps: list[tuple[str, Callable[[], Awaitable[Any]]]] = [
            ("incoming_usdc", self.incoming_usdc),
            ("stripe_sales", self.stripe_sales),
            ("compute", self.host.compute.run_once),
            ("expired_approvals", self.expire_approvals),
            ("loss_stop", self.loss_stop),
            ("daily_summary", self.daily_summary),
        ]
        results: dict[str, Any] = {}
        errors: dict[str, str] = {}
        for name, step in steps:
            try:
                results[name] = await step()
            except Exception as exc:
                logger.warning("watcher {} failed: {}", name, exc)
                errors[name] = str(exc)[:300]
        compute: dict[str, Any] = results.get("compute") or {}
        did_something = bool(
            any(results.get(k) for k in ("incoming_usdc", "stripe_sales", "expired_approvals",
                                         "loss_stop", "daily_summary"))
            or compute.get("recorded_usd") not in (None, "0.00")
            or compute.get("reimbursement")
            or compute.get("topup_notice")
        )
        if errors or did_something:
            await self.host.event("guard", "watcher_run", {"results": results, "errors": errors})
        return {"results": results, "errors": errors}

    async def _loop(self) -> None:
        while True:
            await self.run_once()
            await asyncio.sleep(self.interval_s)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="cashmaxx-guard-watchers")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
