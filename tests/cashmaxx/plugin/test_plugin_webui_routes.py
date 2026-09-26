from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cmx_plugin_testlib import AGENT_TOKEN, FakeGuard
from websockets.datastructures import Headers
from websockets.http11 import Request as WsRequest
from websockets.http11 import Response

from cashmaxx.plugin import webui_routes as routes


def _get(path: str, headers: dict[str, str] | None = None) -> WsRequest:
    return WsRequest(path, Headers(headers or {}))


def _mutation(action: str, payload: dict[str, Any]) -> tuple[WsRequest, str]:
    path = routes.mutation_path(action, payload)
    assert isinstance(path, str)
    request = WsRequest(path, Headers())
    setattr(request, "_nanobot_webui_mutation_request", True)
    setattr(request, "_nanobot_webui_mutation_payload", payload)
    return request, path


def _ok(_: WsRequest) -> bool:
    return True


def _body(response: Response) -> dict[str, Any]:
    return json.loads(bytes(response.body))


async def _dispatch(request: WsRequest, path: str, authorized: bool = True) -> Response:
    response = await routes.dispatch(request, path, check_api_token=lambda _r: authorized)
    assert response is not None
    return response


async def test_other_paths_fall_through() -> None:
    assert await routes.dispatch(_get("/api/settings"), "/api/settings", check_api_token=_ok) is None


async def test_requires_webui_auth(fake_guard: FakeGuard) -> None:
    response = await _dispatch(_get("/api/cashmaxx/wallet"), "/api/cashmaxx/wallet", authorized=False)
    assert response.status_code == 401
    assert fake_guard.requests == []


async def test_read_uses_agent_token(fake_guard: FakeGuard) -> None:
    response = await _dispatch(_get("/api/cashmaxx/wallet", {"X-Cashmaxx-Owner": "s"}),
                               "/api/cashmaxx/wallet")
    assert response.status_code == 200 and _body(response)["address"] == "0xabc"
    assert fake_guard.last.headers["authorization"] == f"Bearer {AGENT_TOKEN}"
    assert "x-cashmaxx-owner" not in fake_guard.last.headers


async def test_ledger_query_and_validation(fake_guard: FakeGuard) -> None:
    response = await _dispatch(_get("/api/cashmaxx/ledger?window=7d"), "/api/cashmaxx/ledger")
    assert response.status_code == 200
    assert fake_guard.last.url.params["window"] == "7d"
    bad = await _dispatch(_get("/api/cashmaxx/ledger?window=1y"), "/api/cashmaxx/ledger")
    assert bad.status_code == 400


async def test_spend_status_and_approvals(fake_guard: FakeGuard) -> None:
    fake_guard.set("GET", "/spend/p1", {"payment_id": "p1", "status": "paid"})
    assert (await _dispatch(_get("/api/cashmaxx/spend/p1"), "/api/cashmaxx/spend/p1")).status_code == 200
    response = await _dispatch(_get("/api/cashmaxx/approvals?status=pending"), "/api/cashmaxx/approvals")
    assert _body(response)["approvals"][0]["id"] == "ap1"


async def test_plain_http_mutation_is_rejected(fake_guard: FakeGuard) -> None:
    assert routes.is_mutation_path("/api/cashmaxx/approvals/ap1/approve")
    assert routes.is_mutation_path("/api/cashmaxx/owner/session")
    assert not routes.is_mutation_path("/api/cashmaxx/settings")
    response = await _dispatch(_get("/api/cashmaxx/approvals/ap1/approve"),
                               "/api/cashmaxx/approvals/ap1/approve")
    assert response.status_code == 405
    assert fake_guard.requests == []


async def test_owner_session_forwards_pin_without_agent_token(fake_guard: FakeGuard) -> None:
    fake_guard.set("POST", "/owner/session", {"session": "sess-1", "expires_at": "x"})
    request, path = _mutation("cashmaxx.owner_session", {"pin": "123456"})
    response = await _dispatch(request, path)
    assert _body(response)["session"] == "sess-1"
    assert fake_guard.last_json() == {"pin": "123456"}
    assert "authorization" not in fake_guard.last.headers
    assert routes.plugin.state.agent_config is not None  # nothing about the session is stored
    assert "sess-1" not in repr(routes.plugin.state)


async def test_approve_uses_owner_session_only(fake_guard: FakeGuard) -> None:
    fake_guard.set("POST", "/approvals/ap1/approve", {"status": "paid"})
    request, path = _mutation("cashmaxx.approve", {"approval_id": "ap1", "owner_session": "sess-1"})
    assert path == "/api/cashmaxx/approvals/ap1/approve"
    response = await _dispatch(request, path)
    assert response.status_code == 200
    assert fake_guard.last.headers["x-cashmaxx-owner"] == "sess-1"
    assert "authorization" not in fake_guard.last.headers


async def test_owner_action_without_session_is_refused(fake_guard: FakeGuard) -> None:
    request, path = _mutation("cashmaxx.deny", {"approval_id": "ap1"})
    response = await _dispatch(request, path)
    assert response.status_code == 401
    assert fake_guard.requests == []


async def test_owner_session_from_header(fake_guard: FakeGuard) -> None:
    fake_guard.set("POST", "/unfreeze", {"frozen": False})
    path = routes.mutation_path("cashmaxx.unfreeze", {})
    assert isinstance(path, str)
    request = WsRequest(path, Headers({"X-Cashmaxx-Owner": "hdr-sess"}))
    setattr(request, "_nanobot_webui_mutation_request", True)
    setattr(request, "_nanobot_webui_mutation_payload", {})
    assert (await _dispatch(request, path)).status_code == 200
    assert fake_guard.last.headers["x-cashmaxx-owner"] == "hdr-sess"


async def test_guard_errors_pass_through(fake_guard: FakeGuard) -> None:
    fake_guard.set("POST", "/approvals/ap1/approve", {"error": "forbidden", "message": "owner only"},
                   status=403)
    request, path = _mutation("cashmaxx.approve", {"approval_id": "ap1", "owner_session": "bad"})
    response = await _dispatch(request, path)
    assert response.status_code == 403 and _body(response)["error"] == "forbidden"


async def test_guard_down(fake_guard: FakeGuard) -> None:
    fake_guard.down = True
    response = await _dispatch(_get("/api/cashmaxx/wallet"), "/api/cashmaxx/wallet")
    assert response.status_code == 503 and _body(response)["error"] == "guard_unavailable"


async def test_freeze_without_owner_uses_agent_token(fake_guard: FakeGuard) -> None:
    request, path = _mutation("cashmaxx.freeze", {"reason": "panic"})
    assert (await _dispatch(request, path)).status_code == 200
    assert fake_guard.last.headers["authorization"] == f"Bearer {AGENT_TOKEN}"


async def test_settings_update_refreshes_workspace(fake_guard: FakeGuard, workspace: Path) -> None:
    fake_guard.set("PATCH", "/settings", {"settings": {
        "network": "base-sepolia", "budgetUsd": "77", "earningMethods": ["bounties"],
    }, "restart_required": False})
    request, path = _mutation("cashmaxx.settings.update",
                              {"patch": {"budgetUsd": "77"}, "owner_session": "sess"})
    response = await _dispatch(request, path)
    assert response.status_code == 200
    patch = next(r for r in fake_guard.requests if r.method == "PATCH")
    assert json.loads(patch.content) == {"budgetUsd": "77"}
    assert "77.00 USD" in (workspace / "RULES.md").read_text()
    assert (workspace / "skills" / "cashmaxx-bounties").exists()
    assert not (workspace / "skills" / "cashmaxx-x402-apis").exists()


def test_mutation_path_mapping() -> None:
    assert routes.mutation_path("settings.agent.update", {}) is None
    bad = routes.mutation_path("cashmaxx.approve", {"approval_id": "../x"})
    assert isinstance(bad, Response) and bad.status_code == 400
    unknown = routes.mutation_path("cashmaxx.nope", {})
    assert isinstance(unknown, Response) and unknown.status_code == 404


def test_ws_http_knows_cashmaxx_actions() -> None:
    from nanobot.webui.ws_http import GatewayHTTPHandler

    path = GatewayHTTPHandler._webui_mutation_path("cashmaxx.deny", {"approval_id": "ap7"})
    assert path == "/api/cashmaxx/approvals/ap7/deny"
