"""Integration status for the agent side: listings, workspace refresh and MCP hot reload.

Guard-kind integrations are reported by the guard (``GET /integrations``). Agent-kind ones
(browser, GitHub, search) are MCP servers in nanobot's own config, read here.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from loguru import logger

from cashmaxx import plugin
from cashmaxx.agent_integrations import AGENT_INTEGRATION_IDS, agent_integration_item
from cashmaxx.config import CashmaxxSettings
from cashmaxx.guard.client import JSON, GuardClient, GuardError
from cashmaxx.integrations_catalog import BY_ID

if TYPE_CHECKING:
    from nanobot.config.schema import Config

MCP_RELOAD_TIMEOUT_S = 60.0
REDUCED_KEYS = ("id", "label", "kind", "category", "connected")
NO_RELOAD: dict[str, Any] = {
    "ok": False,
    "message": "MCP runtime reload is unavailable. Restart nanobot to apply changes.",
    "requires_restart": True,
}


def nanobot_config_path() -> Path:
    from nanobot.config.loader import get_config_path

    return get_config_path()


def load_nanobot_config(path: Path | None = None) -> Config:
    from nanobot.config.loader import load_config

    return load_config(path or nanobot_config_path())


def try_load_nanobot_config() -> Config | None:
    try:
        return load_nanobot_config()
    except Exception as exc:  # unreadable config: agent-kind status is unknown
        logger.warning("cashmaxx: could not read the nanobot config: {}", exc)
        return None


def _items(body: JSON) -> list[dict[str, Any]]:
    raw: object = body.get("integrations")
    if not isinstance(raw, list):
        return []
    return [cast(dict[str, Any], i) for i in cast(list[object], raw) if isinstance(i, dict)]


def reduced_listing(guard_body: JSON, config: Config | None) -> list[dict[str, Any]]:
    """Agent-scope listing: only id/label/kind/category/connected, agent kinds filled in."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in _items(guard_body):
        iid = str(item.get("id") or "")
        if not iid:
            continue
        seen.add(iid)
        entry = {k: item.get(k) for k in REDUCED_KEYS}
        if iid in AGENT_INTEGRATION_IDS:
            entry = (agent_integration_item(config, iid, owner=False) if config is not None
                     else {**entry, "connected": None})
        out.append(entry)
    for iid in AGENT_INTEGRATION_IDS:
        if iid not in seen and iid in BY_ID:
            out.append(agent_integration_item(config, iid, owner=False) if config is not None
                       else {"id": iid, "label": BY_ID[iid].label, "kind": "agent",
                             "category": BY_ID[iid].category, "connected": None})
    return out


def connected_map(items: list[dict[str, Any]]) -> dict[str, bool]:
    return {str(i["id"]): bool(i.get("connected")) for i in items if i.get("connected") is not None}


async def fetch_listing(client: GuardClient) -> list[dict[str, Any]]:
    """Reduced listing for both kinds. Raises ``GuardError`` when the guard can't answer."""
    body = await client.integrations()
    config = await asyncio.to_thread(try_load_nanobot_config)
    return reduced_listing(body, config)


async def refresh_workspace(settings: CashmaxxSettings | JSON | None = None) -> bool:
    """Re-render RULES.md and the gated skills from live settings and integrations.

    Never raises. Returns True when the workspace was rendered.
    """
    workspace = plugin.state.workspace
    cfg = plugin.resolve_agent_config()
    if workspace is None or cfg is None:
        return False
    connected: dict[str, bool] | None = None
    try:
        async with plugin.guard_client(cfg) as client:
            if settings is None:
                settings = await client.settings()
            try:
                connected = connected_map(await fetch_listing(client))
            except GuardError as exc:  # older guard or down: integrations unknown
                logger.debug("cashmaxx: integrations unavailable for the workspace: {}", exc)
        if isinstance(settings, dict):
            raw: object = settings.get("settings", settings)
            settings = CashmaxxSettings.model_validate(raw)
        from cashmaxx.plugin.workspace import install_workspace

        await asyncio.to_thread(install_workspace, workspace, settings, integrations=connected)
        return True
    except Exception as exc:
        logger.warning("cashmaxx: workspace refresh failed: {}", exc)
        return False


async def reload_mcp() -> dict[str, Any]:
    """Ask the running agent to reconnect its MCP servers (nanobot's ``MCPProvider.reload``)."""
    reload = plugin.state.mcp_reload
    if reload is None:
        return dict(NO_RELOAD)
    try:
        return await asyncio.wait_for(reload(), timeout=MCP_RELOAD_TIMEOUT_S)
    except asyncio.TimeoutError:
        return {"ok": False, "message": "MCP hot reload timed out. Restart nanobot to pick up "
                "changes.", "requires_restart": True}
    except Exception as exc:
        logger.exception("cashmaxx: MCP hot reload failed")
        return {"ok": False, "message": "MCP hot reload failed. Restart nanobot to pick up "
                "changes.", "requires_restart": True, "error": str(exc)}
