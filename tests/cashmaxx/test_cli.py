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
        self.down = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.down:
            raise httpx.ConnectError("refused", request=request)
        path = request.url.path
        routes: dict[tuple[str, str], dict[str, Any]] = {
            ("GET", "/health"): {"ok": True, "frozen": False},
            ("GET", "/wallet"): {"address": "0xabc", "network": "base-sepolia",
                                 "balance_usdc": "1.00", "available_budget_usd": "9.00"},
            ("GET", "/ledger"): {"income": "0.00", "costs": "0.50", "net": "-0.50"},
            ("GET", "/approvals"): {"approvals": []},
            ("GET", "/settings"): {"settings": {"network": "base-sepolia", "budgetUsd": "10"}},
            ("POST", "/freeze"): {"frozen": True},
            ("POST", "/owner/session"): {"session": "sess", "expires_at": "x"},
            ("POST", "/unfreeze"): {"frozen": False},
            ("PATCH", "/settings"): {"settings": {"network": "base-sepolia", "dailyCapUsd": "25"},
                                     "restart_required": False},
        }
        if (request.method, path) not in routes:
            return httpx.Response(404, json={"error": "not_found", "message": path})
        return httpx.Response(200, json=routes[(request.method, path)])


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


def test_help_lists_commands() -> None:
    result = runner.invoke(cli.main, ["--help"])
    assert result.exit_code == 0
    for cmd in ("onboard", "guard", "status", "freeze", "unfreeze", "settings"):
        assert cmd in result.output


def test_status(env: tuple[Guard, Path, Path]) -> None:
    guard, cfg, _ = env
    result = runner.invoke(cli.main, ["status", "-c", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "0xabc" in result.output and "net -0.50" in result.output
    assert guard.requests[0].headers["authorization"] == "Bearer tok"


def test_status_guard_down(env: tuple[Guard, Path, Path]) -> None:
    guard, cfg, _ = env
    guard.down = True
    result = runner.invoke(cli.main, ["status", "-c", str(cfg)])
    assert result.exit_code == 2
    assert "Guard unreachable" in result.output


def test_status_without_config(tmp_path: Path) -> None:
    result = runner.invoke(cli.main, ["status", "-c", str(tmp_path / "missing.json")])
    assert result.exit_code == 1 and "cashmaxx onboard" in result.output


def test_freeze(env: tuple[Guard, Path, Path]) -> None:
    guard, cfg, _ = env
    result = runner.invoke(cli.main, ["freeze", "looks wrong", "-c", str(cfg)])
    assert result.exit_code == 0 and "frozen" in result.output
    assert json.loads(guard.requests[-1].content) == {"reason": "looks wrong"}


def test_unfreeze_uses_owner_session_not_agent_token(env: tuple[Guard, Path, Path]) -> None:
    guard, cfg, _ = env
    result = runner.invoke(cli.main, ["unfreeze", "-c", str(cfg)], input="123456\n")
    assert result.exit_code == 0, result.output
    session_req, unfreeze_req = guard.requests[-2:]
    assert json.loads(session_req.content) == {"pin": "123456"}
    assert "authorization" not in session_req.headers
    assert unfreeze_req.headers["x-cashmaxx-owner"] == "sess"
    assert "authorization" not in unfreeze_req.headers


def test_settings_show(env: tuple[Guard, Path, Path]) -> None:
    _, cfg, _ = env
    result = runner.invoke(cli.main, ["settings", "show", "-c", str(cfg)])
    assert result.exit_code == 0 and '"budgetUsd": "10"' in result.output


def test_settings_set_patches_and_refreshes_workspace(env: tuple[Guard, Path, Path]) -> None:
    guard, cfg, ws = env
    result = runner.invoke(cli.main, ["settings", "set", "daily_cap_usd", "25", "-c", str(cfg),
                                      "--pin", "123456"])
    assert result.exit_code == 0, result.output
    assert json.loads(guard.requests[-1].content) == {"dailyCapUsd": "25"}
    assert "25.00 USD" in (ws / "RULES.md").read_text()


def test_parse_setting_value() -> None:
    assert cli.parse_setting_value("earningMethods", "bounties, x402_apis") == (
        "earningMethods", ["bounties", "x402_apis"])
    assert cli.parse_setting_value("public_pnl", "true") == ("publicPnl", True)
    assert cli.parse_setting_value("budgetUsd", "12.5") == ("budgetUsd", "12.5")
    import typer

    with pytest.raises(typer.BadParameter):
        cli.parse_setting_value("nope", "1")
    with pytest.raises(typer.BadParameter):
        cli.parse_setting_value("frozen", "false")


def test_guard_command_calls_guard_run(monkeypatch: pytest.MonkeyPatch) -> None:
    import cashmaxx.guard.app as app

    calls: list[Any] = []
    monkeypatch.setattr(app, "run", lambda path=None: calls.append(path))
    result = runner.invoke(cli.main, ["guard"])
    assert result.exit_code == 0 and calls == [None]


def test_onboard_command_delegates(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import cashmaxx.onboarding as ob

    seen: dict[str, Any] = {}
    monkeypatch.setattr(ob, "run_onboarding", lambda **kw: seen.update(kw))
    result = runner.invoke(cli.main, ["onboard", "-c", str(tmp_path / "c.json"), "--force"])
    assert result.exit_code == 0
    assert seen == {"nanobot_config_path": tmp_path / "c.json", "force_workspace": True}
