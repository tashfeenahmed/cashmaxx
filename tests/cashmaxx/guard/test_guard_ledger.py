from __future__ import annotations

import stat
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from cashmaxx.config import CashmaxxSettings
from cashmaxx.guard import ledger
from cashmaxx.guard.store import Direction, DuplicateIdempotencyKeyError, Store

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "g.sqlite3")


async def add(store: Store, days_ago: float, direction: Direction, category: str, amount: str,
              ref: str | None = None) -> None:
    await store.add_ledger(ts=NOW - timedelta(days=days_ago), direction=direction,
                           category=category, amount=Decimal(amount), source="test", ref=ref)


def test_db_file_is_private(tmp_path: Path) -> None:
    Store(tmp_path / "sub" / "g.sqlite3")
    mode = stat.S_IMODE((tmp_path / "sub" / "g.sqlite3").stat().st_mode)
    assert mode == 0o600


async def test_windows_and_categories(store: Store) -> None:
    await add(store, 1, "income", "sale_stripe", "20")
    await add(store, 2, "cost", "payment_out", "3.5")
    await add(store, 10, "cost", "compute", "4")
    await add(store, 10, "income", "bounty", "1")
    await add(store, 40, "cost", "fees", "0.25")
    await add(store, 40, "cost", "reimbursement", "4")  # settles compute: not a new cost

    week = await ledger.pnl(store, "7d", NOW)
    assert (week["income"], week["costs"], week["net"]) == ("20.00", "3.50", "16.50")
    assert week["by_category"] == {"payment_out": "3.50", "sale_stripe": "20.00"}
    assert len(week["entries"]) == 2

    month = await ledger.pnl(store, "30d", NOW)
    assert (month["income"], month["costs"], month["net"]) == ("21.00", "7.50", "13.50")

    everything = await ledger.pnl(store, "all", NOW)
    assert everything["costs"] == "7.75"
    assert everything["by_category"]["reimbursement"] == "4.00"
    assert everything["net"] == "13.25"

    with pytest.raises(ValueError):
        await ledger.pnl(store, "1y", NOW)


async def test_available_budget_ignores_income(store: Store) -> None:
    settings = CashmaxxSettings(budget_usd=Decimal("50"))
    await add(store, 1, "income", "sale_stripe", "100")
    await add(store, 1, "cost", "payment_out", "5")
    await add(store, 1, "cost", "compute", "2")
    await add(store, 1, "cost", "reimbursement", "2")
    await add(store, 1, "cost", "other", "1")
    assert await ledger.available_budget(store, settings) == Decimal("42")


async def test_ledger_ref_dedupes_and_payment_idempotency(store: Store) -> None:
    await add(store, 0, "income", "transfer_in", "1", ref="tx1")
    await add(store, 0, "income", "transfer_in", "1", ref="tx1")
    assert (await ledger.pnl(store, "all", NOW))["income"] == "1.00"

    await store.insert_payment(ts=NOW, idempotency_key="same", kind="transfer", to_addr="x",
                               amount=Decimal("1"), purpose="p", category="payment_out",
                               status="pending")
    with pytest.raises(DuplicateIdempotencyKeyError):
        await store.insert_payment(ts=NOW, idempotency_key="same", kind="transfer", to_addr="x",
                                   amount=Decimal("1"), purpose="p", category="payment_out",
                                   status="pending")


async def test_kv_and_events(store: Store) -> None:
    assert await store.kv_get("a") is None
    await store.kv_set("a", "1")
    await store.kv_set("a", "2")
    assert await store.kv_get("a") == "2"
    await store.add_event(ts=NOW, actor="guard", type="x", data={"n": 1})
    await store.add_event(ts=NOW + timedelta(minutes=1), actor="agent", type="y")
    assert [e.type for e in await store.events()] == ["x", "y"]
    assert [e.type for e in await store.events(NOW)] == ["y"]
