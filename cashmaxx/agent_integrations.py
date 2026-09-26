"""Agent-kind integrations (browser, GitHub, search): nanobot MCP servers built from its presets.

Shared by the WebUI proxy (``cashmaxx.plugin.webui_routes``), onboarding and the CLI. These
functions change the in-memory ``Config`` (and, for the browser, create its folders in the
workspace); callers confirm the owner session first, then save the config and ask the running
agent to reconnect its MCP servers.

Browser security. The Playwright MCP server runs outside nanobot's exec sandbox, so what it may
touch is set here, not left to the preset defaults (verified against ``@playwright/mcp`` 0.0.82):

- The package is pinned (``PLAYWRIGHT_MCP_PACKAGE``) so a new release cannot change the defaults
  under us. Bump it only after re-checking the flags below.
- ``--allow-unrestricted-file-access`` is never passed (and its env override is forced off), so
  ``file://`` navigation is refused and file reads/writes (uploads, screenshots, PDFs, storage
  state) are confined to the server's cwd and ``--output-dir``, both ``<workspace>/browser-files``.
- The server gets a tool allowlist (``enabled_tools``). ``browser_run_code_unsafe`` (arbitrary
  JavaScript in the server process, i.e. code execution outside the sandbox) and the WebMCP tools
  are not on it; WebMCP is also switched off with ``--no-webmcp``.
- Loopback and cloud-metadata origins are blocked (``--blocked-origins``) so pages cannot drive
  the local WebUI, gateway or guard. Playwright documents this as defence in depth, not a
  boundary; the guard and WebUI still require their own tokens.
- Its own persistent profile in ``<workspace>/browser-profile``, headless, never the owner's
  Chrome (no ``--extension``, ``--cdp-endpoint`` or ``--endpoint``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cashmaxx.integrations_catalog import IntegrationSpec, get_spec
from nanobot.config.schema import Config, MCPServerConfig
from nanobot.webui.mcp_presets_api import (
    McpPresetError,
    _field_value_from_config,  # pyright: ignore[reportPrivateUsage]
    _materialize_server,  # pyright: ignore[reportPrivateUsage]
    _preset_by_name,  # pyright: ignore[reportPrivateUsage]
)

BROWSER_PROFILE_DIR = "browser-profile"
BROWSER_FILES_DIR = "browser-files"
PLAYWRIGHT_MCP_PACKAGE = "@playwright/mcp@0.0.82"
SEARCH_PROVIDERS = ("brave-search", "exa", "firecrawl")
BROWSER_PROVIDERS = ("playwright", "browserbase")
AGENT_INTEGRATION_IDS = ("browser", "github", "search")

# Origins the local browser must never request: the WebUI/gateway/guard on loopback, and the
# cloud metadata service.
PLAYWRIGHT_BLOCKED_ORIGINS = (
    "http://localhost:*",
    "https://localhost:*",
    "ws://localhost:*",
    "http://127.0.0.1:*",
    "https://127.0.0.1:*",
    "ws://127.0.0.1:*",
    "http://[::1]:*",
    "https://[::1]:*",
    "http://0.0.0.0:*",
    "http://169.254.169.254:*",
)

# Tools the agent gets from the local Playwright MCP (its default "core" set in 0.0.82 minus
# the unsafe ones). Deliberately an allowlist: new or unsafe tools (``browser_run_code_unsafe``,
# ``browser_webmcp_*``, ``browser_drop``, tracing/video/devtools) stay off.
PLAYWRIGHT_ENABLED_TOOLS = (
    "browser_navigate",
    "browser_navigate_back",
    "browser_snapshot",
    "browser_take_screenshot",
    "browser_click",
    "browser_hover",
    "browser_drag",
    "browser_type",
    "browser_press_key",
    "browser_fill_form",
    "browser_select_option",
    "browser_find",
    "browser_handle_dialog",
    "browser_file_upload",
    "browser_wait_for",
    "browser_tabs",
    "browser_resize",
    "browser_close",
    "browser_console_messages",
    "browser_network_requests",
    "browser_network_request",
    "browser_evaluate",
)

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


def is_agent_integration(integration_id: str) -> bool:
    try:
        return get_spec(integration_id).kind == "agent"
    except KeyError:
        return False


def active_server(config: Config, integration_id: str) -> str | None:
    """The configured MCP server backing this integration, if any."""
    for name in _servers_for(integration_id):
        if name in config.tools.mcp_servers:
            return name
    return None


def playwright_args(workspace: Path) -> list[str]:
    """The npx arguments for the local Playwright MCP server (see the module docstring).

    Built from scratch: preset or hand-edited args are never carried over, so flags such as
    ``--allow-unrestricted-file-access``, ``--extension`` or ``--cdp-endpoint`` cannot survive.
    """
    return [
        "-y", PLAYWRIGHT_MCP_PACKAGE,
        "--headless",
        "--user-data-dir", str(workspace / BROWSER_PROFILE_DIR),
        "--output-dir", str(workspace / BROWSER_FILES_DIR),
        "--blocked-origins", ";".join(PLAYWRIGHT_BLOCKED_ORIGINS),
        "--block-service-workers",
        "--no-webmcp",
    ]


def _playwright_server(base: MCPServerConfig, workspace: Path) -> MCPServerConfig:
    """Own headless profile and file root inside the workspace, never the owner's browser."""
    files_dir = workspace / BROWSER_FILES_DIR
    for folder in (workspace / BROWSER_PROFILE_DIR, files_dir):
        folder.mkdir(parents=True, exist_ok=True)
    env = {
        k: v for k, v in base.env.items() if not k.startswith("PLAYWRIGHT_MCP_")
    }
    # stdio servers only inherit a few safe variables, but pin this one off regardless.
    env["PLAYWRIGHT_MCP_ALLOW_UNRESTRICTED_FILE_ACCESS"] = "false"
    return base.model_copy(update={
        "command": "npx",
        "args": playwright_args(workspace),
        # The server confines file access to its cwd and output dir.
        "cwd": str(files_dir),
        "env": env,
        "enabled_tools": list(PLAYWRIGHT_ENABLED_TOOLS),
    })


def apply_agent_integration(config: Config, integration_id: str, fields: dict[str, str]) -> str:
    """Add or replace the MCP server for an agent integration. Returns the server name.

    Other servers of the same integration (e.g. the previous browser provider) are removed so
    the agent has exactly one browser and one search provider.
    """
    _agent_spec(integration_id)
    if integration_id == "github":
        server = "github"
    else:
        current = active_server(config, integration_id)
        server = (fields.get("provider") or "").strip() or current or (
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


def _preset_field_set(server: str, preset_field: str, cfg: MCPServerConfig | None) -> bool:
    if cfg is None:
        return False
    try:
        preset = _preset_by_name(server)
    except McpPresetError:
        return False
    for field in preset.fields:
        if field.name == preset_field:
            return bool(_field_value_from_config(field, cfg))
    return False


def agent_integration_item(config: Config, integration_id: str, *, owner: bool) -> dict[str, Any]:
    """Listing entry in the same shape as the guard's ``GET /integrations`` items.

    Never includes a secret value, a server URL (Browserbase keeps its key in the URL), env or
    headers: secret fields only report ``set``.
    """
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
            entry["set"] = bool(server and preset_field
                                and _preset_field_set(server, preset_field, cfg))
        fields.append(entry)
    item.update({
        "summary": spec.summary, "docs_url": spec.docs_url, "fields": fields,
        "server": server, "connected_at": None, "last_test": None,
    })
    return item
