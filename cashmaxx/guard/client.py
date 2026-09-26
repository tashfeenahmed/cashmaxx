"""Async client for the guard HTTP API. Used by agent tools (agent token) and the WebUI proxy.

Every method returns the decoded JSON body. Non-2xx responses raise ``GuardError`` carrying the
guard's ``{"error", "message"}``. Connection failures raise ``GuardUnavailable`` so callers can
fail closed.
"""

from __future__ import annotations

from typing import Any, Literal, cast
from urllib.parse import quote

import httpx

JSON = dict[str, Any]
Window = Literal["7d", "30d", "all"]


class GuardError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.status = status
        self.code = code
        self.message = message


class GuardUnavailableError(GuardError):
    def __init__(self, message: str) -> None:
        super().__init__(503, "guard_unavailable", message)


GuardUnavailable = GuardUnavailableError  # older name, kept for callers


class GuardClient:
    def __init__(
        self,
        base_url: str,
        *,
        agent_token: str | None = None,
        owner_session: str | None = None,
        timeout: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        headers: dict[str, str] = {}
        if agent_token:
            headers["Authorization"] = f"Bearer {agent_token}"
        if owner_session:
            headers["X-Cashmaxx-Owner"] = owner_session
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), headers=headers, timeout=timeout, transport=transport
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> GuardClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def _request(
        self, method: str, path: str, *, json: JSON | None = None, params: JSON | None = None
    ) -> JSON:
        try:
            resp = await self._http.request(method, path, json=json, params=params)
        except httpx.HTTPError as exc:
            raise GuardUnavailableError(f"guard unreachable: {exc}") from exc
        try:
            decoded: object = resp.json()
        except ValueError:
            decoded = {"error": "bad_response", "message": resp.text[:500]}
        if not isinstance(decoded, dict):
            raise GuardError(resp.status_code, "bad_response", "expected a JSON object")
        body: JSON = {str(k): v for k, v in cast(dict[object, Any], decoded).items()}
        if resp.status_code >= 400:
            raise GuardError(
                resp.status_code,
                str(body.get("error", "error")),
                str(body.get("message", resp.reason_phrase)),
            )
        return body

    # --- read ---------------------------------------------------------------------------------
    async def health(self) -> JSON:
        return await self._request("GET", "/health")

    async def wallet(self) -> JSON:
        return await self._request("GET", "/wallet")

    async def payment(self, payment_id: str) -> JSON:
        return await self._request("GET", f"/spend/{payment_id}")

    async def ledger(self, window: Window = "30d") -> JSON:
        return await self._request("GET", "/ledger", params={"window": window})

    async def approvals(self, status: str | None = "pending") -> JSON:
        return await self._request("GET", "/approvals", params={"status": status} if status else None)

    async def settings(self) -> JSON:
        return await self._request("GET", "/settings")

    async def events(self, since: str | None = None) -> JSON:
        return await self._request("GET", "/events", params={"since": since} if since else None)

    # --- agent actions --------------------------------------------------------------------------
    async def spend(
        self,
        *,
        to: str,
        amount_usd: str,
        purpose: str,
        category: str = "payment_out",
        kind: Literal["transfer", "x402"] = "transfer",
        idempotency_key: str,
    ) -> JSON:
        return await self._request(
            "POST",
            "/spend",
            json={
                "kind": kind, "to": to, "amount_usd": amount_usd, "purpose": purpose,
                "category": category, "idempotency_key": idempotency_key,
            },
        )

    async def x402_fetch(
        self,
        *,
        url: str,
        method: str = "GET",
        body: str | None = None,
        max_usd: str,
        purpose: str,
        idempotency_key: str | None = None,
    ) -> JSON:
        payload: JSON = {
            "url": url, "method": method, "body": body, "max_usd": max_usd, "purpose": purpose,
        }
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
        return await self._request("POST", "/x402/fetch", json=payload)

    async def record_cost(self, *, amount_usd: str, category: str, note: str) -> JSON:
        return await self._request(
            "POST", "/ledger/cost", json={"amount_usd": amount_usd, "category": category, "note": note}
        )

    async def create_product(self, *, name: str, description: str, price_usd: str) -> JSON:
        return await self._request(
            "POST",
            "/stripe/product",
            json={"name": name, "description": description, "price_usd": price_usd},
        )

    async def freeze(self, reason: str) -> JSON:
        return await self._request("POST", "/freeze", json={"reason": reason})

    # --- integrations (agent) -------------------------------------------------------------------
    async def integrations(self) -> JSON:
        """Agent scope: ``{"integrations": [{id, label, kind, category, connected}]}``."""
        return await self._request("GET", "/integrations")

    async def email_status(self) -> JSON:
        return await self._request("GET", "/email/status")

    async def email_send(
        self, *, to: list[str], subject: str, text: str, idempotency_key: str,
        html: str | None = None, in_reply_to: str | None = None,
    ) -> JSON:
        payload: JSON = {"to": to, "subject": subject, "text": text,
                         "idempotency_key": idempotency_key}
        if html is not None:
            payload["html"] = html
        if in_reply_to is not None:
            payload["in_reply_to"] = in_reply_to
        return await self._request("POST", "/email/send", json=payload)

    async def email_inbox(self, *, unread_only: bool = True, limit: int = 20) -> JSON:
        return await self._request(
            "GET", "/email/inbox", params={"unread": "1" if unread_only else "0", "limit": limit}
        )

    async def email_message(self, message_id: str) -> JSON:
        return await self._request("GET", f"/email/messages/{quote(message_id, safe='')}")

    async def social_post(
        self, *, platform: str, text: str, idempotency_key: str,
        link: str | None = None, subreddit: str | None = None, title: str | None = None,
    ) -> JSON:
        payload: JSON = {"platform": platform, "text": text, "idempotency_key": idempotency_key}
        for key, value in (("link", link), ("subreddit", subreddit), ("title", title)):
            if value is not None:
                payload[key] = value
        return await self._request("POST", "/social/post", json=payload)

    async def hosting_list(self) -> JSON:
        return await self._request("GET", "/hosting")

    async def hosting_expose(self, *, port: int, name: str) -> JSON:
        return await self._request("POST", "/hosting/expose", json={"port": port, "name": name})

    async def hosting_stop(self, name: str) -> JSON:
        return await self._request("DELETE", f"/hosting/{quote(name, safe='')}")

    # --- owner actions (need an owner session) ------------------------------------------------
    async def owner_session(self, pin: str) -> JSON:
        return await self._request("POST", "/owner/session", json={"pin": pin})

    async def approve(self, approval_id: str) -> JSON:
        return await self._request("POST", f"/approvals/{approval_id}/approve")

    async def deny(self, approval_id: str) -> JSON:
        return await self._request("POST", f"/approvals/{approval_id}/deny")

    async def update_settings(self, patch: JSON) -> JSON:
        return await self._request("PATCH", "/settings", json=patch)

    async def unfreeze(self) -> JSON:
        return await self._request("POST", "/unfreeze")

    async def owner_check(self) -> JSON:
        """200 ``{"ok": true}`` when the owner session is live; used before agent-side config writes."""
        return await self._request("GET", "/owner/check")

    async def owner_integrations(self) -> JSON:
        """Owner scope: full listing with field specs, ``set`` flags and last test results."""
        return await self._request("GET", "/integrations")

    async def update_integration(self, integration_id: str, fields: dict[str, str]) -> JSON:
        """Merge fields. An empty string for a secret keeps the stored value."""
        return await self._request("PUT", f"/integrations/{quote(integration_id, safe='')}", json={"fields": fields})

    async def test_integration(self, integration_id: str) -> JSON:
        return await self._request("POST", f"/integrations/{quote(integration_id, safe='')}/test")

    async def remove_integration(self, integration_id: str) -> JSON:
        return await self._request("DELETE", f"/integrations/{quote(integration_id, safe='')}")
