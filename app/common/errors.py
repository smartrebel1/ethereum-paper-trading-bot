"""Exception hierarchy.

Rule of thumb used across the codebase:
* ``ConfigurationError`` / ``SafetyViolationError`` -> refuse to start.
* ``DataValidationError``      -> reject the datum, never mutate state.
* ``IntegrityViolationError``  -> persist an integrity_event, never "fix up".
* ``IllegalStateTransitionError`` -> programming error; raise loudly.
"""

from __future__ import annotations

from typing import Any


class TradingBotError(Exception):
    """Base class for every error raised deliberately by this codebase."""


class ConfigurationError(TradingBotError):
    """Invalid / inconsistent configuration."""


class SafetyViolationError(TradingBotError):
    """A safety invariant about *trading mode* was violated."""


class StartupGuardError(SafetyViolationError):
    """Pre-flight check refused to start the process."""


class DataValidationError(TradingBotError):
    """A market-data payload failed validation and must be rejected."""

    def __init__(self, code: str, message: str, **context: Any) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.context: dict[str, Any] = context


class IntegrityViolationError(TradingBotError):
    """A hard invariant of the engine was violated (never silently repaired)."""

    def __init__(self, code: str, message: str, **context: Any) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.context: dict[str, Any] = context


class ExecutionInvariantError(IntegrityViolationError):
    """Execution was attempted against the wrong candle."""


class ExecutionTargetMissedError(ExecutionInvariantError):
    """The order's target candle never arrived (gap / reorder / too late)."""


class IllegalStateTransitionError(TradingBotError):
    """A state machine transition that the design forbids."""

    def __init__(self, entity: str, current: str, target: str) -> None:
        super().__init__(f"{entity}: illegal transition {current!r} -> {target!r}")
        self.entity = entity
        self.current = current
        self.target = target


class DeterminismError(TradingBotError):
    """A computation produced a non-reproducible result."""


__all__ = [
    "ConfigurationError",
    "DataValidationError",
    "DeterminismError",
    "ExecutionInvariantError",
    "ExecutionTargetMissedError",
    "IllegalStateTransitionError",
    "IntegrityViolationError",
    "SafetyViolationError",
    "StartupGuardError",
    "TradingBotError",
]
