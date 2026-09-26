"""Spending policy. Pure functions: no I/O, no clock.

``evaluate`` checks a ``SpendRequest`` against the owner's settings and the current state, in the
order fixed by docs/cashmaxx/ARCHITECTURE.md; the first match wins. On approval of a pending
payment, the guard calls it again with ``recheck=True``, which re-runs steps 1-6 only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from cashmaxx.config import CashmaxxSettings

Outcome = Literal["allow", "needs_approval", "deny"]

SUPPORTED_KINDS = frozenset({"transfer", "x402"})
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9.-]+(:\d{1,5})?$")


@dataclass(frozen=True)
class SpendRequest:
    kind: str
    to: str
    amount_usd: Decimal
    purpose: str
    category: str
    idempotency_key: str


@dataclass(frozen=True)
class PolicyState:
    spent_today: Decimal
    payments_last_hour: int
    available_budget: Decimal
    frozen: bool
    known_recipients: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    reason: str

    @property
    def allowed(self) -> bool:
        return self.outcome == "allow"


def is_valid_address(value: str) -> bool:
    return bool(_ADDRESS_RE.match(value))


def _valid_target(request: SpendRequest) -> bool:
    if request.kind == "transfer":
        return is_valid_address(request.to)
    # x402 targets are the paid host (e.g. "api.example.com"); anything else just needs a value.
    if request.kind == "x402":
        return bool(_HOST_RE.match(request.to))
    return bool(request.to.strip())


def evaluate(
    request: SpendRequest,
    settings: CashmaxxSettings,
    state: PolicyState,
    *,
    recheck: bool = False,
) -> Decision:
    """Return the first matching decision. ``recheck=True`` stops after the hard limits (1-6)."""
    # 1. kill switch
    if state.frozen or settings.frozen:
        return Decision("deny", "frozen")
    # 2. malformed amount / address
    if not request.amount_usd.is_finite() or request.amount_usd <= 0 or not _valid_target(request):
        return Decision("deny", "invalid")
    # 3. only USDC transfers and x402 payments exist; never swaps or contract calls
    if request.kind not in SUPPORTED_KINDS:
        return Decision("deny", "unsupported")
    # 4. budget
    if request.amount_usd > state.available_budget:
        return Decision("deny", "over_budget")
    # 5. daily cap
    if state.spent_today + request.amount_usd > settings.daily_cap_usd:
        return Decision("deny", "daily_cap")
    # 6. runaway-loop brake
    if state.payments_last_hour >= settings.max_payments_per_hour:
        return Decision("deny", "rate_limited")
    if recheck:
        return Decision("allow", "recheck_ok")
    # 7. big payments
    if request.amount_usd > settings.per_tx_approval_usd:
        return Decision("needs_approval", "over_threshold")
    # 8. first payment to someone new
    if settings.new_recipient_needs_approval:
        known = {k.lower() for k in state.known_recipients}
        known |= {a.lower() for a in settings.allowlist}
        if request.to.lower() not in known:
            return Decision("needs_approval", "new_recipient")
    # 9.
    return Decision("allow", "ok")
