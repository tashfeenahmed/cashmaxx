"""Guard-side integrations: listing/updating/testing/removing them, and the capped outbound work
(email, social posts, public tunnels) the agent asks for.

Credentials never leave the guard: listings expose only ``set`` flags for secrets, every error
message is scrubbed of stored secrets, and events record field names, never values. Caps are counted
from the ``outbound`` table per UTC day; a ``pending`` row reserves its share of the cap before the
network call, so concurrent requests cannot overshoot and a replayed idempotency key never sends
twice.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol, TypeVar
from urllib.parse import urlparse

import httpx
from loguru import logger

from cashmaxx.config import CashmaxxSettings, GuardConfig
from cashmaxx.guard.compute import OpenRouterClient
from cashmaxx.guard.errors import ApiError
from cashmaxx.guard.integrations import registry, social
from cashmaxx.guard.integrations.email_agentmail import AgentMailProvider
from cashmaxx.guard.integrations.email_common import EmailError, EmailProvider
from cashmaxx.guard.integrations.email_gmail import GmailProvider, ImapFactory, SmtpFactory
from cashmaxx.guard.integrations.hosting import (
    HostingManager,
    check_name,
    check_port,
    token_config_hint,
)
from cashmaxx.guard.store import (
    Actor,
    DuplicateIdempotencyKeyError,
    Outbound,
    Store,
    iso,
    parse_iso,
)
from cashmaxx.guard.stripe_client import StripeClient
from cashmaxx.integrations_catalog import get_spec, gmail_warmup_cap

T = TypeVar("T")
Scope = Literal["agent", "owner"]
MAX_RECIPIENTS = 10
MAX_SUBJECT = 300
MAX_EMAIL_TEXT = 100_000
MAX_EMAIL_HTML = 200_000
MAX_SOCIAL_TEXT = 10_000
MAX_KEY = 200
SUBREDDIT_RE = re.compile(r"^[A-Za-z0-9_]{2,21}$")
MESSAGE_ID_RE = re.compile(r"^[^\s/\\]{1,500}$")
TELEGRAM_API = "https://api.telegram.org"


class IntegrationHost(Protocol):
    config: GuardConfig
    store: Store

    @property
    def settings(self) -> CashmaxxSettings: ...

    def now(self) -> datetime: ...

    async def notify(self, text: str, *, approval_id: str | None = None) -> None: ...

    async def event(self, actor: Actor, type: str, data: dict[str, Any] | None = None) -> None: ...

    async def mutate_config(self, fn: Callable[[GuardConfig], T]) -> T: ...

    async def reload_integration(self, integration_id: str) -> bool: ...


async def default_cdp_check(config: GuardConfig) -> str:
    from cashmaxx.guard.wallet.cdp import CdpWallet

    network = config.settings.network
    wallet = CdpWallet(config.cdp, "base-sepolia" if network == "fake" else network)
    try:
        return f"CDP account {await wallet.address()}"
    finally:
        await wallet.aclose()


@dataclass
class IntegrationDeps:
    """Seams for tests: every outside call goes through one of these."""

    http_transport: httpx.AsyncBaseTransport | None = None
    imap_factory: ImapFactory | None = None
    smtp_factory: SmtpFactory | None = None
    stripe_factory: Callable[[str], StripeClient] = StripeClient
    cdp_check: Callable[[GuardConfig], Awaitable[str]] = default_cdp_check
    hosting: HostingManager = field(default_factory=HostingManager)


def _day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _opt_str(data: dict[str, Any], key: str, max_len: int) -> str | None:
    value = data.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ApiError(422, "invalid", f"{key} must be a string")
    if len(value) > max_len:
        raise ApiError(422, "invalid", f"{key} is too long (max {max_len})")
    return value


def _req_str(data: dict[str, Any], key: str, max_len: int) -> str:
    value = _opt_str(data, key, max_len)
    if value is None or not value.strip():
        raise ApiError(422, "invalid", f"{key} is required")
    return value


def _idempotency_key(data: dict[str, Any]) -> str:
    return _req_str(data, "idempotency_key", MAX_KEY).strip()


def _single_line(value: str, key: str) -> str:
    if any(ch in value for ch in "\r\n\0"):
        raise ApiError(422, "invalid", f"{key} must be a single line")
    return value


class IntegrationsService:
    def __init__(self, host: IntegrationHost, deps: IntegrationDeps | None = None) -> None:
        self.host = host
        self.deps = deps or IntegrationDeps()
        self._outbound_lock = asyncio.Lock()

    @property
    def hosting(self) -> HostingManager:
        return self.deps.hosting

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.deps.http_transport, timeout=20.0)

    def _redact(self, text: str) -> str:
        return registry.redact(text, self.host.config)[:300]

    def _values(self, integration_id: str) -> dict[str, str] | None:
        """Field values when the integration is connected, else ``None``."""
        spec = get_spec(integration_id)
        if not registry.is_connected(self.host.config, spec):
            return None
        return registry.values(self.host.config, spec)

    # --- registry endpoints -------------------------------------------------------------------
    def listing(self, scope: Scope) -> dict[str, Any]:
        return {"integrations": registry.listing(self.host.config, scope)}

    async def update(self, integration_id: str, fields: object) -> dict[str, Any]:
        spec = registry.get_guard_spec(integration_id)
        now = self.host.now()
        changed = await self.host.mutate_config(
            lambda c: registry.apply_update(c, spec, fields, now))
        restart = await self.host.reload_integration(spec.id)
        await self.host.event("owner", "integration_updated",
                              {"id": spec.id, "fields": changed, "restart_required": restart})
        item = registry.owner_item(self.host.config, spec)
        item["restart_required"] = restart
        return item

    async def remove(self, integration_id: str) -> dict[str, Any]:
        spec = registry.get_guard_spec(integration_id)

        def fn(config: GuardConfig) -> bool:
            registry.clear(config, spec)
            if config.settings.email_provider == spec.id:
                config.settings = config.settings.model_copy(update={"email_provider": "none"})
                return True
            return False

        reset_provider = await self.host.mutate_config(fn)
        if spec.id == "hosting":
            await self.hosting.stop_all()
        restart = await self.host.reload_integration(spec.id)
        await self.host.event("owner", "integration_removed",
                              {"id": spec.id, "email_provider_reset": reset_provider})
        item = registry.owner_item(self.host.config, spec)
        item.update({"removed": True, "restart_required": restart,
                     "email_provider": self.host.settings.email_provider})
        return item

    async def test(self, integration_id: str) -> dict[str, Any]:
        spec = registry.get_guard_spec(integration_id)
        vals = registry.values(self.host.config, spec)
        missing = registry.missing_required(spec, vals)
        if missing:
            ok, message = False, f"not connected: missing {', '.join(missing)}"
        else:
            try:
                ok, message = True, await self._live_test(spec.id, vals)
            except ApiError as exc:
                ok, message = False, exc.message
            except Exception as exc:
                logger.info("integration test {} failed: {}", spec.id, type(exc).__name__)
                ok, message = False, str(exc) or type(exc).__name__
        message = self._redact(message)
        now = self.host.now()
        result = await self.host.mutate_config(
            lambda c: registry.record_test(c, spec, ok=ok, message=message, now=now))
        await self.host.event("owner", "integration_tested", {"id": spec.id, "ok": ok})
        return result

    async def _live_test(self, integration_id: str, vals: dict[str, str]) -> str:
        if integration_id == "openrouter":
            async with self._http() as http:
                usage = await OpenRouterClient(vals["apiKey"], http=http).usage()
            return f"OpenRouter key ok; usage ${usage}"
        if integration_id == "cdp":
            return await self.deps.cdp_check(self.host.config)
        if integration_id == "stripe":
            return await self.deps.stripe_factory(vals["restrictedKey"]).ping()
        if integration_id == "telegram":
            async with self._http() as http:
                resp = await http.post(f"{TELEGRAM_API}/bot{vals['botToken']}/getMe")
            data = resp.json()
            if not isinstance(data, dict) or not data.get("ok"):  # pyright: ignore[reportUnknownMemberType]
                raise RuntimeError(f"Telegram getMe failed (HTTP {resp.status_code})")
            username = str(data.get("result", {}).get("username", "?"))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            return f"Telegram bot @{username} ok"
        if integration_id in ("gmail", "agentmail"):
            return await self._email_provider_for(integration_id, vals).test()
        if integration_id in social.PLATFORMS:
            async with self._http() as http:
                return await social.test(http, integration_id, vals)
        if integration_id == "hosting":
            version = await self.hosting.version()
            if vals.get("provider") == "cloudflare_token":
                tunnel = token_config_hint(vals.get("tunnelToken", ""))
                if tunnel is None:
                    return f"{version}; the tunnel token does not look valid"
                return f"{version}; token for tunnel {tunnel}"
            return version
        raise ApiError(400, "invalid", f"no live test for {integration_id}")

    # --- email --------------------------------------------------------------------------------
    def _email_provider_for(self, provider: str, vals: dict[str, str]) -> EmailProvider:
        if provider == "gmail":
            return GmailProvider(vals["address"], vals["appPassword"],
                                 imap_factory=self.deps.imap_factory,
                                 smtp_factory=self.deps.smtp_factory)
        return AgentMailProvider(vals["apiKey"], vals.get("inboxId", ""),
                                 transport=self.deps.http_transport,
                                 on_inbox_created=self._save_inbox)

    async def _save_inbox(self, inbox_id: str) -> None:
        spec = get_spec("agentmail")
        await self.host.mutate_config(lambda c: registry.set_field(c, spec, "inboxId", inbox_id))
        await self.host.event("guard", "agentmail_inbox_created", {"inbox_id": inbox_id})

    def _email_provider(self) -> EmailProvider:
        name = self.host.settings.email_provider
        vals = self._values(name) if name != "none" else None
        if vals is None:
            raise ApiError(409, "email_not_connected",
                           "no email provider is connected; the owner chooses one in settings")
        return self._email_provider_for(name, vals)

    def email_cap(self) -> tuple[int, dict[str, Any] | None]:
        s = self.host.settings
        cap = s.email_daily_cap
        if s.email_provider != "gmail":
            return cap, None
        connected = registry.connected_at(self.host.config, "gmail")
        today = self.host.now().date()
        day = 0
        if connected:
            day = max(0, (today - parse_iso(connected).date()).days)
        warm_cap = gmail_warmup_cap(day)
        if s.email_warmup:
            cap = min(cap, warm_cap)
        return cap, {"on": s.email_warmup, "day": day, "cap": warm_cap}

    async def _used_today(self, channel: Literal["email", "social"]) -> int:
        return await self.host.store.count_outbound(channel, _day_start(self.host.now()))

    async def email_status(self) -> dict[str, Any]:
        s = self.host.settings
        vals = self._values(s.email_provider) if s.email_provider != "none" else None
        address: str | None = None
        if vals is not None:
            address = vals.get("address") or vals.get("inboxId") or None
        cap, warmup = self.email_cap()
        sent = await self._used_today("email")
        return {"provider": s.email_provider, "address": address, "connected": vals is not None,
                "sent_today": sent, "cap_today": cap, "remaining_today": max(0, cap - sent),
                "warmup": warmup}

    async def _cap_notice(self, channel: str, text: str) -> None:
        key = f"cap_notice:{channel}"
        today = self.host.now().date().isoformat()
        if await self.host.store.kv_get(key) == today:
            return
        await self.host.store.kv_set(key, today)
        await self.host.notify(text)

    @staticmethod
    def _check_not_pending(row: Outbound, what: str) -> None:
        if row.status == "pending":
            raise ApiError(409, "in_progress",
                           f"a {what} with this idempotency_key is still being sent")

    async def email_send(self, data: dict[str, Any]) -> dict[str, Any]:
        raw_to = data.get("to")
        if isinstance(raw_to, str):
            raw_to = [raw_to]
        if not isinstance(raw_to, list) or not raw_to:
            raise ApiError(400, "invalid_recipient", "to must be a list of 1-10 email addresses")
        to: list[str] = []
        for item in raw_to:  # pyright: ignore[reportUnknownVariableType]
            if not isinstance(item, str) or not registry.EMAIL_RE.match(item.strip()):
                raise ApiError(400, "invalid_recipient", f"not an email address: {str(item)[:100]}")  # pyright: ignore[reportUnknownArgumentType]
            to.append(item.strip())
        to = list(dict.fromkeys(to))
        if len(to) > MAX_RECIPIENTS:
            raise ApiError(400, "invalid_recipient", f"at most {MAX_RECIPIENTS} recipients")
        subject = _single_line(_opt_str(data, "subject", MAX_SUBJECT) or "", "subject")
        text = _req_str(data, "text", MAX_EMAIL_TEXT)
        html = _opt_str(data, "html", MAX_EMAIL_HTML)
        in_reply_to = _opt_str(data, "in_reply_to", 500)
        if in_reply_to is not None and not MESSAGE_ID_RE.match(in_reply_to):
            raise ApiError(422, "invalid", "in_reply_to must be a message id")
        if not subject and in_reply_to is None:
            raise ApiError(422, "invalid", "subject is required")
        key = "email:" + _idempotency_key(data)

        existing = await self.host.store.get_outbound_by_key(key)
        if existing is not None:
            return await self._email_replay(existing)
        if self.host.settings.frozen:
            await self._refuse("email", "frozen", {"recipients": len(to)})
            raise ApiError(423, "frozen", "the guard is frozen; no email is sent")
        provider = self._email_provider()

        async with self._outbound_lock:
            cap, _ = self.email_cap()
            used = await self._used_today("email")
            if used + len(to) > cap:
                await self._refuse("email", "email_cap",
                                   {"recipients": len(to), "sent_today": used, "cap": cap})
                await self._cap_notice(
                    "email", f"Cashmaxx hit today's email cap ({used}/{cap} recipients). "
                             "Further sends are refused until tomorrow (UTC).")
                raise ApiError(429, "email_cap",
                               f"today's email cap is {cap} recipients; {used} used",
                               remaining_today=max(0, cap - used), cap_today=cap)
            try:
                row = await self.host.store.reserve_outbound(
                    ts=self.host.now(), channel="email", provider=provider.name,
                    idempotency_key=key, recipients=len(to), target=", ".join(to))
            except DuplicateIdempotencyKeyError:
                found = await self.host.store.get_outbound_by_key(key)
                assert found is not None
                return await self._email_replay(found)

        try:
            message_id = await provider.send(to=to, subject=subject, text=text, html=html,
                                             in_reply_to=in_reply_to)
        except (EmailError, httpx.HTTPError) as exc:
            message = self._redact(str(exc))
            await self.host.store.finish_outbound(row.id, status="failed", ref=None)
            await self.host.event("agent", "email_failed", {
                "provider": provider.name, "recipients": len(to), "error": message})
            raise ApiError(502, "email_failed", message) from exc
        await self.host.store.finish_outbound(row.id, status="sent", ref=message_id)
        await self.host.event("agent", "email_sent", {
            "provider": provider.name, "to": to, "recipients": len(to),
            "subject": subject[:200], "message_id": message_id})
        cap, _ = self.email_cap()
        used = await self._used_today("email")
        return {"status": "sent", "message_id": message_id,
                "remaining_today": max(0, cap - used)}

    async def _email_replay(self, row: Outbound) -> dict[str, Any]:
        self._check_not_pending(row, "email")
        cap, _ = self.email_cap()
        used = await self._used_today("email")
        return {"status": "sent", "message_id": row.ref, "remaining_today": max(0, cap - used),
                "replayed": True}

    async def _refuse(self, channel: str, reason: str, data: dict[str, Any]) -> None:
        await self.host.event("agent", f"{channel}_refused", {"reason": reason, **data})

    async def email_inbox(self, *, unread_only: bool, limit: int) -> dict[str, Any]:
        provider = self._email_provider()
        try:
            messages = await provider.inbox(unread_only=unread_only, limit=limit)
        except (EmailError, httpx.HTTPError) as exc:
            raise ApiError(502, "email_failed", self._redact(str(exc))) from exc
        return {"messages": messages, "untrusted": True}

    async def email_message(self, message_id: str) -> dict[str, Any]:
        if not MESSAGE_ID_RE.match(message_id):
            raise ApiError(422, "invalid", "bad message id")
        provider = self._email_provider()
        try:
            message = await provider.message(message_id)
        except (EmailError, httpx.HTTPError) as exc:
            raise ApiError(502, "email_failed", self._redact(str(exc))) from exc
        message["untrusted"] = True
        return message

    # --- social -------------------------------------------------------------------------------
    async def social_post(self, data: dict[str, Any]) -> dict[str, Any]:
        platform = data.get("platform")
        if platform not in social.PLATFORMS:
            raise ApiError(422, "invalid", f"platform must be one of {list(social.PLATFORMS)}")
        assert isinstance(platform, str)
        text = _req_str(data, "text", MAX_SOCIAL_TEXT)
        link = _opt_str(data, "link", 2000)
        if link is not None:
            parsed = urlparse(link)
            if parsed.scheme not in ("http", "https") or not parsed.hostname or " " in link:
                raise ApiError(422, "invalid", "link must be an http(s) URL")
        subreddit = _opt_str(data, "subreddit", 50)
        title = _opt_str(data, "title", 300)
        if platform == "reddit":
            if subreddit is None:
                raise ApiError(422, "invalid", "reddit needs subreddit")
            subreddit = subreddit.removeprefix("r/").removeprefix("/r/")
            if not SUBREDDIT_RE.match(subreddit):
                raise ApiError(422, "invalid", "subreddit must be a subreddit name")
            if title is None or not title.strip():
                raise ApiError(422, "invalid", "reddit needs title")
            _single_line(title, "title")
        key = "social:" + _idempotency_key(data)

        existing = await self.host.store.get_outbound_by_key(key)
        if existing is not None:
            return await self._social_replay(existing)
        if self.host.settings.frozen:
            await self._refuse("social", "frozen", {"platform": platform})
            raise ApiError(423, "frozen", "the guard is frozen; nothing is posted")
        vals = self._values(platform)
        if vals is None:
            raise ApiError(409, "social_not_connected", f"{platform} is not connected")

        async with self._outbound_lock:
            cap = self.host.settings.social_daily_cap
            used = await self._used_today("social")
            if used >= cap:
                await self._refuse("social", "social_cap",
                                   {"platform": platform, "posts_today": used, "cap": cap})
                await self._cap_notice(
                    "social", f"Cashmaxx hit today's social post cap ({used}/{cap}).")
                raise ApiError(429, "social_cap", f"today's social cap is {cap} posts",
                               remaining_today=0, cap_today=cap)
            target = f"r/{subreddit}" if platform == "reddit" else platform
            try:
                row = await self.host.store.reserve_outbound(
                    ts=self.host.now(), channel="social", provider=platform,
                    idempotency_key=key, recipients=1, target=target)
            except DuplicateIdempotencyKeyError:
                found = await self.host.store.get_outbound_by_key(key)
                assert found is not None
                return await self._social_replay(found)

        try:
            async with self._http() as http:
                url = await social.post(http, platform, vals, text=text, link=link,
                                        subreddit=subreddit, title=title)
        except (social.SocialError, httpx.HTTPError) as exc:
            message = self._redact(str(exc) or type(exc).__name__)
            await self.host.store.finish_outbound(row.id, status="failed", ref=None)
            await self.host.event("agent", "social_failed",
                                  {"platform": platform, "error": message})
            raise ApiError(502, "social_failed", message) from exc
        await self.host.store.finish_outbound(row.id, status="sent", ref=url)
        await self.host.event("agent", "social_posted",
                              {"platform": platform, "url": url, "target": target})
        used = await self._used_today("social")
        return {"status": "posted", "url": url,
                "remaining_today": max(0, self.host.settings.social_daily_cap - used)}

    async def _social_replay(self, row: Outbound) -> dict[str, Any]:
        self._check_not_pending(row, "social post")
        used = await self._used_today("social")
        return {"status": "posted", "url": row.ref, "replayed": True,
                "remaining_today": max(0, self.host.settings.social_daily_cap - used)}

    # --- hosting ------------------------------------------------------------------------------
    def hosting_list(self) -> dict[str, Any]:
        return {"enabled": self.host.settings.hosting_enabled, "tunnels": self.hosting.list()}

    async def hosting_expose(self, data: dict[str, Any]) -> dict[str, Any]:
        name = check_name(data.get("name"))
        port = check_port(data.get("port"), self.host.config.port)
        vals = self._values("hosting")
        refusal: ApiError | None = None
        if self.host.settings.frozen:
            refusal = ApiError(423, "frozen", "the guard is frozen; no new tunnels")
        elif not self.host.settings.hosting_enabled:
            refusal = ApiError(403, "hosting_disabled", "the owner has not enabled public hosting")
        elif vals is None:
            refusal = ApiError(409, "hosting_not_connected",
                               "the hosting integration is not connected")
        if refusal is not None or vals is None:
            refusal = refusal or ApiError(409, "hosting_not_connected", "not connected")
            await self._refuse("hosting", refusal.code, {"name": name, "port": port})
            raise refusal
        try:
            tunnel = await self.hosting.expose(
                name=name, port=port, provider=vals["provider"],
                token=vals.get("tunnelToken", ""), started_at=iso(self.host.now()))
        except ApiError as exc:
            exc.message = self._redact(exc.message)
            await self._refuse("hosting", exc.code, {"name": name, "port": port})
            raise
        await self.host.event("agent", "tunnel_started",
                              {"name": name, "port": port, "url": tunnel.url})
        await self.host.notify(f"Cashmaxx exposed local port {port} as {tunnel.url or name}.")
        return tunnel.to_json()

    async def hosting_stop(self, name: str, actor: Actor) -> dict[str, Any]:
        if not await self.hosting.stop(name):
            raise ApiError(404, "not_found", f"no tunnel named {name!r}")
        await self.host.event(actor, "tunnel_stopped", {"name": name})
        return {"stopped": True, "name": name}

    async def stop_all_tunnels(self, reason: str) -> None:
        stopped = await self.hosting.stop_all()
        if stopped:
            await self.host.event("guard", "tunnels_stopped", {"names": stopped,
                                                               "reason": reason})
