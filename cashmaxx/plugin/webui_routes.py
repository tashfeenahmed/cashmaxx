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
    GET /api/cashmaxx/integrations         reduced listing, ``connected`` filled for both kinds

Mutations (WebSocket ``webui_request`` actions; the owner session goes in ``payload.owner_session``
because a browser can't add headers to the socket, and ``X-Cashmaxx-Owner`` is accepted too):

    cashmaxx.owner_session     {pin}                                  -> POST /owner/session
    cashmaxx.approve           {approval_id, owner_session}           -> POST /approvals/<id>/approve
    cashmaxx.deny              {approval_id, owner_session}           -> POST /approvals/<id>/deny
    cashmaxx.settings.update   {patch, owner_session}                 -> PATCH /settings
    cashmaxx.freeze            {reason, owner_session?}               -> POST /freeze
    cashmaxx.unfreeze          {owner_session}                        -> POST /unfreeze
    cashmaxx.integrations.list    {owner_session}                     -> full listing (both kinds)
    cashmaxx.integrations.update  {id, fields, owner_session}         -> guard PUT, or MCP config
    cashmaxx.integrations.test    {id, owner_session}                 -> guard test, or MCP test
    cashmaxx.integrations.remove  {id, owner_session}                 -> guard DELETE, or MCP remove

Agent-kind integrations (browser, GitHub, search) are nanobot MCP servers: the proxy first confirms
the owner session with the guard (``GET /owner/check``), then edits nanobot's config file, saves it
and asks the running agent to reconnect its MCP servers (``MCPProvider.reload``), the same way
nanobot's own MCP preset actions do. A bad session means no config write.

Owner calls go to the guard with the owner session and **no** agent token. The PIN and the
session are never stored here: the session is returned to the browser, which keeps it in memory.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, Literal, cast

from loguru import logger
from websockets.http11 import Request as WsRequest
from websockets.http11 import Response

from cashmaxx import plugin
from cashmaxx.agent_integrations import (
    AgentIntegrationError,
    active_server,
    agent_integration_item,
    apply_agent_integration,
    remove_agent_integration,
)
from cashmaxx.guard.client import JSON, GuardClient, GuardError, GuardUnavailable
from cashmaxx.integrations_catalog import BY_ID
from cashmaxx.plugin import integrations as integ
from nanobot.webui.http_utils import case_insensitive_header, http_json_response, parse_request_path

PREFIX = "/api/cashmaxx/"
OWNER_HEADER = "X-Cashmaxx-Owner"
# Same attribute names ws_http sets on replayed WebSocket mutations.
_MUTATION_REQUEST_ATTR = "_nanobot_webui_mutation_request"
_MUTATION_PAYLOAD_ATTR = "_nanobot_webui_mutation_payload"

_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_APPROVAL_PATH = re.compile(r"^/api/cashmaxx/approvals/([^/]+)/(approve|deny)$")
_SPEND_PATH = re.compile(r"^/api/cashmaxx/spend/([^/]+)$")
_INTEGRATION_PATH = re.compile(r"^/api/cashmaxx/integrations/([^/]+)/(update|test|remove)$")
_INTEGRATIONS_OWNER_PATH = "/api/cashmaxx/integrations/owner"
_MUTATION_ONLY = frozenset({
    "/api/cashmaxx/owner/session",
    "/api/cashmaxx/freeze",
    "/api/cashmaxx/unfreeze",
    _INTEGRATIONS_OWNER_PATH,
})
# Serializes our nanobot config writes (read-modify-write of the config file).
_config_lock = asyncio.Lock()
# Last MCP connect test per agent-kind integration: {ok, message, at}. In memory only.
_agent_tests: dict[str, dict[str, Any]] = {}
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
    return (
        path in _MUTATION_ONLY
        or _APPROVAL_PATH.match(path) is not None
        or _INTEGRATION_PATH.match(path) is not None
    )


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
    if action == "cashmaxx.integrations.list":
        return _INTEGRATIONS_OWNER_PATH
    if action in {"cashmaxx.integrations.update", "cashmaxx.integrations.test",
                  "cashmaxx.integrations.remove"}:
        integration_id = payload.get("id")
        if not isinstance(integration_id, str) or integration_id not in BY_ID:
            return _error(404, "unknown_integration", "missing or unknown integration id")
        verb = action.removeprefix("cashmaxx.integrations.")
        return f"/api/cashmaxx/integrations/{integration_id}/{verb}"
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


async def _refresh_workspace(settings: JSON | None = None) -> None:
    """After a settings or integration change, re-render RULES.md and the gated skills."""
    if plugin.state.workspace is None:
        return
    await integ.refresh_workspace(settings)


# --- integrations --------------------------------------------------------------------------


async def _reduced_listing(client: GuardClient) -> JSON:
    return {"integrations": await integ.fetch_listing(client)}


def _scrub_guard_item(item: dict[str, Any]) -> dict[str, Any]:
    """Defence in depth: never pass on a value for a secret field, whatever the guard sent."""
    out = dict(item)
    raw_fields: object = out.get("fields")
    if isinstance(raw_fields, list):
        fields: list[object] = []
        for f in cast(list[object], raw_fields):
            if isinstance(f, dict):
                entry = dict(cast(dict[str, Any], f))
                if entry.get("secret"):
                    entry.pop("value", None)
                fields.append(entry)
        out["fields"] = fields
    return out


def _agent_item(config: Any, integration_id: str) -> dict[str, Any]:
    if config is None:  # unreadable nanobot config: status unknown, nothing to show
        spec = BY_ID[integration_id]
        return {"id": spec.id, "label": spec.label, "kind": spec.kind,
                "category": spec.category, "connected": None, "summary": spec.summary,
                "docs_url": spec.docs_url, "fields": [], "connected_at": None,
                "last_test": _agent_tests.get(integration_id)}
    item = agent_integration_item(config, integration_id, owner=True)
    item["last_test"] = _agent_tests.get(integration_id)
    return item


async def _owner_listing(session: str | None) -> Response:
    """Full listing: the guard's owner listing (which also proves the session) + agent kinds."""
    async def listing(client: GuardClient) -> JSON:
        body = await client.owner_integrations()
        config = await asyncio.to_thread(integ.try_load_nanobot_config)
        raw: object = body.get("integrations")
        items = [cast(dict[str, Any], i) for i in cast(list[object], raw)
                 if isinstance(i, dict)] if isinstance(raw, list) else []
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            iid = str(item.get("id") or "")
            seen.add(iid)
            spec = BY_ID.get(iid)
            out.append(_agent_item(config, iid) if spec is not None and spec.kind == "agent"
                       else _scrub_guard_item(item))
        for spec in BY_ID.values():
            if spec.kind == "agent" and spec.id not in seen:
                out.append(_agent_item(config, spec.id))
        return {"integrations": out}

    return await _forward(listing, auth="owner", owner_session=session)


def _fields(payload: dict[str, Any], integration_id: str) -> dict[str, str] | Response:
    raw: object = payload.get("fields")
    if not isinstance(raw, dict):
        return _error(400, "invalid", "fields must be an object")
    allowed = {f.name for f in BY_ID[integration_id].fields}
    fields: dict[str, str] = {}
    for key, value in cast(dict[object, object], raw).items():
        if not isinstance(key, str) or key not in allowed:
            return _error(400, "invalid", f"unknown field {key!r}")
        if value is None:
            continue
        if not isinstance(value, str) or len(value) > 4000:
            return _error(400, "invalid", f"field {key!r} must be a string")
        fields[key] = value
    return fields


async def _integration_action(
    integration_id: str, verb: str, payload: dict[str, Any], session: str | None
) -> Response:
    spec = BY_ID.get(integration_id)
    if spec is None:
        return _error(404, "unknown_integration", "unknown integration")
    if spec.kind == "guard":
        return await _guard_integration_action(integration_id, verb, payload, session)
    return await _agent_integration_action(integration_id, verb, payload, session)


async def _guard_integration_action(
    integration_id: str, verb: str, payload: dict[str, Any], session: str | None
) -> Response:
    if verb == "update":
        fields = _fields(payload, integration_id)
        if isinstance(fields, Response):
            return fields
        response = await _forward(lambda c: c.update_integration(integration_id, fields),
                                  auth="owner", owner_session=session)
    elif verb == "test":
        response = await _forward(lambda c: c.test_integration(integration_id),
                                  auth="owner", owner_session=session)
    else:
        response = await _forward(lambda c: c.remove_integration(integration_id),
                                  auth="owner", owner_session=session)
    if response.status_code == 200 and verb != "test":
        await _refresh_workspace()
    if response.status_code == 200 and verb == "update":
        return http_json_response(_scrub_guard_item(json.loads(bytes(response.body))))
    return response


async def _check_owner(session: str | None) -> Response | None:
    """``None`` when the owner session is live; otherwise the refusal to return."""
    response = await _forward(lambda c: c.owner_check(), auth="owner", owner_session=session)
    return None if response.status_code == 200 else response


def _write_agent_config(integration_id: str, verb: str, fields: dict[str, str]) -> Any:
    """Read-modify-write nanobot's config file (runs in a worker thread)."""
    from nanobot.config.loader import save_config

    path = integ.nanobot_config_path()
    config = integ.load_nanobot_config(path)
    if verb == "update":
        apply_agent_integration(config, integration_id, fields)
    else:
        remove_agent_integration(config, integration_id)
    save_config(config, path)
    return config


async def _agent_integration_action(
    integration_id: str, verb: str, payload: dict[str, Any], session: str | None
) -> Response:
    fields: dict[str, str] = {}
    if verb == "update":
        parsed = _fields(payload, integration_id)
        if isinstance(parsed, Response):
            return parsed
        fields = parsed
    refused = await _check_owner(session)
    if refused is not None:
        return refused

    if verb == "test":
        return await _agent_test(integration_id)

    try:
        async with _config_lock:
            config = await asyncio.to_thread(_write_agent_config, integration_id, verb, fields)
    except AgentIntegrationError as exc:
        return _error(400, "invalid", str(exc))
    except Exception as exc:
        logger.exception("cashmaxx: could not write the nanobot config")
        return _error(500, "config_write_failed", f"could not save the nanobot config: {exc}")
    if verb == "remove":
        _agent_tests.pop(integration_id, None)
    hot_reload = await integ.reload_mcp()
    await _refresh_workspace()
    item = _agent_item(config, integration_id)
    item["hot_reload"] = hot_reload
    item["requires_restart"] = bool(hot_reload.get("requires_restart"))
    return http_json_response(item)


async def _agent_test(integration_id: str) -> Response:
    from nanobot.webui.mcp_presets_api import McpPresetError, mcp_presets_test_action

    path = integ.nanobot_config_path()
    try:
        config = await asyncio.to_thread(integ.load_nanobot_config, path)
    except Exception as exc:
        return _error(500, "config_read_failed", f"could not read the nanobot config: {exc}")
    server = active_server(config, integration_id)
    if server is None:
        return _error(409, "not_connected", f"{BY_ID[integration_id].label} is not connected")
    try:
        payload = await mcp_presets_test_action({"name": [server]}, config_path=path)
        raw: object = payload.get("last_action")
        last = cast(dict[str, Any], raw) if isinstance(raw, dict) else {}
        ok = bool(last.get("ok"))
        message = str(last.get("message") or ("connected" if ok else "test failed"))
        if not ok and last.get("error"):
            message = f"{message} ({last['error']})"
        tool_count = int(last.get("tool_count") or 0)
    except McpPresetError as exc:
        ok, message, tool_count = False, str(exc), 0
    result = {"ok": ok, "message": message,
              "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    _agent_tests[integration_id] = result
    return http_json_response({**result, "server": server, "tool_count": tool_count})


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
            await _refresh_workspace(json.loads(bytes(response.body)))
        return response
    if path == "/api/cashmaxx/freeze":
        reason = str(payload.get("reason") or "frozen from the WebUI")
        # Anyone may freeze: use the owner session when present, the agent token otherwise.
        return await _forward(
            lambda c: c.freeze(reason), auth="owner" if session else "agent", owner_session=session,
        )
    if path == "/api/cashmaxx/unfreeze":
        return await _forward(lambda c: c.unfreeze(), auth="owner", owner_session=session)
    if path == _INTEGRATIONS_OWNER_PATH:
        return await _owner_listing(session)
    if match := _INTEGRATION_PATH.match(path):
        return await _integration_action(match.group(1), match.group(2), payload, session)
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
    if path == "/api/cashmaxx/integrations":
        return await _forward(lambda c: _reduced_listing(c))
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
    plugin.kick_workspace_refresh()
    payload = _payload(request)
    if payload is not None:
        return await _dispatch_mutation(request, path, payload)
    if is_mutation_path(path):
        return _error(405, "method_not_allowed", "Cashmaxx mutations require the WebUI socket")
    _, query = parse_request_path(request.path)
    return await _dispatch_read(path, query)
