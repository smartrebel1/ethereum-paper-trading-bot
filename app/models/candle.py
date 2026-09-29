"""Validated OHLCV candles.

Invariants enforced *in the database*, not only in Python:
* ``(symbol, timeframe, open_time)`` unique — re-ingesting the same candle is
  an idempotent upsert, never a second row.
* OHLC coherence (high is the max, low is the min), positive prices,
  non-negative volume, ``close_time > open_time``.
* ``timeframe`` restricted to the four supported values.

Only rows with ``is_complete = True`` may be used by the strategy layer; the
column exists in phase 1 so phase 2's ingestor has nowhere else to put the
in-progress candle (§14 current-candle rule).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.database.types import PRICE, QTY, TS


class Candle(Base):
    __tablename__ = "candles"

    candle_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=False)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)

    open_time: Mapped[datetime] = mapped_column(TS, nullable=False)
    close_time: Mapped[datetime] = mapped_column(TS, nullable=False)

    open: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    high: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    low: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    close: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    volume: Mapped[Decimal] = mapped_column(QTY, nullable=False)

    is_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")

    received_at: Mapped[datetime] = mapped_column(TS, nullable=False)
    validated_at: Mapped[datetime | None] = mapped_column(TS)

    __table_args__ = (
        UniqueConstraint("symbol", "timeframe", "open_time", name="uq_candles_symbol_tf_open"),
        CheckConstraint("timeframe IN ('15m','1h','4h','1d')", name="ck_candles_timeframe_valid"),
        CheckConstraint("close_time > open_time", name="ck_candles_close_after_open"),
        CheckConstraint(
            "open > 0 AND high > 0 AND low > 0 AND close > 0",
            name="ck_candles_prices_positive",
        ),
        CheckConstraint("high >= low", name="ck_candles_high_ge_low"),
        CheckConstraint("high >= open", name="ck_candles_high_ge_open"),
        CheckConstraint("high >= close", name="ck_candles_high_ge_close"),
        CheckConstraint("low <= open", name="ck_candles_low_le_open"),
        CheckConstraint("low <= close", name="ck_candles_low_le_close"),
        CheckConstraint("volume >= 0", name="ck_candles_volume_nonneg"),
        Index("ix_candles_sym_tf_open", "symbol", "timeframe", "open_time"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<Candle {self.symbol} {self.timeframe} open={self.open_time.isoformat()} "
            f"O={self.open} H={self.high} L={self.low} C={self.close} complete={self.is_complete}>"
        )


__all__ = ["Candle"]
