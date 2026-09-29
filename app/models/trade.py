"""Closed round-trips — the immutable ledger of realised outcomes.

A trade row is written exactly once, inside the same transaction that closes
the position. Nothing updates it, ever. ``trade_id`` is derived from the
position, so a replayed run produces the same trade ids and the same PnL.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.database.types import MONEY, PRICE, QTY, TS


class Trade(Base):
    __tablename__ = "trades"

    trade_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    position_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)

    signal_id: Mapped[str] = mapped_column(String(64), ForeignKey("signals.signal_id"), nullable=False)
    entry_order_id: Mapped[str] = mapped_column(String(64), ForeignKey("orders.order_id"), nullable=False)
    exit_order_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("orders.order_id"))

    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(QTY, nullable=False)

    entry_price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    exit_price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)

    entry_time: Mapped[datetime] = mapped_column(TS, nullable=False)
    exit_time: Mapped[datetime] = mapped_column(TS, nullable=False)
    holding_seconds: Mapped[int] = mapped_column(nullable=False)

    entry_fee: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    exit_fee: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))

    gross_pnl: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    net_pnl: Mapped[Decimal] = mapped_column(MONEY, nullable=False)

    exit_reason: Mapped[str] = mapped_column(String(24), nullable=False)

    strategy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    strategy_config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False)

    __table_args__ = (
        CheckConstraint("side IN ('BUY','SELL')", name="ck_trades_side_valid"),
        CheckConstraint("quantity > 0", name="ck_trades_quantity_positive"),
        CheckConstraint("entry_price > 0 AND exit_price > 0", name="ck_trades_prices_positive"),
        CheckConstraint("holding_seconds >= 0", name="ck_trades_holding_nonneg"),
        CheckConstraint("entry_fee >= 0 AND exit_fee >= 0", name="ck_trades_fees_nonneg"),
        CheckConstraint(
            "exit_reason IN ('SL_HIT','TP_HIT','MANUAL','LIQUIDATION')",
            name="ck_trades_exit_reason_valid",
        ),
        Index("ix_trades_sym_tf_exit_time", "symbol", "timeframe", "exit_time"),
        Index("ix_trades_config_hash", "strategy_config_hash"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Trade {self.trade_id} {self.exit_reason} net_pnl={self.net_pnl}>"


__all__ = ["Trade"]
