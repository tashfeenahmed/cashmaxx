"""Guard HTTP service: the only process that holds the wallet and enforces the owner's rules.

``create_app`` builds the aiohttp application; ``run`` loads ``guard.json`` and serves it.
The API contract is the table in docs/cashmaxx/ARCHITECTURE.md; ``cashmaxx.guard.client`` is the
matching client.
"""

from __future__ import annotations

import asyncio
import functools
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, TypeVar
from urllib.parse import urlparse

from aiohttp import web
from loguru import logger
from pydantic import ValidationError
from pydantic.alias_generators import to_camel

from cashmaxx import __version__
from cashmaxx.config import (
    CashmaxxSettings,
    GuardConfig,
    guard_config_path,
    guard_db_path,
    load_guard_config,
    save_guard_config,
)
from cashmaxx.guard import ledger, public
from cashmaxx.guard.auth import OwnerSessions, RateLimiter, check_agent_token, verify_pin
from cashmaxx.guard.compute import ComputeManager, OpenRouterClient
from cashmaxx.guard.errors import ApiError
from cashmaxx.guard.integrations import registry
from cashmaxx.guard.integrations.service import IntegrationDeps, IntegrationsService
from cashmaxx.guard.policy import PolicyState, SpendRequest, evaluate
from cashmaxx.guard.store import (
    Actor,
    Approval,
    DuplicateIdempotencyKeyError,
    Payment,
    Store,
    new_id,
    parse_iso,
)
from cashmaxx.guard.stripe_client import StripeClient
from cashmaxx.guard.telegram_bot import Notifier, TelegramBot
from cashmaxx.guard.wallet import WalletBackend
from cashmaxx.guard.watchers import Watchers
from cashmaxx.money import ZERO, fmt_usd, parse_usd

T = TypeVar("T")
Clock = Callable[[], datetime]
Scope = Literal["agent", "owner"]
Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

OWNER_HEADER = "X-Cashmaxx-Owner"
AGENT_SPEND_CATEGORIES = frozenset({"payment_out", "fees", "other", "compute"})
AGENT_COST_CATEGORIES = frozenset({"compute", "fees", "other"})
X402_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"})
PATCH_FORBIDDEN = frozenset({"frozen", "frozenReason"})
MAX_TEXT = 2000
MAX_X402_BODY = 100_000


def _utcnow() -> datetime:
    return datetime.now(UTC)


# --- the guard service ---------------------------------------------------------------------
class Guard:
    """Business logic shared by the HTTP API, the Telegram bot and the watchers."""

    def __init__(
        self,
        config: GuardConfig,
        *,
        config_path: Path | None,
        store: Store,
        wallet: WalletBackend,
        notifier: Notifier | None,
        clock: Clock,
        stripe: StripeClient | None,
        openrouter: OpenRouterClient | None,
        integration_deps: IntegrationDeps | None = None,
    ) -> None:
        self.config = config
        self.config_path = config_path
        self.store = store
        self.wallet = wallet
        self.notifier = notifier
        self.clock = clock
        self.stripe = stripe
        self.openrouter = openrouter
        self.network = config.settings.network  # fixed for this process (wallet is bound to it)
        self.sessions = OwnerSessions(clock=clock)
        self.pin_limiter = RateLimiter(per_key=5, total=5, clock=clock)
        self.compute = ComputeManager(self, openrouter)
        self._spend_lock = asyncio.Lock()
        self._config_lock = asyncio.Lock()
        self.bot: TelegramBot | None = None  # set when the guard runs its own bot (not in tests)
        self.running = False
        self.integrations = IntegrationsService(self, integration_deps)

    # --- plumbing ---------------------------------------------------------------------------
    @property
    def settings(self) -> CashmaxxSettings:
        return self.config.settings

    def now(self) -> datetime:
        return self.clock()

    async def notify(self, text: str, *, approval_id: str | None = None) -> None:
        if self.notifier is None:
            return
        try:
            await self.notifier.send(text, approval_id=approval_id)
        except Exception as exc:
            logger.warning("notify failed: {}", exc)

    async def event(self, actor: Actor, type: str, data: dict[str, Any] | None = None) -> None:
        await self.store.add_event(ts=self.now(), actor=actor, type=type, data=data)

    async def _set_settings(self, settings: CashmaxxSettings) -> None:
        async with self._config_lock:
            self.config.settings = settings
            if self.config_path is not None:
                await asyncio.to_thread(save_guard_config, self.config, self.config_path)

    async def mutate_config(self, fn: Callable[[GuardConfig], T]) -> T:
        """Change the config under the lock and persist it (``guard.json``, 0600)."""
        async with self._config_lock:
            result = fn(self.config)
            if self.config_path is not None:
                await asyncio.to_thread(save_guard_config, self.config, self.config_path)
            return result

    async def reload_integration(self, integration_id: str) -> bool:
        """Rebuild the client an integration change affects. Returns ``restart_required``.

        Email and social clients are built per call from the config, so they need nothing here.
        The CDP wallet is bound at startup: a change there needs a guard restart.
        """
        c = self.config
        if integration_id == "telegram" and self.bot is not None:
            old = self.bot
            new = TelegramBot(c.telegram.bot_token, c.telegram.owner_chat_id, actions=self)
            self.bot = new
            self.notifier = new
            await old.stop()
            if self.running:
                new.start()
        elif integration_id == "stripe":
            try:
                deps = self.integrations.deps
                self.stripe = deps.stripe_factory(c.stripe_restricted_key) \
                    if c.stripe_restricted_key else None
            except Exception as exc:
                logger.warning("guard: Stripe client reload failed: {}", type(exc).__name__)
                self.stripe = None
        elif integration_id == "openrouter":
            old_or = self.openrouter
            self.openrouter = OpenRouterClient(c.openrouter_api_key) \
                if c.openrouter_api_key else None
            self.compute.openrouter = self.openrouter
            if old_or is not None:
                try:
                    await old_or.aclose()
                except Exception as exc:
                    logger.warning("guard: OpenRouter close failed: {}", exc)
        elif integration_id == "hosting":
            if not registry.is_connected(c, registry.get_guard_spec("hosting")):
                await self.integrations.stop_all_tunnels("hosting disconnected")
        elif integration_id == "cdp":
            return self.network != "fake"
        return False

    # --- settings / freeze ------------------------------------------------------------------
    async def update_settings(self, patch: dict[str, Any]) -> tuple[CashmaxxSettings, bool]:
        """Validate and persist a partial update. Returns ``(settings, restart_required)``."""
        known = {to_camel(name) for name in CashmaxxSettings.model_fields}
        normalized: dict[str, Any] = {}
        for key, value in patch.items():
            camel = to_camel(key) if "_" in key else key
            if camel not in known:
                raise ApiError(422, "invalid", f"unknown setting: {key}")
            if camel in PATCH_FORBIDDEN:
                raise ApiError(422, "invalid",
                               "use /freeze and /unfreeze to change the kill switch")
            normalized[camel] = value
        merged = {**self.settings.model_dump(mode="json", by_alias=True), **normalized}
        try:
            new = CashmaxxSettings.model_validate(merged)
        except ValidationError as exc:
            raise ApiError(422, "invalid", _validation_message(exc)) from exc
        if "emailProvider" in normalized and new.email_provider != "none":
            spec = registry.get_guard_spec(new.email_provider)
            if not registry.is_connected(self.config, spec):
                raise ApiError(400, "email_not_connected",
                               f"connect {spec.label} before choosing it as the email provider")
        restart = new.network != self.network
        await self._set_settings(new)
        if not new.hosting_enabled:
            await self.integrations.stop_all_tunnels("hosting disabled")
        await self.event("owner", "settings_updated", {"fields": sorted(normalized),
                                                       "restart_required": restart})
        return new, restart

    async def freeze(self, reason: str, *, actor: Actor) -> bool:
        if self.settings.frozen:
            return False
        await self._set_settings(
            self.settings.model_copy(update={"frozen": True, "frozen_reason": reason})
        )
        await self.event(actor, "freeze", {"reason": reason})
        await self.notify(f"Cashmaxx guard FROZEN by {actor}: {reason}. No payments will be made.")
        return True

    async def unfreeze(self) -> bool:
        if not self.settings.frozen:
            return False
        await self._set_settings(
            self.settings.model_copy(update={"frozen": False, "frozen_reason": None})
        )
        await self.event("owner", "unfreeze", {})
        await self.notify("Cashmaxx guard unfrozen by the owner.")
        return True

    # --- spending ---------------------------------------------------------------------------
    async def policy_state(
        self, request: SpendRequest, *, exclude_payment_id: str | None = None
    ) -> PolicyState:
        now = self.now()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        spent_today = await self.store.sum_ledger(direction="cost", source="guard",
                                                  since=day_start)
        last_hour = await self.store.count_payments_since(
            now - timedelta(hours=1), exclude_id=exclude_payment_id
        )
        known = await self.store.known_recipients()
        if self.settings.owner_wallet:
            known.add(self.settings.owner_wallet.lower())
        available = await ledger.available_budget(self.store, self.settings)
        if request.category == "reimbursement":
            # The compute being reimbursed is already subtracted from the budget.
            available += request.amount_usd
        return PolicyState(
            spent_today=spent_today, payments_last_hour=last_hour, available_budget=available,
            frozen=self.settings.frozen, known_recipients=frozenset(known),
        )

    async def request_spend(
        self,
        request: SpendRequest,
        *,
        actor: Actor,
        x402_call: dict[str, Any] | None = None,
    ) -> Payment:
        async with self._spend_lock:
            existing = await self.store.get_payment_by_key(request.idempotency_key)
            if existing is not None:
                return existing
            state = await self.policy_state(request)
            decision = evaluate(request, self.settings, state)
            status: Literal["pending", "denied"] = (
                "denied" if decision.outcome == "deny" else "pending"
            )
            try:
                payment = await self.store.insert_payment(
                    ts=self.now(), idempotency_key=request.idempotency_key, kind=request.kind,
                    to_addr=request.to, amount=request.amount_usd, purpose=request.purpose,
                    category=request.category, status=status,
                    reason=None if decision.allowed else decision.reason,
                )
            except DuplicateIdempotencyKeyError:
                found = await self.store.get_payment_by_key(request.idempotency_key)
                assert found is not None  # the unique constraint just proved it exists
                return found
            if x402_call is not None:
                await self.store.kv_set(f"x402_req:{payment.id}", json.dumps(x402_call))
            await self.event(actor, "spend_decision", {
                "payment_id": payment.id, "kind": request.kind, "to": request.to,
                "amount_usd": fmt_usd(request.amount_usd), "category": request.category,
                "purpose": request.purpose, "outcome": decision.outcome,
                "reason": decision.reason,
            })
            if decision.outcome == "deny":
                return payment
            if decision.outcome == "needs_approval":
                approval = await self.store.create_approval(
                    ts=self.now(), payment_id=payment.id, reason=decision.reason
                )
                refreshed = await self.store.get_payment(payment.id)
                assert refreshed is not None
                await self.notify(self.approval_card(refreshed, approval), approval_id=approval.id)
                return refreshed
            return await self._execute(payment)

    async def _execute(self, payment: Payment) -> Payment:
        """Move money for an allowed payment. Caller holds ``_spend_lock``."""
        if payment.kind == "x402":
            return await self._execute_x402(payment)
        try:
            tx_hash = await self.wallet.transfer_usdc(payment.to_addr, payment.amount_usd)
        except Exception as exc:
            logger.warning("transfer {} failed: {}", payment.id, exc)
            failed = await self.store.update_payment(payment.id, status="failed",
                                                     reason=str(exc)[:300])
            await self.event("guard", "payment_failed", {"payment_id": payment.id,
                                                         "error": str(exc)[:300]})
            await self.notify(f"Payment {payment.id} FAILED: {str(exc)[:200]}")
            return failed
        paid = await self.store.update_payment(payment.id, status="paid", tx_hash=tx_hash,
                                               reason=None)
        await self.store.add_ledger(
            ts=self.now(), direction="cost", category=payment.category, amount=payment.amount_usd,
            source="guard", ref=payment.id, note=payment.purpose[:200], verified=True,
        )
        await self.event("guard", "payment_paid", {"payment_id": payment.id, "tx_hash": tx_hash})
        await self.notify(
            f"Paid ${fmt_usd(payment.amount_usd)} to {payment.to_addr} ({payment.purpose[:120]})."
            f"\ntx {tx_hash}"
        )
        return paid

    async def _execute_x402(self, payment: Payment) -> Payment:
        raw = await self.store.kv_get(f"x402_req:{payment.id}")
        if raw is None:
            return await self.store.update_payment(payment.id, status="failed",
                                                   reason="x402 request details missing")
        call = json.loads(raw)
        try:
            http_status, body_text, paid = await self.wallet.x402_fetch(
                call["url"], call["method"], call.get("body"), payment.amount_usd
            )
        except Exception as exc:
            logger.warning("x402 {} failed: {}", payment.id, exc)
            await self.event("guard", "payment_failed", {"payment_id": payment.id,
                                                         "error": str(exc)[:300]})
            return await self.store.update_payment(payment.id, status="failed",
                                                   reason=str(exc)[:300])
        result = {"http_status": http_status, "body_text": body_text[:MAX_X402_BODY],
                  "paid_usd": fmt_usd(paid)}
        await self.store.kv_set(f"x402_result:{payment.id}", json.dumps(result))
        if http_status == 402 and paid <= 0:
            await self.event("guard", "payment_failed", {"payment_id": payment.id,
                                                         "error": "x402 payment not made"})
            return await self.store.update_payment(payment.id, status="failed",
                                                   reason="x402_not_paid", amount_usd=ZERO)
        updated = await self.store.update_payment(
            payment.id, status="paid", amount_usd=paid,
            reason=None if paid > 0 else "no_payment_required",
        )
        if paid > 0:
            await self.store.add_ledger(
                ts=self.now(), direction="cost", category=payment.category, amount=paid,
                source="guard", ref=payment.id, note=f"x402 {call['url'][:150]}", verified=True,
            )
        await self.event("guard", "payment_paid", {"payment_id": payment.id,
                                                   "paid_usd": fmt_usd(paid),
                                                   "http_status": http_status})
        return updated

    async def decide(self, approval_id: str, *, approve: bool, via: str) -> Payment:
        async with self._spend_lock:
            approval = await self.store.get_approval(approval_id)
            if approval is None:
                raise ApiError(404, "not_found", f"no approval {approval_id}")
            if approval.status != "pending":
                raise ApiError(409, "not_pending", f"approval is {approval.status}")
            payment = await self.store.get_payment(approval.payment_id)
            if payment is None:
                raise ApiError(404, "not_found", f"no payment {approval.payment_id}")
            decided = await self.store.decide_approval(
                approval_id, status="approved" if approve else "denied", ts=self.now(), via=via
            )
            if decided is None:
                raise ApiError(409, "not_pending", "approval was decided concurrently")
            if not approve:
                await self.event("owner", "approval_denied", {"approval_id": approval_id,
                                                              "via": via})
                return await self.store.update_payment(payment.id, status="denied",
                                                       reason="owner_denied")
            request = SpendRequest(
                kind=payment.kind, to=payment.to_addr, amount_usd=payment.amount_usd,
                purpose=payment.purpose, category=payment.category,
                idempotency_key=payment.idempotency_key,
            )
            state = await self.policy_state(request, exclude_payment_id=payment.id)
            decision = evaluate(request, self.settings, state, recheck=True)
            await self.event("owner", "approval_approved", {
                "approval_id": approval_id, "via": via, "recheck": decision.outcome,
                "reason": decision.reason,
            })
            if not decision.allowed:
                return await self.store.update_payment(payment.id, status="denied",
                                                       reason=decision.reason)
            return await self._execute(payment)

    async def expire_approval(self, approval_id: str) -> None:
        async with self._spend_lock:
            approval = await self.store.decide_approval(
                approval_id, status="expired", ts=self.now(), via=None
            )
            if approval is None:
                return
            await self.store.update_payment(approval.payment_id, status="denied",
                                            reason="approval_expired")
            await self.event("guard", "approval_expired", {"approval_id": approval_id})

    # --- read models ------------------------------------------------------------------------
    async def payment_json(self, payment: Payment) -> dict[str, Any]:
        data = payment.to_json()
        if payment.kind == "x402":
            raw = await self.store.kv_get(f"x402_result:{payment.id}")
            if raw:
                data.update(json.loads(raw))
        if payment.approval_id:
            approval = await self.store.get_approval(payment.approval_id)
            data["approval_status"] = approval.status if approval else None
        return data

    def approval_card(self, payment: Payment, approval: Approval) -> str:
        why = {"over_threshold": "above your per-payment threshold",
               "new_recipient": "first payment to this recipient"}.get(approval.reason,
                                                                        approval.reason)
        return (
            f"Approval needed ({why})\n"
            f"${fmt_usd(payment.amount_usd)} {payment.kind} to {payment.to_addr}\n"
            f"Purpose: {payment.purpose[:300]}\nCategory: {payment.category}\n"
            f"id: {approval.id}"
        )

    async def balance_or_none(self) -> Decimal | None:
        try:
            return await self.wallet.balance_usdc()
        except Exception as exc:
            logger.warning("balance lookup failed: {}", exc)
            return None

    async def daily_summary_text(self) -> str:
        day = await ledger.pnl(self.store, "7d", self.now(), include_entries=False)
        return "Daily summary\n" + await self.bot_status() + (
            f"\n7d: income ${day['income']}, costs ${day['costs']}, net ${day['net']}"
        )

    # --- Telegram bot actions ---------------------------------------------------------------
    async def bot_status(self) -> str:
        s = self.settings
        balance = await self.balance_or_none()
        available = await ledger.available_budget(self.store, s)
        now = self.now()
        spent = await self.store.sum_ledger(
            direction="cost", source="guard",
            since=now.replace(hour=0, minute=0, second=0, microsecond=0),
        )
        pending = await self.store.list_approvals("pending")
        state = f"FROZEN ({s.frozen_reason})" if s.frozen else "active"
        return (
            f"Cashmaxx guard on {self.network}: {state}\n"
            f"Balance: {'unknown' if balance is None else '$' + fmt_usd(balance)} USDC\n"
            f"Available budget: ${fmt_usd(available)} of ${fmt_usd(s.budget_usd)}\n"
            f"Spent today: ${fmt_usd(spent)} of ${fmt_usd(s.daily_cap_usd)} cap\n"
            f"Pending approvals: {len(pending)}"
        )

    async def bot_pnl(self) -> str:
        lines = ["P&L"]
        for window in ("7d", "30d", "all"):
            p = await ledger.pnl(self.store, window, self.now(), include_entries=False)
            lines.append(f"{window}: income ${p['income']}, costs ${p['costs']}, net ${p['net']}")
        return "\n".join(lines)

    async def bot_pending(self) -> list[tuple[str, str]]:
        cards: list[tuple[str, str]] = []
        for approval in await self.store.list_approvals("pending"):
            payment = await self.store.get_payment(approval.payment_id)
            if payment is not None:
                cards.append((approval.id, self.approval_card(payment, approval)))
        return cards

    async def bot_freeze(self, reason: str) -> str:
        changed = await self.freeze(reason, actor="owner")
        return "Frozen." if changed else "Already frozen."

    async def bot_unfreeze(self) -> str:
        return "Unfrozen." if await self.unfreeze() else "Not frozen."

    async def bot_decide(self, approval_id: str, approve: bool) -> str:
        try:
            payment = await self.decide(approval_id, approve=approve, via="telegram")
        except ApiError as exc:
            return f"{approval_id}: {exc.message}"
        if not approve:
            return f"Denied {approval_id}."
        if payment.status == "paid":
            return f"Approved {approval_id}: paid ${fmt_usd(payment.amount_usd)}."
        return f"Approved {approval_id}, but not paid: {payment.status} ({payment.reason})."


# --- HTTP helpers -------------------------------------------------------------------------------
GUARD_KEY = web.AppKey("cashmaxx_guard", Guard)
WATCHERS_KEY = web.AppKey("cashmaxx_watchers", Watchers)
SCOPE_KEY = web.RequestKey("cashmaxx_scope", str)


def _validation_message(exc: ValidationError) -> str:
    parts: list[str] = []
    for err in exc.errors()[:5]:
        loc = ".".join(str(x) for x in err.get("loc", ()))
        parts.append(f"{loc}: {err.get('msg')}" if loc else str(err.get("msg")))
    return "; ".join(parts)


def _error(status: int, code: str, message: str, **extra: Any) -> web.Response:
    return web.json_response({"error": code, "message": message, **extra}, status=status)


@web.middleware
async def error_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    try:
        return await handler(request)
    except ApiError as exc:
        return _error(exc.status, exc.code, exc.message, **exc.extra)
    except web.HTTPException as exc:
        if exc.status < 400:
            raise
        code = {404: "not_found", 405: "method_not_allowed", 413: "too_large"}.get(
            exc.status, "http_error")
        return _error(exc.status, code, exc.reason)
    except Exception as exc:
        logger.exception("guard: unhandled error on {} {}: {}", request.method, request.path, exc)
        return _error(500, "internal", "internal error")


def request_scope(request: web.Request) -> Scope | None:
    guard = request.app[GUARD_KEY]
    if guard.sessions.check(request.headers.get(OWNER_HEADER)):
        return "owner"
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        if check_agent_token(auth[7:].strip(), guard.config.agent_token_hash):
            return "agent"
    return None


def requires(*scopes: Scope) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        @functools.wraps(fn)
        async def wrapper(request: web.Request) -> web.StreamResponse:
            scope = request_scope(request)
            if scope is None:
                raise ApiError(401, "unauthorized", "missing or invalid credentials")
            if scope not in scopes:
                raise ApiError(403, "forbidden", f"this action needs scope: {', '.join(scopes)}")
            request[SCOPE_KEY] = scope
            return await fn(request)

        return wrapper

    return deco


def _scope(request: web.Request) -> Scope:
    return "owner" if request.get(SCOPE_KEY) == "owner" else "agent"


async def _body(request: web.Request) -> dict[str, Any]:
    if not request.can_read_body:
        return {}
    try:
        data = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ApiError(422, "invalid", "body must be JSON") from exc
    if not isinstance(data, dict):
        raise ApiError(422, "invalid", "body must be a JSON object")
    return data  # pyright: ignore[reportUnknownVariableType]


def _str(data: dict[str, Any], key: str, *, default: str | None = None,
         max_len: int = MAX_TEXT) -> str:
    value = data.get(key, default)
    if value is None:
        raise ApiError(422, "invalid", f"{key} is required")
    if not isinstance(value, str):
        raise ApiError(422, "invalid", f"{key} must be a string")
    if len(value) > max_len:
        raise ApiError(422, "invalid", f"{key} is too long (max {max_len})")
    return value


def _amount(data: dict[str, Any], key: str) -> Decimal:
    value = data.get(key)
    if value is None or not isinstance(value, str | int | float):
        raise ApiError(422, "invalid", f"{key} must be a decimal string")
    try:
        return parse_usd(value)
    except ValueError as exc:
        raise ApiError(422, "invalid", f"{key}: {exc}") from exc


def _payment_response(data: dict[str, Any]) -> web.Response:
    """Frozen denials are 409 (``error: frozen``); every other decision is 200."""
    if data["status"] == "denied" and data.get("reason") == "frozen":
        return web.json_response(
            {"error": "frozen", "message": "the guard is frozen; no payments are made", **data},
            status=409,
        )
    return web.json_response(data)


# --- handlers -------------------------------------------------------------------------------
async def health(request: web.Request) -> web.Response:
    guard = request.app[GUARD_KEY]
    return web.json_response({"ok": True, "network": guard.network,
                              "frozen": guard.settings.frozen, "version": __version__})


@requires("agent", "owner")
async def wallet_info(request: web.Request) -> web.Response:
    guard = request.app[GUARD_KEY]
    balance = await guard.balance_or_none()
    available = await ledger.available_budget(guard.store, guard.settings)
    return web.json_response({
        "address": await guard.wallet.address(),
        "network": guard.network,
        "balance_usdc": None if balance is None else fmt_usd(balance),
        "available_budget_usd": fmt_usd(available),
        "frozen": guard.settings.frozen,
    })


@requires("agent")
async def spend(request: web.Request) -> web.Response:
    guard = request.app[GUARD_KEY]
    data = await _body(request)
    category = _str(data, "category", default="payment_out", max_len=40)
    if category not in AGENT_SPEND_CATEGORIES:
        raise ApiError(422, "invalid",
                       f"category must be one of {sorted(AGENT_SPEND_CATEGORIES)}")
    spend_request = SpendRequest(
        kind=_str(data, "kind", default="transfer", max_len=40),
        to=_str(data, "to", max_len=200).strip(),
        amount_usd=_amount(data, "amount_usd"),
        purpose=_str(data, "purpose", max_len=500),
        category=category,
        idempotency_key=_str(data, "idempotency_key", max_len=200),
    )
    if not spend_request.idempotency_key.strip():
        raise ApiError(422, "invalid", "idempotency_key must not be empty")
    payment = await guard.request_spend(spend_request, actor="agent")
    return _payment_response(await guard.payment_json(payment))


@requires("agent", "owner")
async def get_payment(request: web.Request) -> web.Response:
    guard = request.app[GUARD_KEY]
    payment = await guard.store.get_payment(request.match_info["payment_id"])
    if payment is None:
        raise ApiError(404, "not_found", "no such payment")
    return web.json_response(await guard.payment_json(payment))


async def _check_x402_url(guard: Guard, url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ApiError(422, "invalid", "url must be an http(s) URL with a host")
    if guard.network != "fake":
        from nanobot.security.network import validate_url_target

        ok, err = await asyncio.to_thread(validate_url_target, url)
        if not ok:
            raise ApiError(422, "blocked_url", err)
    return parsed.hostname.lower()


@requires("agent")
async def x402_fetch(request: web.Request) -> web.Response:
    guard = request.app[GUARD_KEY]
    data = await _body(request)
    url = _str(data, "url", max_len=2000)
    method = _str(data, "method", default="GET", max_len=10).upper()
    if method not in X402_METHODS:
        raise ApiError(422, "invalid", f"method must be one of {sorted(X402_METHODS)}")
    body = data.get("body")
    if body is not None and (not isinstance(body, str) or len(body) > MAX_X402_BODY):
        raise ApiError(422, "invalid", "body must be a string (max 100k chars)")
    max_usd = _amount(data, "max_usd")
    purpose = _str(data, "purpose", max_len=500)
    host = await _check_x402_url(guard, url)
    idempotency_key = data.get("idempotency_key")
    if idempotency_key is not None and (
        not isinstance(idempotency_key, str) or not idempotency_key.strip()
        or len(idempotency_key) > 200
    ):
        raise ApiError(422, "invalid", "idempotency_key must be a non-empty string (max 200)")
    spend_request = SpendRequest(
        kind="x402", to=host, amount_usd=max_usd, purpose=purpose, category="payment_out",
        idempotency_key=idempotency_key or new_id("x402"),
    )
    payment = await guard.request_spend(
        spend_request, actor="agent", x402_call={"url": url, "method": method, "body": body}
    )
    out = await guard.payment_json(payment)
    out.setdefault("http_status", None)
    out.setdefault("body_text", "")
    out.setdefault("paid_usd", "0.00")
    return _payment_response(out)


@requires("agent", "owner")
async def get_ledger(request: web.Request) -> web.Response:
    guard = request.app[GUARD_KEY]
    window = request.query.get("window", "30d")
    if window not in ledger.WINDOWS:
        raise ApiError(422, "invalid", "window must be 7d, 30d or all")
    data = await ledger.pnl(guard.store, window, guard.now())
    data["available_budget_usd"] = fmt_usd(await ledger.available_budget(guard.store,
                                                                         guard.settings))
    return web.json_response(data)


@requires("agent")
async def record_cost(request: web.Request) -> web.Response:
    guard = request.app[GUARD_KEY]
    data = await _body(request)
    amount = _amount(data, "amount_usd")
    if amount <= 0:
        raise ApiError(422, "invalid", "amount_usd must be > 0")
    category = _str(data, "category", default="other", max_len=40)
    if category not in AGENT_COST_CATEGORIES:
        raise ApiError(422, "invalid", f"category must be one of {sorted(AGENT_COST_CATEGORIES)}")
    note = _str(data, "note", default="", max_len=500)
    entry_id = await guard.store.add_ledger(
        ts=guard.now(), direction="cost", category=category, amount=amount,
        source="agent_reported", note=note, verified=False,
    )
    await guard.event("agent", "cost_reported", {"id": entry_id, "amount_usd": fmt_usd(amount),
                                                 "category": category, "note": note})
    return web.json_response({"id": entry_id, "amount_usd": fmt_usd(amount),
                              "category": category, "source": "agent_reported"})


@requires("agent", "owner")
async def list_approvals(request: web.Request) -> web.Response:
    guard = request.app[GUARD_KEY]
    status = request.query.get("status") or None
    if status not in (None, "pending", "approved", "denied", "expired"):
        raise ApiError(422, "invalid", "unknown status")
    items: list[dict[str, Any]] = []
    for approval in await guard.store.list_approvals(status):
        item = approval.to_json()
        payment = await guard.store.get_payment(approval.payment_id)
        item["payment"] = payment.to_json() if payment else None
        items.append(item)
    return web.json_response({"approvals": items})


@requires("owner")
async def approve(request: web.Request) -> web.Response:
    guard = request.app[GUARD_KEY]
    payment = await guard.decide(request.match_info["approval_id"], approve=True, via="webui")
    return _payment_response(await guard.payment_json(payment))


@requires("owner")
async def deny(request: web.Request) -> web.Response:
    guard = request.app[GUARD_KEY]
    payment = await guard.decide(request.match_info["approval_id"], approve=False, via="webui")
    return web.json_response(await guard.payment_json(payment))


@requires("agent")
async def stripe_product(request: web.Request) -> web.Response:
    guard = request.app[GUARD_KEY]
    data = await _body(request)
    name = _str(data, "name", max_len=250)
    description = _str(data, "description", default="", max_len=2000)
    price = _amount(data, "price_usd")
    if guard.settings.frozen:
        raise ApiError(409, "frozen", "the guard is frozen")
    if "digital_products" not in guard.settings.earning_methods:
        raise ApiError(403, "method_disabled", "the owner has not enabled digital_products")
    if guard.stripe is None:
        raise ApiError(409, "stripe_not_configured", "no Stripe restricted key in guard config")
    if price < Decimal("0.50") or price > Decimal("10000"):
        raise ApiError(422, "invalid", "price_usd must be between 0.50 and 10000")
    if not name.strip():
        raise ApiError(422, "invalid", "name must not be empty")
    result = await guard.stripe.create_product(name=name, description=description,
                                               price_usd=price)
    await guard.event("agent", "stripe_product", {**result, "name": name})
    return web.json_response(result)


def _settings_json(guard: Guard, scope: Scope) -> dict[str, Any]:
    out: dict[str, Any] = {"settings": guard.settings.model_dump(mode="json", by_alias=True)}
    base = guard.config.public_base_url
    out["publicPnlUrl"] = (
        f"{base.rstrip('/')}/public/pnl" if guard.settings.public_pnl and base else None
    )
    if scope == "owner":
        c = guard.config
        out["integrations"] = {
            "telegram": bool(c.telegram.bot_token and c.telegram.owner_chat_id),
            "stripe": bool(c.stripe_restricted_key),
            "openrouter": bool(c.openrouter_api_key),
            "cdp": bool(c.cdp.api_key_id and c.cdp.api_key_secret and c.cdp.wallet_secret),
            "public_base_url": c.public_base_url,
        }
        for spec_item in registry.listing(c, "agent"):
            if spec_item["kind"] == "guard":
                out["integrations"].setdefault(spec_item["id"], spec_item["connected"])
    return out


@requires("agent", "owner")
async def get_settings(request: web.Request) -> web.Response:
    guard = request.app[GUARD_KEY]
    return web.json_response(_settings_json(guard, _scope(request)))


@requires("owner")
async def patch_settings(request: web.Request) -> web.Response:
    guard = request.app[GUARD_KEY]
    _, restart = await guard.update_settings(await _body(request))
    out = _settings_json(guard, "owner")
    out["restart_required"] = restart
    return web.json_response(out)


@requires("agent", "owner")
async def freeze(request: web.Request) -> web.Response:
    guard = request.app[GUARD_KEY]
    data = await _body(request)
    scope = _scope(request)
    reason = _str(data, "reason", default=f"frozen by {scope}", max_len=500)
    changed = await guard.freeze(reason, actor=scope)
    return web.json_response({"frozen": True, "changed": changed,
                              "reason": guard.settings.frozen_reason})


@requires("owner")
async def unfreeze(request: web.Request) -> web.Response:
    guard = request.app[GUARD_KEY]
    changed = await guard.unfreeze()
    return web.json_response({"frozen": False, "changed": changed})


async def owner_session(request: web.Request) -> web.Response:
    guard = request.app[GUARD_KEY]
    remote = request.remote or "unknown"
    if not guard.pin_limiter.allow(remote):
        await guard.event("guard", "owner_login_rate_limited", {"remote": remote})
        raise ApiError(429, "rate_limited", "too many PIN attempts; wait a minute")
    data = await _body(request)
    pin = data.get("pin")
    if not isinstance(pin, str) or not pin:
        raise ApiError(422, "invalid", "pin is required")
    if not guard.config.owner_pin_hash:
        raise ApiError(409, "not_configured", "no owner PIN is set; run `cashmaxx onboard`")
    if not await asyncio.to_thread(verify_pin, pin[:128], guard.config.owner_pin_hash):
        await guard.event("guard", "owner_login_failed", {"remote": remote})
        raise ApiError(401, "invalid_pin", "wrong PIN")
    token, expires = guard.sessions.create()
    await guard.event("owner", "owner_login", {"remote": remote})
    return web.json_response({"session": token, "expires_at": expires.isoformat()})


@requires("agent", "owner")
async def events(request: web.Request) -> web.Response:
    guard = request.app[GUARD_KEY]
    since_raw = request.query.get("since")
    try:
        since = parse_iso(since_raw) if since_raw else None
    except ValueError as exc:
        raise ApiError(422, "invalid", "since must be an ISO-8601 time") from exc
    items = await guard.store.events(since)
    return web.json_response({"events": [e.to_json() for e in items]})


# --- integrations -----------------------------------------------------------------------------
@requires("agent", "owner")
async def list_integrations(request: web.Request) -> web.Response:
    guard = request.app[GUARD_KEY]
    return web.json_response(guard.integrations.listing(_scope(request)))


@requires("owner")
async def update_integration(request: web.Request) -> web.Response:
    guard = request.app[GUARD_KEY]
    data = await _body(request)
    item = await guard.integrations.update(request.match_info["integration_id"],
                                           data.get("fields"))
    return web.json_response(item)


@requires("owner")
async def test_integration(request: web.Request) -> web.Response:
    guard = request.app[GUARD_KEY]
    return web.json_response(await guard.integrations.test(request.match_info["integration_id"]))


@requires("owner")
async def remove_integration(request: web.Request) -> web.Response:
    guard = request.app[GUARD_KEY]
    return web.json_response(
        await guard.integrations.remove(request.match_info["integration_id"]))


@requires("owner")
async def owner_check(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


@requires("agent", "owner")
async def email_status(request: web.Request) -> web.Response:
    guard = request.app[GUARD_KEY]
    return web.json_response(await guard.integrations.email_status())


@requires("agent")
async def email_send(request: web.Request) -> web.Response:
    guard = request.app[GUARD_KEY]
    return web.json_response(await guard.integrations.email_send(await _body(request)))


@requires("agent", "owner")
async def email_inbox(request: web.Request) -> web.Response:
    guard = request.app[GUARD_KEY]
    unread = request.query.get("unread", "1").lower() not in ("0", "false", "no")
    try:
        limit = int(request.query.get("limit", "20"))
    except ValueError as exc:
        raise ApiError(422, "invalid", "limit must be an integer") from exc
    if not 1 <= limit <= 50:
        raise ApiError(422, "invalid", "limit must be between 1 and 50")
    return web.json_response(await guard.integrations.email_inbox(unread_only=unread,
                                                                  limit=limit))


@requires("agent", "owner")
async def email_message(request: web.Request) -> web.Response:
    guard = request.app[GUARD_KEY]
    return web.json_response(
        await guard.integrations.email_message(request.match_info["message_id"]))


@requires("agent")
async def social_post(request: web.Request) -> web.Response:
    guard = request.app[GUARD_KEY]
    return web.json_response(await guard.integrations.social_post(await _body(request)))


@requires("agent", "owner")
async def hosting_list(request: web.Request) -> web.Response:
    guard = request.app[GUARD_KEY]
    return web.json_response(guard.integrations.hosting_list())


@requires("agent")
async def hosting_expose(request: web.Request) -> web.Response:
    guard = request.app[GUARD_KEY]
    return web.json_response(await guard.integrations.hosting_expose(await _body(request)))


@requires("agent", "owner")
async def hosting_stop(request: web.Request) -> web.Response:
    guard = request.app[GUARD_KEY]
    return web.json_response(
        await guard.integrations.hosting_stop(request.match_info["name"], _scope(request)))


async def public_pnl_json(request: web.Request) -> web.Response:
    guard = request.app[GUARD_KEY]
    if not guard.settings.public_pnl:
        raise ApiError(404, "not_found", "not found")
    data = await public.public_pnl(guard.store, guard.settings, guard.wallet, guard.now())
    return web.json_response(data, headers=public.CORS_HEADERS)


async def public_pnl_html(request: web.Request) -> web.Response:
    guard = request.app[GUARD_KEY]
    if not guard.settings.public_pnl:
        raise ApiError(404, "not_found", "not found")
    data = await public.public_pnl(guard.store, guard.settings, guard.wallet, guard.now())
    return web.Response(text=public.render_pnl_html(data), content_type="text/html",
                        headers=public.CORS_HEADERS)


# --- factory ----------------------------------------------------------------------------------
def build_wallet(config: GuardConfig) -> WalletBackend:
    network = config.settings.network
    if network == "fake":
        from cashmaxx.guard.wallet.fake import FakeWallet

        logger.warning("guard: network=fake, using the in-memory wallet (no real money)")
        return FakeWallet()
    from cashmaxx.guard.wallet.cdp import CdpWallet

    return CdpWallet(config.cdp, network)


def create_app(
    config: GuardConfig,
    *,
    config_path: Path | None = None,
    db_path: Path | None = None,
    wallet: WalletBackend | None = None,
    notifier: Notifier | None = None,
    clock: Clock | None = None,
    stripe: StripeClient | None = None,
    openrouter: OpenRouterClient | None = None,
    watch_interval_s: float | None = 60.0,
    integration_deps: IntegrationDeps | None = None,
) -> web.Application:
    """Build the guard app.

    ``config_path=None`` keeps settings changes in memory only. ``notifier=None`` builds the
    guard's Telegram bot from the config (a no-op without a token). ``watch_interval_s=None``
    disables the background watchers (tests drive ``app[WATCHERS_KEY].run_once()`` directly).
    """
    clock = clock or _utcnow
    store = Store(db_path or guard_db_path())
    wallet = wallet or build_wallet(config)
    if stripe is None and config.stripe_restricted_key:
        factory = integration_deps.stripe_factory if integration_deps else StripeClient
        stripe = factory(config.stripe_restricted_key)
    if openrouter is None and config.openrouter_api_key:
        openrouter = OpenRouterClient(config.openrouter_api_key)
    bot: TelegramBot | None = None
    if notifier is None:
        bot = TelegramBot(config.telegram.bot_token, config.telegram.owner_chat_id)
        notifier = bot
    guard = Guard(
        config, config_path=config_path, store=store, wallet=wallet, notifier=notifier,
        clock=clock, stripe=stripe, openrouter=openrouter, integration_deps=integration_deps,
    )
    if bot is not None:
        bot.actions = guard
        guard.bot = bot
    watchers = Watchers(guard, interval_s=watch_interval_s or 60.0)

    app = web.Application(middlewares=[error_middleware], client_max_size=512 * 1024)
    app[GUARD_KEY] = guard
    app[WATCHERS_KEY] = watchers
    app.router.add_get("/health", health)
    app.router.add_get("/wallet", wallet_info)
    app.router.add_post("/spend", spend)
    app.router.add_get("/spend/{payment_id}", get_payment)
    app.router.add_post("/x402/fetch", x402_fetch)
    app.router.add_get("/ledger", get_ledger)
    app.router.add_post("/ledger/cost", record_cost)
    app.router.add_get("/approvals", list_approvals)
    app.router.add_post("/approvals/{approval_id}/approve", approve)
    app.router.add_post("/approvals/{approval_id}/deny", deny)
    app.router.add_post("/stripe/product", stripe_product)
    app.router.add_get("/settings", get_settings)
    app.router.add_patch("/settings", patch_settings)
    app.router.add_post("/freeze", freeze)
    app.router.add_post("/unfreeze", unfreeze)
    app.router.add_post("/owner/session", owner_session)
    app.router.add_get("/events", events)
    app.router.add_get("/owner/check", owner_check)
    app.router.add_get("/integrations", list_integrations)
    app.router.add_put("/integrations/{integration_id}", update_integration)
    app.router.add_post("/integrations/{integration_id}/test", test_integration)
    app.router.add_delete("/integrations/{integration_id}", remove_integration)
    app.router.add_get("/email/status", email_status)
    app.router.add_post("/email/send", email_send)
    app.router.add_get("/email/inbox", email_inbox)
    app.router.add_get("/email/messages/{message_id}", email_message)
    app.router.add_post("/social/post", social_post)
    app.router.add_get("/hosting", hosting_list)
    app.router.add_post("/hosting/expose", hosting_expose)
    app.router.add_delete("/hosting/{name}", hosting_stop)
    app.router.add_get("/public/pnl", public_pnl_html)
    app.router.add_get("/public/pnl.json", public_pnl_json)

    async def on_startup(_: web.Application) -> None:
        await guard.event("guard", "startup", {"network": guard.network,
                                               "version": __version__})
        guard.running = True
        if watch_interval_s:
            watchers.start()
        if guard.bot is not None:
            guard.bot.start()

    async def on_cleanup(_: web.Application) -> None:
        guard.running = False
        await watchers.stop()
        try:
            await guard.integrations.stop_all_tunnels("guard shutdown")
        except Exception as exc:
            logger.warning("guard: stopping tunnels failed: {}", exc)
        if guard.bot is not None:
            await guard.bot.stop()
        for closer in (wallet.aclose, guard.openrouter.aclose if guard.openrouter else None):
            if closer is not None:
                try:
                    await closer()
                except Exception as exc:
                    logger.warning("guard: close failed: {}", exc)
        store.close()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def run(config_path: Path | None = None) -> None:
    """Load ``guard.json`` and serve the guard until interrupted."""
    from nanobot.security.network import is_loopback_host

    path = config_path or guard_config_path()
    config = load_guard_config(path)
    if not config.agent_token_hash:
        raise SystemExit("guard.json has no agentTokenHash; run `cashmaxx onboard` first")
    if not is_loopback_host(config.host):
        logger.warning("guard: listening on {} (not loopback); put it behind TLS and a firewall",
                       config.host)
    app = create_app(config, config_path=path, db_path=path.parent / guard_db_path().name)
    logger.info("cashmaxx guard {} on http://{}:{} ({})", __version__, config.host, config.port,
                config.settings.network)
    web.run_app(app, host=config.host, port=config.port, print=None)
