"""The frozen baseline strategy: EMA(200) trend + ATR(14) risk.

Decision rule (deliberately simple, deliberately explainable — the point of
phase 3 is the *plumbing*, not alpha):

    LONG when, on a **closed** candle C:
        close(C)   >  EMA200(C)                    (trend direction)
        EMA200(C)  >  EMA200(C-1)                  (trend slope positive)
        confidence(C) >= MIN_CONFIDENCE

    confidence = 0.5 * min(1, |close - EMA200| / (1.5 * ATR))
               + 0.5 * min(1, |EMA200(C) - EMA200(C-1)| / (0.25 * ATR))

    SL = entry - ATR_SL_MULTIPLIER * ATR(14)       (entry = next candle open)
    TP = entry + ATR_TP_MULTIPLIER * ATR(14)

Exits are the execution engine's job (SL/TP); the strategy only *enters*.
There is no shorting: the baseline is a long-only trend follower, which also
keeps the margin/liquidation surface out of phase 1.

Every returned signal carries the explicit execution target
(``target_execution_open_time`` = next candle open) — the strategy never decides
"when" in wall-clock terms, only "which candle".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.common.enums import Action, Side
from app.common.hashing import canonical_decimal
from app.common.ids import candle_id as make_candle_id
from app.common.ids import signal_id as make_signal_id
from app.common.rounding import price as price_of
from app.common.rounding import ratio
from app.common.time_utils import next_open_time
from app.config.strategy_config import StrategyConfig
from app.strategy.indicators import (
    SLOPE_ATR_FRACTION,
    TREND_ATR_FRACTION,
    IndicatorSnapshot,
    atr,
    ema,
)

ZERO = Decimal(0)
ONE = Decimal(1)

SUPPRESSION_WARMUP = "WARMUP"
SUPPRESSION_TREND = "TREND_FILTER"
SUPPRESSION_SLOPE = "SLOPE_FILTER"
SUPPRESSION_CONFIDENCE = "MIN_CONFIDENCE"
SUPPRESSION_ATR_UNAVAILABLE = "ATR_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class Decision:
    """The strategy's verdict on one candle. ``action == HOLD`` means "no trade"."""

    action: Action
    candle_open_time: datetime
    target_execution_open_time: datetime
    confidence: Decimal | None
    snapshot: IndicatorSnapshot
    suppressed_reason: str | None = None
    reason: str = ""

    @property
    def is_entry(self) -> bool:
        return self.action in (Action.BUY, Action.SELL)


@dataclass(frozen=True, slots=True)
class StrategySignal:
    """A persisted-able entry signal (immutable, fully specified)."""

    signal_id: str
    symbol: str
    timeframe: str
    action: Action
    side: Side
    confidence: Decimal
    signal_candle_open_time: datetime
    signal_candle_close_time: datetime
    target_execution_open_time: datetime
    target_execution_candle_id: str
    strategy_name: str
    strategy_version: str
    strategy_config_hash: str
    indicator_context: dict[str, object]
    reason: str

    @property
    def entry_side(self) -> Side:
        return self.side


class EMAAtrStrategy:
    """Stateless-per-call baseline strategy.

    ``on_candle`` is pure with respect to its inputs: given the same closed
    candles and config it produces the same decision. The engine keeps the
    window; the strategy computes indicators over it (a 200+ candle window is
    microseconds of Decimal arithmetic, so caching would add risk, not value).
    """

    name = "EMA200_ATR_BASELINE"

    def __init__(self, config: StrategyConfig) -> None:
        self.config = config
        self.symbol = config.symbol
        self.timeframe = config.timeframe
        self.ema_period = config.ema_period
        self.atr_period = config.atr_period

    # ------------------------------------------------------------------ window
    def warmup_candles(self) -> int:
        """Candles needed before an entry can be produced at all."""
        return max(self.ema_period + 1, self.atr_period + 1, self.config.warmup_candles)

    def evaluate(self, candles: Sequence[CandleView]) -> Decision:
        """Decide on the *last* candle of ``candles`` (all must be closed)."""
        if not candles:
            raise ValueError("evaluate() needs at least one candle")
        index = len(candles) - 1
        candle = candles[index]

        closes = [c.close for c in candles]
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        ema_values = ema(closes, self.ema_period)
        atr_values = atr(highs, lows, closes, self.atr_period)

        ema_now = ema_values[index]
        ema_prev = ema_values[index - 1] if index >= 1 else None
        atr_now = atr_values[index]
        snapshot = IndicatorSnapshot(
            index=index, close=candle.close, ema=ema_now, ema_previous=ema_prev, atr=atr_now
        )

        target = next_open_time(candle.open_time, self.timeframe)

        if atr_now is None or atr_now <= ZERO:
            return self._hold(candle, target, snapshot, SUPPRESSION_ATR_UNAVAILABLE, "atr unavailable")
        if not snapshot.warm:
            return self._hold(candle, target, snapshot, SUPPRESSION_WARMUP, "warmup incomplete")

        confidence = self.confidence(snapshot)
        assert ema_now is not None and ema_prev is not None  # snapshot.warm guarantees it

        if candle.close <= ema_now:
            return self._hold(
                candle, target, snapshot, SUPPRESSION_TREND, "close at or below EMA", confidence
            )
        if ema_now <= ema_prev:
            return self._hold(
                candle, target, snapshot, SUPPRESSION_SLOPE, "EMA slope not positive", confidence
            )
        if confidence < self.config.min_confidence_decimal:
            return self._hold(
                candle,
                target,
                snapshot,
                SUPPRESSION_CONFIDENCE,
                f"confidence {canonical_decimal(confidence)} below minimum",
                confidence,
            )

        return Decision(
            action=Action.BUY,
            candle_open_time=candle.open_time,
            target_execution_open_time=target,
            confidence=confidence,
            snapshot=snapshot,
            reason=(
                f"close {canonical_decimal(candle.close)} above EMA{self.ema_period} "
                f"{canonical_decimal(ema_now)} with rising slope and confidence "
                f"{canonical_decimal(confidence)}"
            ),
        )

    def confidence(self, snapshot: IndicatorSnapshot) -> Decimal:
        """Deterministic 0..1 score (see module docstring)."""
        assert snapshot.ema is not None and snapshot.atr is not None and snapshot.atr > ZERO
        atr_value = snapshot.atr

        trend_component = min(ONE, abs(snapshot.close - snapshot.ema) / (TREND_ATR_FRACTION * atr_value))
        slope = snapshot.ema_slope or ZERO
        slope_component = min(ONE, abs(slope) / (SLOPE_ATR_FRACTION * atr_value))
        raw = trend_component * Decimal("0.5") + slope_component * Decimal("0.5")
        return ratio(min(ONE, max(ZERO, raw)))

    def build_signal(self, decision: Decision) -> StrategySignal:
        """Materialise an immutable signal row payload from an entry decision."""
        if not decision.is_entry or decision.confidence is None:
            raise ValueError("build_signal() requires an entry decision")
        config = self.config
        target = decision.target_execution_open_time
        return StrategySignal(
            signal_id=make_signal_id(
                self.symbol,
                self.timeframe,
                decision.candle_open_time,
                config.name,
                config.version,
                config.config_hash,
            ),
            symbol=self.symbol,
            timeframe=self.timeframe,
            action=decision.action,
            side=Side.BUY,
            confidence=decision.confidence,
            signal_candle_open_time=decision.candle_open_time,
            signal_candle_close_time=target,
            target_execution_open_time=target,
            target_execution_candle_id=make_candle_id(self.symbol, self.timeframe, target),
            strategy_name=config.name,
            strategy_version=config.version,
            strategy_config_hash=config.config_hash,
            indicator_context=decision.snapshot.as_dict(),
            reason=decision.reason,
        )

    def stop_and_target(
        self, *, entry_price: Decimal, atr_value: Decimal, side: Side
    ) -> tuple[Decimal, Decimal]:
        """SL/TP distances from the entry, using the config multipliers."""
        sl_distance = self.config.sl_multiplier * atr_value
        tp_distance = self.config.tp_multiplier * atr_value
        if side is Side.BUY:
            stop_loss = price_of(entry_price - sl_distance)
            take_profit = price_of(entry_price + tp_distance)
        else:  # pragma: no cover - long-only baseline; kept correct for future use
            stop_loss = price_of(entry_price + sl_distance)
            take_profit = price_of(entry_price - tp_distance)
        return stop_loss, take_profit

    # ----------------------------------------------------------------- helpers
    def _hold(
        self,
        candle: CandleView,
        target: datetime,
        snapshot: IndicatorSnapshot,
        suppressed_reason: str,
        reason: str,
        confidence: Decimal | None = None,
    ) -> Decision:
        return Decision(
            action=Action.HOLD,
            candle_open_time=candle.open_time,
            target_execution_open_time=target,
            confidence=confidence,
            snapshot=snapshot,
            suppressed_reason=suppressed_reason,
            reason=reason,
        )


class CandleView:
    """Structural protocol-ish base: anything with the OHLCV + identity fields.

    Both :class:`app.models.candle.Candle` rows and
    :class:`app.market_data.base.RawCandle` satisfy it, which lets the same
    strategy run over the database (live paper) and over an in-memory series
    (backtest/replay) without a conversion layer.
    """

    symbol: str
    timeframe: str
    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


__all__ = [
    "SUPPRESSION_ATR_UNAVAILABLE",
    "SUPPRESSION_CONFIDENCE",
    "SUPPRESSION_SLOPE",
    "SUPPRESSION_TREND",
    "SUPPRESSION_WARMUP",
    "CandleView",
    "Decision",
    "EMAAtrStrategy",
    "StrategySignal",
]
