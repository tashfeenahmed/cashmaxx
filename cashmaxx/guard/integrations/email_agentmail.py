"""AgentMail (https://agentmail.to) over its REST API, ``https://api.agentmail.to/v0``.

Paths checked against the published OpenAPI spec (docs.agentmail.to/openapi.json):
``GET/POST /inboxes``, ``GET /inboxes/{inbox_id}/messages`` (``labels``, ``limit``),
``GET|PATCH /inboxes/{inbox_id}/messages/{message_id}``, ``POST .../messages/send`` and
``POST .../messages/{message_id}/reply``. The inbox id is its email address.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, cast
from urllib.parse import quote

import httpx

from cashmaxx.guard.integrations.email_common import EmailError, clip, html_to_text, snippet

API_BASE = "https://api.agentmail.to/v0"
DEFAULT_USERNAME = "cashmaxx"
MAX_LIST = 50

OnInboxCreated = Callable[[str], Awaitable[None]]


def _q(value: str) -> str:
    return quote(value, safe="")


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in cast(list[Any], value)]
    return [str(value)] if value else []


class AgentMailProvider:
    name = "agentmail"

    def __init__(
        self,
        api_key: str,
        inbox_id: str = "",
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        on_inbox_created: OnInboxCreated | None = None,
    ) -> None:
        self._key = api_key
        self._inbox_id = inbox_id.strip()
        self._transport = transport
        self._on_inbox_created = on_inbox_created

    @property
    def address(self) -> str | None:
        return self._inbox_id or None

    async def _call(
        self, method: str, path: str, *, json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(
            base_url=API_BASE, transport=self._transport, timeout=30.0,
            headers={"Authorization": f"Bearer {self._key}"},
        ) as http:
            try:
                resp = await http.request(method, path, json=json, params=params)
            except httpx.HTTPError as exc:
                raise EmailError(f"AgentMail unreachable: {type(exc).__name__}") from exc
        if resp.status_code >= 400:
            detail = ""
            try:
                body = resp.json()
                if isinstance(body, dict):
                    b = cast(dict[str, Any], body)
                    detail = str(b.get("message") or b.get("name") or "")[:200]
            except ValueError:
                pass
            raise EmailError(f"AgentMail {method} {path.split('?')[0]}: HTTP "
                             f"{resp.status_code} {detail}".strip())
        try:
            data = resp.json()
        except ValueError as exc:
            raise EmailError("AgentMail returned a non-JSON response") from exc
        return cast(dict[str, Any], data) if isinstance(data, dict) else {}

    async def ensure_inbox(self) -> str:
        """The inbox id, creating ``cashmaxx@agentmail.to`` (or a random name) when unset."""
        if self._inbox_id:
            return self._inbox_id
        try:
            created = await self._call("POST", "/inboxes", json={
                "username": DEFAULT_USERNAME, "display_name": "Cashmaxx"})
        except EmailError:
            created = await self._call("POST", "/inboxes", json={"display_name": "Cashmaxx"})
        inbox_id = str(created.get("inbox_id") or created.get("email") or "")
        if not inbox_id:
            raise EmailError("AgentMail did not return an inbox id")
        self._inbox_id = inbox_id
        if self._on_inbox_created is not None:
            await self._on_inbox_created(inbox_id)
        return inbox_id

    async def test(self) -> str:
        data = await self._call("GET", "/inboxes", params={"limit": 10})
        inboxes = cast(list[dict[str, Any]], data.get("inboxes") or [])
        if self._inbox_id and not any(
            str(i.get("inbox_id")) == self._inbox_id for i in inboxes
        ):
            # The listing is paged; a direct lookup settles it.
            await self._call("GET", f"/inboxes/{_q(self._inbox_id)}")
        inbox = await self.ensure_inbox()
        return f"AgentMail key ok; inbox {inbox}"

    async def send(
        self, *, to: list[str], subject: str, text: str, html: str | None,
        in_reply_to: str | None,
    ) -> str:
        inbox = await self.ensure_inbox()
        payload: dict[str, Any] = {"to": to, "text": text}
        if html:
            payload["html"] = html
        if in_reply_to:
            path = f"/inboxes/{_q(inbox)}/messages/{_q(in_reply_to)}/reply"
        else:
            payload["subject"] = subject
            path = f"/inboxes/{_q(inbox)}/messages/send"
        data = await self._call("POST", path, json=payload)
        return str(data.get("message_id") or "")

    async def inbox(self, *, unread_only: bool, limit: int) -> list[dict[str, Any]]:
        inbox = await self.ensure_inbox()
        params: dict[str, Any] = {"limit": max(1, min(limit, MAX_LIST))}
        if unread_only:
            params["labels"] = "unread"
        data = await self._call("GET", f"/inboxes/{_q(inbox)}/messages", params=params)
        items: list[dict[str, Any]] = []
        for m in cast(list[dict[str, Any]], data.get("messages") or []):
            labels = _as_list(m.get("labels"))
            items.append({
                "id": str(m.get("message_id", "")),
                "from": str(m.get("from", "")),
                "to": _as_list(m.get("to")),
                "subject": str(m.get("subject") or ""),
                "date": m.get("timestamp"),
                "snippet": snippet(str(m.get("preview") or "")),
                "thread_id": m.get("thread_id"),
                "unread": "unread" in labels,
            })
        return items

    async def message(self, message_id: str) -> dict[str, Any]:
        inbox = await self.ensure_inbox()
        path = f"/inboxes/{_q(inbox)}/messages/{_q(message_id)}"
        m = await self._call("GET", path)
        text = str(m.get("text") or "") or html_to_text(str(m.get("html") or ""))
        if "unread" in _as_list(m.get("labels")):
            try:
                await self._call("PATCH", path, json={"remove_labels": ["unread"]})
            except EmailError:
                pass  # marking read is best effort
        return {
            "id": str(m.get("message_id", message_id)),
            "from": str(m.get("from", "")),
            "to": _as_list(m.get("to")),
            "cc": _as_list(m.get("cc")),
            "subject": str(m.get("subject") or ""),
            "date": m.get("timestamp"),
            "text": clip(text.strip()),
            "thread_id": m.get("thread_id"),
        }
