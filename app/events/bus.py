"""In-process event bus.

Design notes (what is different from "just call the next function"):

* Events are **immutable envelopes** (:class:`EventEnvelope`) carrying the
  deterministic ``event_id`` that is also used as the ``system_events`` PK, so
  a replayed run produces the same event log.
* Handler failures are **contained**: one broken observer (an AI shadow, the
  dashboard, a metrics collector) must never abort candle processing. Failures
  are collected and returned to the publisher, which persists them as
  integrity events.
* Delivery is synchronous and ordered: determinism beats throughput here.

Phase 1 ships the bus + tests; phases 2-8 subscribe the ingestor, strategy,
risk, execution and integrity monitor to it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.common.clock import Clock, get_clock
from app.common.ids import event_id as make_event_id
from app.common.time_utils import to_iso_z
from app.integrity.enums import EventType

Handler = Callable[["EventEnvelope"], None]

#: Wildcard subscription token.
ANY = "*"


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    """Immutable event envelope."""

    event_type: str
    timestamp: datetime
    entity_id: str | None = None
    symbol: str | None = None
    timeframe: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    seq: int = 0

    @property
    def event_id(self) -> str:
        return make_event_id(self.event_type, self.entity_id or "-", self.timestamp, self.seq)

    def to_row(self, *, software_version: str, strategy_version: str | None = None) -> dict[str, Any]:
        """Column mapping for ``system_events``."""
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "timestamp": self.timestamp,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "entity_id": self.entity_id,
            "payload": dict(self.payload),
            "software_version": software_version,
            "strategy_version": strategy_version,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "timestamp": to_iso_z(self.timestamp),
            "entity_id": self.entity_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "payload": dict(self.payload),
        }


@dataclass(frozen=True, slots=True)
class HandlerFailure:
    event: EventEnvelope
    handler: str
    error: str


class EventBus:
    """Tiny synchronous pub/sub with deterministic ordering."""

    def __init__(self, *, clock: Clock | None = None) -> None:
        self._clock = clock or get_clock()
        self._handlers: dict[str, list[Handler]] = {}
        self._published: list[EventEnvelope] = []
        self._failures: list[HandlerFailure] = []

    # ------------------------------------------------------------- wiring
    def subscribe(self, event_type: EventType | str, handler: Handler) -> Handler:
        key = event_type.value if isinstance(event_type, EventType) else str(event_type)
        self._handlers.setdefault(key, []).append(handler)
        return handler

    def unsubscribe(self, event_type: EventType | str, handler: Handler) -> None:
        key = event_type.value if isinstance(event_type, EventType) else str(event_type)
        handlers = self._handlers.get(key, [])
        if handler in handlers:
            handlers.remove(handler)

    def subscribe_all(self, handler: Handler) -> Handler:
        return self.subscribe(ANY, handler)

    def handlers_for(self, event_type: str) -> tuple[Handler, ...]:
        return tuple(self._handlers.get(event_type, ())) + tuple(self._handlers.get(ANY, ()))

    # ---------------------------------------------------------- publishing
    def emit(
        self,
        event_type: EventType | str,
        *,
        entity_id: str | None = None,
        symbol: str | None = None,
        timeframe: str | None = None,
        payload: dict[str, Any] | None = None,
        timestamp: datetime | None = None,
        seq: int = 0,
    ) -> EventEnvelope:
        """Build and publish an event."""
        envelope = EventEnvelope(
            event_type=event_type.value if isinstance(event_type, EventType) else str(event_type),
            timestamp=timestamp or self._clock.now(),
            entity_id=entity_id,
            symbol=symbol,
            timeframe=timeframe,
            payload=payload or {},
            seq=seq,
        )
        self.publish(envelope)
        return envelope

    def publish(self, event: EventEnvelope) -> list[HandlerFailure]:
        """Deliver *event*; never raises because a handler raised."""
        self._published.append(event)
        new_failures: list[HandlerFailure] = []
        for handler in self.handlers_for(event.event_type):
            try:
                handler(event)
            except Exception as exc:  # noqa: BLE001 - containment is the point
                failure = HandlerFailure(
                    event=event,
                    handler=getattr(handler, "__qualname__", repr(handler)),
                    error=f"{type(exc).__name__}: {exc}",
                )
                new_failures.append(failure)
                self._failures.append(failure)
        return new_failures

    def publish_many(self, events: Iterable[EventEnvelope]) -> list[HandlerFailure]:
        failures: list[HandlerFailure] = []
        for event in events:
            failures.extend(self.publish(event))
        return failures

    # ------------------------------------------------------------ inspection
    @property
    def published(self) -> tuple[EventEnvelope, ...]:
        return tuple(self._published)

    @property
    def failures(self) -> tuple[HandlerFailure, ...]:
        return tuple(self._failures)

    def events_of(self, event_type: EventType | str) -> tuple[EventEnvelope, ...]:
        key = event_type.value if isinstance(event_type, EventType) else str(event_type)
        return tuple(e for e in self._published if e.event_type == key)

    def clear(self) -> None:
        self._published.clear()
        self._failures.clear()

    def reset(self) -> None:
        """Remove all subscriptions and history (test hook)."""
        self._handlers.clear()
        self.clear()


__all__ = ["ANY", "EventBus", "EventEnvelope", "Handler", "HandlerFailure"]
