"""Agent-kind integrations: MCP server config built from nanobot presets, and safe listings."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from cashmaxx import agent_integrations as ai
from nanobot.config.schema import Config

BB_KEY = "bb_live_SECRETKEY123"
GH_TOKEN = "github_pat_SECRET456"
BRAVE_KEY = "BSA_SECRET789"


@pytest.fixture(autouse=True)
def isolated_nanobot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep nanobot's runtime dirs (managed MCP cwd) under tmp, and no env fallbacks."""
    from nanobot.config import loader

    for var in ("BROWSERBASE_API_KEY", "GITHUB_PERSONAL_ACCESS_TOKEN", "BRAVE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    previous = loader._current_config_path  # pyright: ignore[reportPrivateUsage]
    loader.set_config_path(tmp_path / "nanobot" / "config.json")
    yield
    loader._current_config_path = previous  # pyright: ignore[reportPrivateUsage]


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config.model_validate({"agents": {"defaults": {"workspace": str(tmp_path / "ws")}}})


def _flag(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


def test_playwright_args_carry_the_security_flags(config: Config) -> None:
    assert ai.apply_agent_integration(config, "browser", {"provider": "playwright"}) == "playwright"
    cfg = config.tools.mcp_servers["playwright"]
    ws = config.workspace_path
    assert cfg.command == "npx"
    args = list(cfg.args)
    assert args[:2] == ["-y", ai.PLAYWRIGHT_MCP_PACKAGE]
    assert "@latest" not in " ".join(args)  # pinned: flags were verified against this version
    assert "--headless" in args
    assert _flag(args, "--user-data-dir") == str(ws / ai.BROWSER_PROFILE_DIR)
    assert _flag(args, "--output-dir") == str(ws / ai.BROWSER_FILES_DIR)
    blocked = _flag(args, "--blocked-origins").split(";")
    for origin in ("http://localhost:*", "http://127.0.0.1:*", "http://[::1]:*",
                   "http://169.254.169.254:*"):
        assert origin in blocked
    assert "--no-webmcp" in args and "--block-service-workers" in args
    for dangerous in ("--allow-unrestricted-file-access", "--extension", "--cdp-endpoint",
                      "--endpoint", "--isolated", "--no-sandbox", "--caps", "--config"):
        assert dangerous not in args
    # File access is confined to the server's cwd (and output dir), inside the workspace.
    assert cfg.cwd == str(ws / ai.BROWSER_FILES_DIR)
    assert (ws / ai.BROWSER_FILES_DIR).is_dir() and (ws / ai.BROWSER_PROFILE_DIR).is_dir()
    assert cfg.env["PLAYWRIGHT_MCP_ALLOW_UNRESTRICTED_FILE_ACCESS"] == "false"
    # Tool allowlist: no code execution in the server process, no WebMCP.
    tools = set(cfg.enabled_tools)
    assert "*" not in tools and "browser_navigate" in tools
    assert "browser_run_code_unsafe" not in tools
    assert not any(t.startswith("browser_webmcp") for t in tools)


def test_playwright_args_ignore_existing_unsafe_args(config: Config) -> None:
    ai.apply_agent_integration(config, "browser", {"provider": "playwright"})
    cfg = config.tools.mcp_servers["playwright"]
    config.tools.mcp_servers["playwright"] = cfg.model_copy(update={
        "args": [*cfg.args, "--allow-unrestricted-file-access", "--extension"],
        "enabled_tools": ["*"],
    })
    ai.apply_agent_integration(config, "browser", {})  # keeps provider, rebuilds the args
    cfg = config.tools.mcp_servers["playwright"]
    assert "--allow-unrestricted-file-access" not in cfg.args and "--extension" not in cfg.args
    assert "*" not in cfg.enabled_tools


def test_switching_browser_provider_keeps_one_server(config: Config) -> None:
    ai.apply_agent_integration(config, "browser", {"provider": "playwright"})
    ai.apply_agent_integration(config, "browser", {"provider": "browserbase",
                                                   "browserbaseApiKey": BB_KEY})
    assert "playwright" not in config.tools.mcp_servers
    assert ai.active_server(config, "browser") == "browserbase"
    assert BB_KEY in config.tools.mcp_servers["browserbase"].url  # where the preset keeps it


def test_bad_provider_and_missing_secret(config: Config) -> None:
    with pytest.raises(ai.AgentIntegrationError):
        ai.apply_agent_integration(config, "search", {"provider": "google"})
    with pytest.raises(ai.AgentIntegrationError):
        ai.apply_agent_integration(config, "github", {})
    with pytest.raises(ai.AgentIntegrationError):
        ai.apply_agent_integration(config, "gmail", {})


@pytest.mark.parametrize("owner", [False, True])
def test_listing_never_contains_secrets(config: Config, owner: bool) -> None:
    ai.apply_agent_integration(config, "browser", {"provider": "browserbase",
                                                   "browserbaseApiKey": BB_KEY})
    ai.apply_agent_integration(config, "github", {"token": GH_TOKEN})
    ai.apply_agent_integration(config, "search", {"provider": "brave-search", "apiKey": BRAVE_KEY})
    items = [ai.agent_integration_item(config, i, owner=owner) for i in ai.AGENT_INTEGRATION_IDS]
    dumped = json.dumps(items)
    for secret in (BB_KEY, GH_TOKEN, BRAVE_KEY, "mcp.browserbase.com"):
        assert secret not in dumped
    assert all(item["connected"] for item in items)
    if owner:
        browser = items[0]
        fields = {f["name"]: f for f in browser["fields"]}
        assert fields["provider"]["value"] == "browserbase"
        assert fields["browserbaseApiKey"]["set"] is True
        assert "value" not in fields["browserbaseApiKey"]
        github = {f["name"]: f for f in items[1]["fields"]}
        assert github["token"]["set"] is True
    else:
        assert set(items[0]) == {"id", "label", "kind", "category", "connected"}


def test_remove(config: Config) -> None:
    ai.apply_agent_integration(config, "search", {"provider": "exa"})
    assert ai.remove_agent_integration(config, "search") == ["exa"]
    assert ai.active_server(config, "search") is None
    item = ai.agent_integration_item(config, "search", owner=True)
    assert item["connected"] is False
    assert {f["name"]: f for f in item["fields"]}["provider"]["set"] is False
