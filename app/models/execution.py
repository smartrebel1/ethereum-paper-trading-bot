"""Executions (paper fills).

Exactly one execution per order (``order_id`` unique) — a paper fill is a
deterministic function of the target candle's open price, so a second row
would be a bug, not a partial fill.

``raw_open_price`` and ``actual_execution_price`` are stored separately so the
slippage model is auditable: you can always re-derive
``actual = raw +/- slippage`` from the row itself.

``ck_executions_paper_only`` mirrors the order-level guarantee.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.database.types import MONEY, PRICE, QTY, RATIO, TS


class Execution(Base):
    __tablename__ = "executions"

    execution_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    order_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("orders.order_id"), nullable=False, unique=True
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)

    #: Must equal the order's target execution open time — enforced in code and
    #: checked by tests; the pair (execution_candle_open_time, order.target) is
    #: verified before insert and any mismatch is an integrity failure.
    execution_candle_open_time: Mapped[datetime] = mapped_column(TS, nullable=False)
    execution_candle_id: Mapped[str] = mapped_column(String(128), nullable=False)

    raw_open_price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    slippage_bps: Mapped[Decimal] = mapped_column(RATIO, nullable=False, default=Decimal(0))
    actual_execution_price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)

    quantity: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    notional: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    fee: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))

    execution_timestamp: Mapped[datetime] = mapped_column(TS, nullable=False)
    provider: Mapped[str] = mapped_column(String(16), nullable=False, default="paper")

    __table_args__ = (
        CheckConstraint("side IN ('BUY','SELL')", name="ck_executions_side_valid"),
        CheckConstraint("provider = 'paper'", name="ck_executions_paper_only"),
        CheckConstraint("actual_execution_price > 0", name="ck_executions_price_positive"),
        CheckConstraint("raw_open_price > 0", name="ck_executions_raw_price_positive"),
        CheckConstraint("quantity > 0", name="ck_executions_quantity_positive"),
        CheckConstraint("notional >= 0", name="ck_executions_notional_nonneg"),
        CheckConstraint("fee >= 0", name="ck_executions_fee_nonneg"),
        CheckConstraint("slippage_bps >= 0", name="ck_executions_slippage_nonneg"),
        Index("ix_executions_sym_tf_candle", "symbol", "timeframe", "execution_candle_open_time"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Execution {self.execution_id} {self.side} qty={self.quantity} @ {self.actual_execution_price}>"
        )


__all__ = ["Execution"]
