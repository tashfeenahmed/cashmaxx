"""``/api/cashmaxx/*``: WebUI proxy routes to the guard.

nanobot's WebUI HTTP surface is served from the websockets handshake, so it only takes GET
requests with no body. Anything that changes state goes over the authenticated WebUI socket as a
``webui_request`` with an ``action`` and a ``payload``, which ``ws_http`` maps to a path and
replays through the same dispatcher. This module follows that pattern:

Reads (plain authenticated GET, forwarded with the **agent token**):

    GET /api/cashmaxx/health | wallet | settings
    GET /api/cashmaxx/ledger?window=7d|30d|all
    GET /api/cashmaxx/approvals?status=pending
    GET /api/cashmaxx/events?since=<iso>
    GET /api/cashmaxx/spend/<payment_id>

Mutations (WebSocket ``webui_request`` actions; the owner session goes in ``payload.owner_session``
because a browser can't add headers to the socket, and ``X-Cashmaxx-Owner`` is accepted too):

    cashmaxx.owner_session     {pin}                                  -> POST /owner/session
    cashmaxx.approve           {approval_id, owner_session}           -> POST /approvals/<id>/approve
    cashmaxx.deny              {approval_id, owner_session}           -> POST /approvals/<id>/deny
    cashmaxx.settings.update   {patch, owner_session}                 -> PATCH /settings
    cashmaxx.freeze            {reason, owner_session?}               -> POST /freeze
    cashmaxx.unfreeze          {owner_session}                        -> POST /unfreeze

Owner calls go to the guard with the owner session and **no** agent token. The PIN and the
session are never stored here: the session is returned to the browser, which keeps it in memory.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any, Literal, cast

from loguru import logger
from websockets.http11 import Request as WsRequest
from websockets.http11 import Response

from cashmaxx import plugin
from cashmaxx.config import CashmaxxSettings
from cashmaxx.guard.client import JSON, GuardClient, GuardError, GuardUnavailable
from nanobot.webui.http_utils import case_insensitive_header, http_json_response, parse_request_path

PREFIX = "/api/cashmaxx/"
OWNER_HEADER = "X-Cashmaxx-Owner"
# Same attribute names ws_http sets on replayed WebSocket mutations.
_MUTATION_REQUEST_ATTR = "_nanobot_webui_mutation_request"
_MUTATION_PAYLOAD_ATTR = "_nanobot_webui_mutation_payload"

_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_APPROVAL_PATH = re.compile(r"^/api/cashmaxx/approvals/([^/]+)/(approve|deny)$")
_SPEND_PATH = re.compile(r"^/api/cashmaxx/spend/([^/]+)$")
_MUTATION_ONLY = frozenset({
    "/api/cashmaxx/owner/session",
    "/api/cashmaxx/freeze",
    "/api/cashmaxx/unfreeze",
})
_SIMPLE_ACTIONS = {
    "cashmaxx.owner_session": "/api/cashmaxx/owner/session",
    "cashmaxx.settings.update": "/api/cashmaxx/settings",
    "cashmaxx.freeze": "/api/cashmaxx/freeze",
    "cashmaxx.unfreeze": "/api/cashmaxx/unfreeze",
}


def _error(status: int, code: str, message: str) -> Response:
    return http_json_response({"error": code, "message": message}, status=status)


def is_mutation_path(path: str) -> bool:
    """Paths that only exist as WebSocket mutations (plain HTTP gets a 405)."""
    return path in _MUTATION_ONLY or _APPROVAL_PATH.match(path) is not None


def mutation_path(action: str, payload: dict[str, Any]) -> str | Response | None:
    """Map a ``cashmaxx.*`` WebUI action to its route path. ``None`` for other actions."""
    if not action.startswith("cashmaxx."):
        return None
    if action in _SIMPLE_ACTIONS:
        return _SIMPLE_ACTIONS[action]
    if action in {"cashmaxx.approve", "cashmaxx.deny"}:
        approval_id = payload.get("approval_id")
        if not isinstance(approval_id, str) or not _ID.match(approval_id):
            return _error(400, "invalid", "missing or invalid approval_id")
        verb = action.removeprefix("cashmaxx.")
        return f"/api/cashmaxx/approvals/{approval_id}/{verb}"
    return _error(404, "not_found", "unknown Cashmaxx action")


def _payload(request: WsRequest) -> dict[str, Any] | None:
    if not getattr(request, _MUTATION_REQUEST_ATTR, False):
        return None
    payload = getattr(request, _MUTATION_PAYLOAD_ATTR, None)
    return cast(dict[str, Any], payload) if isinstance(payload, dict) else {}


def _owner_session(request: WsRequest, payload: dict[str, Any]) -> str | None:
    session = payload.get("owner_session")
    if isinstance(session, str) and session.strip():
        return session.strip()
    header = case_insensitive_header(request.headers, OWNER_HEADER)
    return header or None


async def _forward(
    fn: Callable[[GuardClient], Awaitable[JSON]],
    *,
    auth: Literal["agent", "owner", "none"] = "agent",
    owner_session: str | None = None,
) -> Response:
    """Call the guard as the agent (reads), as the owner (session only), or with no credentials."""
    cfg = plugin.resolve_agent_config()
    if cfg is None:
        return _error(503, "not_configured", "Cashmaxx is not configured on this gateway")
    if auth == "owner" and not owner_session:
        return _error(401, "owner_required", "This action needs the owner PIN")
    try:
        async with plugin.guard_client(
            cfg,
            owner_session=owner_session if auth == "owner" else None,
            use_agent_token=auth == "agent",
        ) as client:
            return http_json_response(await fn(client))
    except GuardUnavailable:
        return _error(503, "guard_unavailable", "Guard unreachable — spending is disabled")
    except GuardError as exc:
        return _error(exc.status, exc.code, exc.message)


def _refresh_workspace(result: JSON) -> None:
    """After a settings change, re-render RULES.md and the enabled skills."""
    workspace = plugin.state.workspace
    if workspace is None:
        return
    raw = result.get("settings", result)
    try:
        from cashmaxx.plugin.workspace import install_workspace

        install_workspace(workspace, CashmaxxSettings.model_validate(raw))
    except Exception as exc:
        logger.warning("cashmaxx: workspace refresh after settings update failed: {}", exc)


async def _dispatch_mutation(request: WsRequest, path: str, payload: dict[str, Any]) -> Response:
    session = _owner_session(request, payload)
    if path == "/api/cashmaxx/owner/session":
        pin = payload.get("pin")
        if not isinstance(pin, str) or not pin:
            return _error(400, "invalid", "missing pin")
        return await _forward(lambda c: c.owner_session(pin), auth="none")
    if match := _APPROVAL_PATH.match(path):
        approval_id, verb = match.group(1), match.group(2)
        if not _ID.match(approval_id):
            return _error(400, "invalid", "invalid approval id")
        if verb == "approve":
            return await _forward(lambda c: c.approve(approval_id), auth="owner", owner_session=session)
        return await _forward(lambda c: c.deny(approval_id), auth="owner", owner_session=session)
    if path == "/api/cashmaxx/settings":
        patch = payload.get("patch")
        if not isinstance(patch, dict) or not patch:
            return _error(400, "invalid", "missing settings patch")
        response = await _forward(
            lambda c: c.update_settings(cast(dict[str, Any], patch)),
            auth="owner", owner_session=session,
        )
        if response.status_code == 200:
            _refresh_workspace(json.loads(bytes(response.body)))
        return response
    if path == "/api/cashmaxx/freeze":
        reason = str(payload.get("reason") or "frozen from the WebUI")
        # Anyone may freeze: use the owner session when present, the agent token otherwise.
        return await _forward(
            lambda c: c.freeze(reason), auth="owner" if session else "agent", owner_session=session,
        )
    if path == "/api/cashmaxx/unfreeze":
        return await _forward(lambda c: c.unfreeze(), auth="owner", owner_session=session)
    return _error(404, "not_found", "Cashmaxx route not found")


def _first(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


async def _dispatch_read(path: str, query: dict[str, list[str]]) -> Response:
    if path == "/api/cashmaxx/health":
        return await _forward(lambda c: c.health())
    if path == "/api/cashmaxx/wallet":
        return await _forward(lambda c: c.wallet())
    if path == "/api/cashmaxx/settings":
        return await _forward(lambda c: c.settings())
    if path == "/api/cashmaxx/ledger":
        window = _first(query, "window") or "30d"
        if window not in {"7d", "30d", "all"}:
            return _error(400, "invalid", "window must be 7d, 30d or all")
        return await _forward(lambda c: c.ledger(window))  # type: ignore[arg-type]
    if path == "/api/cashmaxx/approvals":
        status = _first(query, "status")
        return await _forward(lambda c: c.approvals(status or None))
    if path == "/api/cashmaxx/events":
        since = _first(query, "since")
        return await _forward(lambda c: c.events(since or None))
    if match := _SPEND_PATH.match(path):
        payment_id = match.group(1)
        if not _ID.match(payment_id):
            return _error(400, "invalid", "invalid payment id")
        return await _forward(lambda c: c.payment(payment_id))
    return _error(404, "not_found", "Cashmaxx route not found")


async def dispatch(
    request: WsRequest,
    path: str,
    *,
    check_api_token: Callable[[WsRequest], bool],
) -> Response | None:
    """Handle ``/api/cashmaxx/*``. Returns ``None`` for any other path."""
    if not path.startswith(PREFIX):
        return None
    if not check_api_token(request):
        return _error(401, "unauthorized", "Unauthorized")
    payload = _payload(request)
    if payload is not None:
        return await _dispatch_mutation(request, path, payload)
    if is_mutation_path(path):
        return _error(405, "method_not_allowed", "Cashmaxx mutations require the WebUI socket")
    _, query = parse_request_path(request.path)
    return await _dispatch_read(path, query)
