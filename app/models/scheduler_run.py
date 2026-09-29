"""Scheduler heartbeats.

One row per tick attempt. ``MISSED`` / ``FAILED`` rows are what the dashboard
and the integrity monitor alert on: a silently dead scheduler is the most
dangerous failure mode of a candle-driven engine (orders pile up pending and
never execute).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.database.types import TS

RUN_STATUSES = ("RUNNING", "SUCCESS", "FAILED", "MISSED", "SKIPPED")


class SchedulerRun(Base):
    __tablename__ = "scheduler_runs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(TS, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(TS)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="RUNNING")
    candles_processed: Mapped[int] = mapped_column(nullable=False, default=0)
    details: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_scheduler_runs_name_started", "name", "started_at"),
        Index("ix_scheduler_runs_status", "status"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SchedulerRun {self.name} {self.status} {self.started_at.isoformat()}>"


__all__ = ["RUN_STATUSES", "SchedulerRun"]
