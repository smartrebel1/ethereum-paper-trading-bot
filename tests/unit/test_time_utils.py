"""Time rules: boundaries, alignment, gaps, timezone handling."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.common.enums import Timeframe
from app.common.time_utils import (
    close_time_of,
    ensure_aware_utc,
    floor_to_timeframe,
    is_aligned,
    is_candle_complete,
    iter_open_times,
    next_open_time,
    parse_iso_z,
    previous_open_time,
    timeframe_seconds,
    to_utc,
    to_utc_iso_z,
    utcnow,
)

UTC = UTC


def test_seconds_map() -> None:
    assert timeframe_seconds(Timeframe.M15) == 900
    assert timeframe_seconds(Timeframe.H1) == 3600
    assert timeframe_seconds(Timeframe.H4) == 14400
    assert timeframe_seconds(Timeframe.D1) == 86400
    assert timeframe_seconds("4h") == 14400


def test_next_open_and_close_are_the_same_boundary() -> None:
    t = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    assert next_open_time(t, Timeframe.H4) == datetime(2026, 1, 1, 4, 0, tzinfo=UTC)
    assert close_time_of(t, Timeframe.H4) == datetime(2026, 1, 1, 4, 0, tzinfo=UTC)


def test_boundaries_cross_day_and_month() -> None:
    assert next_open_time(datetime(2026, 1, 31, 20, 0, tzinfo=UTC), Timeframe.H4) == datetime(
        2026, 2, 1, 0, 0, tzinfo=UTC
    )
    assert next_open_time(datetime(2026, 12, 31, 20, 0, tzinfo=UTC), Timeframe.H4) == datetime(
        2027, 1, 1, 0, 0, tzinfo=UTC
    )


def test_previous_open_time_inverts_next() -> None:
    t = datetime(2026, 3, 5, 12, 0, tzinfo=UTC)
    assert next_open_time(previous_open_time(t, Timeframe.H1), Timeframe.H1) == t


def test_alignment() -> None:
    aligned = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
    unaligned = datetime(2026, 1, 1, 8, 15, tzinfo=UTC)
    assert is_aligned(aligned, Timeframe.H4) is True
    assert is_aligned(unaligned, Timeframe.H4) is False
    assert is_aligned(unaligned, Timeframe.M15) is True


def test_alignment_converts_non_utc_input() -> None:
    """Regression: a +01:00 stamp at 05:00 local is 04:00Z -> aligned for 4h."""
    tz_plus_one = timezone(timedelta(hours=1))
    assert is_aligned(datetime(2026, 1, 1, 5, 0, tzinfo=tz_plus_one), Timeframe.H4) is True


def test_floor() -> None:
    ts = datetime(2026, 1, 1, 9, 47, 30, tzinfo=UTC)
    assert floor_to_timeframe(ts, Timeframe.H4) == datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
    assert floor_to_timeframe(ts, Timeframe.D1) == datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


def test_naive_datetimes_rejected_on_boundary_math() -> None:
    with pytest.raises(ValueError):
        next_open_time(datetime(2026, 1, 1, 0, 0), Timeframe.H4)
    with pytest.raises(ValueError):
        floor_to_timeframe(datetime(2026, 1, 1, 0, 0), Timeframe.H4)
    with pytest.raises(ValueError):
        is_aligned(datetime(2026, 1, 1, 0, 0), Timeframe.H4)


def test_to_utc_treats_naive_as_utc_but_converts_aware() -> None:
    naive = datetime(2026, 1, 1, 0, 0)
    assert to_utc(naive) == datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    tz_plus_two = timezone(timedelta(hours=2))
    assert to_utc(datetime(2026, 1, 1, 2, 0, tzinfo=tz_plus_two)) == datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


def test_iso_z_round_trip() -> None:
    t = datetime(2026, 1, 1, 4, 0, 0, tzinfo=UTC)
    assert to_utc_iso_z(t) == "2026-01-01T04:00:00Z"
    assert parse_iso_z("2026-01-01T04:00:00Z") == t
    assert parse_iso_z("2026-01-01T06:00:00+02:00") == t


def test_ensure_aware_utc_rejects_naive() -> None:
    with pytest.raises(ValueError):
        ensure_aware_utc(datetime(2026, 1, 1))


def test_candle_completion_rule() -> None:
    open_time = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    assert is_candle_complete(open_time, Timeframe.H4, now=datetime(2026, 1, 1, 3, 59, tzinfo=UTC)) is False
    assert is_candle_complete(open_time, Timeframe.H4, now=datetime(2026, 1, 1, 4, 0, tzinfo=UTC)) is True


def test_iter_open_times_inclusive_start_exclusive_end() -> None:
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    end = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert list(iter_open_times(start, end, Timeframe.H4)) == [
        datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        datetime(2026, 1, 1, 4, 0, tzinfo=UTC),
        datetime(2026, 1, 1, 8, 0, tzinfo=UTC),
    ]


def test_iter_open_times_floors_unaligned_start() -> None:
    start = datetime(2026, 1, 1, 1, 30, tzinfo=UTC)
    end = datetime(2026, 1, 1, 5, 0, tzinfo=UTC)
    assert list(iter_open_times(start, end, Timeframe.H4)) == [
        datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        datetime(2026, 1, 1, 4, 0, tzinfo=UTC),
    ]


def test_utcnow_is_aware_utc() -> None:
    now = utcnow()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)
