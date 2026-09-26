"""A fake guard behind ``httpx.MockTransport`` and a pinned plugin config."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from cmx_plugin_testlib import AGENT_TOKEN, FakeGuard

from cashmaxx import plugin
from cashmaxx.config import CashmaxxAgentConfig


@pytest.fixture
def fake_guard() -> FakeGuard:
    guard = FakeGuard()
    guard.set("GET", "/health", {"ok": True, "network": "base-sepolia", "frozen": False})
    guard.set("GET", "/wallet", {
        "address": "0xabc", "network": "base-sepolia", "balance_usdc": "12.50",
        "available_budget_usd": "40.00",
    })
    guard.set("GET", "/ledger", {
        "income": "3.00", "costs": "1.25", "net": "1.75", "by_category": {"compute": "1.25"},
        "entries": [],
    })
    guard.set("GET", "/approvals", {"approvals": [{
        "id": "ap1", "reason": "new_recipient", "payment_id": "p1",
        "payment": {"amount_usd": "2.00", "to": "0xdef", "purpose": "buy data"},
    }]})
    guard.set("GET", "/settings", {"settings": {"network": "base-sepolia", "budgetUsd": "50"}})
    guard.set("POST", "/freeze", {"frozen": True, "changed": True})
    return guard


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


@pytest.fixture(autouse=True)
def nanobot_config_path(tmp_path: Path) -> Iterator[Path]:
    """Point nanobot's config loader at a temp file so no test reads or writes the real one."""
    from nanobot.config import loader

    previous = loader._current_config_path  # pyright: ignore[reportPrivateUsage]
    path = tmp_path / "nanobot-config.json"
    loader.set_config_path(path)
    yield path
    loader._current_config_path = previous  # pyright: ignore[reportPrivateUsage]


@pytest.fixture(autouse=True)
def pinned_plugin(fake_guard: FakeGuard, workspace: Path) -> Iterator[None]:
    plugin.reset()
    plugin.configure(
        CashmaxxAgentConfig(guard_url="http://guard.test", agent_token=AGENT_TOKEN),
        workspace=workspace,
        transport=httpx.MockTransport(fake_guard.handler),
    )
    yield
    plugin.reset()
