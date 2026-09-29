"""Explicit, deterministic decimal rounding.

Why this module exists: ``Decimal`` arithmetic is exact but *banker's/half-up*
defaults and float leakage are the two classic ways a backtest and a live run
disagree. All money/quantity values crossing a persistence boundary are
quantized **here**, explicitly, at the call site.

Scales (frozen — they are part of the persisted format):
* PRICE_SCALE = 8   (ETHUSDT tick precision comfortably fits)
* QTY_SCALE   = 10
* MONEY_SCALE = 8   (fees, PnL, notionals in quote currency)
* RATIO_SCALE = 8   (percentages/confidence stored as decimals)
"""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal, localcontext

PRICE_SCALE = 8
QTY_SCALE = 10
MONEY_SCALE = 8
RATIO_SCALE = 8

ROUNDING = ROUND_HALF_EVEN


def to_decimal(value: Decimal | int | float | str) -> Decimal:
    """Convert to Decimal *via str* so floats never inject binary noise."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"non-finite float not allowed: {value!r}")
        return Decimal(repr(value))
    return Decimal(str(value))


def _quantize(value: Decimal | int | float | str, scale: int) -> Decimal:
    q = Decimal(1).scaleb(-scale)
    with localcontext() as ctx:
        ctx.prec = 60
        return to_decimal(value).quantize(q, rounding=ROUNDING)


def price(value: Decimal | int | float | str) -> Decimal:
    """Money price, 8 dp."""
    return _quantize(value, PRICE_SCALE)


def qty(value: Decimal | int | float | str) -> Decimal:
    """Base-asset quantity, 10 dp."""
    return _quantize(value, QTY_SCALE)


def money(value: Decimal | int | float | str) -> Decimal:
    """Quote-currency amount (cash, fee, PnL, notional), 8 dp."""
    return _quantize(value, MONEY_SCALE)


def ratio(value: Decimal | int | float | str) -> Decimal:
    """Dimensionless ratio (confidence, pct), 8 dp."""
    return _quantize(value, RATIO_SCALE)


def floor_to_increment(value: Decimal, increment: Decimal) -> Decimal:
    """Round *down* to a venue lot/tick increment (never round exposure up)."""
    if increment <= 0:
        raise ValueError("increment must be > 0")
    return (to_decimal(value) / increment).to_integral_value(rounding=ROUND_DOWN) * increment


def bps(notional: Decimal | int | float | str, basis_points: Decimal | int | float | str) -> Decimal:
    """Apply basis points (1 bp = 0.01%) to a notional. Used for slippage."""
    return money(to_decimal(notional) * to_decimal(basis_points) / Decimal(10_000))


def zero() -> Decimal:
    return Decimal(0)


def is_zero(value: Decimal) -> bool:
    return value == 0


__all__ = [
    "MONEY_SCALE",
    "PRICE_SCALE",
    "QTY_SCALE",
    "RATIO_SCALE",
    "ROUNDING",
    "bps",
    "floor_to_increment",
    "is_zero",
    "money",
    "price",
    "qty",
    "ratio",
    "to_decimal",
    "zero",
]
