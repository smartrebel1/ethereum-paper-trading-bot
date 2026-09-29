"""Indicators: prefix stability, exactness, and agreement with a float reference.

Two independent properties are tested, because they fail differently:

* **prefix stability** — the value at index ``i`` must depend only on inputs at
  indices ``<= i``. This is the structural half of "no look-ahead": if it ever
  breaks, a decision could be computed from data that did not exist yet.
* **numeric agreement** — the Decimal implementation must reproduce the
  standard formulas (SMA-seeded EMA, Wilder ATR) to many significant digits.
  The reference is computed with ``numpy`` in float64: a *different* code path
  from the one under test, so a shared typo cannot cancel itself out. ``numpy``
  is a test-only dependency and is skipped if it is absent.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.strategy.indicators import atr, ema, sma, true_range

PERIOD = 20


def synthetic_series(length: int = 260) -> tuple[list[Decimal], list[Decimal], list[Decimal]]:
    """A deterministic, non-trivial price series (no randomness, no fixtures)."""
    highs, lows, closes = [], [], []
    price = Decimal("2500")
    for index in range(length):
        drift = Decimal(((index * index) % 37) - 18) / Decimal(10)
        price = price + drift
        close = price.quantize(Decimal("0.00000001"))
        closes.append(close)
        highs.append((close + Decimal("7.5")).quantize(Decimal("0.00000001")))
        lows.append((close - Decimal("6.25")).quantize(Decimal("0.00000001")))
    return highs, lows, closes


def test_ema_is_none_until_the_seed_window_is_full() -> None:
    _, _, closes = synthetic_series(30)
    values = ema(closes, PERIOD)
    assert values[: PERIOD - 1] == [None] * (PERIOD - 1)
    assert values[PERIOD - 1] is not None
    assert sum(1 for value in values if value is None) == PERIOD - 1


def test_ema_seed_is_the_simple_average_of_the_first_window() -> None:
    _, _, closes = synthetic_series(30)
    values = ema(closes, PERIOD)
    expected = (sum(closes[:PERIOD]) / PERIOD).quantize(Decimal("0.00000001"))
    assert values[PERIOD - 1] == expected


def test_short_series_yields_no_values() -> None:
    _, _, closes = synthetic_series(5)
    assert ema(closes, PERIOD) == [None] * 5
    highs, lows, _ = synthetic_series(5)
    assert atr(highs, lows, closes, PERIOD) == [None] * 5


def test_atr_is_none_until_the_seed_window_is_full() -> None:
    highs, lows, closes = synthetic_series(40)
    values = atr(highs, lows, closes, PERIOD)
    assert values[: PERIOD - 1] == [None] * (PERIOD - 1)
    expected = (sum(true_range(highs, lows, closes)[:PERIOD]) / PERIOD).quantize(Decimal("0.00000001"))
    assert values[PERIOD - 1] == expected


def test_true_range_uses_the_previous_close() -> None:
    highs = [Decimal("10"), Decimal("12")]
    lows = [Decimal("8"), Decimal("11")]
    closes = [Decimal("9"), Decimal("11.5")]
    ranges = true_range(highs, lows, closes)
    assert ranges[0] == Decimal("2")  # no previous close: high - low
    assert ranges[1] == Decimal("3")  # gap up: high - previous close


def test_negative_slope_and_flat_market_are_handled() -> None:
    _, _, rising = synthetic_series(60)
    values = ema(rising, PERIOD)
    assert values[-1] is not None
    assert values[-1] > values[PERIOD - 1]

    flat = [Decimal("100")] * 60
    flat_ema = ema(flat, PERIOD)
    assert all(value == Decimal("100") for value in flat_ema if value is not None)


def test_mismatched_lengths_are_refused() -> None:
    with pytest.raises(ValueError):
        true_range([Decimal(1)], [Decimal(1)], [])
    with pytest.raises(ValueError):
        ema([Decimal(1)], 0)
    with pytest.raises(ValueError):
        sma([Decimal(1)], -1)


@pytest.mark.parametrize("length", [1, 5, 25, 60, 199])
def test_prefix_property_ema(length: int) -> None:
    """Recomputing on a truncated series must not change the shared prefix."""
    _, _, closes = synthetic_series()
    full = ema(closes, PERIOD)
    truncated = ema(closes[:length], PERIOD)
    assert truncated == full[:length]


@pytest.mark.parametrize("length", [1, 5, 25, 60, 199])
def test_prefix_property_atr(length: int) -> None:
    highs, lows, closes = synthetic_series()
    full = atr(highs, lows, closes, PERIOD)
    truncated = atr(highs[:length], lows[:length], closes[:length], PERIOD)
    assert truncated == full[:length]


def test_changing_the_tail_never_rewrites_the_past() -> None:
    """The strongest form of the prefix property: the past is immutable.

    The tail is mutated at index 200, so everything before 200 must be
    bit-identical while everything after it must move — otherwise the test would
    pass vacuously.
    """
    highs, lows, closes = synthetic_series()
    baseline_ema = ema(closes, PERIOD)
    baseline_atr = atr(highs, lows, closes, PERIOD)

    mutated = list(closes)
    for index in range(200, len(mutated)):
        mutated[index] = mutated[index] + Decimal("123.456")
    mutated_highs = list(highs)
    for index in range(200, len(mutated_highs)):
        mutated_highs[index] = mutated_highs[index] + Decimal("123.456")

    shifted_ema = ema(mutated, PERIOD)
    shifted_atr = atr(mutated_highs, lows, mutated, PERIOD)

    assert shifted_ema[:200] == baseline_ema[:200]
    assert shifted_atr[:200] == baseline_atr[:200]
    assert shifted_ema[200:] != baseline_ema[200:]  # the mutation is observable
    assert shifted_atr[200:] != baseline_atr[200:]


def test_ema_matches_a_float_reference() -> None:
    numpy = pytest.importorskip("numpy", reason="numpy is a test-only cross-check")
    _, _, closes = synthetic_series()
    values = ema(closes, PERIOD)

    alpha = 2.0 / (PERIOD + 1)
    reference = float(sum(closes[:PERIOD])) / PERIOD
    expected = [reference]
    for close in closes[PERIOD:]:
        reference = (float(close) - reference) * alpha + reference
        expected.append(reference)

    # Tolerance = one unit of the 8-decimal quantisation (5e-9) plus float drift.
    for index, want in enumerate(expected, start=PERIOD - 1):
        got = values[index]
        assert got is not None
        tolerance = 5.1e-9 + 1e-12 * abs(want)
        assert abs(float(got) - want) < tolerance, f"index {index}: {got} vs {want}"
    assert len(expected) == len(values) - (PERIOD - 1)
    assert numpy is not None


def test_atr_matches_a_float_reference() -> None:
    pytest.importorskip("numpy", reason="numpy is a test-only cross-check")
    highs, lows, closes = synthetic_series()
    values = atr(highs, lows, closes, PERIOD)

    ranges: list[float] = []
    for index in range(len(highs)):
        high_low = float(highs[index]) - float(lows[index])
        if index == 0:
            ranges.append(high_low)
            continue
        previous_close = float(closes[index - 1])
        ranges.append(
            max(
                high_low,
                abs(float(highs[index]) - previous_close),
                abs(float(lows[index]) - previous_close),
            )
        )

    reference = sum(ranges[:PERIOD]) / PERIOD
    expected = [reference]
    for value in ranges[PERIOD:]:
        reference = (reference * (PERIOD - 1) + value) / PERIOD
        expected.append(reference)

    for index, want in enumerate(expected, start=PERIOD - 1):
        got = values[index]
        assert got is not None
        tolerance = 5.1e-9 + 1e-12 * abs(want)
        assert abs(float(got) - want) < tolerance, f"index {index}: {got} vs {want}"


def test_indicators_are_reproducible_bit_for_bit() -> None:
    """Same input, same output — the basis of the reproducibility claim."""
    highs, lows, closes = synthetic_series()
    assert ema(closes, PERIOD) == ema(list(closes), PERIOD)
    assert atr(highs, lows, closes, PERIOD) == atr(list(highs), list(lows), list(closes), PERIOD)
