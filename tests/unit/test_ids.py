"""Deterministic id helpers: determinism, sensitivity, collision resistance."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.common.enums import Timeframe
from app.common.ids import (
    candle_id,
    det_id,
    event_id,
    execution_id,
    integrity_event_id,
    observation_id,
    order_id,
    position_id,
    scheduler_run_id,
    signal_id,
    snapshot_id,
    trade_id,
)

UTC = UTC


def ts(hour: int = 0) -> datetime:
    return datetime(2026, 1, 1, hour, tzinfo=UTC)


def sig(**overrides: object) -> str:
    kwargs: dict[str, object] = {
        "symbol": "ETHUSDT",
        "timeframe": Timeframe.H4,
        "candle_open_time": ts(),
        "strategy_name": "EMA200_ATR_BASELINE",
        "strategy_version": "1.0.0",
        "strategy_config_hash": "abc123",
    }
    kwargs.update(overrides)
    return signal_id(**kwargs)  # type: ignore[arg-type]


def test_det_id_shape_and_determinism() -> None:
    a = det_id("X", "a", 1, 2.5)
    b = det_id("X", "a", 1, 2.5)
    assert a == b
    assert a.startswith("X::")
    assert len(a) == len("X::") + 32


def test_candle_id_is_readable_and_deterministic() -> None:
    a = candle_id("ETHUSDT", Timeframe.H4, ts())
    b = candle_id("ETHUSDT", Timeframe.H4, ts())
    assert a == b
    assert a == "CDL::ETHUSDT::4h::2026-01-01T00:00:00Z"


def test_candle_id_accepts_string_timeframe() -> None:
    assert candle_id("ETHUSDT", "4h", ts()) == candle_id("ETHUSDT", Timeframe.H4, ts())


def test_signal_id_sensitivity() -> None:
    base = sig()
    assert sig(candle_open_time=ts(4)) != base, "different candle must differ"
    assert sig(strategy_version="1.0.1") != base, "version must differ"
    assert sig(strategy_config_hash="def456") != base, "config hash must differ"
    assert sig(symbol="BTCUSDT") != base, "symbol must differ"
    assert sig(timeframe=Timeframe.H1) != base, "timeframe must differ"
    assert sig(strategy_name="OTHER") != base, "strategy name must differ"


def test_order_execution_position_trade_chain_is_deterministic() -> None:
    sid = sig()
    o1, o2 = order_id(sid), order_id(sid)
    assert o1 == o2
    target = ts(4)
    e1, e2 = execution_id(o1, target), execution_id(o1, target)
    assert e1 == e2
    assert e1 != execution_id(o1, ts(8)), "different target candle must differ"
    p1, p2 = position_id("ETHUSDT", Timeframe.H4, o1), position_id("ETHUSDT", Timeframe.H4, o1)
    assert p1 == p2
    assert trade_id(p1) == trade_id(p2) != p1


def test_event_id_sequence_disambiguates() -> None:
    a = event_id("CANDLE_RECEIVED", "X", ts(), seq=0)
    b = event_id("CANDLE_RECEIVED", "X", ts(), seq=1)
    assert a != b
    assert event_id("CANDLE_VALIDATED", "X", ts()) != a


def test_other_ids_are_deterministic() -> None:
    assert integrity_event_id("DUPLICATE_CANDLE", "X", ts()) == integrity_event_id(
        "DUPLICATE_CANDLE", "X", ts()
    )
    assert observation_id("gemini", "ETHUSDT", "4h", ts()) == observation_id("gemini", "ETHUSDT", "4h", ts())
    assert snapshot_id("ETHUSDT", "4h", ts()) == snapshot_id("ETHUSDT", Timeframe.H4, ts())
    assert scheduler_run_id("ingest", ts()) == scheduler_run_id("ingest", ts())


def test_naive_timestamps_rejected_everywhere() -> None:
    naive = datetime(2026, 1, 1)
    with pytest.raises(ValueError):
        candle_id("ETHUSDT", Timeframe.H4, naive)
    with pytest.raises(ValueError):
        signal_id("ETHUSDT", Timeframe.H4, naive, "S", "1", "h")
    with pytest.raises(ValueError):
        execution_id("ORD::x", naive)
    with pytest.raises(ValueError):
        event_id("E", "x", naive)


def test_non_utc_aware_timestamps_are_normalised() -> None:
    tz_two = timezone(timedelta(hours=2))
    assert candle_id("ETHUSDT", Timeframe.H4, datetime(2026, 1, 1, 2, tzinfo=tz_two)) == candle_id(
        "ETHUSDT", Timeframe.H4, ts()
    )


def test_no_collisions_over_a_realistic_grid() -> None:
    """6000 candle ids, 4 timeframes x 5 symbols x 300 candles -> all unique."""
    timeframes = list(Timeframe)
    ids: set[str] = set()
    expected = 0
    for symbol in ("ETHUSDT", "BTCUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"):
        for tf in timeframes:
            for i in range(300):
                open_time = ts() + timedelta(minutes=15 * i)
                ids.add(candle_id(symbol, tf, open_time))
                expected += 1
    assert len(ids) == expected

    signal_ids = {
        signal_id(symbol, tf, ts() + timedelta(hours=4 * i), "EMA200_ATR_BASELINE", "1.0.0", "h")
        for symbol in ("ETHUSDT", "BTCUSDT")
        for tf in timeframes
        for i in range(200)
    }
    assert len(signal_ids) == 2 * len(timeframes) * 200
