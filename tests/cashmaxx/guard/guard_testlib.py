"""Shared helpers for the guard tests (imported by conftest and test modules)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from aiohttp.test_utils import TestClient

from cashmaxx.config import CashmaxxSettings, GuardConfig
from cashmaxx.guard.app import Guard
from cashmaxx.guard.auth import hash_agent_token, hash_pin
from cashmaxx.guard.client import GuardClient
from cashmaxx.guard.wallet.fake import FakeWallet
from cashmaxx.guard.watchers import Watchers

AGENT_TOKEN = "agent-token-for-tests"
PIN = "4321"
PIN_HASH = hash_pin(PIN, n=2**10)  # cheap scrypt params keep the suite fast
ALICE = "0x" + "a" * 40
BOB = "0x" + "b" * 40
OWNER_WALLET = "0x" + "0" * 39 + "1"


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.t = start or datetime(2026, 9, 1, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kwargs: float) -> None:
        self.t += timedelta(**kwargs)


@dataclass
class RecordingNotifier:
    sent: list[tuple[str, str | None]] = field(default_factory=list[tuple[str, str | None]])

    async def send(self, text: str, *, approval_id: str | None = None) -> None:
        self.sent.append((text, approval_id))

    def texts(self) -> list[str]:
        return [t for t, _ in self.sent]


@dataclass
class Env:
    app: Any
    client: TestClient[Any, Any]
    guard: Guard
    watchers: Watchers
    wallet: FakeWallet
    notifier: RecordingNotifier
    clock: FakeClock
    config_path: Path
    agent: dict[str, str]

    @property
    def base_url(self) -> str:
        return str(self.client.make_url("")).rstrip("/")

    async def owner(self) -> dict[str, str]:
        resp = await self.client.post("/owner/session", json={"pin": PIN})
        assert resp.status == 200, await resp.text()
        return {"X-Cashmaxx-Owner": (await resp.json())["session"]}

    def guard_client(self, **kwargs: Any) -> GuardClient:
        return GuardClient(self.base_url, **kwargs)

    async def spend(self, to: str = ALICE, amount: str = "1", key: str = "k1",
                    **extra: Any) -> tuple[int, dict[str, Any]]:
        body = {"to": to, "amount_usd": amount, "purpose": "test", "idempotency_key": key,
                **extra}
        resp = await self.client.post("/spend", json=body, headers=self.agent)
        return resp.status, await resp.json()


def make_config(**settings: Any) -> GuardConfig:
    base: dict[str, Any] = {
        "network": "fake", "budget_usd": "50", "per_tx_approval_usd": "5",
        "daily_cap_usd": "10", "allowlist": [ALICE],
    }
    base.update(settings)
    return GuardConfig(
        settings=CashmaxxSettings.model_validate(base),
        agent_token_hash=hash_agent_token(AGENT_TOKEN),
        owner_pin_hash=PIN_HASH,
    )


def usd(value: str) -> Decimal:
    return Decimal(value)


EnvFactory = Callable[..., Awaitable[Env]]
