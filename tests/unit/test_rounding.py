"""Deterministic decimal rounding."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.common.rounding import (
    MONEY_SCALE,
    PRICE_SCALE,
    QTY_SCALE,
    bps,
    floor_to_increment,
    money,
    price,
    qty,
    ratio,
    to_decimal,
)


def test_to_decimal_uses_string_conversion_for_floats() -> None:
    assert to_decimal(0.1) == Decimal("0.1")  # not 0.1000000000000000055511151231257827
    assert to_decimal("1.5") == Decimal("1.5")
    assert to_decimal(Decimal("2.5")) is not None


def test_non_finite_floats_rejected() -> None:
    with pytest.raises(ValueError):
        to_decimal(float("inf"))
    with pytest.raises(ValueError):
        to_decimal(float("nan"))


def test_scale_constants() -> None:
    assert price("1").as_tuple().exponent == -PRICE_SCALE
    assert qty("1").as_tuple().exponent == -QTY_SCALE
    assert money("1").as_tuple().exponent == -MONEY_SCALE
    assert ratio("1").as_tuple().exponent == -8


def test_half_even_rounding_is_used() -> None:
    assert price("1.000000005") == Decimal("1.00000000")  # 5 -> even (0)
    assert price("1.000000015") == Decimal("1.00000002")  # 5 -> even (2)


def test_no_float_drift_over_many_operations() -> None:
    """0.1 added 10 times is exactly 1.0 in Decimal, not 0.9999999..."""
    total = Decimal(0)
    for _ in range(10):
        total = money(total + Decimal("0.1"))
    assert total == Decimal("1.00000000")


def test_bps() -> None:
    assert bps("1000", "10") == Decimal("1.00000000")  # 10 bps of 1000 = 1
    assert bps("200", "0") == Decimal("0.00000000")


def test_floor_to_increment_never_rounds_up() -> None:
    assert floor_to_increment(Decimal("1.2345"), Decimal("0.01")) == Decimal("1.23")
    assert floor_to_increment(Decimal("1.9999"), Decimal("0.01")) == Decimal("1.99")
    with pytest.raises(ValueError):
        floor_to_increment(Decimal("1"), Decimal("0"))


def test_quantized_values_are_exact_for_db_round_trip() -> None:
    value = price("2500.12345678")
    assert value == Decimal("2500.12345678")
    assert int(value.scaleb(PRICE_SCALE)) == 250012345678
