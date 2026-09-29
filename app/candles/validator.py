"""Candle validation — the gate between "raw" and "usable".

Every check returns a *reason*, never a boolean, and every rejection maps to an
:class:`~app.integrity.enums.IntegrityCode`, so the operator always learns
*which* invariant failed and *by how much*.

Checks, in the order they are applied (cheapest and most fundamental first):

1. identity matches the configured symbol/timeframe,
2. ``open_time`` is aligned to the timeframe grid and ``close_time`` is exactly
   the exclusive boundary,
3. OHLC coherence and positivity,
4. volume is non-negative,
5. the candle is complete (``is_complete`` **and** ``close_time <= now``) —
   this is the "current candle rule" (§14) expressed in code, not convention,
6. ordering relative to the previous candle of the same series.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.common.enums import Timeframe
from app.common.time_utils import ensure_aware_utc, is_aligned, next_open_time
from app.integrity.enums import IntegrityCode
from app.market_data.base import RawCandle

ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Outcome of validating one candle."""

    ok: bool
    code: IntegrityCode | None = None
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def valid(cls) -> ValidationResult:
        return cls(ok=True)

    @classmethod
    def invalid(cls, code: IntegrityCode, message: str, **details: Any) -> ValidationResult:
        return cls(ok=False, code=code, message=message, details=details)


class CandleValidator:
    """Stateless validator bound to one symbol/timeframe pair."""

    def __init__(self, symbol: str, timeframe: Timeframe | str) -> None:
        self.symbol = symbol
        self.timeframe = timeframe.value if isinstance(timeframe, Timeframe) else str(timeframe)

    # ---------------------------------------------------------------- public
    def validate(
        self,
        candle: RawCandle,
        *,
        now: datetime,
        previous_open_time: datetime | None = None,
    ) -> ValidationResult:
        """Validate one candle. ``now`` drives the completion rule."""
        for check in (
            self._check_identity,
            self._check_alignment,
            self._check_ohlc,
            self._check_volume,
        ):
            result = check(candle)
            if not result.ok:
                return result

        result = self._check_completeness(candle, now=now)
        if not result.ok:
            return result

        if previous_open_time is not None:
            result = self._check_order(candle, previous_open_time)
            if not result.ok:
                return result

        return ValidationResult.valid()

    # ---------------------------------------------------------------- checks
    def _check_identity(self, candle: RawCandle) -> ValidationResult:
        if candle.symbol != self.symbol:
            return ValidationResult.invalid(
                IntegrityCode.MISALIGNED_CANDLE,
                f"symbol mismatch: expected {self.symbol}, got {candle.symbol}",
                expected=self.symbol,
                actual=candle.symbol,
            )
        if candle.timeframe != self.timeframe:
            return ValidationResult.invalid(
                IntegrityCode.MISALIGNED_CANDLE,
                f"timeframe mismatch: expected {self.timeframe}, got {candle.timeframe}",
                expected=self.timeframe,
                actual=candle.timeframe,
            )
        return ValidationResult.valid()

    def _check_alignment(self, candle: RawCandle) -> ValidationResult:
        try:
            ensure_aware_utc(candle.open_time, field="open_time")
            ensure_aware_utc(candle.close_time, field="close_time")
        except ValueError as exc:
            return ValidationResult.invalid(
                IntegrityCode.MISALIGNED_CANDLE, str(exc), open_time=str(candle.open_time)
            )

        if not is_aligned(candle.open_time, self.timeframe):
            return ValidationResult.invalid(
                IntegrityCode.MISALIGNED_CANDLE,
                f"open_time {candle.open_time.isoformat()} is not aligned to {self.timeframe}",
                open_time=candle.open_time.isoformat(),
            )

        expected_close = next_open_time(candle.open_time, self.timeframe)
        if candle.close_time != expected_close:
            return ValidationResult.invalid(
                IntegrityCode.MISALIGNED_CANDLE,
                "close_time is not the exclusive boundary of open_time",
                expected_close=expected_close.isoformat(),
                actual_close=candle.close_time.isoformat(),
            )
        return ValidationResult.valid()

    def _check_ohlc(self, candle: RawCandle) -> ValidationResult:
        values = {
            "open": candle.open,
            "high": candle.high,
            "low": candle.low,
            "close": candle.close,
        }
        zero_or_negative = {name: str(value) for name, value in values.items() if value <= ZERO}
        if zero_or_negative:
            return ValidationResult.invalid(
                IntegrityCode.NON_POSITIVE_PRICE,
                f"non-positive price(s): {zero_or_negative}",
                prices={name: str(value) for name, value in values.items()},
            )

        errors: list[str] = []
        if candle.high < candle.low:
            errors.append("high < low")
        if candle.high < candle.open:
            errors.append("high < open")
        if candle.high < candle.close:
            errors.append("high < close")
        if candle.low > candle.open:
            errors.append("low > open")
        if candle.low > candle.close:
            errors.append("low > close")
        if errors:
            return ValidationResult.invalid(
                IntegrityCode.OHLC_INVARIANT_BROKEN,
                "; ".join(errors),
                ohlc={name: str(value) for name, value in values.items()},
            )
        return ValidationResult.valid()

    def _check_volume(self, candle: RawCandle) -> ValidationResult:
        if candle.volume < ZERO:
            return ValidationResult.invalid(
                IntegrityCode.NEGATIVE_VOLUME,
                f"negative volume: {candle.volume}",
                volume=str(candle.volume),
            )
        return ValidationResult.valid()

    def _check_completeness(self, candle: RawCandle, *, now: datetime) -> ValidationResult:
        aware_now = ensure_aware_utc(now, field="now")
        if not candle.is_complete:
            return ValidationResult.invalid(
                IntegrityCode.INCOMPLETE_CANDLE_USED,
                "provider marked the candle as in-progress",
                open_time=candle.open_time.isoformat(),
                close_time=candle.close_time.isoformat(),
            )
        if candle.close_time > aware_now:
            return ValidationResult.invalid(
                IntegrityCode.INCOMPLETE_CANDLE_USED,
                "candle has not closed yet",
                close_time=candle.close_time.isoformat(),
                now=aware_now.isoformat(),
            )
        return ValidationResult.valid()

    def _check_order(self, candle: RawCandle, previous_open_time: datetime) -> ValidationResult:
        if candle.open_time <= previous_open_time:
            return ValidationResult.invalid(
                IntegrityCode.OUT_OF_ORDER_CANDLE,
                "candle is not strictly newer than the previously processed candle",
                previous_open_time=previous_open_time.isoformat(),
                open_time=candle.open_time.isoformat(),
            )
        return ValidationResult.valid()


__all__ = ["CandleValidator", "ValidationResult"]
