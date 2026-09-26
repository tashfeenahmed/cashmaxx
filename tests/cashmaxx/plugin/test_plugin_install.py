"""Gateway smoke test: ``install()`` wires commands onto a real AgentLoop."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from cashmaxx import plugin
from cashmaxx.config import CashmaxxAgentConfig
from nanobot.agent.loop import AgentLoop
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import Config


def _loop(workspace: Path) -> AgentLoop:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    return AgentLoop(bus=MessageBus(), provider=provider, workspace=workspace, model="test-model")


def test_install_registers_commands_and_tools(workspace: Path) -> None:
    loop = _loop(workspace)
    # The fixture pins a config, so the entry-point tools were loaded with the loop.
    for name in ("cashmaxx_wallet", "cashmaxx_pay", "cashmaxx_freeze"):
        assert loop.tools.has(name)

    config = Config.model_validate({
        "agents": {"defaults": {"workspace": str(workspace)}},
        "cashmaxx": {"guardUrl": "http://127.0.0.1:18790", "agentToken": "tok"},
    })
    plugin.reset()
    plugin.install(loop, None, config)

    assert plugin.state.installed
    assert plugin.state.agent_config == CashmaxxAgentConfig(guard_url="http://127.0.0.1:18790",
                                                            agent_token="tok")
    assert plugin.state.workspace == workspace
    for cmd in ("/cashmaxx", "/pause", "/resume", "/freeze"):
        assert loop.commands.is_dispatchable_command(cmd) or loop.commands.is_priority(cmd)
    assert loop.commands.is_priority("/freeze")
    assert "/approve" not in loop.commands._registered_commands()


def test_install_is_noop_without_config(workspace: Path) -> None:
    loop = _loop(workspace)
    plugin.reset()
    plugin.install(loop, None, Config())
    assert not plugin.state.installed
    assert "/cashmaxx" not in loop.commands._registered_commands()
