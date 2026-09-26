"""Cashmaxx configuration.

``CashmaxxSettings`` holds the owner's choices (budget, approvals, rules, earning methods, compute
payment mode). ``GuardConfig`` adds the secrets and lives only in the guard's own file
(``~/.cashmaxx/guard.json``, mode 0600). The agent side only ever sees ``CashmaxxAgentConfig``
(guard URL + agent token), which nanobot's root config embeds as ``cashmaxx``.
"""

from __future__ import annotations

import json
import os
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from nanobot.config_base import Base

Network = Literal["base-sepolia", "base", "fake"]
EarningMethod = Literal["digital_products", "x402_apis", "bounties", "agent_marketplaces"]
Rule = Literal["no_trading", "no_spam", "bounty_review", "loss_stop"]
ComputePaymentMode = Literal["virtual", "reimburse", "owner_topup", "x402_gateway"]

ALL_EARNING_METHODS: tuple[EarningMethod, ...] = (
    "digital_products",
    "x402_apis",
    "bounties",
    "agent_marketplaces",
)
ALL_RULES: tuple[Rule, ...] = ("no_trading", "no_spam", "bounty_review", "loss_stop")

# Onboarding presets: budget -> (per-transaction approval threshold, daily cap).
BUDGET_PRESETS: dict[str, tuple[Decimal, Decimal, Decimal]] = {
    "10": (Decimal("10"), Decimal("1"), Decimal("3")),
    "50": (Decimal("50"), Decimal("5"), Decimal("10")),
    "100": (Decimal("100"), Decimal("10"), Decimal("25")),
}

DEFAULT_GUARD_PORT = 18799  # nanobot's gateway owns 18790


def cashmaxx_home() -> Path:
    """Guard data dir. ``CASHMAXX_HOME`` overrides it (tests use a tmp dir)."""
    return Path(os.environ.get("CASHMAXX_HOME") or Path.home() / ".cashmaxx").expanduser()


def guard_config_path() -> Path:
    return cashmaxx_home() / "guard.json"


def guard_db_path() -> Path:
    return cashmaxx_home() / "guard.sqlite3"


class CashmaxxSettings(Base):
    """Owner-chosen settings. Safe to show to the agent (no secrets)."""

    network: Network = "base-sepolia"
    budget_usd: Decimal = Decimal("50")
    per_tx_approval_usd: Decimal = Decimal("5")
    daily_cap_usd: Decimal = Decimal("10")
    new_recipient_needs_approval: bool = True
    max_payments_per_hour: int = Field(default=20, ge=1, le=1000)
    allowlist: list[str] = Field(default_factory=list)
    earning_methods: set[EarningMethod] = Field(default_factory=lambda: set(ALL_EARNING_METHODS))
    rules: set[Rule] = Field(default_factory=lambda: set(ALL_RULES))
    loss_stop_usd: Decimal = Decimal("10")
    compute_payment_mode: ComputePaymentMode = "virtual"
    owner_wallet: str | None = None
    reimburse_interval_hours: int = Field(default=24, ge=1, le=24 * 30)
    owner_topup_threshold_usd: Decimal = Decimal("2")
    x402_gateway_url: str | None = None
    public_pnl: bool = False
    frozen: bool = False
    frozen_reason: str | None = None

    @field_validator(
        "budget_usd", "per_tx_approval_usd", "daily_cap_usd", "loss_stop_usd",
        "owner_topup_threshold_usd",
    )
    @classmethod
    def _non_negative(cls, v: Decimal) -> Decimal:
        if v < 0:
            raise ValueError("must be >= 0")
        return v

    @model_validator(mode="after")
    def _check_modes(self) -> CashmaxxSettings:
        if self.compute_payment_mode == "reimburse" and not self.owner_wallet:
            raise ValueError("computePaymentMode 'reimburse' requires ownerWallet")
        if self.compute_payment_mode == "x402_gateway" and not self.x402_gateway_url:
            raise ValueError("computePaymentMode 'x402_gateway' requires x402GatewayUrl")
        return self

    @classmethod
    def from_preset(cls, preset: str, **overrides: object) -> CashmaxxSettings:
        budget, per_tx, daily = BUDGET_PRESETS[preset]
        data: dict[str, object] = {
            "budget_usd": budget, "per_tx_approval_usd": per_tx, "daily_cap_usd": daily,
        }
        data.update(overrides)
        return cls.model_validate(data)


class CdpConfig(Base):
    api_key_id: str = ""
    api_key_secret: str = ""
    wallet_secret: str = ""
    account_name: str = "cashmaxx"


class GuardTelegramConfig(Base):
    """The guard's own bot. Must NOT be the same token as the agent's Telegram channel."""

    bot_token: str = ""
    owner_chat_id: str = ""


class GuardConfig(Base):
    """Full guard config, including secrets. Only the guard process reads this file."""

    settings: CashmaxxSettings = Field(default_factory=CashmaxxSettings)
    host: str = "127.0.0.1"
    port: int = DEFAULT_GUARD_PORT
    public_base_url: str | None = None
    agent_token_hash: str = ""
    owner_pin_hash: str = ""
    cdp: CdpConfig = Field(default_factory=CdpConfig)
    stripe_restricted_key: str = ""
    openrouter_api_key: str = ""
    telegram: GuardTelegramConfig = Field(default_factory=GuardTelegramConfig)


class CashmaxxAgentConfig(Base):
    """What the agent (nanobot) process knows about Cashmaxx: where the guard is and its token."""

    guard_url: str = f"http://127.0.0.1:{DEFAULT_GUARD_PORT}"
    agent_token: str = ""
    request_timeout_s: float = Field(default=20.0, gt=0, le=300)


def load_guard_config(path: Path | None = None) -> GuardConfig:
    path = path or guard_config_path()
    if not path.exists():
        raise FileNotFoundError(f"No guard config at {path}. Run `cashmaxx onboard` first.")
    return GuardConfig.model_validate(json.loads(path.read_text(encoding="utf-8")))


def save_guard_config(config: GuardConfig, path: Path | None = None) -> Path:
    """Atomic write with mode 0600."""
    path = path or guard_config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = config.model_dump(mode="json", by_alias=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".guard-", suffix=".json")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path
