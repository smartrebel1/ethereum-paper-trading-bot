"""Orders (intents), created by the order engine from an approved signal.

* ``(signal_id, purpose)`` is unique: at most one ENTRY and one EXIT order per
  signal (idempotent retries). An exit is part of the same signal's lifecycle —
  it is not a new decision — so it reuses the entry signal id and is told apart
  by ``purpose``. This keeps ``ck_signals_target_after_candle`` (the database's
  expression of "no look-ahead") untouched: an exit is settled at a level that
  was already known when the entry was created, never at a later candle's open.
* ``target_execution_open_time`` is copied from the signal and is the only
  candle the order may be filled by.
* ``ck_orders_paper_only`` makes a non-paper provider literally unrepresentable
  in the database, even if application code were compromised.
* ``ck_orders_state_valid`` enumerates the six legal states (see
  docs/STATE_MACHINE.md).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.database.types import MONEY, QTY, TS


class Order(Base):
    __tablename__ = "orders"

    order_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    signal_id: Mapped[str] = mapped_column(String(64), ForeignKey("signals.signal_id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)

    side: Mapped[str] = mapped_column(String(8), nullable=False)  # BUY/SELL
    order_type: Mapped[str] = mapped_column(String(16), nullable=False, default="MARKET_ON_OPEN")
    purpose: Mapped[str] = mapped_column(String(16), nullable=False, default="ENTRY")  # ENTRY/EXIT

    target_execution_open_time: Mapped[datetime] = mapped_column(TS, nullable=False)
    target_execution_candle_id: Mapped[str] = mapped_column(String(128), nullable=False)

    intended_quantity: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    intended_notional: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    stop_loss_price: Mapped[Decimal | None] = mapped_column(MONEY)
    take_profit_price: Mapped[Decimal | None] = mapped_column(MONEY)

    state: Mapped[str] = mapped_column(String(24), nullable=False, default="PENDING")

    execution_provider: Mapped[str] = mapped_column(String(16), nullable=False, default="paper")

    #: Which strategy build produced this order. Denormalised on purpose: an
    #: order must be explainable without a join, and the value must survive even
    #: if the signal row were ever archived away.
    strategy_version: Mapped[str | None] = mapped_column(String(32))
    strategy_config_hash: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(TS, nullable=False)
    state_updated_at: Mapped[datetime] = mapped_column(TS, nullable=False)
    state_reason: Mapped[str] = mapped_column(String(128), nullable=False, default="")

    __table_args__ = (
        UniqueConstraint("signal_id", "purpose", name="uq_orders_signal_purpose"),
        CheckConstraint("side IN ('BUY','SELL')", name="ck_orders_side_valid"),
        CheckConstraint(
            "state IN ('CREATED','PENDING','TARGET_REACHED','EXECUTED','TARGET_MISSED','CANCELLED')",
            name="ck_orders_state_valid",
        ),
        CheckConstraint("order_type IN ('MARKET_ON_OPEN')", name="ck_orders_type_valid"),
        CheckConstraint("purpose IN ('ENTRY','EXIT')", name="ck_orders_purpose_valid"),
        CheckConstraint("execution_provider = 'paper'", name="ck_orders_paper_only"),
        CheckConstraint("intended_quantity > 0", name="ck_orders_quantity_positive"),
        CheckConstraint("intended_notional > 0", name="ck_orders_notional_positive"),
        CheckConstraint("stop_loss_price IS NULL OR stop_loss_price > 0", name="ck_orders_sl_positive"),
        CheckConstraint("take_profit_price IS NULL OR take_profit_price > 0", name="ck_orders_tp_positive"),
        Index("ix_orders_state_target", "state", "target_execution_open_time"),
        Index("ix_orders_sym_tf_created", "symbol", "timeframe", "created_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Order {self.order_id} {self.side} {self.state} "
            f"target={self.target_execution_open_time.isoformat()}>"
        )


__all__ = ["Order"]
