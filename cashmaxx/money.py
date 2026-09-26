"""Money helpers. Amounts are ``Decimal`` in code and decimal strings on the wire."""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal, InvalidOperation

USDC_DECIMALS = 6
_CENT = Decimal("0.01")
_USDC_UNIT = Decimal(10) ** -USDC_DECIMALS
ZERO = Decimal("0")


def parse_usd(value: str | int | float | Decimal) -> Decimal:
    """Parse a USD amount. Floats are converted through ``str`` to avoid binary noise."""
    if isinstance(value, bool):
        raise ValueError("amount must be a number, not a bool")
    try:
        amount = Decimal(str(value)) if not isinstance(value, Decimal) else value
    except InvalidOperation as exc:
        raise ValueError(f"invalid amount: {value!r}") from exc
    if not amount.is_finite():
        raise ValueError(f"invalid amount: {value!r}")
    return amount.quantize(_USDC_UNIT, rounding=ROUND_DOWN)


def fmt_usd(amount: Decimal) -> str:
    """Wire format: plain decimal string with 2 to 6 decimals (``"1.50"``, ``"0.001"``)."""
    q = amount.quantize(_USDC_UNIT, rounding=ROUND_DOWN).normalize()
    if q.as_tuple().exponent > -2:  # type: ignore[operator]
        q = q.quantize(_CENT)
    return format(q, "f")


def to_usdc_units(amount: Decimal) -> int:
    """USD (1 USDC == 1 USD) to on-chain base units (6 decimals)."""
    return int((amount / _USDC_UNIT).to_integral_value(rounding=ROUND_DOWN))


def from_usdc_units(units: int) -> Decimal:
    return (Decimal(units) * _USDC_UNIT).quantize(_USDC_UNIT)
