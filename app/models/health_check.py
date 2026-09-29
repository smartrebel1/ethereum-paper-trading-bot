"""Persisted health probes.

The HTTP ``/health`` endpoint reports the *current instant*; this table keeps
the history, so "was the engine healthy during the 08:00 candle?" is a query
rather than a guess.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.database.types import TS

HEALTH_STATUSES = ("OK", "DEGRADED", "DOWN")


class HealthCheck(Base):
    __tablename__ = "health_checks"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    component: Mapped[str] = mapped_column(String(48), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # OK/DEGRADED/DOWN
    checked_at: Mapped[datetime] = mapped_column(TS, nullable=False)
    details: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_health_checks_component_time", "component", "checked_at"),
        Index("ix_health_checks_status_time", "status", "checked_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<HealthCheck {self.component} {self.status} {self.checked_at.isoformat()}>"


__all__ = ["HEALTH_STATUSES", "HealthCheck"]
