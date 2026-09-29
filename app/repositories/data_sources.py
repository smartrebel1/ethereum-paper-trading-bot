"""Data provenance: which dataset produced a result, and when was it ingested.

A ``dataset_hash`` pins the exact candle series (see
``app.common.hashing.dataset_hash`` and the archive manifest). If a report's
hash does not match what the archive produces today, the report is not
comparable — that is the point.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.data_source import DataSource
from app.repositories.base import get_or_create


def ensure_registered(
    session: Session,
    *,
    name: str,
    dataset_hash: str,
    symbol: str,
    timeframe: str,
    now: datetime,
    software_version: str,
    kind: str = "archive",
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    candle_count: int | None = None,
    metadata: dict | None = None,
) -> tuple[DataSource, bool]:
    return get_or_create(
        session,
        DataSource,
        name=name,
        dataset_hash=dataset_hash,
        defaults={
            "kind": kind,
            "symbol": symbol,
            "timeframe": timeframe,
            "start_time": start_time,
            "end_time": end_time,
            "candle_count": candle_count,
            "metadata_json": {**(metadata or {}), "software_version": software_version},
            "registered_at": now,
        },
    )


def latest(session: Session, *, symbol: str, timeframe: str) -> DataSource | None:
    stmt = (
        select(DataSource)
        .where(DataSource.symbol == symbol, DataSource.timeframe == timeframe)
        .order_by(DataSource.registered_at.desc())
        .limit(1)
    )
    return session.execute(stmt).scalars().first()


def all_for(session: Session, *, symbol: str, timeframe: str) -> list[DataSource]:
    stmt = (
        select(DataSource)
        .where(DataSource.symbol == symbol, DataSource.timeframe == timeframe)
        .order_by(DataSource.registered_at.asc())
    )
    return list(session.execute(stmt).scalars().all())


__all__ = ["all_for", "ensure_registered", "latest"]
