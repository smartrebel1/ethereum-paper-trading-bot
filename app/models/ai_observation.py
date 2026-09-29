"""AI shadow observations — a write-only side channel.

Contamination rule (master prompt §AI isolation): the AI observer may see
market context only, and **nothing it produces may be read by the strategy,
risk or execution engines**. That rule is enforced structurally:

* the baseline pipeline never imports this module (checked by a test),
* observations live in their own table with no FK into the trading path,
* ``input_context`` records exactly what was sent, so the isolation claim is
  auditable rather than aspirational.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.database.types import TS


class AIObservation(Base):
    __tablename__ = "ai_observations"

    observation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")

    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)
    at_candle_open_time: Mapped[datetime] = mapped_column(TS, nullable=False)

    #: Only market context is ever sent: never signals, risk output or orders.
    input_context: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    raw_response: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    parsed_summary: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    latency_ms: Mapped[int | None] = mapped_column()
    error: Mapped[str | None] = mapped_column(String(256))

    created_at: Mapped[datetime] = mapped_column(TS, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "provider",
            "symbol",
            "timeframe",
            "at_candle_open_time",
            name="uq_ai_observations_provider_candle",
        ),
        Index("ix_ai_observations_sym_tf_time", "symbol", "timeframe", "at_candle_open_time"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<AIObservation {self.provider} {self.at_candle_open_time.isoformat()}>"


__all__ = ["AIObservation"]
