"""Injectable clock.

Phase 1 has no engine that needs time, but the refactor the original plan
deferred to phase 5 ("clock source should later be injected") costs ~0 now and
is impossible to retrofit safely once replay exists. So: every component that
needs "now" takes a :class:`Clock`.

* :class:`SystemClock`  — real wall clock (live paper run).
* :class:`FrozenClock`  — deterministic time for tests and replay.
* :class:`ShiftedClock` — SystemClock + offset, for testing "what if it ran late".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from app.common.time_utils import utcnow


@runtime_checkable
class Clock(Protocol):
    """Anything that can answer "what time is it" (UTC, aware)."""

    def now(self) -> datetime:  # pragma: no cover - protocol
        ...


@dataclass(frozen=True, slots=True)
class SystemClock:
    """Delegates to the process wall clock."""

    def now(self) -> datetime:
        return utcnow()


@dataclass(slots=True)
class FrozenClock:
    """Deterministic clock. ``advance`` is the only way time moves."""

    _now: datetime
    auto_advance: timedelta | None = None

    def __post_init__(self) -> None:
        if self._now.tzinfo is None:
            raise ValueError("FrozenClock requires a timezone-aware UTC datetime")
        self._now = self._now.astimezone(UTC)

    def now(self) -> datetime:
        current = self._now
        if self.auto_advance is not None:
            self._now = current + self.auto_advance
        return current

    def set(self, when: datetime) -> None:
        if when.tzinfo is None:
            raise ValueError("FrozenClock.set requires a timezone-aware datetime")
        self._now = when.astimezone(UTC)

    def advance(self, delta: timedelta) -> datetime:
        self._now = self._now + delta
        return self._now


@dataclass(frozen=True, slots=True)
class ShiftedClock:
    """Real clock plus a fixed offset (latency / clock-drift simulation)."""

    offset: timedelta = field(default=timedelta(0))

    def now(self) -> datetime:
        return utcnow() + self.offset


_clock: Clock = SystemClock()


def get_clock() -> Clock:
    """Process-wide clock used by code that cannot take one via constructor."""
    return _clock


def set_clock(clock: Clock | None = None) -> Clock:
    """Install a clock (tests/replay). Pass ``None`` to restore the system clock."""
    global _clock
    _clock = clock if clock is not None else SystemClock()
    return _clock


__all__ = [
    "Clock",
    "FrozenClock",
    "ShiftedClock",
    "SystemClock",
    "get_clock",
    "set_clock",
]
