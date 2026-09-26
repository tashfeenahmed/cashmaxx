from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from guard_testlib import ALICE, BOB

from cashmaxx.config import CashmaxxSettings
from cashmaxx.guard.policy import PolicyState, SpendRequest, evaluate


def settings(**kw: Any) -> CashmaxxSettings:
    base: dict[str, Any] = {"budget_usd": "50", "per_tx_approval_usd": "5", "daily_cap_usd": "10",
                            "max_payments_per_hour": 3}
    base.update(kw)
    return CashmaxxSettings.model_validate(base)


def state(**kw: Any) -> PolicyState:
    base: dict[str, Any] = {"spent_today": Decimal("0"), "payments_last_hour": 0,
                            "available_budget": Decimal("50"), "frozen": False,
                            "known_recipients": frozenset({ALICE})}
    base.update(kw)
    return PolicyState(**base)


def req(amount: str = "1", to: str = ALICE, kind: str = "transfer") -> SpendRequest:
    return SpendRequest(kind=kind, to=to, amount_usd=Decimal(amount), purpose="p",
                        category="payment_out", idempotency_key="k")


@pytest.mark.parametrize(
    ("request_", "settings_kw", "state_kw", "outcome", "reason"),
    [
        (req(), {}, {"frozen": True}, "deny", "frozen"),
        (req(), {"frozen": True}, {}, "deny", "frozen"),
        (req("0"), {}, {}, "deny", "invalid"),
        (req("-1"), {}, {}, "deny", "invalid"),
        (req(to="not-an-address"), {}, {}, "deny", "invalid"),
        (req(kind="swap"), {}, {}, "deny", "unsupported"),
        (req("4"), {}, {"available_budget": Decimal("3")}, "deny", "over_budget"),
        (req("4"), {}, {"spent_today": Decimal("7")}, "deny", "daily_cap"),
        (req(), {}, {"payments_last_hour": 3}, "deny", "rate_limited"),
        (req("6"), {}, {}, "needs_approval", "over_threshold"),
        (req(to=BOB), {}, {}, "needs_approval", "new_recipient"),
        (req(to=BOB), {"allowlist": [BOB.upper().replace("0X", "0x")]}, {}, "allow", "ok"),
        (req(to=BOB), {"new_recipient_needs_approval": False}, {}, "allow", "ok"),
        (req("5"), {}, {}, "allow", "ok"),
        (req("0.5", to="api.example.com", kind="x402"), {"allowlist": ["api.example.com"]}, {},
         "allow", "ok"),
        (req("0.5", to="api.example.com", kind="x402"), {}, {}, "needs_approval",
         "new_recipient"),
        (req("0.5", to="http://bad host", kind="x402"), {}, {}, "deny", "invalid"),
    ],
)
def test_every_branch(
    request_: SpendRequest, settings_kw: dict[str, Any], state_kw: dict[str, Any], outcome: str,
    reason: str,
) -> None:
    decision = evaluate(request_, settings(**settings_kw), state(**state_kw))
    assert (decision.outcome, decision.reason) == (outcome, reason)


def test_order_first_match_wins() -> None:
    # frozen beats invalid, invalid beats unsupported, over_budget beats daily_cap
    assert evaluate(req("0"), settings(), state(frozen=True)).reason == "frozen"
    assert evaluate(req("0", kind="swap"), settings(), state()).reason == "invalid"
    s = state(available_budget=Decimal("1"), spent_today=Decimal("10"))
    assert evaluate(req("2"), settings(), s).reason == "over_budget"


def test_recheck_skips_approval_steps() -> None:
    big_new = req("8", to=BOB)
    assert evaluate(big_new, settings(), state()).outcome == "needs_approval"
    assert evaluate(big_new, settings(), state(), recheck=True).outcome == "allow"
    assert evaluate(big_new, settings(), state(frozen=True), recheck=True).reason == "frozen"
    assert evaluate(big_new, settings(), state(spent_today=Decimal("5")),
                    recheck=True).reason == "daily_cap"
