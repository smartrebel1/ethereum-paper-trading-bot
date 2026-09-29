"""Integrity domain: event catalog + violation codes.

This is the vocabulary the rest of the system speaks. It is deliberately in
one file with no imports so that any layer (strategy, execution, ai, api) can
emit events without creating import cycles.
"""

from __future__ import annotations

from enum import StrEnum


class EventType(StrEnum):
    """Append-only system event catalog (``system_events.event_type``)."""

    # --- data ---------------------------------------------------------------
    CANDLE_RECEIVED = "CANDLE_RECEIVED"
    CANDLE_VALIDATED = "CANDLE_VALIDATED"
    CANDLE_REJECTED = "CANDLE_REJECTED"
    DATA_GAP_DETECTED = "DATA_GAP_DETECTED"
    DATA_SOURCE_REGISTERED = "DATA_SOURCE_REGISTERED"

    # --- strategy / risk / order -------------------------------------------
    SIGNAL_CREATED = "SIGNAL_CREATED"
    SIGNAL_SUPPRESSED = "SIGNAL_SUPPRESSED"
    RISK_APPROVED = "RISK_APPROVED"
    RISK_REJECTED = "RISK_REJECTED"
    ORDER_CREATED = "ORDER_CREATED"
    ORDER_EXECUTION_TARGET_REACHED = "ORDER_EXECUTION_TARGET_REACHED"
    ORDER_EXECUTED = "ORDER_EXECUTED"
    ORDER_TARGET_MISSED = "ORDER_TARGET_MISSED"
    ORDER_TARGET_UNREACHABLE = "ORDER_TARGET_UNREACHABLE"
    ORDER_CANCELLED = "ORDER_CANCELLED"

    # --- position lifecycle -------------------------------------------------
    POSITION_OPENED = "POSITION_OPENED"
    POSITION_CLOSED = "POSITION_CLOSED"
    STOP_LOSS_HIT = "STOP_LOSS_HIT"
    TAKE_PROFIT_HIT = "TAKE_PROFIT_HIT"
    PORTFOLIO_SNAPSHOT_TAKEN = "PORTFOLIO_SNAPSHOT_TAKEN"
    TRADE_RECORDED = "TRADE_RECORDED"

    # --- system -------------------------------------------------------------
    STATE_RECOVERED = "STATE_RECOVERED"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"
    STARTUP_GUARD_PASSED = "STARTUP_GUARD_PASSED"
    STARTUP_GUARD_FAILED = "STARTUP_GUARD_FAILED"
    SCHEDULER_TICK = "SCHEDULER_TICK"
    SCHEDULER_MISSED = "SCHEDULER_MISSED"
    STRATEGY_VERSION_REGISTERED = "STRATEGY_VERSION_REGISTERED"
    CONFIGURATION_REGISTERED = "CONFIGURATION_REGISTERED"

    # --- ai (never fed back into the baseline path) -------------------------
    AI_OBSERVATION_CREATED = "AI_OBSERVATION_CREATED"
    AI_ERROR = "AI_ERROR"
    AI_DISABLED = "AI_DISABLED"


class IntegrityCode(StrEnum):
    """Specific invariant violations (``integrity_events.code``)."""

    # execution-target invariants (the FINDING-001 family)
    EXECUTION_TARGET_MISSED = "EXECUTION_TARGET_MISSED"
    EXECUTION_TARGET_UNREACHABLE = "EXECUTION_TARGET_UNREACHABLE"
    EXECUTION_TARGET_MISMATCH = "EXECUTION_TARGET_MISMATCH"
    LATE_EXECUTION_ATTEMPT = "LATE_EXECUTION_ATTEMPT"
    EARLY_EXECUTION_ATTEMPT = "EARLY_EXECUTION_ATTEMPT"
    DUPLICATE_EXECUTION = "DUPLICATE_EXECUTION"

    # look-ahead
    LOOKAHEAD_DETECTED = "LOOKAHEAD_DETECTED"
    INCOMPLETE_CANDLE_USED = "INCOMPLETE_CANDLE_USED"

    # candles
    DUPLICATE_CANDLE = "DUPLICATE_CANDLE"
    OUT_OF_ORDER_CANDLE = "OUT_OF_ORDER_CANDLE"
    MISALIGNED_CANDLE = "MISALIGNED_CANDLE"
    OHLC_INVARIANT_BROKEN = "OHLC_INVARIANT_BROKEN"
    NEGATIVE_VOLUME = "NEGATIVE_VOLUME"
    NON_POSITIVE_PRICE = "NON_POSITIVE_PRICE"
    DATA_GAP = "DATA_GAP"

    # state machines
    ORDER_STATE_ILLEGAL_TRANSITION = "ORDER_STATE_ILLEGAL_TRANSITION"
    POSITION_STATE_ILLEGAL_TRANSITION = "POSITION_STATE_ILLEGAL_TRANSITION"

    # accounting
    BALANCE_INVARIANT_BROKEN = "BALANCE_INVARIANT_BROKEN"
    LEDGER_MISMATCH = "LEDGER_MISMATCH"
    DUPLICATE_LEDGER_ENTRY = "DUPLICATE_LEDGER_ENTRY"

    # safety / recovery
    STARTUP_MODE_INVALID = "STARTUP_MODE_INVALID"
    CRASH_DURING_TRANSACTION = "CRASH_DURING_TRANSACTION"
    UNKNOWN_STATE_ON_RECOVERY = "UNKNOWN_STATE_ON_RECOVERY"


#: Codes that mean "the process should stop trading" (used by the phase-8
#: scheduler kill-switch and by the dashboard badge).
FATAL_INTEGRITY_CODES: frozenset[IntegrityCode] = frozenset(
    {
        IntegrityCode.EXECUTION_TARGET_MISMATCH,
        IntegrityCode.LATE_EXECUTION_ATTEMPT,
        IntegrityCode.EARLY_EXECUTION_ATTEMPT,
        IntegrityCode.LOOKAHEAD_DETECTED,
        IntegrityCode.BALANCE_INVARIANT_BROKEN,
        IntegrityCode.LEDGER_MISMATCH,
        IntegrityCode.STARTUP_MODE_INVALID,
    }
)

ALL_EVENT_TYPES: frozenset[str] = frozenset(member.value for member in EventType)
ALL_INTEGRITY_CODES: frozenset[str] = frozenset(member.value for member in IntegrityCode)


__all__ = [
    "ALL_EVENT_TYPES",
    "ALL_INTEGRITY_CODES",
    "FATAL_INTEGRITY_CODES",
    "EventType",
    "IntegrityCode",
]
