"""WebUI proxy: integration listing and the cashmaxx.integrations.* socket actions."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from cmx_plugin_testlib import AGENT_TOKEN, FakeGuard
from websockets.datastructures import Headers
from websockets.http11 import Request as WsRequest
from websockets.http11 import Response

from cashmaxx import plugin
from cashmaxx.plugin import webui_routes as routes
from nanobot.config.loader import load_config

BB_KEY = "bb_live_TOPSECRET"
GUARD_AGENT_LISTING = {"integrations": [
    {"id": "gmail", "label": "Gmail", "kind": "guard", "category": "email", "connected": True},
    {"id": "bluesky", "label": "Bluesky", "kind": "guard", "category": "social",
     "connected": False},
    {"id": "browser", "label": "Browser", "kind": "agent", "category": "browser",
     "connected": None},
]}


def _owner_item(iid: str, kind: str = "guard") -> dict[str, Any]:
    return {"id": iid, "label": iid, "kind": kind, "category": "email", "connected": True,
            "summary": "", "docs_url": "", "connected_at": None, "last_test": None,
            "fields": [{"name": "appPassword", "label": "p", "secret": True, "required": True,
                        "placeholder": "", "choices": [], "set": True, "value": "LEAKED"},
                       {"name": "address", "label": "a", "secret": False, "required": True,
                        "placeholder": "", "choices": [], "set": True, "value": "a@b.c"}]}


@pytest.fixture(autouse=True)
def nanobot_config(nanobot_config_path: Path, workspace: Path,
                   monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("BROWSERBASE_API_KEY", raising=False)
    monkeypatch.delenv("GITHUB_PERSONAL_ACCESS_TOKEN", raising=False)
    nanobot_config_path.write_text(json.dumps({
        "agents": {"defaults": {"workspace": str(workspace)}},
        "cashmaxx": {"guardUrl": "http://guard.test", "agentToken": AGENT_TOKEN},
    }), encoding="utf-8")
    return nanobot_config_path


@pytest.fixture
def reloads() -> Iterator[list[int]]:
    calls: list[int] = []

    async def fake_reload() -> dict[str, Any]:
        calls.append(1)
        return {"ok": True, "message": "MCP servers reloaded.", "requires_restart": False}

    plugin.state.mcp_reload = fake_reload
    yield calls
    plugin.state.mcp_reload = None
    routes._agent_tests.clear()  # pyright: ignore[reportPrivateUsage]


def _body(response: Response) -> dict[str, Any]:
    return json.loads(bytes(response.body))


async def _action(action: str, payload: dict[str, Any]) -> Response:
    path = routes.mutation_path(action, payload)
    if isinstance(path, Response):
        return path
    assert isinstance(path, str)
    request = WsRequest(path, Headers())
    setattr(request, "_nanobot_webui_mutation_request", True)
    setattr(request, "_nanobot_webui_mutation_payload", payload)
    response = await routes.dispatch(request, path, check_api_token=lambda _r: True)
    assert response is not None
    return response


def _servers(config_path: Path) -> dict[str, Any]:
    return dict(load_config(config_path).tools.mcp_servers)


# --- reduced listing ----------------------------------------------------------------------------


async def test_reduced_listing_fills_agent_kinds(fake_guard: FakeGuard, nanobot_config: Path,
                                                 reloads: list[int]) -> None:
    fake_guard.set("GET", "/owner/check", {"ok": True})
    await _action("cashmaxx.integrations.update",
                  {"id": "browser", "fields": {"provider": "browserbase",
                                               "browserbaseApiKey": BB_KEY},
                   "owner_session": "sess"})
    fake_guard.set("GET", "/integrations", GUARD_AGENT_LISTING)
    request = WsRequest("/api/cashmaxx/integrations", Headers())
    response = await routes.dispatch(request, "/api/cashmaxx/integrations",
                                     check_api_token=lambda _r: True)
    assert response is not None and response.status_code == 200
    items = {i["id"]: i for i in _body(response)["integrations"]}
    assert items["gmail"]["connected"] is True and items["bluesky"]["connected"] is False
    assert items["browser"]["connected"] is True  # from nanobot config, not the guard's null
    assert items["github"]["connected"] is False and items["search"]["connected"] is False
    for item in items.values():
        assert set(item) == {"id", "label", "kind", "category", "connected"}
    assert BB_KEY not in bytes(response.body).decode()
    assert fake_guard.last.headers["authorization"] == f"Bearer {AGENT_TOKEN}"


async def test_owner_listing_needs_session_and_hides_secrets(
    fake_guard: FakeGuard, nanobot_config: Path, reloads: list[int]
) -> None:
    assert (await _action("cashmaxx.integrations.list", {})).status_code == 401
    assert fake_guard.requests == []

    fake_guard.set("GET", "/owner/check", {"ok": True})
    await _action("cashmaxx.integrations.update",
                  {"id": "browser", "fields": {"provider": "browserbase",
                                               "browserbaseApiKey": BB_KEY},
                   "owner_session": "sess"})
    fake_guard.set("GET", "/integrations", {"integrations": [
        _owner_item("gmail"), {"id": "browser", "kind": "agent", "connected": None}]})
    response = await _action("cashmaxx.integrations.list", {"owner_session": "sess"})
    assert response.status_code == 200
    text = bytes(response.body).decode()
    assert "LEAKED" not in text and BB_KEY not in text and "mcp.browserbase.com" not in text
    items = {i["id"]: i for i in _body(response)["integrations"]}
    assert set(items) >= {"gmail", "browser", "github", "search"}
    assert items["browser"]["connected"] is True and items["browser"]["fields"]
    assert fake_guard.last.headers["x-cashmaxx-owner"] == "sess"
    assert "authorization" not in fake_guard.last.headers


# --- guard kind ---------------------------------------------------------------------------------


async def test_guard_kind_update_forwards_with_owner_session(fake_guard: FakeGuard) -> None:
    fake_guard.set("PUT", "/integrations/gmail", _owner_item("gmail"))
    response = await _action("cashmaxx.integrations.update",
                             {"id": "gmail", "fields": {"address": "a@b.c", "appPassword": "pw"},
                              "owner_session": "sess"})
    assert response.status_code == 200
    assert "LEAKED" not in bytes(response.body).decode()
    put = next(r for r in fake_guard.requests if r.method == "PUT")
    assert json.loads(put.content) == {"fields": {"address": "a@b.c", "appPassword": "pw"}}
    assert put.headers["x-cashmaxx-owner"] == "sess" and "authorization" not in put.headers


async def test_guard_kind_test_and_remove(fake_guard: FakeGuard) -> None:
    fake_guard.set("POST", "/integrations/bluesky/test", {"ok": False, "message": "bad password"})
    response = await _action("cashmaxx.integrations.test",
                             {"id": "bluesky", "owner_session": "sess"})
    assert _body(response) == {"ok": False, "message": "bad password"}
    fake_guard.set("DELETE", "/integrations/bluesky", {"ok": True})
    response = await _action("cashmaxx.integrations.remove",
                             {"id": "bluesky", "owner_session": "sess"})
    assert response.status_code == 200
    assert any(r.method == "DELETE" for r in fake_guard.requests)


async def test_guard_kind_needs_session(fake_guard: FakeGuard) -> None:
    response = await _action("cashmaxx.integrations.update",
                             {"id": "gmail", "fields": {"address": "a@b.c"}})
    assert response.status_code == 401 and fake_guard.requests == []


async def test_unknown_field_and_integration(fake_guard: FakeGuard) -> None:
    response = await _action("cashmaxx.integrations.update",
                             {"id": "gmail", "fields": {"smtpHost": "evil"},
                              "owner_session": "s"})
    assert response.status_code == 400
    response = await _action("cashmaxx.integrations.update", {"id": "../x", "fields": {}})
    assert response.status_code == 404
    assert fake_guard.requests == []


# --- agent kind ---------------------------------------------------------------------------------


async def test_agent_update_writes_config_and_reloads(
    fake_guard: FakeGuard, nanobot_config: Path, workspace: Path, reloads: list[int]
) -> None:
    fake_guard.set("GET", "/owner/check", {"ok": True})
    response = await _action("cashmaxx.integrations.update",
                             {"id": "browser", "fields": {"provider": "playwright"},
                              "owner_session": "sess"})
    assert response.status_code == 200, _body(response)
    body = _body(response)
    assert body["connected"] is True and body["hot_reload"]["ok"] is True
    assert body["requires_restart"] is False
    assert reloads == [1]
    check = fake_guard.requests[0]
    assert check.url.path == "/owner/check" and check.headers["x-cashmaxx-owner"] == "sess"
    servers = _servers(nanobot_config)
    assert set(servers) == {"playwright"}
    assert "--blocked-origins" in servers["playwright"].args
    assert str(workspace / "browser-profile") in servers["playwright"].args
    # The rest of the config survived the write.
    saved = json.loads(nanobot_config.read_text())
    assert saved["cashmaxx"]["guardUrl"] == "http://guard.test"


async def test_agent_update_with_bad_session_writes_nothing(
    fake_guard: FakeGuard, nanobot_config: Path, reloads: list[int]
) -> None:
    before = nanobot_config.read_text()
    fake_guard.set("GET", "/owner/check", {"error": "owner_required", "message": "bad"},
                   status=401)
    response = await _action("cashmaxx.integrations.update",
                             {"id": "github", "fields": {"token": "github_pat_x"},
                              "owner_session": "wrong"})
    assert response.status_code == 401
    assert nanobot_config.read_text() == before and reloads == []
    response = await _action("cashmaxx.integrations.remove", {"id": "github"})
    assert response.status_code == 401  # no session at all: never reaches the guard
    assert nanobot_config.read_text() == before


async def test_agent_update_guard_down_writes_nothing(
    fake_guard: FakeGuard, nanobot_config: Path, reloads: list[int]
) -> None:
    before = nanobot_config.read_text()
    fake_guard.down = True
    response = await _action("cashmaxx.integrations.update",
                             {"id": "search", "fields": {"provider": "exa"},
                              "owner_session": "sess"})
    assert response.status_code == 503
    assert nanobot_config.read_text() == before and reloads == []


async def test_agent_update_invalid_provider(fake_guard: FakeGuard, nanobot_config: Path,
                                             reloads: list[int]) -> None:
    before = nanobot_config.read_text()
    fake_guard.set("GET", "/owner/check", {"ok": True})
    response = await _action("cashmaxx.integrations.update",
                             {"id": "search", "fields": {"provider": "google"},
                              "owner_session": "sess"})
    assert response.status_code == 400
    assert nanobot_config.read_text() == before


async def test_agent_remove(fake_guard: FakeGuard, nanobot_config: Path,
                            reloads: list[int]) -> None:
    fake_guard.set("GET", "/owner/check", {"ok": True})
    await _action("cashmaxx.integrations.update",
                  {"id": "search", "fields": {"provider": "exa"}, "owner_session": "s"})
    assert "exa" in _servers(nanobot_config)
    response = await _action("cashmaxx.integrations.remove", {"id": "search",
                                                              "owner_session": "s"})
    assert response.status_code == 200 and _body(response)["connected"] is False
    assert "exa" not in _servers(nanobot_config)
    assert reloads == [1, 1]


async def test_agent_update_without_reload_hook_says_restart(
    fake_guard: FakeGuard, nanobot_config: Path
) -> None:
    fake_guard.set("GET", "/owner/check", {"ok": True})
    plugin.state.mcp_reload = None
    response = await _action("cashmaxx.integrations.update",
                             {"id": "search", "fields": {"provider": "exa"},
                              "owner_session": "s"})
    assert response.status_code == 200 and _body(response)["requires_restart"] is True


async def test_agent_test_runs_nanobot_mcp_test(
    fake_guard: FakeGuard, nanobot_config: Path, reloads: list[int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nanobot.webui.mcp_presets_api as presets

    seen: list[Any] = []

    async def fake_test(query: dict[str, list[str]], *, config_path: Path | None = None
                        ) -> dict[str, Any]:
        seen.append((query, config_path))
        return {"last_action": {"ok": True, "message": "Exa connected with 3 tools.",
                                "tool_count": 3}}

    monkeypatch.setattr(presets, "mcp_presets_test_action", fake_test)
    fake_guard.set("GET", "/owner/check", {"ok": True})

    response = await _action("cashmaxx.integrations.test", {"id": "search",
                                                            "owner_session": "s"})
    assert response.status_code == 409  # not connected yet

    await _action("cashmaxx.integrations.update",
                  {"id": "search", "fields": {"provider": "exa"}, "owner_session": "s"})
    response = await _action("cashmaxx.integrations.test", {"id": "search",
                                                            "owner_session": "s"})
    body = _body(response)
    assert body["ok"] is True and body["tool_count"] == 3 and body["server"] == "exa"
    assert seen == [({"name": ["exa"]}, nanobot_config)]

    fake_guard.set("GET", "/integrations", {"integrations": []})
    listing = _body(await _action("cashmaxx.integrations.list", {"owner_session": "s"}))
    search = next(i for i in listing["integrations"] if i["id"] == "search")
    assert search["last_test"]["ok"] is True


async def test_agent_test_needs_owner(fake_guard: FakeGuard, nanobot_config: Path) -> None:
    fake_guard.set("GET", "/owner/check", {"error": "owner_required", "message": "no"},
                   status=401)
    response = await _action("cashmaxx.integrations.test", {"id": "browser",
                                                            "owner_session": "bad"})
    assert response.status_code == 401


async def test_integration_change_refreshes_workspace(
    fake_guard: FakeGuard, nanobot_config: Path, workspace: Path, reloads: list[int]
) -> None:
    fake_guard.set("GET", "/owner/check", {"ok": True})
    fake_guard.set("GET", "/settings", {"settings": {"network": "base-sepolia"}})
    fake_guard.set("GET", "/integrations", GUARD_AGENT_LISTING)
    await _action("cashmaxx.integrations.update",
                  {"id": "browser", "fields": {"provider": "playwright"},
                   "owner_session": "s"})
    assert (workspace / "skills" / "cashmaxx-browser" / "SKILL.md").exists()
    rules = (workspace / "RULES.md").read_text()
    assert "Connected integrations: Gmail, Browser." in rules


def test_mutation_paths() -> None:
    assert routes.mutation_path("cashmaxx.integrations.list", {}) == \
        "/api/cashmaxx/integrations/owner"
    assert routes.mutation_path("cashmaxx.integrations.test", {"id": "github"}) == \
        "/api/cashmaxx/integrations/github/test"
    assert routes.is_mutation_path("/api/cashmaxx/integrations/owner")
    assert routes.is_mutation_path("/api/cashmaxx/integrations/gmail/remove")
    assert not routes.is_mutation_path("/api/cashmaxx/integrations")

    from nanobot.webui.ws_http import GatewayHTTPHandler

    assert GatewayHTTPHandler._webui_mutation_path(  # pyright: ignore[reportPrivateUsage]
        "cashmaxx.integrations.update", {"id": "browser"}
    ) == "/api/cashmaxx/integrations/browser/update"


async def test_plain_get_of_owner_paths_is_refused(fake_guard: FakeGuard) -> None:
    for path in ("/api/cashmaxx/integrations/owner", "/api/cashmaxx/integrations/gmail/remove"):
        response = await routes.dispatch(WsRequest(path, Headers()), path,
                                         check_api_token=lambda _r: True)
        assert response is not None and response.status_code == 405
    assert fake_guard.requests == []
