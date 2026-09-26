"""Compute payment modes (see "Compute payment modes" in docs/cashmaxx/ARCHITECTURE.md).

- ``virtual``: poll OpenRouter (``GET /api/v1/key`` -> ``data.usage``, cumulative USD) and record
  the delta as a ``compute`` cost. The first poll only sets the baseline.
- ``reimburse``: as ``virtual``; every ``reimburse_interval_hours`` pay the unreimbursed compute to
  ``owner_wallet`` through the normal spend path (category ``reimbursement``).
- ``owner_topup``: as ``virtual``; when credit (``GET /api/v1/credits``) drops below
  ``owner_topup_threshold_usd`` ask the owner to top up via Telegram (at most once a day). If the
  key may not read credits, only usage is recorded and no low-credit notice can be sent.
- ``x402_gateway``: nothing to poll; each inference call is already a recorded x402 payment.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

import httpx
from loguru import logger

from cashmaxx.config import CashmaxxSettings
from cashmaxx.guard.ledger import unreimbursed_compute
from cashmaxx.guard.policy import SpendRequest
from cashmaxx.guard.store import Actor, Payment, Store, iso, parse_iso
from cashmaxx.money import ZERO, fmt_usd, parse_usd

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
TOPUP_URL = "https://openrouter.ai/settings/credits"
MIN_REIMBURSE_USD = Decimal("0.01")

KV_USAGE = "openrouter_usage"
KV_REIMBURSE_TS = "reimburse_last_ts"
KV_TOPUP_TS = "topup_notified_ts"


class OpenRouterClient:
    def __init__(self, api_key: str, *, http: httpx.AsyncClient | None = None) -> None:
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._http = http or httpx.AsyncClient(timeout=20.0)

    async def usage(self) -> Decimal:
        """Cumulative USD spent by this key."""
        resp = await self._http.get(f"{OPENROUTER_BASE}/key", headers=self._headers)
        resp.raise_for_status()
        return parse_usd(resp.json()["data"]["usage"])

    async def credits(self) -> tuple[Decimal, Decimal] | None:
        """``(total_credits, total_usage)``, or ``None`` if the key may not read credits."""
        resp = await self._http.get(f"{OPENROUTER_BASE}/credits", headers=self._headers)
        if resp.status_code in (401, 403):
            return None
        resp.raise_for_status()
        data = resp.json()["data"]
        return parse_usd(data["total_credits"]), parse_usd(data["total_usage"])

    async def aclose(self) -> None:
        await self._http.aclose()


class ComputeHost(Protocol):
    """The slice of the guard the compute manager needs."""

    store: Store

    @property
    def settings(self) -> CashmaxxSettings: ...

    def now(self) -> datetime: ...

    async def notify(self, text: str, *, approval_id: str | None = None) -> None: ...

    async def event(self, actor: Actor, type: str, data: dict[str, Any] | None = None) -> None: ...

    async def request_spend(self, request: SpendRequest, *, actor: Actor) -> Payment: ...


class ComputeManager:
    def __init__(self, host: ComputeHost, openrouter: OpenRouterClient | None) -> None:
        self.host = host
        self.openrouter = openrouter

    async def poll_usage(self) -> Decimal:
        """Record new OpenRouter spend as a ``compute`` cost. Returns the recorded delta."""
        if self.openrouter is None:
            return ZERO
        store = self.host.store
        usage = await self.openrouter.usage()
        last_raw = await store.kv_get(KV_USAGE)
        await store.kv_set(KV_USAGE, str(usage))
        if last_raw is None:
            await self.host.event("guard", "compute_baseline", {"usage_usd": fmt_usd(usage)})
            return ZERO
        last = Decimal(last_raw)
        if usage < last:  # key rotated or counter reset: re-baseline
            await self.host.event("guard", "compute_baseline", {"usage_usd": fmt_usd(usage)})
            return ZERO
        delta = usage - last
        if delta > 0:
            await store.add_ledger(
                ts=self.host.now(), direction="cost", category="compute", amount=delta,
                source="openrouter", ref=f"usage:{usage}", note="OpenRouter spend", verified=True,
            )
        return delta

    async def reimburse_if_due(self) -> Payment | None:
        settings = self.host.settings
        if settings.compute_payment_mode != "reimburse" or not settings.owner_wallet:
            return None
        store = self.host.store
        now = self.host.now()
        last_raw = await store.kv_get(KV_REIMBURSE_TS)
        if last_raw is None:
            await store.kv_set(KV_REIMBURSE_TS, iso(now))
            return None
        if now - parse_iso(last_raw) < timedelta(hours=settings.reimburse_interval_hours):
            return None
        await store.kv_set(KV_REIMBURSE_TS, iso(now))
        owed = parse_usd(await unreimbursed_compute(store))
        if owed < MIN_REIMBURSE_USD:
            return None
        request = SpendRequest(
            kind="transfer", to=settings.owner_wallet, amount_usd=owed,
            purpose="compute reimbursement (OpenRouter)", category="reimbursement",
            idempotency_key=f"reimburse:{iso(now)}",
        )
        payment = await self.host.request_spend(request, actor="guard")
        logger.info("compute reimbursement {} -> {}", fmt_usd(owed), payment.status)
        return payment

    async def check_topup(self) -> bool:
        """Notify the owner when OpenRouter credit is low. Returns True if a notice was sent."""
        settings = self.host.settings
        if settings.compute_payment_mode != "owner_topup" or self.openrouter is None:
            return False
        credits = await self.openrouter.credits()
        if credits is None:
            return False
        total, used = credits
        remaining = total - used
        if remaining >= settings.owner_topup_threshold_usd:
            return False
        store = self.host.store
        now = self.host.now()
        last = await store.kv_get(KV_TOPUP_TS)
        if last is not None and now - parse_iso(last) < timedelta(hours=24):
            return False
        await store.kv_set(KV_TOPUP_TS, iso(now))
        suggested = max(Decimal("10"), settings.owner_topup_threshold_usd * 5)
        await self.host.notify(
            f"OpenRouter credit is low: ${fmt_usd(remaining)} left "
            f"(threshold ${fmt_usd(settings.owner_topup_threshold_usd)}). "
            f"Please top up about ${fmt_usd(suggested)}: {TOPUP_URL}"
        )
        await self.host.event("guard", "low_credit", {"remaining_usd": fmt_usd(remaining)})
        return True

    async def run_once(self) -> dict[str, Any]:
        mode = self.host.settings.compute_payment_mode
        result: dict[str, Any] = {"mode": mode}
        if mode == "x402_gateway":
            return result
        result["recorded_usd"] = fmt_usd(await self.poll_usage())
        if mode == "reimburse":
            payment = await self.reimburse_if_due()
            result["reimbursement"] = payment.to_json() if payment else None
        elif mode == "owner_topup":
            result["topup_notice"] = await self.check_topup()
        return result
