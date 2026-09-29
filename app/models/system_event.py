"""Append-only system event log (the audit trail).

Never updated, never deleted. Every row carries ``software_version`` so a
behaviour change is attributable to a build, and the optional
``strategy_version`` so strategy-driven events are attributable to a config.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.database.types import TS


class SystemEvent(Base):
    __tablename__ = "system_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(TS, nullable=False)

    sequence: Mapped[int] = mapped_column(nullable=False, default=0)
    symbol: Mapped[str | None] = mapped_column(String(32))
    timeframe: Mapped[str | None] = mapped_column(String(8))
    entity_id: Mapped[str | None] = mapped_column(String(128))

    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    software_version: Mapped[str] = mapped_column(String(16), nullable=False)
    strategy_version: Mapped[str | None] = mapped_column(String(32))

    __table_args__ = (
        Index("ix_system_events_type_time", "event_type", "timestamp"),
        Index("ix_system_events_entity", "entity_id"),
        Index("ix_system_events_sym_tf_time", "symbol", "timeframe", "timestamp"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SystemEvent {self.event_type} {self.timestamp.isoformat()} {self.entity_id}>"


__all__ = ["SystemEvent"]
