"""Agent-kind integrations (browser, GitHub, search): nanobot MCP servers built from its presets.

Shared by the WebUI proxy (``cashmaxx.plugin.webui_routes``) and the CLI. These functions only
change the in-memory ``Config``; callers confirm the owner session first, then save the config
and ask the running agent to reconnect its MCP servers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cashmaxx.integrations_catalog import IntegrationSpec, get_spec
from nanobot.config.schema import Config, MCPServerConfig
from nanobot.webui.mcp_presets_api import (
    McpPresetError,
    _materialize_server,  # pyright: ignore[reportPrivateUsage]
    _preset_by_name,  # pyright: ignore[reportPrivateUsage]
)

BROWSER_PROFILE_DIR = "browser-profile"
SEARCH_PROVIDERS = ("brave-search", "exa", "firecrawl")
BROWSER_PROVIDERS = ("playwright", "browserbase")
# Catalog field -> nanobot preset field, per MCP server name.
_PRESET_FIELDS: dict[str, dict[str, str]] = {
    "browserbase": {"browserbaseApiKey": "browserbase_api_key"},
    "github": {"token": "github_token"},
    "brave-search": {"apiKey": "brave_api_key"},
}


class AgentIntegrationError(ValueError):
    pass


def _servers_for(integration_id: str) -> tuple[str, ...]:
    if integration_id == "browser":
        return BROWSER_PROVIDERS
    if integration_id == "github":
        return ("github",)
    if integration_id == "search":
        return SEARCH_PROVIDERS
    raise AgentIntegrationError(f"{integration_id!r} is not an agent integration")


def _agent_spec(integration_id: str) -> IntegrationSpec:
    spec = get_spec(integration_id)
    if spec.kind != "agent":
        raise AgentIntegrationError(f"{integration_id!r} is a guard integration")
    return spec


def active_server(config: Config, integration_id: str) -> str | None:
    """The configured MCP server backing this integration, if any."""
    for name in _servers_for(integration_id):
        if name in config.tools.mcp_servers:
            return name
    return None


def _playwright_server(base: MCPServerConfig, workspace: Path) -> MCPServerConfig:
    """Own headless profile inside the workspace, never the owner's browser profile."""
    args = [a for a in base.args if a not in ("--headless", "--isolated")]
    args += ["--headless", "--user-data-dir", str(workspace / BROWSER_PROFILE_DIR)]
    return base.model_copy(update={"args": args})


def apply_agent_integration(config: Config, integration_id: str, fields: dict[str, str]) -> str:
    """Add or replace the MCP server for an agent integration. Returns the server name.

    Other servers of the same integration (e.g. the previous browser provider) are removed so
    the agent has exactly one browser and one search provider.
    """
    _agent_spec(integration_id)
    if integration_id == "github":
        server = "github"
    else:
        server = (fields.get("provider") or "").strip() or (
            "playwright" if integration_id == "browser" else "exa"
        )
        allowed = BROWSER_PROVIDERS if integration_id == "browser" else SEARCH_PROVIDERS
        if server not in allowed:
            raise AgentIntegrationError(f"provider must be one of {', '.join(allowed)}")

    query: dict[str, list[str]] = {}
    for ours, theirs in _PRESET_FIELDS.get(server, {}).items():
        value = (fields.get(ours) or "").strip()
        if value:
            query[theirs] = [value]
    existing = config.tools.mcp_servers.get(server)
    try:
        cfg = _materialize_server(_preset_by_name(server), query, existing)
    except McpPresetError as exc:
        raise AgentIntegrationError(str(exc)) from exc
    if server == "playwright":
        cfg = _playwright_server(cfg, config.workspace_path)

    for other in _servers_for(integration_id):
        if other != server:
            config.tools.mcp_servers.pop(other, None)
    config.tools.mcp_servers[server] = cfg
    return server


def remove_agent_integration(config: Config, integration_id: str) -> list[str]:
    """Remove every MCP server backing this integration. Returns the removed names."""
    removed = [n for n in _servers_for(integration_id) if n in config.tools.mcp_servers]
    for name in removed:
        del config.tools.mcp_servers[name]
    return removed


def agent_integration_item(config: Config, integration_id: str, *, owner: bool) -> dict[str, Any]:
    """Listing entry in the same shape as the guard's ``GET /integrations`` items."""
    spec = _agent_spec(integration_id)
    server = active_server(config, integration_id)
    item: dict[str, Any] = {
        "id": spec.id, "label": spec.label, "kind": spec.kind, "category": spec.category,
        "connected": server is not None,
    }
    if not owner:
        return item
    cfg = config.tools.mcp_servers.get(server) if server else None
    fields: list[dict[str, Any]] = []
    for f in spec.fields:
        entry: dict[str, Any] = {
            "name": f.name, "label": f.label, "secret": f.secret, "required": f.required,
            "placeholder": f.placeholder, "choices": list(f.choices),
        }
        if f.name == "provider":
            entry["set"] = server is not None
            entry["value"] = server
        else:
            preset_field = _PRESET_FIELDS.get(server or "", {}).get(f.name)
            entry["set"] = bool(cfg and preset_field and _secret_present(cfg))
        fields.append(entry)
    item.update({
        "summary": spec.summary, "docs_url": spec.docs_url, "fields": fields,
        "connected_at": None, "last_test": None,
    })
    return item


def _secret_present(cfg: MCPServerConfig) -> bool:
    return bool(cfg.env or cfg.headers or "Key=" in cfg.url or "key=" in cfg.url)
