"""Live position state.

**One OPEN position per (symbol, timeframe)** is enforced by a *partial* unique
index (``uq_positions_one_open_per_sym_tf ... WHERE state = 'OPEN'``) created in
the migration; a plain UNIQUE constraint could not express "only when OPEN".

SL/TP are stored on the row (not recomputed) so a position can always be
explained by the numbers that were in force when it was opened — essential for
crash recovery (phase 7) and for auditing against the strategy config hash that
is also stored here.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.database.types import MONEY, PRICE, QTY, TS


class Position(Base):
    __tablename__ = "positions"

    position_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)

    side: Mapped[str] = mapped_column(String(8), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="OPEN")

    quantity: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    entry_time: Mapped[datetime] = mapped_column(TS, nullable=False)
    entry_candle_open_time: Mapped[datetime] = mapped_column(TS, nullable=False)
    entry_order_id: Mapped[str | None] = mapped_column(String(64))
    signal_id: Mapped[str | None] = mapped_column(String(64))

    stop_loss: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    take_profit: Mapped[Decimal] = mapped_column(MONEY, nullable=False)

    entry_fee: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    unrealized_pnl: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    realized_pnl: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))

    strategy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    strategy_config_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    created_at: Mapped[datetime] = mapped_column(TS, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(TS)
    close_reason: Mapped[str | None] = mapped_column(String(24))

    __table_args__ = (
        CheckConstraint("side IN ('BUY','SELL')", name="ck_positions_side_valid"),
        CheckConstraint("state IN ('OPEN','CLOSING','CLOSED')", name="ck_positions_state_valid"),
        CheckConstraint("quantity > 0", name="ck_positions_quantity_positive"),
        CheckConstraint("entry_price > 0", name="ck_positions_entry_price_positive"),
        CheckConstraint("stop_loss > 0 AND take_profit > 0", name="ck_positions_sl_tp_positive"),
        CheckConstraint(
            "close_reason IS NULL OR close_reason IN ('SL_HIT','TP_HIT','MANUAL','LIQUIDATION')",
            name="ck_positions_close_reason_valid",
        ),
        Index("ix_positions_sym_tf_state", "symbol", "timeframe", "state"),
        # Full logical definition of the "at most one open position" rule.
        # The migration materialises the same index (see 0001_initial_schema).
        Index(
            "uq_positions_one_open_per_sym_tf",
            "symbol",
            "timeframe",
            unique=True,
            sqlite_where=text("state = 'OPEN'"),
            postgresql_where=text("state = 'OPEN'"),
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Position {self.position_id} {self.side} {self.state} qty={self.quantity}>"


__all__ = ["Position"]
