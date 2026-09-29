"""Integrity violations.

Separate from ``system_events`` on purpose: this table is the "something is
wrong, investigate before trading again" queue, and it is the table the
dashboard/alerting reads. Rows are never deleted or edited; resolution is
recorded as a *new* ``STATE_RECOVERED`` system event, keeping the audit trail
append-only.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.database.types import TS


class IntegrityEvent(Base):
    __tablename__ = "integrity_events"

    integrity_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    code: Mapped[str] = mapped_column(String(48), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="ERROR")

    entity_id: Mapped[str | None] = mapped_column(String(128))
    symbol: Mapped[str | None] = mapped_column(String(32))
    timeframe: Mapped[str | None] = mapped_column(String(8))

    details: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    detected_at: Mapped[datetime] = mapped_column(TS, nullable=False)
    software_version: Mapped[str] = mapped_column(String(16), nullable=False)

    __table_args__ = (
        Index("ix_integrity_events_code_time", "code", "detected_at"),
        Index("ix_integrity_events_severity_time", "severity", "detected_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<IntegrityEvent {self.code} {self.severity} {self.detected_at.isoformat()}>"


__all__ = ["IntegrityEvent"]
