from __future__ import annotations

from pathlib import Path

import pytest

from cashmaxx.config import CashmaxxSettings
from cashmaxx.plugin import workspace as ws
from nanobot.agent.skills import SkillsLoader, parse_skill_metadata, valid_skill_metadata


def _settings(**kw: object) -> CashmaxxSettings:
    return CashmaxxSettings.model_validate(kw)


def _skill_dirs(workspace: Path) -> set[str]:
    return {p.name for p in (workspace / "skills").iterdir() if p.is_dir()}


def test_installs_only_enabled_skills(workspace: Path) -> None:
    ws.install_workspace(workspace, _settings(earning_methods=["bounties"]))
    assert _skill_dirs(workspace) == {"cashmaxx-core", "cashmaxx-bounties"}
    for name in ("HEARTBEAT.md", "SOUL.md", "RULES.md", "cashmaxx/experiments.md"):
        assert (workspace / name).exists()


def test_all_methods_enabled_by_default(workspace: Path) -> None:
    ws.install_workspace(workspace, CashmaxxSettings())
    assert _skill_dirs(workspace) == set(ws.SKILLS)


def test_idempotent(workspace: Path) -> None:
    settings = CashmaxxSettings()
    first = ws.install_workspace(workspace, settings)
    assert first.changed
    snapshot = {p: p.read_bytes() for p in workspace.rglob("*") if p.is_file()}
    second = ws.install_workspace(workspace, settings)
    assert second.changed == [] and second.skipped == []
    assert set(second.results.values()) == {"unchanged"}
    assert {p: p.read_bytes() for p in workspace.rglob("*") if p.is_file()} == snapshot


def test_settings_change_updates_rules_and_removes_disabled_skill(workspace: Path) -> None:
    ws.install_workspace(workspace, CashmaxxSettings())
    report = ws.install_workspace(
        workspace, _settings(earning_methods=["x402_apis"], rules=["no_trading"], budgetUsd="100"),
    )
    assert report.results["RULES.md"] == "updated"
    assert report.results["skills/cashmaxx-bounties/SKILL.md"] == "removed"
    assert _skill_dirs(workspace) == {"cashmaxx-core", "cashmaxx-x402-apis"}
    rules = (workspace / "RULES.md").read_text()
    assert "100.00 USD" in rules and "No trading" in rules and "Loss stop" not in rules


def test_user_edits_are_kept_unless_forced(workspace: Path) -> None:
    ws.install_workspace(workspace, CashmaxxSettings())
    rules = workspace / "RULES.md"
    rules.write_text(rules.read_text().replace("Hard rules", "My hard rules"))
    skill = workspace / "skills" / "cashmaxx-core" / "SKILL.md"
    skill.write_text(skill.read_text() + "\nmy note\n")

    report = ws.install_workspace(workspace, _settings(budgetUsd="99"))
    assert set(report.skipped) == {"RULES.md", "skills/cashmaxx-core/SKILL.md"}
    assert "My hard rules" in rules.read_text()
    assert "my note" in skill.read_text()

    forced = ws.install_workspace(workspace, _settings(budgetUsd="99"), force=True)
    assert forced.results["RULES.md"] == "updated"
    assert "My hard rules" not in rules.read_text() and "99.00 USD" in rules.read_text()


def test_soul_block_appended_to_existing_soul(workspace: Path) -> None:
    soul = workspace / "SOUL.md"
    soul.write_text("# Soul\n\nI like tea.\n")
    ws.install_workspace(workspace, CashmaxxSettings())
    text = soul.read_text()
    assert text.startswith("# Soul\n\nI like tea.")
    assert "## Cashmaxx" in text and text.count("cashmaxx:begin soul") == 1
    ws.install_workspace(workspace, CashmaxxSettings())
    assert soul.read_text() == text

    edited = text.replace("I keep turns short", "I keep turns VERY short")
    soul.write_text(edited)
    assert ws.install_workspace(workspace, CashmaxxSettings()).results["SOUL.md"] == "skipped"
    assert soul.read_text() == edited


def test_heartbeat_block_coexists_with_user_tasks(workspace: Path) -> None:
    from nanobot.cli.gateway_runtime import _heartbeat_has_active_tasks

    hb = workspace / "HEARTBEAT.md"
    hb.write_text("# Heartbeat Tasks\n\n## Active Tasks\n\n- water the plants\n")
    ws.install_workspace(workspace, CashmaxxSettings())
    ws.set_loop_paused(workspace, True)
    text = hb.read_text()
    assert "- water the plants" in text
    assert _heartbeat_has_active_tasks(text)  # the owner's own task still runs


def test_pause_survives_reinstall_and_is_not_a_user_edit(workspace: Path) -> None:
    ws.install_workspace(workspace, CashmaxxSettings())
    ws.set_loop_paused(workspace, True)
    report = ws.install_workspace(workspace, _settings(budgetUsd="20"))
    assert report.results["HEARTBEAT.md"] in {"updated", "unchanged"}
    assert ws.loop_paused(workspace) is True


def test_set_loop_paused_without_block(workspace: Path) -> None:
    with pytest.raises(LookupError):
        ws.set_loop_paused(workspace, True)
    assert ws.loop_paused(workspace) is None


def test_skills_are_valid_nanobot_skills(workspace: Path) -> None:
    ws.install_workspace(workspace, CashmaxxSettings())
    for name in ws.SKILLS:
        content = (workspace / "skills" / name / "SKILL.md").read_text()
        meta = parse_skill_metadata(content)
        assert meta is not None and valid_skill_metadata(meta, name), name
    loader = SkillsLoader(workspace)
    names = {s["name"] for s in loader.list_skills(filter_unavailable=False)}
    assert set(ws.SKILLS) <= names
    assert "cashmaxx-core" in loader.get_always_skills()


def test_rules_render_every_compute_mode() -> None:
    modes = {
        "virtual": {},
        "reimburse": {"ownerWallet": "0x" + "1" * 40},
        "owner_topup": {},
        "x402_gateway": {"x402GatewayUrl": "https://gw.example"},
    }
    texts = {ws.render_rules(_settings(computePaymentMode=m, **extra)) for m, extra in modes.items()}
    assert len(texts) == 4


@pytest.mark.parametrize("network", ["fake", "base-sepolia", "base"])
def test_rules_explain_where_money_arrives(network: str) -> None:
    rules = ws.render_rules(_settings(network=network))
    assert "I get paid at the address from `cashmaxx_wallet`" in rules
    assert "I never need it to earn" in rules  # ownerWallet is not a prerequisite
    assert "I never create, generate or import another wallet or private key" in rules
    if network == "base":
        assert "payouts to my address are real USDC" in rules
    else:
        assert "I don't wait for the network to change" in rules


def test_heartbeat_does_the_next_free_step_instead_of_idling() -> None:
    block = ws.render_heartbeat_block(_settings())
    assert "work on the next free step" in block
    assert 'Reply "All clear." only when every experiment is truly blocked' in block


def test_heartbeat_asks_for_honest_reports_and_a_second_experiment() -> None:
    block = ws.render_heartbeat_block(_settings())
    assert "start a second one (at most two active)" in block
    assert "Report only what you did in this run" in block


def test_reset_heartbeat_session_clears_history(workspace: Path) -> None:
    from types import SimpleNamespace

    from cashmaxx.plugin import reset_heartbeat_session
    from nanobot.session.manager import SessionManager

    sessions = SessionManager(workspace)
    session = sessions.get_or_create("heartbeat")
    session.add_message("assistant", "Waiting on you to connect a wallet.")
    sessions.save(session)
    reset_heartbeat_session(SimpleNamespace(sessions=sessions), "heartbeat")
    sessions.invalidate("heartbeat")
    assert sessions.get_or_create("heartbeat").messages == []


def test_reset_heartbeat_session_never_raises() -> None:
    from types import SimpleNamespace

    from cashmaxx.plugin import reset_heartbeat_session

    reset_heartbeat_session(SimpleNamespace(), "heartbeat")  # no sessions attribute: logged, ignored
