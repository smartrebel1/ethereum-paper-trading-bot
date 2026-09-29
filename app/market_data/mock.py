"""Scripted provider for tests, replay fixtures and adversarial cases.

Deterministic by construction: you hand it the exact candles you want the
engine to see, including hostile ones (unaligned, out of order, duplicated,
with gaps, with the in-progress candle present). It never invents data.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from app.common.enums import Timeframe
from app.common.time_utils import next_open_time
from app.market_data.base import MarketDataProvider, ProviderHealth, RawCandle

DEFAULT_PRICE = Decimal("2500")


def make_candle(
    open_time: datetime,
    timeframe: Timeframe | str = Timeframe.H4,
    *,
    symbol: str = "ETHUSDT",
    open_price: Decimal | str = DEFAULT_PRICE,
    high: Decimal | str | None = None,
    low: Decimal | str | None = None,
    close: Decimal | str | None = None,
    volume: Decimal | str = "100",
    is_complete: bool = True,
    source: str = "mock",
) -> RawCandle:
    """Build a well-formed candle; override fields to make it malformed."""
    open_value = Decimal(str(open_price))
    close_value = Decimal(str(close)) if close is not None else open_value
    high_value = Decimal(str(high)) if high is not None else max(open_value, close_value)
    low_value = Decimal(str(low)) if low is not None else min(open_value, close_value)
    tf = timeframe.value if isinstance(timeframe, Timeframe) else str(timeframe)
    return RawCandle(
        symbol=symbol,
        timeframe=tf,
        open_time=open_time,
        close_time=next_open_time(open_time, tf),
        open=open_value,
        high=high_value,
        low=low_value,
        close=close_value,
        volume=Decimal(str(volume)),
        is_complete=is_complete,
        source=source,
    )


class MockProvider(MarketDataProvider):
    """Returns exactly the candles it was given (filtered by range)."""

    name = "mock"
    is_offline = True

    def __init__(self, candles: list[RawCandle] | None = None, *, name: str | None = None) -> None:
        self.candles: list[RawCandle] = list(candles or [])
        if name:
            self.name = name

    def add(self, candle: RawCandle) -> MockProvider:
        self.candles.append(candle)
        return self

    def extend(self, candles: list[RawCandle]) -> MockProvider:
        self.candles.extend(candles)
        return self

    def fetch_candles(
        self,
        symbol: str,
        timeframe: Timeframe | str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        include_incomplete: bool = False,
    ) -> list[RawCandle]:
        tf = timeframe.value if isinstance(timeframe, Timeframe) else str(timeframe)
        selected = [candle for candle in self.candles if candle.symbol == symbol and candle.timeframe == tf]
        if not include_incomplete:
            selected = [candle for candle in selected if candle.is_complete]
        if start is not None:
            selected = [candle for candle in selected if candle.open_time >= start]
        if end is not None:
            selected = [candle for candle in selected if candle.open_time < end]
        # Deliberately preserves caller-provided ordering: adversarial tests rely
        # on being able to present out-of-order data to the ingestor.
        return selected

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            name=self.name,
            reachable=True,
            detail=f"{len(self.candles)} scripted candles",
            last_candle_open_time=self.candles[-1].open_time if self.candles else None,
        )


__all__ = ["DEFAULT_PRICE", "MockProvider", "make_candle"]
