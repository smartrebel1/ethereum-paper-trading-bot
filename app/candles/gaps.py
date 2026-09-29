"""Gap detection.

A gap is *missing candles inside a declared span*, not "the newest candle is
old" (that is simply the end of the archive). Gaps are reported, never filled:
the engine's job is to know what it does not know.

Phase 5 turns a gap that swallows an order's execution target into a
``TARGET_MISSED`` integrity failure; the trade-suppression decision belongs to
the engine, not to this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.common.enums import Timeframe
from app.common.time_utils import ensure_aware_utc, iter_open_times, timeframe_seconds


@dataclass(frozen=True, slots=True)
class Gap:
    """A contiguous run of missing candle open times."""

    start: datetime
    end_exclusive: datetime
    missing: int
    expected: int = 0

    @property
    def duration_seconds(self) -> int:
        return int((self.end_exclusive - self.start).total_seconds())

    def as_dict(self) -> dict[str, object]:
        return {
            "start": self.start.isoformat(),
            "end_exclusive": self.end_exclusive.isoformat(),
            "missing": self.missing,
            "expected": self.expected,
            "candles": self.missing,
        }


def find_missing_open_times(
    present: set[datetime],
    start: datetime,
    end_exclusive: datetime,
    timeframe: Timeframe | str,
) -> list[datetime]:
    """Ascending list of expected-but-absent open times in ``[start, end)``."""
    return [
        open_time
        for open_time in iter_open_times(start, end_exclusive, timeframe)
        if open_time not in present
    ]


def detect_gaps(
    present: set[datetime],
    start: datetime,
    end_exclusive: datetime,
    timeframe: Timeframe | str,
) -> list[Gap]:
    """Collapse missing open times into contiguous :class:`Gap` runs."""
    missing = find_missing_open_times(present, start, end_exclusive, timeframe)
    if not missing:
        return []

    step_seconds = timeframe_seconds(timeframe)
    total_expected = len(list(iter_open_times(start, end_exclusive, timeframe)))

    gaps: list[Gap] = []
    run_start = missing[0]
    previous = missing[0]
    run_length = 1

    for open_time in missing[1:]:
        if int((open_time - previous).total_seconds()) == step_seconds:
            run_length += 1
        else:
            gaps.append(
                Gap(
                    start=run_start,
                    end_exclusive=_after(previous, step_seconds),
                    missing=run_length,
                    expected=total_expected,
                )
            )
            run_start, run_length = open_time, 1
        previous = open_time

    gaps.append(
        Gap(
            start=run_start,
            end_exclusive=_after(previous, step_seconds),
            missing=run_length,
            expected=total_expected,
        )
    )
    return gaps


def _after(open_time: datetime, step_seconds: int) -> datetime:
    from datetime import timedelta

    return ensure_aware_utc(open_time) + timedelta(seconds=step_seconds)


def gap_between(
    previous_open_time: datetime,
    next_open_time_value: datetime,
    timeframe: Timeframe | str,
) -> Gap | None:
    """Gap between two *adjacent stored* candles, if they are not adjacent in time."""
    step_seconds = timeframe_seconds(timeframe)
    delta = int((next_open_time_value - previous_open_time).total_seconds())
    if delta <= step_seconds:
        return None
    missing = delta // step_seconds - 1
    return Gap(
        start=previous_open_time + _delta(step_seconds),
        end_exclusive=next_open_time_value,
        missing=missing,
        expected=missing + 2,
    )


def _delta(seconds: int):  # noqa: ANN202
    from datetime import timedelta

    return timedelta(seconds=seconds)


__all__ = ["Gap", "detect_gaps", "find_missing_open_times", "gap_between"]
