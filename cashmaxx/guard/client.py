"""Async client for the guard HTTP API. Used by agent tools (agent token) and the WebUI proxy.

Every method returns the decoded JSON body. Non-2xx responses raise ``GuardError`` carrying the
guard's ``{"error", "message"}``. Connection failures raise ``GuardUnavailable`` so callers can
fail closed.
"""

from __future__ import annotations

from typing import Any, Literal

import httpx

JSON = dict[str, Any]
Window = Literal["7d", "30d", "all"]


class GuardError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.status = status
        self.code = code
        self.message = message


class GuardUnavailable(GuardError):
    def __init__(self, message: str) -> None:
        super().__init__(503, "guard_unavailable", message)


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
            raise GuardUnavailable(f"guard unreachable: {exc}") from exc
        try:
            body = resp.json()
        except ValueError:
            body = {"error": "bad_response", "message": resp.text[:500]}
        if resp.status_code >= 400:
            raise GuardError(
                resp.status_code,
                str(body.get("error", "error")),
                str(body.get("message", resp.reason_phrase)),
            )
        if not isinstance(body, dict):
            raise GuardError(resp.status_code, "bad_response", "expected a JSON object")
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
        self, *, url: str, method: str = "GET", body: str | None = None, max_usd: str, purpose: str
    ) -> JSON:
        return await self._request(
            "POST",
            "/x402/fetch",
            json={"url": url, "method": method, "body": body, "max_usd": max_usd, "purpose": purpose},
        )

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
