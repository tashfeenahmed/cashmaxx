"""Cashmaxx's nanobot plugin: tools, slash commands, WebUI proxy routes and workspace templates.

The agent side only knows ``CashmaxxAgentConfig`` (guard URL + agent token). Every call to the
guard goes through :class:`cashmaxx.guard.client.GuardClient`.

Tools are registered by nanobot's tool loader through the ``nanobot.tools`` entry points, while
the ``AgentLoop`` is being built. ``ToolContext.config`` is nanobot's ``ToolsConfig`` (not the root
config), so tools resolve the Cashmaxx config through :func:`resolve_agent_config`, which reads the
active nanobot config file. :func:`install` runs later in the gateway and pins the config that was
actually loaded, then registers the slash commands and the WebUI proxy routes.

This module stays import-light: ``nanobot.webui.ws_http`` imports ``webui_routes`` at start-up.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import httpx
from loguru import logger

from cashmaxx.config import CashmaxxAgentConfig
from cashmaxx.guard.client import GuardClient

if TYPE_CHECKING:
    from nanobot.agent.loop import AgentLoop
    from nanobot.config.schema import Config
    from nanobot.cron.service import CronService

__all__ = [
    "PluginState",
    "configure",
    "guard_client",
    "install",
    "reset",
    "resolve_agent_config",
    "state",
]


@dataclass
class PluginState:
    agent_config: CashmaxxAgentConfig | None = None
    workspace: Path | None = None
    # Test seam: an ``httpx.MockTransport`` used for every GuardClient the plugin builds.
    transport: httpx.AsyncBaseTransport | None = None
    installed: bool = False


state = PluginState()
_file_cache: dict[str, tuple[float, CashmaxxAgentConfig | None]] = {}


def configure(
    agent_config: CashmaxxAgentConfig | None,
    *,
    workspace: Path | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """Pin the agent config (and optionally a workspace / test transport)."""
    state.agent_config = agent_config
    if workspace is not None:
        state.workspace = workspace
    if transport is not None:
        state.transport = transport


def reset() -> None:
    """Forget all pinned state (tests)."""
    fresh = PluginState()
    state.agent_config = fresh.agent_config
    state.workspace = fresh.workspace
    state.transport = fresh.transport
    state.installed = fresh.installed
    _file_cache.clear()


def _read_agent_config_from_file(path: Path) -> CashmaxxAgentConfig | None:
    """Read ``cashmaxx`` from a nanobot config file without the loader's global side effects."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    cached = _file_cache.get(str(path))
    if cached is not None and cached[0] == mtime:
        return cached[1]
    result: CashmaxxAgentConfig | None = None
    try:
        from nanobot.config.loader import resolve_env_refs

        data: object = json.loads(path.read_text(encoding="utf-8"))
        raw: object = cast(dict[str, object], data).get("cashmaxx") if isinstance(data, dict) else None
        if isinstance(raw, dict):
            resolved = {
                str(key): resolve_env_refs(value)
                for key, value in cast(dict[object, object], raw).items()
            }
            result = CashmaxxAgentConfig.model_validate(resolved)
    except Exception as exc:  # malformed file or missing env var: Cashmaxx stays off
        logger.warning("cashmaxx: could not read agent config from {}: {}", path, exc)
        result = None
    _file_cache[str(path)] = (mtime, result)
    return result


def resolve_agent_config(ctx: Any = None) -> CashmaxxAgentConfig | None:
    """Return the active Cashmaxx agent config, or ``None`` when Cashmaxx is not configured."""
    if state.agent_config is not None:
        return state.agent_config
    direct = getattr(getattr(ctx, "config", None), "cashmaxx", None)
    if isinstance(direct, CashmaxxAgentConfig):
        return direct
    from nanobot.config.loader import get_config_path

    return _read_agent_config_from_file(get_config_path())


def guard_client(
    agent_config: CashmaxxAgentConfig,
    *,
    owner_session: str | None = None,
    use_agent_token: bool = True,
) -> GuardClient:
    """Build a GuardClient. Owner calls pass ``owner_session`` and ``use_agent_token=False``."""
    return GuardClient(
        agent_config.guard_url,
        agent_token=agent_config.agent_token if use_agent_token else None,
        owner_session=owner_session,
        timeout=agent_config.request_timeout_s,
        transport=state.transport,
    )


def install(agent_loop: AgentLoop, cron_service: CronService | None, config: Config) -> None:
    """Gateway hook: pin the config, register slash commands and enable the WebUI proxy routes.

    The money loop itself runs on nanobot's heartbeat (``<workspace>/HEARTBEAT.md``), so no extra
    cron job is registered here. ``cron_service`` is accepted for the documented signature.
    """
    del cron_service  # the heartbeat system job already drives the loop
    agent_config = getattr(config, "cashmaxx", None)
    if not isinstance(agent_config, CashmaxxAgentConfig):
        return
    from cashmaxx.plugin.commands import register_commands

    configure(agent_config, workspace=config.workspace_path)
    register_commands(agent_loop.commands)
    state.installed = True
    if not agent_config.agent_token:
        logger.warning("cashmaxx: no agent token configured; the guard will reject every call")
    if not config.gateway.heartbeat.enabled:
        logger.warning(
            "cashmaxx: gateway.heartbeat is disabled, so the money loop in HEARTBEAT.md never runs"
        )
    logger.info("cashmaxx: plugin installed (guard {})", agent_config.guard_url)
