"""Candle persistence: idempotent upsert, order questions, gap materialisation.

The database is the single source of truth for "what have we seen". Everything
here is written so that calling it twice is safe: the deterministic
``candle_id`` plus the ``(symbol, timeframe, open_time)`` unique constraint make
re-ingestion a no-op instead of a duplicate.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.common.ids import candle_id as make_candle_id
from app.market_data.base import RawCandle
from app.models.candle import Candle

UPSERT_COLUMNS = (
    "close_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "is_complete",
    "source",
    "received_at",
    "validated_at",
)


def to_row(candle: RawCandle, *, now: datetime) -> dict[str, object]:
    """Map a validated candle onto column values (fixed-point types quantise)."""
    return {
        "candle_id": make_candle_id(candle.symbol, candle.timeframe, candle.open_time),
        "symbol": candle.symbol,
        "timeframe": candle.timeframe,
        "open_time": candle.open_time,
        "close_time": candle.close_time,
        "open": candle.open,
        "high": candle.high,
        "low": candle.low,
        "close": candle.close,
        "volume": candle.volume,
        "is_complete": candle.is_complete,
        "source": candle.source,
        "received_at": now,
        "validated_at": now,
    }


def count(session: Session, *, symbol: str | None = None, timeframe: str | None = None) -> int:
    stmt = select(func.count()).select_from(Candle)
    if symbol:
        stmt = stmt.where(Candle.symbol == symbol)
    if timeframe:
        stmt = stmt.where(Candle.timeframe == timeframe)
    return int(session.execute(stmt).scalar() or 0)


def get(session: Session, candle_id: str) -> Candle | None:
    return session.get(Candle, candle_id)


def find(session: Session, symbol: str, timeframe: str, open_time: datetime) -> Candle | None:
    stmt = select(Candle).where(
        Candle.symbol == symbol,
        Candle.timeframe == timeframe,
        Candle.open_time == open_time,
    )
    return session.execute(stmt).scalars().first()


def upsert(session: Session, candle: RawCandle, *, now: datetime) -> tuple[Candle, bool]:
    """Insert or repair a candle. Returns ``(row, created)``.

    A row that already exists is only touched when a value actually differs, so
    a re-ingest of identical data issues no UPDATE and leaves the audit trail
    (``received_at``) untouched.
    """
    values = to_row(candle, now=now)
    existing = find(session, candle.symbol, candle.timeframe, candle.open_time)
    if existing is None:
        row = Candle(**values)
        session.add(row)
        session.flush()
        return row, True

    changed = False
    for column in UPSERT_COLUMNS:
        if getattr(existing, column) != values[column]:
            setattr(existing, column, values[column])
            changed = True
    if changed:
        session.flush()
    return existing, False


def upsert_many(session: Session, candles: Sequence[RawCandle], *, now: datetime) -> tuple[int, int]:
    """Upsert a batch. Returns ``(created, updated_or_unchanged)``."""
    created = 0
    for candle in candles:
        _, was_created = upsert(session, candle, now=now)
        if was_created:
            created += 1
    return created, len(candles) - created


def last_candle(session: Session, symbol: str, timeframe: str) -> Candle | None:
    stmt = (
        select(Candle)
        .where(Candle.symbol == symbol, Candle.timeframe == timeframe)
        .order_by(Candle.open_time.desc())
        .limit(1)
    )
    return session.execute(stmt).scalars().first()


def first_candle(session: Session, symbol: str, timeframe: str) -> Candle | None:
    stmt = (
        select(Candle)
        .where(Candle.symbol == symbol, Candle.timeframe == timeframe)
        .order_by(Candle.open_time.asc())
        .limit(1)
    )
    return session.execute(stmt).scalars().first()


def last_open_time(session: Session, symbol: str, timeframe: str) -> datetime | None:
    row = last_candle(session, symbol, timeframe)
    return row.open_time if row else None


def open_times(
    session: Session,
    symbol: str,
    timeframe: str,
    *,
    start: datetime | None = None,
    end_exclusive: datetime | None = None,
) -> set[datetime]:
    """Stored open times in a range (used by gap detection)."""
    stmt = select(Candle.open_time).where(Candle.symbol == symbol, Candle.timeframe == timeframe)
    if start is not None:
        stmt = stmt.where(Candle.open_time >= start)
    if end_exclusive is not None:
        stmt = stmt.where(Candle.open_time < end_exclusive)
    return {row[0] for row in session.execute(stmt).all()}


def ordered_closed_candles(
    session: Session,
    symbol: str,
    timeframe: str,
    *,
    start: datetime | None = None,
    end_exclusive: datetime | None = None,
    limit: int | None = None,
) -> list[Candle]:
    """Ascending closed candles — the input to strategy/backtest/replay."""
    stmt = (
        select(Candle)
        .where(Candle.symbol == symbol, Candle.timeframe == timeframe, Candle.is_complete.is_(True))
        .order_by(Candle.open_time.asc())
    )
    if start is not None:
        stmt = stmt.where(Candle.open_time >= start)
    if end_exclusive is not None:
        stmt = stmt.where(Candle.open_time < end_exclusive)
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(session.execute(stmt).scalars().all())


def coverage(session: Session, symbol: str, timeframe: str) -> dict[str, object]:
    """First/last open time + counts, for the dashboard and ``data_sources``."""
    first = first_candle(session, symbol, timeframe)
    last = last_candle(session, symbol, timeframe)
    total = count(session, symbol=symbol, timeframe=timeframe)
    complete = int(
        session.execute(
            select(func.count())
            .select_from(Candle)
            .where(
                Candle.symbol == symbol,
                Candle.timeframe == timeframe,
                Candle.is_complete.is_(True),
            )
        ).scalar()
        or 0
    )
    return {
        "first_open_time": first.open_time if first else None,
        "last_open_time": last.open_time if last else None,
        "candles": total,
        "complete_candles": complete,
        "sources": sorted(
            {
                row[0]
                for row in session.execute(
                    select(Candle.source).where(Candle.symbol == symbol, Candle.timeframe == timeframe)
                ).all()
            }
        ),
    }


__all__ = [
    "UPSERT_COLUMNS",
    "coverage",
    "count",
    "find",
    "first_candle",
    "get",
    "last_candle",
    "last_open_time",
    "open_times",
    "ordered_closed_candles",
    "to_row",
    "upsert",
    "upsert_many",
]
