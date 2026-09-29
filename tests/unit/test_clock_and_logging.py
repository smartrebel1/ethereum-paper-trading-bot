"""Injectable clock + JSON logging (including the format bug regression)."""

from __future__ import annotations

import io
import json
import logging
from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.common.clock import FrozenClock, ShiftedClock, SystemClock, get_clock, set_clock
from app.common.logging_setup import JsonFormatter, configure_logging

UTC = UTC


def test_system_clock_is_aware_utc() -> None:
    now = SystemClock().now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_frozen_clock_requires_aware_datetime() -> None:
    with pytest.raises(ValueError):
        FrozenClock(datetime(2026, 1, 1))


def test_frozen_clock_does_not_move_on_its_own() -> None:
    clock = FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))
    assert clock.now() == clock.now() == datetime(2026, 1, 1, tzinfo=UTC)


def test_frozen_clock_set_and_advance() -> None:
    clock = FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))
    clock.advance(timedelta(hours=4))
    assert clock.now() == datetime(2026, 1, 1, 4, tzinfo=UTC)
    clock.set(datetime(2026, 2, 1, tzinfo=UTC))
    assert clock.now() == datetime(2026, 2, 1, tzinfo=UTC)


def test_frozen_clock_auto_advance() -> None:
    clock = FrozenClock(datetime(2026, 1, 1, tzinfo=UTC), auto_advance=timedelta(hours=1))
    assert clock.now() == datetime(2026, 1, 1, 0, tzinfo=UTC)
    assert clock.now() == datetime(2026, 1, 1, 1, tzinfo=UTC)


def test_frozen_clock_normalises_to_utc() -> None:
    clock = FrozenClock(datetime(2026, 1, 1, 2, tzinfo=timezone(timedelta(hours=2))))
    assert clock.now() == datetime(2026, 1, 1, 0, tzinfo=UTC)


def test_shifted_clock_offsets_system_time() -> None:
    now = ShiftedClock(offset=timedelta(seconds=30)).now()
    delta = (now - SystemClock().now()).total_seconds()
    assert 25 < delta < 35, f"expected ~+30s, got {delta}s"


def test_set_clock_installs_and_restores() -> None:
    frozen = FrozenClock(datetime(2026, 5, 5, tzinfo=UTC))
    assert set_clock(frozen) is frozen
    assert get_clock().now() == datetime(2026, 5, 5, tzinfo=UTC)
    set_clock(None)
    assert isinstance(get_clock(), SystemClock)


def test_json_formatter_emits_valid_json_with_string_message() -> None:
    """Regression: ``%(message)r`` produced Python reprs, not JSON strings."""
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="candle accepted for %s",
        args=("ETHUSDT",),
        exc_info=None,
    )
    payload = json.loads(formatter.format(record))
    assert payload["message"] == "candle accepted for ETHUSDT"
    assert isinstance(payload["message"], str)
    assert payload["level"] == "INFO"
    assert payload["component"] == "app.test"
    assert payload["ts"].endswith("Z")


def test_json_formatter_promotes_extra_fields() -> None:
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="app.test",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="mismatch",
        args=(),
        exc_info=None,
    )
    record.strategy_config_hash = "abc"
    record.pending = 3
    payload = json.loads(formatter.format(record))
    assert payload["strategy_config_hash"] == "abc"
    assert payload["pending"] == 3


def test_json_formatter_includes_traceback() -> None:
    formatter = JsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.LogRecord(
            name="app.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="failed",
            args=(),
            exc_info=sys.exc_info(),
        )
    payload = json.loads(formatter.format(record))
    assert "ValueError: boom" in payload["exception"]


def test_configure_logging_writes_one_json_line_per_record() -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    logging.getLogger("app.unit").info("hello", extra={"symbol": "ETHUSDT"})
    lines = [line for line in stream.getvalue().splitlines() if line.strip()]
    assert lines
    payload = json.loads(lines[-1])
    assert payload["message"] == "hello"
    assert payload["symbol"] == "ETHUSDT"
    # Restore a normal logger for the rest of the session.
    logging.basicConfig(level=logging.INFO)
