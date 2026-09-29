from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from cmx_plugin_testlib import FakeGuard

from cashmaxx.config import CashmaxxSettings
from cashmaxx.plugin import workspace as ws
from cashmaxx.plugin.commands import register_commands
from nanobot.bus.events import InboundMessage
from nanobot.command.router import CommandContext, CommandRouter


def _router() -> CommandRouter:
    router = CommandRouter()
    register_commands(router)
    return router


async def _send(router: CommandRouter, text: str, workspace: Path) -> str:
    msg = InboundMessage(channel="telegram", sender_id="u", chat_id="42", content=text)
    ctx = CommandContext(msg=msg, session=None, key=msg.session_key, raw=text,
                         loop=SimpleNamespace(workspace=workspace))  # type: ignore[arg-type]
    if router.is_priority(text):
        out = await router.dispatch_priority(ctx)
    else:
        out = await router.dispatch(ctx)
    assert out is not None
    return out.content


async def test_status(fake_guard: FakeGuard, workspace: Path) -> None:
    ws.install_workspace(workspace, CashmaxxSettings())
    text = await _send(_router(), "/cashmaxx", workspace)
    for part in ("0xabc", "12.50 USDC", "40.00", "net 1.75", "Pending approvals: 1", "ap1",
                 "Frozen: no", "Money loop: running"):
        assert part in text


async def test_status_guard_down(fake_guard: FakeGuard, workspace: Path) -> None:
    fake_guard.down = True
    assert "Guard unreachable" in await _send(_router(), "/cashmaxx", workspace)


async def test_pause_and_resume_toggle_heartbeat(workspace: Path) -> None:
    from nanobot.cli.gateway_runtime import _heartbeat_has_active_tasks

    ws.install_workspace(workspace, CashmaxxSettings())
    heartbeat = workspace / "HEARTBEAT.md"
    assert _heartbeat_has_active_tasks(heartbeat.read_text())
    router = _router()

    assert "paused" in await _send(router, "/pause", workspace)
    assert ws.loop_paused(workspace) is True
    assert not _heartbeat_has_active_tasks(heartbeat.read_text())
    assert "already paused" in await _send(router, "/pause", workspace)

    assert "resumed" in await _send(router, "/resume", workspace)
    assert ws.loop_paused(workspace) is False
    assert _heartbeat_has_active_tasks(heartbeat.read_text())


async def test_pause_without_install(workspace: Path) -> None:
    assert "cashmaxx onboard" in await _send(_router(), "/pause", workspace)


async def test_freeze_with_reason(fake_guard: FakeGuard, workspace: Path) -> None:
    text = await _send(_router(), "/freeze weird payment", workspace)
    assert "frozen" in text
    assert fake_guard.last.url.path == "/freeze"
    assert fake_guard.last_json() == {"reason": "weird payment"}


async def test_bare_freeze_is_priority(fake_guard: FakeGuard, workspace: Path) -> None:
    router = _router()
    assert router.is_priority("/freeze")
    await _send(router, "/freeze", workspace)
    assert fake_guard.last_json()["reason"].startswith("/freeze from telegram")


async def test_freeze_guard_down(fake_guard: FakeGuard, workspace: Path) -> None:
    fake_guard.down = True
    assert "Guard unreachable" in await _send(_router(), "/freeze now", workspace)


async def test_there_is_no_approve_command(fake_guard: FakeGuard, workspace: Path) -> None:
    text = await _send(_router(), "/approve ap1", workspace)
    assert text.startswith("Unknown command")
    assert not any(r.url.path.endswith("/approve") for r in fake_guard.requests)


async def test_status_lists_connected_integrations(fake_guard: FakeGuard, workspace: Path) -> None:
    fake_guard.set("GET", "/integrations", {"integrations": [
        {"id": "gmail", "label": "Gmail", "kind": "guard", "category": "email",
         "connected": True},
        {"id": "x", "label": "X (Twitter)", "kind": "guard", "category": "social",
         "connected": False},
        {"id": "bluesky", "label": "Bluesky", "kind": "guard", "category": "social",
         "connected": True},
    ]})
    fake_guard.set("GET", "/email/status", {"provider": "gmail", "address": "bot@example.com",
                                            "connected": True, "sent_today": 2, "cap_today": 10})
    text = await _send(_router(), "/cashmaxx", workspace)
    assert "Integrations:" in text
    assert "  - Gmail (email): connected, bot@example.com, 2/10 sent today" in text
    assert "  - Bluesky (social): connected" in text
    assert "X (Twitter)" not in text and "Browser" not in text


async def test_status_integrations_unavailable(fake_guard: FakeGuard, workspace: Path) -> None:
    text = await _send(_router(), "/cashmaxx", workspace)  # fake guard has no /integrations
    assert "Integrations: unavailable" in text and "0xabc" in text
