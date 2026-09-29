"""Canonical enums for the whole engine.

Design rules
------------
* Values are the *persisted* strings. Never rename a value; add a new one.
* ``LIVE`` is intentionally absent from :class:`TradingMode` and
  :class:`ExecutionProvider`. Adding it requires a full architecture review
  plus an explicit safety approval (master prompt §40/§56).
"""

from __future__ import annotations

from enum import StrEnum


class TradingMode(StrEnum):
    """Execution mode of the process. PAPER only, by construction."""

    PAPER = "paper"


class Timeframe(StrEnum):
    M15 = "15m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"


#: Duration of one closed candle per timeframe, in seconds.
TIMEFRAME_SECONDS: dict[Timeframe, int] = {
    Timeframe.M15: 15 * 60,
    Timeframe.H1: 60 * 60,
    Timeframe.H4: 4 * 60 * 60,
    Timeframe.D1: 24 * 60 * 60,
}


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class Action(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class OrderState(StrEnum):
    """Order lifecycle. See docs/STATE_MACHINE.md for legal transitions."""

    CREATED = "CREATED"
    PENDING = "PENDING"
    TARGET_REACHED = "TARGET_REACHED"
    EXECUTED = "EXECUTED"
    TARGET_MISSED = "TARGET_MISSED"
    CANCELLED = "CANCELLED"


class PositionState(StrEnum):
    OPEN = "OPEN"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"


class RiskOutcome(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ExecutionProvider(StrEnum):
    """Only paper execution exists in this codebase."""

    PAPER = "paper"


class ExitReason(StrEnum):
    """Why a position was closed (mirrored by ck_trade_exit_reason)."""

    SL_HIT = "SL_HIT"
    TP_HIT = "TP_HIT"
    MANUAL = "MANUAL"
    LIQUIDATION = "LIQUIDATION"


class CloseCause(StrEnum):
    """Internal trigger that produced a close (superset of ExitReason)."""

    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    MANUAL = "MANUAL"
    FORCED = "FORCED"


class Severity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


def opposite_side(side: Side) -> Side:
    """Return the reducing side for a given exposure side."""
    return Side.SELL if side is Side.BUY else Side.BUY


__all__ = [
    "TIMEFRAME_SECONDS",
    "Action",
    "CloseCause",
    "ExecutionProvider",
    "ExitReason",
    "OrderState",
    "PositionState",
    "RiskOutcome",
    "Severity",
    "Side",
    "Timeframe",
    "TradingMode",
    "opposite_side",
]
