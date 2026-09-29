"""Append-only writes to ``system_events``.

No update/delete helpers exist on purpose: the event log is evidence.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.common.ids import event_id as make_event_id
from app.events.bus import EventEnvelope
from app.models.system_event import SystemEvent

MAX_PAYLOAD_BYTES = 16_384


def _truncate_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep a single pathological payload from bloating the log."""
    import json

    text = json.dumps(payload, default=str)
    if len(text.encode("utf-8")) <= MAX_PAYLOAD_BYTES:
        return payload
    return {"_truncated": True, "_preview": text[:2048], "_original_bytes": len(text)}


def record_event(
    session: Session,
    envelope: EventEnvelope,
    *,
    software_version: str,
    strategy_version: str | None = None,
) -> SystemEvent:
    """Persist an envelope. Idempotent: an identical event id is a no-op."""
    existing = session.get(SystemEvent, envelope.event_id)
    if existing is not None:
        return existing
    row = SystemEvent(
        event_id=envelope.event_id,
        event_type=envelope.event_type,
        timestamp=envelope.timestamp,
        sequence=envelope.seq,
        symbol=envelope.symbol,
        timeframe=envelope.timeframe,
        entity_id=envelope.entity_id,
        payload=_truncate_payload(dict(envelope.payload)),
        software_version=software_version,
        strategy_version=strategy_version,
    )
    session.add(row)
    session.flush()
    return row


def record_many(
    session: Session,
    envelopes: Iterable[EventEnvelope],
    *,
    software_version: str,
    strategy_version: str | None = None,
) -> int:
    """Bulk-append events: one existence query, one executemany insert.

    Per-row ``session.get`` + ``flush`` is the difference between replaying
    20k candles in seconds and in minutes, so the batch path is the one the
    ingestor uses.
    """
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for envelope in envelopes:
        event_key = envelope.event_id
        if event_key in seen:
            continue
        seen.add(event_key)
        rows.append(
            {
                "event_id": event_key,
                "event_type": envelope.event_type,
                "timestamp": envelope.timestamp,
                "sequence": envelope.seq,
                "symbol": envelope.symbol,
                "timeframe": envelope.timeframe,
                "entity_id": envelope.entity_id,
                "payload": _truncate_payload(dict(envelope.payload)),
                "software_version": software_version,
                "strategy_version": strategy_version,
            }
        )
    if not rows:
        return 0

    existing: set[str] = set()
    identifiers = [row["event_id"] for row in rows]
    for chunk_start in range(0, len(identifiers), 500):
        chunk = identifiers[chunk_start : chunk_start + 500]
        existing.update(
            session.execute(select(SystemEvent.event_id).where(SystemEvent.event_id.in_(chunk))).scalars()
        )

    fresh = [row for row in rows if row["event_id"] not in existing]
    if fresh:
        session.execute(SystemEvent.__table__.insert(), fresh)
    return len(fresh)


def record_simple(
    session: Session,
    *,
    event_type: str,
    timestamp: datetime,
    software_version: str,
    entity_id: str | None = None,
    symbol: str | None = None,
    timeframe: str | None = None,
    payload: dict[str, Any] | None = None,
    seq: int = 0,
    strategy_version: str | None = None,
) -> SystemEvent:
    """Convenience wrapper when no bus envelope exists yet (startup, scripts)."""
    envelope = EventEnvelope(
        event_type=event_type,
        timestamp=timestamp,
        entity_id=entity_id,
        symbol=symbol,
        timeframe=timeframe,
        payload=payload or {},
        seq=seq,
    )
    return record_event(
        session, envelope, software_version=software_version, strategy_version=strategy_version
    )


def recent(session: Session, *, limit: int = 100, event_type: str | None = None) -> list[SystemEvent]:
    stmt: Select = (
        select(SystemEvent).order_by(SystemEvent.timestamp.desc(), SystemEvent.sequence.desc()).limit(limit)
    )
    if event_type:
        stmt = stmt.where(SystemEvent.event_type == event_type)
    return list(session.execute(stmt).scalars().all())


def count(session: Session, *, event_type: str | None = None) -> int:
    from sqlalchemy import func

    stmt = select(func.count()).select_from(SystemEvent)
    if event_type:
        stmt = stmt.where(SystemEvent.event_type == event_type)
    return int(session.execute(stmt).scalar() or 0)


def event_id_for(kind: str, entity_id: str, ts: datetime, seq: int = 0) -> str:
    """Exposed so callers can precompute an id (tests, dry runs)."""
    return make_event_id(kind, entity_id, ts, seq)


__all__ = [
    "MAX_PAYLOAD_BYTES",
    "count",
    "event_id_for",
    "recent",
    "record_event",
    "record_many",
    "record_simple",
]
