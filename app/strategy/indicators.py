"""Indicators, implemented deterministically on :class:`Decimal`.

Why not a library:

* **Exactness.** pandas/TA-Lib work in float64; a decision that flips on the
  15th significant digit is a decision that cannot be reproduced. Here every
  value is Decimal, computed at high internal precision and quantised once, so
  the same input always yields the same output - across machines and versions.
* **No look-ahead by construction.** Both functions take a sequence and return
  a value *per index*, where the value at index ``i`` depends only on inputs at
  indices ``<= i``. There is no "shift" or "center" parameter to misuse. A test
  asserts the prefix property directly: recomputing over a truncated series
  gives identical values for the shared prefix.

Seeding convention (matches TradingView/Binance charts for EMA and the original
Wilder definition for ATR):

* EMA: seeded with the SMA of the first ``period`` values.
* ATR (Wilder smoothing): seeded with the mean of the first ``period`` true
  ranges, then ``ATR_i = (ATR_{i-1} * (period - 1) + TR_i) / period``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext

from app.common.hashing import canonical_decimal
from app.common.rounding import PRICE_SCALE

#: Internal working precision. Generous: 50 digits, quantised at the end.
WORKING_PRECISION = 50

#: Multiplier used to detect a "meaningful" EMA slope, in ATR units.
SLOPE_ATR_FRACTION = Decimal("0.25")

#: Displacement from the EMA used to normalise the trend component, in ATR units.
TREND_ATR_FRACTION = Decimal("1.5")


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-PRICE_SCALE))


def sma(values: Sequence[Decimal], period: int) -> list[Decimal | None]:
    """Simple moving average; ``None`` until the window is full."""
    if period <= 0:
        raise ValueError("period must be positive")
    out: list[Decimal | None] = [None] * len(values)
    with localcontext() as ctx:
        ctx.prec = WORKING_PRECISION
        running = Decimal(0)
        for index, value in enumerate(values):
            running += value
            if index >= period:
                running -= values[index - period]
            if index >= period - 1:
                out[index] = _quantize(running / period)
    return out


def ema(values: Sequence[Decimal], period: int) -> list[Decimal | None]:
    """Exponential moving average, seeded with the SMA of the first window."""
    if period <= 0:
        raise ValueError("period must be positive")
    out: list[Decimal | None] = [None] * len(values)
    if len(values) < period:
        return out
    with localcontext() as ctx:
        ctx.prec = WORKING_PRECISION
        alpha = Decimal(2) / Decimal(period + 1)
        seed = sum(values[:period]) / period
        out[period - 1] = _quantize(seed)
        previous = seed
        for index in range(period, len(values)):
            previous = (values[index] - previous) * alpha + previous
            out[index] = _quantize(previous)
    return out


def true_range(highs: Sequence[Decimal], lows: Sequence[Decimal], closes: Sequence[Decimal]) -> list[Decimal]:
    """Wilder's true range. Index 0 has no previous close, so it is high - low."""
    if not (len(highs) == len(lows) == len(closes)):
        raise ValueError("highs, lows and closes must have equal length")
    out: list[Decimal] = []
    for index in range(len(highs)):
        high_low = highs[index] - lows[index]
        if index == 0:
            out.append(high_low)
            continue
        previous_close = closes[index - 1]
        out.append(
            max(
                high_low,
                abs(highs[index] - previous_close),
                abs(lows[index] - previous_close),
            )
        )
    return out


def atr(
    highs: Sequence[Decimal], lows: Sequence[Decimal], closes: Sequence[Decimal], period: int
) -> list[Decimal | None]:
    """Average true range with Wilder smoothing."""
    if period <= 0:
        raise ValueError("period must be positive")
    ranges = true_range(highs, lows, closes)
    out: list[Decimal | None] = [None] * len(ranges)
    if len(ranges) < period:
        return out
    with localcontext() as ctx:
        ctx.prec = WORKING_PRECISION
        previous = sum(ranges[:period]) / period
        out[period - 1] = _quantize(previous)
        for index in range(period, len(ranges)):
            previous = (previous * (period - 1) + ranges[index]) / period
            out[index] = _quantize(previous)
    return out


@dataclass(frozen=True, slots=True)
class IndicatorSnapshot:
    """Indicator values at one candle index (all derived from data up to it)."""

    index: int
    close: Decimal
    ema: Decimal | None
    ema_previous: Decimal | None
    atr: Decimal | None

    @property
    def warm(self) -> bool:
        return self.ema is not None and self.atr is not None and self.ema_previous is not None

    @property
    def ema_slope(self) -> Decimal | None:
        if self.ema is None or self.ema_previous is None:
            return None
        return self.ema - self.ema_previous

    def as_dict(self) -> dict[str, str | None]:
        """Canonical string form for persistence in ``signals.indicator_context``."""
        return {
            "close": canonical_decimal(self.close),
            "ema": None if self.ema is None else canonical_decimal(self.ema),
            "ema_prev": None if self.ema_previous is None else canonical_decimal(self.ema_previous),
            "atr": None if self.atr is None else canonical_decimal(self.atr),
            "ema_slope": None if self.ema_slope is None else canonical_decimal(self.ema_slope),
            "warm": self.warm,
        }


__all__ = [
    "SLOPE_ATR_FRACTION",
    "TREND_ATR_FRACTION",
    "WORKING_PRECISION",
    "IndicatorSnapshot",
    "atr",
    "ema",
    "sma",
    "true_range",
]
