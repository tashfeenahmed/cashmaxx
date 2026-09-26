"""Wallet backends. The guard talks to the chain only through ``WalletBackend``."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable

# Canonical USDC contracts (6 decimals).
USDC_ADDRESSES: dict[str, str] = {
    "base": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "base-sepolia": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
}


class WalletError(Exception):
    """A wallet operation failed (insufficient funds, RPC error, signer error...)."""


@dataclass(frozen=True)
class Incoming:
    """An incoming USDC transfer to the guard's address."""

    tx_hash: str
    log_index: int
    from_addr: str
    amount_usd: Decimal
    block: int

    @property
    def ref(self) -> str:
        return f"{self.tx_hash.lower()}:{self.log_index}"


@runtime_checkable
class WalletBackend(Protocol):
    network: str

    async def address(self) -> str: ...

    async def balance_usdc(self) -> Decimal: ...

    async def transfer_usdc(self, to: str, amount: Decimal) -> str:
        """Send USDC; return the transaction hash. Raises ``WalletError`` on failure."""
        ...

    async def x402_fetch(
        self, url: str, method: str, body: str | None, max_usd: Decimal
    ) -> tuple[int, str, Decimal]:
        """Fetch ``url`` paying at most ``max_usd`` if it answers 402.

        Returns ``(http_status, body_text, paid_usd)``.
        """
        ...

    async def incoming_transfers(
        self, since_cursor: str | None
    ) -> tuple[list[Incoming], str | None]:
        """USDC transfers to our address after ``since_cursor``; returns the new cursor."""
        ...

    async def aclose(self) -> None: ...


__all__ = ["USDC_ADDRESSES", "Incoming", "WalletBackend", "WalletError"]
