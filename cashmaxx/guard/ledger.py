"""P&L math over the store.

Budget model (kept deliberately simple):

    available_budget = budget_usd - costs_all_time

where ``costs_all_time`` is every ``cost`` ledger row (payment_out, compute, fees, other, ...)
**except** ``reimbursement``. A reimbursement pays the owner back for compute that is already in
the ledger as a ``compute`` cost, so counting it again would charge the same spend twice.
For the same reason ``reimbursement`` rows are listed in ``by_category`` but not added to
``costs``/``net``.

Earnings never raise the budget automatically. Income shows up in the P&L (and so in the loss
stop), but only the owner can grant more budget by editing ``budget_usd``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from cashmaxx.config import CashmaxxSettings
from cashmaxx.guard.store import COST_CATEGORIES, LedgerEntry, Store
from cashmaxx.money import ZERO, fmt_usd

Window = Literal["7d", "30d", "all"]
WINDOWS: dict[str, timedelta | None] = {"7d": timedelta(days=7), "30d": timedelta(days=30),
                                         "all": None}

# Costs that settle other costs; shown per category, excluded from totals.
SETTLEMENT_CATEGORIES = frozenset({"reimbursement"})
BUDGET_COST_CATEGORIES = COST_CATEGORIES - SETTLEMENT_CATEGORIES
MAX_ENTRIES = 200


def window_start(window: str, now: datetime) -> datetime | None:
    if window not in WINDOWS:
        raise ValueError(f"window must be one of {sorted(WINDOWS)}")
    delta = WINDOWS[window]
    return now - delta if delta else None


def summarize(entries: list[LedgerEntry]) -> tuple[Decimal, Decimal, dict[str, Decimal]]:
    """(income, costs, by_category) for a list of entries."""
    income = ZERO
    costs = ZERO
    by_category: dict[str, Decimal] = {}
    for e in entries:
        by_category[e.category] = by_category.get(e.category, ZERO) + e.amount_usd
        if e.direction == "income":
            income += e.amount_usd
        elif e.category not in SETTLEMENT_CATEGORIES:
            costs += e.amount_usd
    return income, costs, by_category


async def pnl(
    store: Store, window: str, now: datetime, *, include_entries: bool = True
) -> dict[str, Any]:
    entries = await store.ledger_entries(window_start(window, now))
    income, costs, by_category = summarize(entries)
    out: dict[str, Any] = {
        "window": window,
        "income": fmt_usd(income),
        "costs": fmt_usd(costs),
        "net": fmt_usd(income - costs),
        "by_category": {k: fmt_usd(v) for k, v in sorted(by_category.items())},
    }
    if include_entries:
        out["entries"] = [e.to_json() for e in entries[:MAX_ENTRIES]]
    return out


async def net_since(store: Store, since: datetime) -> Decimal:
    income, costs, _ = summarize(await store.ledger_entries(since))
    return income - costs


async def total_costs(store: Store) -> Decimal:
    return await store.sum_ledger(direction="cost", categories=BUDGET_COST_CATEGORIES)


async def available_budget(store: Store, settings: CashmaxxSettings) -> Decimal:
    return settings.budget_usd - await total_costs(store)


async def unreimbursed_compute(store: Store) -> Decimal:
    """Compute cost not yet paid back (paid reimbursements and pending ones both count)."""
    compute = await store.sum_ledger(direction="cost", categories={"compute"})
    paid = await store.sum_ledger(direction="cost", categories={"reimbursement"})
    pending = await store.sum_pending_payments("reimbursement")
    return max(ZERO, compute - paid - pending)

