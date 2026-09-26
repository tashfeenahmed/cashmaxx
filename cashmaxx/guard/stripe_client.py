"""Stripe calls made with the owner's *restricted* key.

The key needs write access to Products, Prices and Payment Links and read access to Checkout
Sessions. Every call runs in a worker thread (the ``stripe`` SDK is synchronous here).
Tests pass a stub in place of ``stripe.StripeClient`` via ``client_factory``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from cashmaxx.money import ZERO, fmt_usd

_CENT = Decimal("0.01")


@dataclass(frozen=True)
class StripeSale:
    session_id: str
    created: int
    amount_usd: Decimal
    currency: str
    note: str


def usd_to_cents(amount: Decimal) -> int:
    return int((amount / _CENT).to_integral_value())


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)  # type: ignore[union-attr]
    return getattr(obj, key, default)


def _default_factory(api_key: str) -> Any:
    import stripe

    return stripe.StripeClient(api_key)


class StripeClient:
    def __init__(
        self, api_key: str, *, client_factory: Callable[[str], Any] | None = None
    ) -> None:
        if not api_key:
            raise ValueError("Stripe restricted key is not configured")
        self._client = (client_factory or _default_factory)(api_key)

    async def create_product(
        self, *, name: str, description: str, price_usd: Decimal
    ) -> dict[str, str]:
        def run() -> dict[str, str]:
            v1 = self._client.v1
            params: dict[str, Any] = {"name": name, "metadata": {"source": "cashmaxx"}}
            if description:
                params["description"] = description
            product = v1.products.create(params=params)
            price = v1.prices.create(params={
                "product": _get(product, "id"), "unit_amount": usd_to_cents(price_usd),
                "currency": "usd",
            })
            link = v1.payment_links.create(params={
                "line_items": [{"price": _get(price, "id"), "quantity": 1}],
                "metadata": {"source": "cashmaxx"},
            })
            return {
                "product_id": str(_get(product, "id")),
                "price_id": str(_get(price, "id")),
                "payment_link_url": str(_get(link, "url")),
                "price_usd": fmt_usd(price_usd),
            }

        return await asyncio.to_thread(run)

    async def completed_sessions_since(
        self, cursor: int | None
    ) -> tuple[list[StripeSale], int | None]:
        """Paid, completed Checkout Sessions created at or after ``cursor`` (unix seconds).

        The new cursor is the newest ``created`` seen. Sessions at the cursor second are returned
        again on the next call; callers dedupe on the session id.
        """

        def run() -> tuple[list[StripeSale], int | None]:
            params: dict[str, Any] = {"status": "complete", "limit": 100}
            if cursor is not None:
                params["created"] = {"gte": cursor}
            page = self._client.v1.checkout.sessions.list(params=params)
            sales: list[StripeSale] = []
            newest = cursor
            for s in page.auto_paging_iter():
                created = int(_get(s, "created", 0))
                newest = created if newest is None else max(newest, created)
                if _get(s, "payment_status") != "paid":
                    continue
                currency = str(_get(s, "currency") or "").lower()
                cents = int(_get(s, "amount_total") or 0)
                amount = (Decimal(cents) * _CENT) if currency == "usd" else ZERO
                note = str(_get(s, "payment_link") or "checkout")
                if currency != "usd":
                    note = f"non-USD sale ({currency} {cents}), not converted"
                sales.append(StripeSale(
                    session_id=str(_get(s, "id")), created=created, amount_usd=amount,
                    currency=currency, note=note,
                ))
            return sales, newest

        return await asyncio.to_thread(run)
