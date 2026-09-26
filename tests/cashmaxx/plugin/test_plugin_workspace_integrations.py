"""Workspace rendering with and without integration information."""

from __future__ import annotations

from pathlib import Path

from cmx_plugin_testlib import FakeGuard

from cashmaxx import plugin
from cashmaxx.config import CashmaxxSettings
from cashmaxx.plugin import integrations as integ
from cashmaxx.plugin import workspace as ws
from nanobot.agent.skills import parse_skill_metadata, valid_skill_metadata


def _settings(**kw: object) -> CashmaxxSettings:
    return CashmaxxSettings.model_validate(kw)


def _skills(workspace: Path) -> set[str]:
    return {p.name for p in (workspace / "skills").iterdir() if p.is_dir()}


def test_without_integrations_only_email_follows_settings(workspace: Path) -> None:
    ws.install_workspace(workspace, CashmaxxSettings())
    assert not _skills(workspace) & set(ws.INTEGRATION_SKILLS)
    rules = (workspace / "RULES.md").read_text()
    assert "Email: not connected" in rules and "Connected integrations" not in rules
    assert "at most 3 per UTC day" in rules and "Public hosting: off" in rules

    ws.install_workspace(workspace, _settings(emailProvider="gmail", emailDailyCap=15))
    assert "cashmaxx-email" in _skills(workspace)
    rules = (workspace / "RULES.md").read_text()
    assert "Email (gmail): at most 15 recipients" in rules and "warm-up" in rules


def test_with_integrations(workspace: Path) -> None:
    connected = {"gmail": True, "bluesky": True, "x": False, "browser": True, "github": False}
    ws.install_workspace(workspace, _settings(emailProvider="agentmail", hostingEnabled=True),
                         integrations=connected)
    assert {"cashmaxx-email", "cashmaxx-social", "cashmaxx-browser"} <= _skills(workspace)
    rules = (workspace / "RULES.md").read_text()
    assert "Connected integrations: Gmail, Bluesky, Browser." in rules
    assert "Connected: Bluesky." in rules and "Public hosting: on" in rules
    assert "warm-up" not in rules  # AgentMail has no warm-up


def test_disconnect_removes_skill_unknown_keeps_it(workspace: Path) -> None:
    ws.install_workspace(workspace, CashmaxxSettings(), integrations={"browser": True,
                                                                      "reddit": True})
    assert {"cashmaxx-browser", "cashmaxx-social"} <= _skills(workspace)
    ws.install_workspace(workspace, CashmaxxSettings(), integrations=None)  # guard down
    assert {"cashmaxx-browser", "cashmaxx-social"} <= _skills(workspace)
    report = ws.install_workspace(workspace, CashmaxxSettings(), integrations={})
    assert report.results["skills/cashmaxx-browser/SKILL.md"] == "removed"
    assert not _skills(workspace) & {"cashmaxx-browser", "cashmaxx-social"}
    assert "Connected integrations: none." in (workspace / "RULES.md").read_text()


def test_social_skill_off_when_cap_is_zero(workspace: Path) -> None:
    ws.install_workspace(workspace, _settings(socialDailyCap=0), integrations={"x": True})
    assert "cashmaxx-social" not in _skills(workspace)


def test_idempotent_with_integrations(workspace: Path) -> None:
    connected = {"gmail": True, "browser": True}
    settings = _settings(emailProvider="gmail")
    ws.install_workspace(workspace, settings, integrations=connected)
    again = ws.install_workspace(workspace, settings, integrations=connected)
    assert again.changed == [] and again.skipped == []


def test_new_skills_have_valid_metadata() -> None:
    from importlib.resources import files

    for name, (src, _gate) in ws.INTEGRATION_SKILLS.items():
        text = files("cashmaxx").joinpath("skills", src, "SKILL.md").read_text(encoding="utf-8")
        meta = parse_skill_metadata(text)
        assert meta is not None and valid_skill_metadata(meta, name), name


async def test_refresh_workspace_reads_guard(fake_guard: FakeGuard, workspace: Path) -> None:
    fake_guard.set("GET", "/settings", {"settings": {"network": "base-sepolia",
                                                     "emailProvider": "gmail"}})
    fake_guard.set("GET", "/integrations", {"integrations": [
        {"id": "gmail", "label": "Gmail", "kind": "guard", "category": "email",
         "connected": True}]})
    assert await integ.refresh_workspace() is True
    assert "cashmaxx-email" in _skills(workspace)
    assert "Connected integrations: Gmail." in (workspace / "RULES.md").read_text()


async def test_refresh_workspace_tolerates_failures(fake_guard: FakeGuard, workspace: Path) -> None:
    fake_guard.down = True
    assert await integ.refresh_workspace() is False
    fake_guard.down = False
    fake_guard.set("GET", "/settings", {"settings": {"network": "base-sepolia"}})
    # /integrations is missing (older guard): still rendered, without the connected line.
    assert await integ.refresh_workspace() is True
    assert "Connected integrations" not in (workspace / "RULES.md").read_text()


async def test_install_marks_refresh_and_first_call_runs_it(
    fake_guard: FakeGuard, workspace: Path
) -> None:
    import asyncio

    fake_guard.set("GET", "/settings", {"settings": {"network": "base-sepolia"}})
    plugin.state.refresh_pending = True
    plugin.kick_workspace_refresh()
    assert plugin.state.refresh_pending is False
    for _ in range(20):
        if (workspace / "RULES.md").exists():
            break
        await asyncio.sleep(0.01)
    assert (workspace / "RULES.md").exists()
