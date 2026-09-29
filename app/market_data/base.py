"""Market data provider abstraction.

Contract (every implementation must honour it, tests enforce it):

1. **Closed candles only.** ``fetch_candles`` never returns the in-progress
   candle unless ``include_incomplete=True`` is explicitly requested. The
   strategy layer must be structurally unable to see a forming candle.
2. **Ascending order.** Results are sorted by ``open_time`` ascending, with no
   duplicates.
3. **Exact decimals.** Prices/volumes arrive as :class:`Decimal` (parsed from
   text), never as floats, so nothing is lost before it reaches the validator.
4. **Aware UTC.** Timestamps are timezone-aware; a provider that cannot promise
   this must convert before returning.
5. **No silent gaps.** A provider reports what it could not deliver
   (:class:`ProviderHealth`, and the ingestor's gap detector), instead of
   interpolating or skipping quietly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.common.enums import Timeframe


@dataclass(frozen=True, slots=True)
class RawCandle:
    """A candle exactly as it left the provider (not yet validated)."""

    symbol: str
    timeframe: str
    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    is_complete: bool
    source: str
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def identity(self) -> tuple[str, str, datetime]:
        return (self.symbol, self.timeframe, self.open_time)

    def to_values(self) -> dict[str, Decimal]:
        """OHLCV as a mapping - used for duplicate-content comparison."""
        return {
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    name: str
    reachable: bool
    detail: str = ""
    last_candle_open_time: datetime | None = None
    dataset_hash: str | None = None


class MarketDataProvider(ABC):
    """Source of OHLCV candles."""

    #: Stable, human-readable name (recorded in ``candles.source`` and ``data_sources.name``).
    name: str = "abstract"

    #: True when the provider reads from local files/static archives.
    is_offline: bool = False

    @abstractmethod
    def fetch_candles(
        self,
        symbol: str,
        timeframe: Timeframe | str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        include_incomplete: bool = False,
    ) -> list[RawCandle]:
        """Return candles with ``start <= open_time < end``, ascending."""

    @abstractmethod
    def health(self) -> ProviderHealth:
        """Cheap liveness/shape report (never raises)."""

    # ---------------------------------------------------------------- helpers
    def latest_closed(
        self,
        symbol: str,
        timeframe: Timeframe | str,
        *,
        now: datetime,
    ) -> RawCandle | None:
        """Most recent candle that is closed as of ``now`` (or ``None``)."""
        candles = self.fetch_candles(symbol, timeframe, end=now, include_incomplete=False)
        closed = [c for c in candles if c.close_time <= now]
        return closed[-1] if closed else None


__all__ = ["MarketDataProvider", "ProviderHealth", "RawCandle"]
