"""Time rules for the engine.

Single source of truth for "what does a candle boundary mean".

Conventions (frozen, all persisted timestamps obey them):
* All datetimes are **timezone-aware UTC**. Naive datetimes raise.
* ``open_time`` is inclusive, ``close_time`` is exclusive and equals
  ``next_open_time`` — this mirrors Binance's ``closeTime = openTime + tf - 1ms``
  but avoids millisecond arithmetic: a candle is complete when
  ``now >= close_time``.
* A candle stamped ``open_time`` aggregates ``[open_time, close_time)``.
* Candle boundaries are aligned to the UTC epoch, which Binance also uses.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

from app.common.enums import TIMEFRAME_SECONDS, Timeframe

UTC = UTC


def utcnow() -> datetime:
    """Timezone-aware UTC now.

    Prefer :func:`app.common.clock.get_clock().now` inside engines so that
    replay/backtest can freeze time; this function is the process-clock
    fallback (and the clock the default ``SystemClock`` delegates to).
    """
    return datetime.now(tz=UTC)


def to_utc(dt: datetime) -> datetime:
    """Coerce a datetime to aware UTC.

    Naive input is *assumed* to be UTC (SQLite drops tzinfo on the way in),
    never local time — assuming local time is how silent look-ahead bugs are
    born. Aware input is converted.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def ensure_aware_utc(dt: datetime, *, field: str = "timestamp") -> datetime:
    """Strict variant used on *external* input: naive input is rejected."""
    if dt.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware UTC, got naive datetime")
    return dt.astimezone(UTC)


def is_utc(dt: datetime) -> bool:
    return dt.tzinfo is not None and dt.utcoffset() == timedelta(0)


def to_utc_iso_z(dt: datetime) -> str:
    """Second-precision ``YYYY-MM-DDTHH:MM:SSZ`` (the format used inside ids)."""
    aware = ensure_aware_utc(dt)
    return aware.strftime("%Y-%m-%dT%H:%M:%SZ")


def to_iso_z(dt: datetime, *, micros: bool = True) -> str:
    """ISO-8601 UTC with a literal ``Z`` (used in API payloads and logs)."""
    aware = ensure_aware_utc(dt)
    if not micros:
        return to_utc_iso_z(aware)
    return aware.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_iso_z(text: str) -> datetime:
    """Parse an ISO-8601 string, tolerating ``Z`` and offsets, returning UTC."""
    cleaned = text.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(cleaned)
    return ensure_aware_utc(dt)


def timeframe_seconds(tf: Timeframe | str) -> int:
    key = tf if isinstance(tf, Timeframe) else Timeframe(tf)
    return TIMEFRAME_SECONDS[key]


def timeframe_delta(tf: Timeframe | str) -> timedelta:
    return timedelta(seconds=timeframe_seconds(tf))


def is_aligned(ts: datetime, tf: Timeframe | str) -> bool:
    """True iff ``ts`` sits exactly on a timeframe boundary (UTC epoch aligned).

    Note: an aware non-UTC datetime is converted first, so
    ``2026-01-01T05:00+01:00`` is correctly recognised as ``04:00Z``.
    """
    aware = ensure_aware_utc(ts)
    return int(aware.timestamp()) % timeframe_seconds(tf) == 0


def next_open_time(open_time: datetime, tf: Timeframe | str) -> datetime:
    """The exact next candle ``open_time`` (== current candle's close_time)."""
    aware = ensure_aware_utc(open_time, field="open_time")
    return datetime.fromtimestamp(int(aware.timestamp()) + timeframe_seconds(tf), tz=UTC)


def close_time_of(open_time: datetime, tf: Timeframe | str) -> datetime:
    """Exclusive close boundary of the candle that opened at ``open_time``."""
    return next_open_time(open_time, tf)


def previous_open_time(open_time: datetime, tf: Timeframe | str) -> datetime:
    aware = ensure_aware_utc(open_time, field="open_time")
    return datetime.fromtimestamp(int(aware.timestamp()) - timeframe_seconds(tf), tz=UTC)


def floor_to_timeframe(ts: datetime, tf: Timeframe | str) -> datetime:
    """Largest boundary <= ts."""
    aware = ensure_aware_utc(ts)
    secs = timeframe_seconds(tf)
    epoch = int(aware.timestamp())
    return datetime.fromtimestamp(epoch - (epoch % secs), tz=UTC)


def is_candle_complete(open_time: datetime, tf: Timeframe | str, *, now: datetime) -> bool:
    """A candle is only usable once its exclusive close boundary has passed."""
    return ensure_aware_utc(now) >= close_time_of(open_time, tf)


def iter_open_times(start: datetime, end_exclusive: datetime, tf: Timeframe | str) -> Iterator[datetime]:
    """Ascending candle boundaries in ``[start, end_exclusive)``.

    ``start`` may be unaligned; it is floored first. Used by gap detection and
    by the phase-2 ingestor to request exactly the missing ranges.
    """
    cursor = floor_to_timeframe(start, tf)
    end = ensure_aware_utc(end_exclusive)
    while cursor < end:
        yield cursor
        cursor = next_open_time(cursor, tf)


__all__ = [
    "UTC",
    "close_time_of",
    "ensure_aware_utc",
    "floor_to_timeframe",
    "is_aligned",
    "is_candle_complete",
    "is_utc",
    "iter_open_times",
    "next_open_time",
    "parse_iso_z",
    "previous_open_time",
    "timeframe_delta",
    "timeframe_seconds",
    "to_iso_z",
    "to_utc",
    "to_utc_iso_z",
    "utcnow",
]
