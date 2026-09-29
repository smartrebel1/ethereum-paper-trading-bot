"""Adversarial candle validation and gap detection (Phase 2).

The validator is the only gate between "bytes on disk" and "data the strategy is
allowed to reason about", so every test here mutates exactly one field of a
valid candle and asserts the *specific* reason that must come back. Reason codes
are part of the contract: they end up in ``integrity_events`` and on the
operator's screen.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.candles.gaps import detect_gaps, find_missing_open_times, gap_between
from app.candles.validator import CandleValidator
from app.integrity.enums import IntegrityCode
from app.market_data.mock import make_candle

SYMBOL = "ETHUSDT"
TIMEFRAME = "4h"
#: One minute after the last candle of the series below: everything before it is
#: complete, everything at or after it is still forming.
NOW = datetime(2026, 1, 8, 0, 1, tzinfo=UTC)


@pytest.fixture
def validator() -> CandleValidator:
    return CandleValidator(SYMBOL, TIMEFRAME)


def test_a_valid_candle_passes(validator: CandleValidator) -> None:
    result = validator.validate(make_candle(datetime(2026, 1, 1, tzinfo=UTC)), now=NOW)
    assert result.ok, result.message
    assert result.code is None


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("high", Decimal("2499"), IntegrityCode.OHLC_INVARIANT_BROKEN),  # high < open
        ("low", Decimal("2501"), IntegrityCode.OHLC_INVARIANT_BROKEN),  # low > close
        ("open", Decimal("0"), IntegrityCode.NON_POSITIVE_PRICE),
        ("close", Decimal("-1"), IntegrityCode.NON_POSITIVE_PRICE),
        ("volume", Decimal("-0.00000001"), IntegrityCode.NEGATIVE_VOLUME),
    ],
)
def test_each_broken_field_is_named(
    validator: CandleValidator, field: str, value: Decimal, code: IntegrityCode
) -> None:
    broken = dataclasses.replace(make_candle(datetime(2026, 1, 1, tzinfo=UTC)), **{field: value})
    result = validator.validate(broken, now=NOW)
    assert not result.ok
    assert result.code is code
    assert field in result.message or field in result.details


def test_zero_volume_is_allowed(validator: CandleValidator) -> None:
    """A quiet 4h candle with no trades is real data, not a rejection."""
    result = validator.validate(make_candle(datetime(2026, 1, 1, tzinfo=UTC), volume="0"), now=NOW)
    assert result.ok


def test_misaligned_open_time_is_rejected(validator: CandleValidator) -> None:
    off_grid = datetime(2026, 1, 1, 0, 30, tzinfo=UTC)
    result = validator.validate(make_candle(off_grid), now=NOW)
    assert not result.ok
    assert result.code is IntegrityCode.MISALIGNED_CANDLE


def test_wrong_symbol_is_rejected(validator: CandleValidator) -> None:
    result = validator.validate(make_candle(datetime(2026, 1, 1, tzinfo=UTC), symbol="BTCUSDT"), now=NOW)
    assert not result.ok
    assert result.code is IntegrityCode.MISALIGNED_CANDLE


def test_wrong_close_time_is_rejected(validator: CandleValidator) -> None:
    candle = dataclasses.replace(
        make_candle(datetime(2026, 1, 1, tzinfo=UTC)),
        close_time=datetime(2026, 1, 1, 1, 0, tzinfo=UTC),  # 4h candle claiming a 1h window
    )
    result = validator.validate(candle, now=NOW)
    assert not result.ok
    assert result.code is IntegrityCode.MISALIGNED_CANDLE


def test_incomplete_candle_is_never_usable(validator: CandleValidator) -> None:
    """The current, still-forming candle must not reach the strategy."""
    forming = make_candle(datetime(2026, 1, 7, 20, 0, tzinfo=UTC), is_complete=False)
    result = validator.validate(forming, now=NOW)
    assert not result.ok
    assert result.code is IntegrityCode.INCOMPLETE_CANDLE_USED


def test_candle_whose_window_has_not_closed_is_rejected_even_if_flagged_complete(
    validator: CandleValidator,
) -> None:
    """``is_complete`` is a claim; the clock is the judge."""
    future = make_candle(datetime(2026, 1, 20, 0, 0, tzinfo=UTC), is_complete=True)
    result = validator.validate(future, now=NOW)
    assert not result.ok
    assert result.code is IntegrityCode.INCOMPLETE_CANDLE_USED


def test_out_of_order_candle_is_rejected(validator: CandleValidator) -> None:
    previous = datetime(2026, 1, 1, 4, 0, tzinfo=UTC)
    repeated = make_candle(datetime(2026, 1, 1, 0, 0, tzinfo=UTC))
    result = validator.validate(repeated, now=NOW, previous_open_time=previous)
    assert not result.ok
    assert result.code is IntegrityCode.OUT_OF_ORDER_CANDLE


def test_first_reason_wins_and_is_stable(validator: CandleValidator) -> None:
    """Several violations at once: the cheapest, most fundamental check reports."""
    bad = make_candle(datetime(2026, 1, 1, 0, 30, tzinfo=UTC), volume="-1")
    first = validator.validate(bad, now=NOW)
    second = validator.validate(bad, now=NOW)
    assert first == second
    assert first.code is IntegrityCode.MISALIGNED_CANDLE  # alignment precedes volume


# --------------------------------------------------------------- gap detection


def _grid(start: str, count: int, step_hours: int = 4) -> list[datetime]:
    base = datetime.fromisoformat(start).replace(tzinfo=UTC)
    return [base + timedelta(hours=step_hours * i) for i in range(count)]


def test_no_gaps_in_a_contiguous_series() -> None:
    times = _grid("2026-01-01T00:00", 6)
    assert detect_gaps(set(times), times[0], times[-1] + timedelta(hours=4), TIMEFRAME) == []
    assert find_missing_open_times(set(times), times[0], times[-1] + timedelta(hours=4), "4h") == []


def test_a_hole_is_reported_as_one_gap_with_its_length() -> None:
    times = _grid("2026-01-01T00:00", 6)
    del times[2:4]  # remove two consecutive candles
    gaps = detect_gaps(set(times), times[0], times[-1] + timedelta(hours=4), TIMEFRAME)
    assert len(gaps) == 1
    assert gaps[0].missing == 2
    assert gaps[0].start == datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
    assert gaps[0].end_exclusive == datetime(2026, 1, 1, 16, 0, tzinfo=UTC)


def test_separated_holes_are_separate_gaps() -> None:
    times = _grid("2026-01-01T00:00", 8)
    del times[1]
    del times[4]
    gaps = detect_gaps(set(times), times[0], times[-1] + timedelta(hours=4), TIMEFRAME)
    assert [gap.missing for gap in gaps] == [1, 1]


def test_gap_between_two_consecutive_candles_is_none() -> None:
    first = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    assert gap_between(first, first + timedelta(hours=4), TIMEFRAME) is None


def test_gap_between_names_the_missing_window() -> None:
    first = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    gap = gap_between(first, first + timedelta(hours=12), TIMEFRAME)
    assert gap is not None
    assert gap.missing == 2
    assert gap.start == first + timedelta(hours=4)
    assert gap.end_exclusive == first + timedelta(hours=12)


def test_detection_is_independent_of_insertion_order() -> None:
    times = _grid("2026-01-01T00:00", 10)
    shuffled = set(times[3:] + times[:3])
    forward = detect_gaps(set(times), times[0], times[-1] + timedelta(hours=4), TIMEFRAME)
    backward = detect_gaps(shuffled, times[0], times[-1] + timedelta(hours=4), TIMEFRAME)
    assert forward == backward
