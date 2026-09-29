"""Event bus: ordering, containment, idempotent ids, immutability."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from app.events.bus import EventBus, EventEnvelope
from app.integrity.enums import EventType

UTC = UTC
TS = datetime(2026, 1, 1, 4, tzinfo=UTC)


def test_publish_delivers_in_subscription_order() -> None:
    bus = EventBus()
    seen: list[str] = []
    bus.subscribe(EventType.CANDLE_VALIDATED, lambda _event: seen.append("first"))
    bus.subscribe(EventType.CANDLE_VALIDATED, lambda _event: seen.append("second"))
    bus.emit(EventType.CANDLE_VALIDATED, timestamp=TS, entity_id="CDL::x")
    assert seen == ["first", "second"]


def test_wildcard_subscribers_receive_everything() -> None:
    bus = EventBus()
    seen: list[str] = []
    bus.subscribe_all(lambda e: seen.append(e.event_type))
    bus.emit(EventType.CANDLE_VALIDATED, timestamp=TS)
    bus.emit(EventType.SIGNAL_CREATED, timestamp=TS)
    assert seen == ["CANDLE_VALIDATED", "SIGNAL_CREATED"]


def test_handler_failure_is_contained_and_reported() -> None:
    bus = EventBus()
    delivered: list[str] = []

    def broken(_: EventEnvelope) -> None:
        raise RuntimeError("observer exploded")

    bus.subscribe(EventType.CANDLE_VALIDATED, broken)
    bus.subscribe(EventType.CANDLE_VALIDATED, lambda event: delivered.append(event.event_type))

    failures = bus.publish(EventEnvelope(event_type="CANDLE_VALIDATED", timestamp=TS))
    assert delivered == ["CANDLE_VALIDATED"], "a broken observer must not stop delivery"
    assert len(failures) == 1
    assert "observer exploded" in failures[0].error


def test_unsubscribe() -> None:
    bus = EventBus()
    seen: list[str] = []
    handler = bus.subscribe(EventType.SCHEDULER_TICK, lambda _event: seen.append("x"))
    bus.unsubscribe(EventType.SCHEDULER_TICK, handler)
    bus.emit(EventType.SCHEDULER_TICK, timestamp=TS)
    assert seen == []


def test_event_id_is_deterministic_and_unique_per_sequence() -> None:
    a = EventEnvelope(event_type="X", timestamp=TS, entity_id="e1")
    b = EventEnvelope(event_type="X", timestamp=TS, entity_id="e1")
    c = EventEnvelope(event_type="X", timestamp=TS, entity_id="e1", seq=1)
    assert a.event_id == b.event_id
    assert a.event_id != c.event_id


def test_envelope_is_immutable() -> None:
    envelope = EventEnvelope(event_type="X", timestamp=TS)
    with pytest.raises(FrozenInstanceError):
        envelope.event_type = "Y"  # type: ignore[misc]


def test_to_row_matches_system_events_columns() -> None:
    envelope = EventEnvelope(
        event_type="CANDLE_VALIDATED",
        timestamp=TS,
        entity_id="CDL::x",
        symbol="ETHUSDT",
        timeframe="4h",
        payload={"close": "2500.1"},
    )
    row = envelope.to_row(software_version="0.1.0", strategy_version="1.0.0")
    assert set(row) == {
        "event_id",
        "event_type",
        "timestamp",
        "symbol",
        "timeframe",
        "entity_id",
        "payload",
        "software_version",
        "strategy_version",
    }
    assert row["event_id"] == envelope.event_id


def test_events_of_filters_history() -> None:
    bus = EventBus()
    bus.emit(EventType.CANDLE_VALIDATED, timestamp=TS)
    bus.emit(EventType.SIGNAL_CREATED, timestamp=TS)
    assert len(bus.events_of(EventType.CANDLE_VALIDATED)) == 1
    assert len(bus.published) == 2
    bus.clear()
    assert bus.published == ()
