"""Append-only writes to ``integrity_events``.

``record_violation`` always writes a row *and* returns it, so the caller cannot
accidentally swallow a violation without persisting it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.common.ids import integrity_event_id
from app.integrity.enums import FATAL_INTEGRITY_CODES, IntegrityCode
from app.models.integrity_event import IntegrityEvent


def record_violation(
    session: Session,
    *,
    code: IntegrityCode | str,
    detected_at: datetime,
    software_version: str,
    entity_id: str | None = None,
    symbol: str | None = None,
    timeframe: str | None = None,
    severity: str | None = None,
    details: dict[str, Any] | None = None,
    seq: int = 0,
) -> IntegrityEvent:
    code_value = code.value if isinstance(code, IntegrityCode) else str(code)
    computed_severity = severity or (
        "CRITICAL" if code_value in {c.value for c in FATAL_INTEGRITY_CODES} else "ERROR"
    )
    row = IntegrityEvent(
        integrity_id=integrity_event_id(code_value, entity_id or "-", detected_at, seq),
        code=code_value,
        severity=computed_severity,
        entity_id=entity_id,
        symbol=symbol,
        timeframe=timeframe,
        details=details or {},
        detected_at=detected_at,
        software_version=software_version,
    )
    session.add(row)
    session.flush()
    return row


def recent(session: Session, *, limit: int = 100, code: str | None = None) -> list[IntegrityEvent]:
    stmt = select(IntegrityEvent).order_by(IntegrityEvent.detected_at.desc()).limit(limit)
    if code:
        stmt = stmt.where(IntegrityEvent.code == code)
    return list(session.execute(stmt).scalars().all())


def count(session: Session, *, code: str | None = None) -> int:
    stmt = select(func.count()).select_from(IntegrityEvent)
    if code:
        stmt = stmt.where(IntegrityEvent.code == code)
    return int(session.execute(stmt).scalar() or 0)


def has_fatal(session: Session) -> bool:
    """True if any CRITICAL violation exists (kill-switch input, phase 8)."""
    stmt = select(func.count()).select_from(IntegrityEvent).where(IntegrityEvent.severity == "CRITICAL")
    return int(session.execute(stmt).scalar() or 0) > 0


__all__ = ["count", "has_fatal", "recent", "record_violation"]
