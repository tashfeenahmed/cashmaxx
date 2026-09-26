"""CLI integration commands against a fake guard (httpx MockTransport)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from cashmaxx import cli, plugin

runner = CliRunner()


@pytest.fixture(autouse=True)
def _restore_config_path(monkeypatch: pytest.MonkeyPatch) -> None:
    import nanobot.config.loader as loader

    monkeypatch.setattr(loader, "_current_config_path", loader._current_config_path)


class Guard:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.connected: dict[str, bool] = {"openrouter": True, "gmail": False, "hosting": False}
        self.test_ok = True
        self.owner_ok = True

    def _listing(self, owner: bool) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for iid, on in self.connected.items():
            item: dict[str, Any] = {"id": iid, "label": iid, "kind": "guard", "category": "x",
                                    "connected": on}
            if owner:
                item["last_test"] = {"ok": True, "message": "key works", "at": "t"} if on else None
            items.append(item)
        items.append({"id": "github", "label": "GitHub", "kind": "agent", "category": "code",
                      "connected": None})
        return {"integrations": items}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        method, path = request.method, request.url.path
        owner = request.headers.get("x-cashmaxx-owner") == "sess"
        if (method, path) == ("POST", "/owner/session"):
            if json.loads(request.content) != {"pin": "123456"}:
                return httpx.Response(401, json={"error": "bad_pin", "message": "wrong PIN"})
            return httpx.Response(200, json={"session": "sess", "expires_at": "x"})
        if (method, path) == ("GET", "/integrations"):
            return httpx.Response(200, json=self._listing(owner))
        if not owner:
            return httpx.Response(401, json={"error": "owner_required", "message": "PIN"})
        if (method, path) == ("GET", "/owner/check"):
            if not self.owner_ok:
                return httpx.Response(401, json={"error": "owner_required", "message": "expired"})
            return httpx.Response(200, json={"ok": True})
        if method == "PUT" and path.startswith("/integrations/"):
            iid = path.rsplit("/", 1)[1]
            self.connected[iid] = True
            return httpx.Response(200, json={"id": iid, "connected": True})
        if method == "POST" and path.endswith("/test"):
            return httpx.Response(200, json={"ok": self.test_ok,
                                             "message": "login ok" if self.test_ok else "bad password"})
        if method == "DELETE" and path.startswith("/integrations/"):
            self.connected[path.rsplit("/", 1)[1]] = False
            return httpx.Response(200, json={"ok": True})
        if (method, path) == ("PATCH", "/settings"):
            patch = json.loads(request.content)
            return httpx.Response(200, json={"settings": {"network": "base-sepolia", **patch},
                                             "restart_required": False})
        return httpx.Response(404, json={"error": "not_found", "message": path})

    def calls(self) -> list[tuple[str, str]]:
        return [(r.method, r.url.path) for r in self.requests]


@pytest.fixture
def env(tmp_path: Path) -> Iterator[tuple[Guard, Path, Path]]:
    guard = Guard()
    ws = tmp_path / "ws"
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "agents": {"defaults": {"workspace": str(ws)}},
        "cashmaxx": {"guardUrl": "http://guard.test", "agentToken": "tok"},
    }))
    plugin.reset()
    plugin.configure(None, transport=httpx.MockTransport(guard.handler))
    yield guard, cfg, ws
    plugin.reset()


def _servers(cfg: Path) -> dict[str, Any]:
    return json.loads(cfg.read_text()).get("tools", {}).get("mcpServers", {})


def test_help_lists_integration_commands() -> None:
    result = runner.invoke(cli.main, ["--help"])
    for cmd in ("integrations", "connect", "disconnect", "test"):
        assert cmd in result.output


def test_integrations_agent_scope(env: tuple[Guard, Path, Path]) -> None:
    guard, cfg, _ = env
    result = runner.invoke(cli.main, ["integrations", "-c", str(cfg)])
    assert result.exit_code == 0, result.output
    rows = {line.split("│")[1].strip(): line for line in result.output.splitlines()
            if line.count("│") >= 5}
    assert "yes" in rows["openrouter"] and "no" in rows["gmail"]
    assert "no" in rows["github"]  # agent kind: from the local nanobot config, not the guard
    assert "?" in rows["stripe"]  # not in the guard listing
    assert guard.requests[0].headers["authorization"] == "Bearer tok"
    assert "key works" not in result.output


def test_integrations_owner_view_shows_last_test(env: tuple[Guard, Path, Path]) -> None:
    guard, cfg, _ = env
    result = runner.invoke(cli.main, ["integrations", "-c", str(cfg), "--pin", "123456"])
    assert result.exit_code == 0, result.output
    assert "ok: key works" in result.output
    listing = guard.requests[-1]
    assert listing.headers["x-cashmaxx-owner"] == "sess" and "authorization" not in listing.headers


def test_unknown_integration(env: tuple[Guard, Path, Path]) -> None:
    _, cfg, _ = env
    result = runner.invoke(cli.main, ["connect", "myspace", "-c", str(cfg)])
    assert result.exit_code != 0 and "unknown integration" in result.output


def test_connect_gmail_updates_tests_and_sets_provider(env: tuple[Guard, Path, Path]) -> None:
    guard, cfg, ws = env
    # address, app password, PIN, use as email provider
    result = runner.invoke(cli.main, ["connect", "gmail", "-c", str(cfg)],
                           input="agent@gmail.com\napppassword16chr\n123456\ny\n")
    assert result.exit_code == 0, result.output
    put = next(r for r in guard.requests if r.method == "PUT")
    assert put.url.path == "/integrations/gmail"
    assert json.loads(put.content) == {"fields": {"address": "agent@gmail.com",
                                                  "appPassword": "apppassword16chr"}}
    assert ("POST", "/integrations/gmail/test") in guard.calls()
    assert "test passed: login ok" in result.output
    assert "week 1" in result.output and "10" in result.output
    patch = next(r for r in guard.requests if r.method == "PATCH")
    assert json.loads(patch.content) == {"emailProvider": "gmail"}
    assert "apppassword16chr" not in result.output
    # one owner session for the whole command
    assert guard.calls().count(("POST", "/owner/session")) == 1
    assert (ws / "RULES.md").exists()


def test_connect_existing_keeps_secrets_on_empty(env: tuple[Guard, Path, Path]) -> None:
    guard, cfg, _ = env
    guard.connected["gmail"] = True
    result = runner.invoke(cli.main, ["connect", "gmail", "-c", str(cfg), "--pin", "123456"],
                           input="\n\nn\n")
    assert result.exit_code == 0, result.output
    assert "leave empty to keep" in result.output
    put = next(r for r in guard.requests if r.method == "PUT")
    assert json.loads(put.content) == {"fields": {"appPassword": ""}}
    assert not any(r.method == "PATCH" for r in guard.requests)


def test_connect_required_field_reprompts(env: tuple[Guard, Path, Path]) -> None:
    guard, cfg, _ = env
    result = runner.invoke(cli.main, ["connect", "gmail", "-c", str(cfg), "--pin", "123456"],
                           input="\nagent@gmail.com\n\npw\nn\n")
    assert result.exit_code == 0, result.output
    put = next(r for r in guard.requests if r.method == "PUT")
    assert json.loads(put.content)["fields"] == {"address": "agent@gmail.com", "appPassword": "pw"}


def test_connect_hosting_offers_hosting_enabled(env: tuple[Guard, Path, Path]) -> None:
    guard, cfg, _ = env
    # provider select (quick tunnel skips the token), PIN, turn hosting on
    result = runner.invoke(cli.main, ["connect", "hosting", "-c", str(cfg)],
                           input="cloudflare_quick\n123456\ny\n")
    assert result.exit_code == 0, result.output
    put = next(r for r in guard.requests if r.method == "PUT")
    assert json.loads(put.content) == {"fields": {"provider": "cloudflare_quick"}}
    patch = next(r for r in guard.requests if r.method == "PATCH")
    assert json.loads(patch.content) == {"hostingEnabled": True}


def test_connect_social_mentions_cap(env: tuple[Guard, Path, Path]) -> None:
    _, cfg, _ = env
    result = runner.invoke(cli.main, ["connect", "bluesky", "-c", str(cfg), "--pin", "123456"],
                           input="me.bsky.social\napp-pw\n")
    assert result.exit_code == 0, result.output
    assert "socialDailyCap" in result.output


def test_connect_failed_test_is_reported(env: tuple[Guard, Path, Path]) -> None:
    guard, cfg, _ = env
    guard.test_ok = False
    result = runner.invoke(cli.main, ["connect", "bluesky", "-c", str(cfg), "--pin", "123456"],
                           input="me.bsky.social\napp-pw\n")
    assert result.exit_code == 0
    assert "test FAILED: bad password" in result.output


def test_connect_wrong_pin(env: tuple[Guard, Path, Path]) -> None:
    guard, cfg, _ = env
    result = runner.invoke(cli.main, ["connect", "bluesky", "-c", str(cfg), "--pin", "000000"],
                           input="me.bsky.social\napp-pw\n")
    assert result.exit_code == 1 and "bad_pin" in result.output
    assert not any(r.method == "PUT" for r in guard.requests)


def test_connect_agent_kind_writes_nanobot_config(env: tuple[Guard, Path, Path]) -> None:
    guard, cfg, _ = env
    result = runner.invoke(cli.main, ["connect", "github", "-c", str(cfg)],
                           input="github_pat_123\n123456\n")
    assert result.exit_code == 0, result.output
    assert ("GET", "/owner/check") in guard.calls()
    assert not any(r.method == "PUT" for r in guard.requests)
    server = _servers(cfg)["github"]
    assert server["env"]["GITHUB_PERSONAL_ACCESS_TOKEN"] == "github_pat_123"
    assert "MCP reload" in result.output
    assert json.loads(cfg.read_text())["cashmaxx"]["agentToken"] == "tok"


def test_connect_agent_kind_browser_uses_own_profile(env: tuple[Guard, Path, Path]) -> None:
    _, cfg, ws = env
    result = runner.invoke(cli.main, ["connect", "browser", "-c", str(cfg), "--pin", "123456"],
                           input="playwright\n")
    assert result.exit_code == 0, result.output
    args = _servers(cfg)["playwright"]["args"]
    assert str(ws / "browser-profile") in args


def test_connect_agent_kind_refused_without_owner_session(env: tuple[Guard, Path, Path]) -> None:
    guard, cfg, _ = env
    guard.owner_ok = False
    result = runner.invoke(cli.main, ["connect", "github", "-c", str(cfg), "--pin", "123456"],
                           input="github_pat_123\n")
    assert result.exit_code == 1
    assert "github" not in _servers(cfg)


def test_disconnect_guard_kind(env: tuple[Guard, Path, Path]) -> None:
    guard, cfg, _ = env
    guard.connected["gmail"] = True
    result = runner.invoke(cli.main, ["disconnect", "gmail", "-c", str(cfg)], input="y\n123456\n")
    assert result.exit_code == 0, result.output
    assert ("DELETE", "/integrations/gmail") in guard.calls()


def test_disconnect_can_be_declined(env: tuple[Guard, Path, Path]) -> None:
    guard, cfg, _ = env
    result = runner.invoke(cli.main, ["disconnect", "gmail", "-c", str(cfg)], input="n\n")
    assert result.exit_code == 1 and guard.requests == []


def test_disconnect_agent_kind(env: tuple[Guard, Path, Path]) -> None:
    guard, cfg, _ = env
    runner.invoke(cli.main, ["connect", "github", "-c", str(cfg), "--pin", "123456"],
                  input="github_pat_123\n")
    assert "github" in _servers(cfg)
    result = runner.invoke(cli.main, ["disconnect", "github", "-c", str(cfg), "--pin", "123456",
                                      "--yes"])
    assert result.exit_code == 0, result.output
    assert "github" not in _servers(cfg)
    assert guard.calls().count(("GET", "/owner/check")) == 2


def test_test_guard_kind(env: tuple[Guard, Path, Path]) -> None:
    guard, cfg, _ = env
    result = runner.invoke(cli.main, ["test", "gmail", "-c", str(cfg)], input="123456\n")
    assert result.exit_code == 0 and "passed" in result.output
    guard.test_ok = False
    result = runner.invoke(cli.main, ["test", "gmail", "-c", str(cfg), "--pin", "123456"])
    assert result.exit_code == 1 and "bad password" in result.output


def test_test_agent_kind_checks_local_config(env: tuple[Guard, Path, Path]) -> None:
    guard, cfg, _ = env
    result = runner.invoke(cli.main, ["test", "search", "-c", str(cfg)])
    assert result.exit_code == 1 and "cashmaxx connect search" in result.output
    runner.invoke(cli.main, ["connect", "search", "-c", str(cfg), "--pin", "123456"],
                  input="exa\n\n")
    result = runner.invoke(cli.main, ["test", "search", "-c", str(cfg)])
    assert result.exit_code == 0 and "'exa'" in result.output
