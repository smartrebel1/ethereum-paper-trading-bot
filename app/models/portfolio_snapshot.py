"""Equity-curve points, one per processed candle.

Snapshots are taken at candle boundaries (not on a wall-clock timer) so the
equity curve is index-aligned with the candles: reopening the dashboard
tomorrow reproduces today's curve exactly.

``equity = cash + reserved + open_position_value`` and the CHECK constraints
assert the accounting identity at the row level.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.database.types import MONEY, RATIO, TS


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"

    snapshot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)

    at_candle_open_time: Mapped[datetime] = mapped_column(TS, nullable=False)

    cash: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    reserved: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    open_position_value: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    unrealized_pnl: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    realized_pnl_cum: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    fees_cum: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    equity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    peak_equity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    drawdown_pct: Mapped[Decimal] = mapped_column(RATIO, nullable=False, default=Decimal(0))

    created_at: Mapped[datetime] = mapped_column(TS, nullable=False)

    __table_args__ = (
        UniqueConstraint("symbol", "timeframe", "at_candle_open_time", name="uq_snapshots_symbol_tf_time"),
        CheckConstraint("cash >= 0", name="ck_snapshots_cash_nonneg"),
        CheckConstraint("reserved >= 0", name="ck_snapshots_reserved_nonneg"),
        CheckConstraint(
            "equity = cash + reserved + open_position_value", name="ck_snapshots_equity_identity"
        ),
        CheckConstraint("drawdown_pct >= 0", name="ck_snapshots_drawdown_nonneg"),
        Index("ix_snapshots_sym_tf_time", "symbol", "timeframe", "at_candle_open_time"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<PortfolioSnapshot {self.at_candle_open_time.isoformat()} equity={self.equity}>"


__all__ = ["PortfolioSnapshot"]
