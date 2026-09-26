"""Deterministic in-memory wallet for tests and ``network="fake"``. No network access."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from decimal import Decimal

from cashmaxx.guard.wallet import Incoming, WalletError
from cashmaxx.money import ZERO


@dataclass
class FakeX402Resource:
    """What a fake x402 URL returns: status/body when paid, and its price (0 = free)."""

    body: str = "ok"
    price_usd: Decimal = ZERO
    status: int = 200


@dataclass
class FakeWallet:
    network: str = "fake"
    wallet_address: str = "0x000000000000000000000000000000000000fa4e"
    balance: Decimal = Decimal("100")
    incoming: list[Incoming] = field(default_factory=list[Incoming])
    resources: dict[str, FakeX402Resource] = field(default_factory=dict[str, FakeX402Resource])
    transfers: list[tuple[str, Decimal, str]] = field(
        default_factory=list[tuple[str, Decimal, str]]
    )
    fail_transfers: bool = False
    closed: bool = False

    async def address(self) -> str:
        return self.wallet_address

    async def balance_usdc(self) -> Decimal:
        return self.balance

    async def transfer_usdc(self, to: str, amount: Decimal) -> str:
        if self.fail_transfers:
            raise WalletError("fake transfer failure")
        if amount > self.balance:
            raise WalletError("insufficient USDC balance")
        self.balance -= amount
        seed = f"{len(self.transfers)}:{to.lower()}:{amount}".encode()
        tx_hash = "0x" + hashlib.sha256(seed).hexdigest()
        self.transfers.append((to, amount, tx_hash))
        return tx_hash

    async def x402_fetch(
        self, url: str, method: str, body: str | None, max_usd: Decimal
    ) -> tuple[int, str, Decimal]:
        res = self.resources.get(url)
        if res is None:
            return 404, "not found", ZERO
        if res.price_usd <= 0:
            return res.status, res.body, ZERO
        if res.price_usd > max_usd:
            return 402, f"payment required: {res.price_usd} > max {max_usd}", ZERO
        if res.price_usd > self.balance:
            raise WalletError("insufficient USDC balance")
        self.balance -= res.price_usd
        return res.status, res.body, res.price_usd

    def inject_incoming(self, from_addr: str, amount: Decimal) -> Incoming:
        n = len(self.incoming)
        item = Incoming(
            tx_hash="0x" + hashlib.sha256(f"in:{n}".encode()).hexdigest(),
            log_index=0, from_addr=from_addr, amount_usd=amount, block=n + 1,
        )
        self.incoming.append(item)
        self.balance += amount
        return item

    async def incoming_transfers(
        self, since_cursor: str | None
    ) -> tuple[list[Incoming], str | None]:
        start = int(since_cursor) if since_cursor else 0
        return list(self.incoming[start:]), str(len(self.incoming))

    async def aclose(self) -> None:
        self.closed = True
