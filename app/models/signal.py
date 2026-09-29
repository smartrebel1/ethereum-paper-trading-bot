"""Immutable strategy signals.

The single most important row in the schema. A signal is produced from a
**closed** candle and states, explicitly and immutably, which candle the
resulting order is allowed to execute on:

* ``signal_candle_open_time``    — the candle the decision was derived from.
* ``target_execution_open_time`` — the *next* candle's open time. An order for
  this signal may only be filled by a candle whose ``open_time`` equals this
  value exactly; anything else is an integrity failure, never a fill.
* ``target_execution_candle_id`` — precomputed deterministic id of that candle,
  so the execution engine compares ids (cheap, unambiguous) rather than
  re-deriving time arithmetic at fill time.

``ck_signals_target_after_candle`` forbids ``target <= signal_candle``, which
is the database-level expression of "no look-ahead, no same-candle fill".

Signals are append-only: no update path exists. A changed decision produces a
new ``signal_id`` (it includes the strategy config hash).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, CheckConstraint, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.common.rounding import RATIO_SCALE
from app.database.base import Base
from app.database.types import RATIO, TS

#: 1.0 expressed in the scaled-integer units that RATIO columns are stored in.
#: CHECK constraints compare against the *stored* integer, so a range bound of
#: ``1`` would reject every real confidence value (0.75 is stored as 75000000).
RATIO_ONE = 10**RATIO_SCALE


class Signal(Base):
    __tablename__ = "signals"

    signal_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)

    action: Mapped[str] = mapped_column(String(8), nullable=False)  # BUY/SELL/HOLD
    confidence: Mapped[Decimal] = mapped_column(RATIO, nullable=False)

    signal_candle_open_time: Mapped[datetime] = mapped_column(TS, nullable=False)
    signal_candle_close_time: Mapped[datetime] = mapped_column(TS, nullable=False)

    #: THE critical field: explicit, immutable execution target.
    target_execution_open_time: Mapped[datetime] = mapped_column(TS, nullable=False)
    target_execution_candle_id: Mapped[str] = mapped_column(String(128), nullable=False)

    strategy_name: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    strategy_config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_version: Mapped[str] = mapped_column(String(32), nullable=False, default="1")

    indicator_context: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    reason: Mapped[str] = mapped_column(String(128), nullable=False, default="")

    generation_timestamp: Mapped[datetime] = mapped_column(TS, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False)

    __table_args__ = (
        CheckConstraint("action IN ('BUY','SELL','HOLD')", name="ck_signals_action_valid"),
        CheckConstraint(
            f"confidence >= 0 AND confidence <= {RATIO_ONE}",
            name="ck_signals_confidence_range",
        ),
        CheckConstraint(
            "target_execution_open_time > signal_candle_open_time",
            name="ck_signals_target_after_candle",
        ),
        CheckConstraint(
            "signal_candle_close_time > signal_candle_open_time",
            name="ck_signals_close_after_open",
        ),
        CheckConstraint("timeframe IN ('15m','1h','4h','1d')", name="ck_signals_timeframe_valid"),
        Index("ix_signals_sym_tf_target", "symbol", "timeframe", "target_execution_open_time"),
        Index("ix_signals_sym_tf_candle", "symbol", "timeframe", "signal_candle_open_time"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Signal {self.signal_id} {self.action} conf={self.confidence} "
            f"candle={self.signal_candle_open_time.isoformat()} "
            f"target={self.target_execution_open_time.isoformat()}>"
        )


__all__ = ["Signal"]
