"""Database-level invariants, exercised against a real migrated SQLite file.

These are the tests that prove the schema — not just the application code —
refuses to represent an invalid trading state.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from app.models import (
    Candle,
    Execution,
    Order,
    PortfolioSnapshot,
    Position,
    RiskDecision,
    Signal,
    Trade,
)
from tests.factories import (
    BASE_OPEN,
    candle_row,
    execution_row,
    order_row,
    position_row,
    signal_row,
    snapshot_row,
    trade_row,
)

UTC = UTC


def insert(session: Session, model: type, row: dict) -> None:  # noqa: ANN401
    session.add(model(**row))
    session.flush()


# ------------------------------------------------------------------ candles
def test_valid_candle_round_trips_exactly(session: Session) -> None:
    row = candle_row(close=Decimal("2500.12345678"))
    insert(session, Candle, row)
    session.commit()

    stored = session.get(Candle, row["candle_id"])
    assert stored is not None
    assert stored.close == Decimal("2500.12345678"), "decimal precision must survive the round trip"
    assert stored.open_time.tzinfo is not None, "timestamps must come back timezone-aware"
    assert stored.open_time.utcoffset() == timedelta(0)
    assert stored.is_complete is True


def test_ohlc_violation_is_rejected_by_the_database(session: Session) -> None:
    with pytest.raises(IntegrityError):
        insert(session, Candle, candle_row(high=Decimal("2400"), low=Decimal("2490")))
    session.rollback()


def test_negative_volume_is_rejected(session: Session) -> None:
    with pytest.raises(IntegrityError):
        insert(session, Candle, candle_row(volume=Decimal("-1")))
    session.rollback()


def test_non_positive_price_is_rejected(session: Session) -> None:
    with pytest.raises(IntegrityError):
        insert(
            session,
            Candle,
            candle_row(low=Decimal("0"), high=Decimal("10"), open=Decimal("5"), close=Decimal("5")),
        )
    session.rollback()


def test_duplicate_candle_is_rejected_on_symbol_timeframe_open_time(session: Session) -> None:
    insert(session, Candle, candle_row())
    session.commit()
    with pytest.raises(IntegrityError):
        # Different candle_id entirely, same identity -> still a duplicate.
        insert(session, Candle, candle_row(candle_id="CDL::manual-row"))
    session.rollback()


def test_unsupported_timeframe_is_rejected(session: Session) -> None:
    with pytest.raises(IntegrityError):
        insert(session, Candle, candle_row(timeframe="3h", candle_id="CDL::x"))
    session.rollback()


# ------------------------------------------------------------------ signals
def test_signal_target_must_be_after_the_signal_candle(session: Session) -> None:
    """The core no-lookahead rule, enforced by the database."""
    bad = signal_row(
        target_execution_open_time=BASE_OPEN,
        signal_candle_close_time=BASE_OPEN + timedelta(hours=4),
    )
    with pytest.raises(IntegrityError):
        insert(session, Signal, bad)
    session.rollback()


def test_signal_with_next_candle_target_is_accepted(session: Session) -> None:
    row = signal_row()
    insert(session, Signal, row)
    session.commit()
    assert session.get(Signal, row["signal_id"]) is not None


def test_confidence_out_of_range_is_rejected(session: Session) -> None:
    with pytest.raises(IntegrityError):
        insert(session, Signal, signal_row(confidence=Decimal("1.5")))
    session.rollback()


def test_action_must_be_buy_sell_or_hold(session: Session) -> None:
    with pytest.raises(IntegrityError):
        insert(session, Signal, signal_row(action="LONG"))
    session.rollback()


# ------------------------------------------------------------------- orders
def test_order_requires_an_existing_signal(session: Session) -> None:
    with pytest.raises(IntegrityError):
        insert(session, Order, order_row(signal_row(), signal_id="SIG::does-not-exist"))
    session.rollback()


def test_order_provider_must_be_paper(session: Session) -> None:
    signal = signal_row()
    insert(session, Signal, signal)
    with pytest.raises(IntegrityError):
        insert(session, Order, order_row(signal, execution_provider="live"))
    session.rollback()


def test_order_state_is_restricted(session: Session) -> None:
    signal = signal_row()
    insert(session, Signal, signal)
    with pytest.raises(IntegrityError):
        insert(session, Order, order_row(signal, state="FILLED"))
    session.rollback()


def test_one_order_per_signal(session: Session) -> None:
    signal = signal_row()
    insert(session, Signal, signal)
    insert(session, Order, order_row(signal))
    session.commit()
    with pytest.raises(IntegrityError):
        insert(session, Order, order_row(signal, order_id="ORD::duplicate"))
    session.rollback()


# --------------------------------------------------------------- executions
def test_one_execution_per_order(session: Session) -> None:
    signal = signal_row()
    insert(session, Signal, signal)
    order = order_row(signal)
    insert(session, Order, order)
    insert(session, Execution, execution_row(order))
    session.commit()
    with pytest.raises(IntegrityError):
        insert(session, Execution, execution_row(order, execution_id="EXE::second"))
    session.rollback()


def test_execution_provider_must_be_paper(session: Session) -> None:
    signal = signal_row()
    insert(session, Signal, signal)
    order = order_row(signal)
    insert(session, Order, order)
    with pytest.raises(IntegrityError):
        insert(session, Execution, execution_row(order, provider="binance"))
    session.rollback()


# ---------------------------------------------------------------- positions
def test_only_one_open_position_per_symbol_and_timeframe(session: Session) -> None:
    signal = signal_row()
    insert(session, Signal, signal)
    order = order_row(signal)
    insert(session, Order, order)

    insert(session, Position, position_row(order))
    session.commit()

    second = position_row(order, position_id="POS::second-open")
    with pytest.raises(IntegrityError) as exc:
        insert(session, Position, second)
    # SQLite reports the offending key tuple and omits the index name; the
    # constraint itself is verified by name in the migration/schema tests.
    assert "UNIQUE constraint failed: positions.symbol, positions.timeframe" in str(exc.value)
    session.rollback()


def test_a_closed_position_does_not_block_a_new_open_one(session: Session) -> None:
    signal = signal_row()
    insert(session, Signal, signal)
    order = order_row(signal)
    insert(session, Order, order)

    closed = position_row(
        order,
        position_id="POS::closed",
        state="CLOSED",
        closed_at=BASE_OPEN + timedelta(hours=12),
        close_reason="TP_HIT",
    )
    insert(session, Position, closed)
    insert(session, Position, position_row(order))
    session.commit()  # must not raise


def test_position_requires_positive_sl_and_tp(session: Session) -> None:
    signal = signal_row()
    insert(session, Signal, signal)
    order = order_row(signal)
    insert(session, Order, order)
    with pytest.raises(IntegrityError):
        insert(session, Position, position_row(order, stop_loss=Decimal("0")))
    session.rollback()


def test_position_close_reason_is_restricted(session: Session) -> None:
    signal = signal_row()
    insert(session, Signal, signal)
    order = order_row(signal)
    insert(session, Order, order)
    with pytest.raises(IntegrityError):
        insert(session, Position, position_row(order, state="CLOSED", close_reason="OOPS"))
    session.rollback()


# ------------------------------------------------------------------- trades
def test_trade_exit_reason_is_restricted(session: Session) -> None:
    signal = signal_row()
    insert(session, Signal, signal)
    order = order_row(signal)
    insert(session, Order, order)
    position = position_row(order)
    insert(session, Position, position)
    with pytest.raises(IntegrityError):
        insert(session, Trade, trade_row(position, exit_reason="MOON"))
    session.rollback()


def test_trade_requires_an_existing_entry_order(session: Session) -> None:
    signal = signal_row()
    insert(session, Signal, signal)
    order = order_row(signal)
    insert(session, Order, order)
    position = position_row(order)
    insert(session, Position, position)
    with pytest.raises(IntegrityError):
        insert(session, Trade, trade_row(position, entry_order_id="ORD::missing"))
    session.rollback()


# ------------------------------------------------------- risk + snapshots
def test_approved_risk_decision_requires_sizing(session: Session) -> None:
    signal = signal_row()
    insert(session, Signal, signal)
    with pytest.raises(IntegrityError):
        insert(
            session,
            RiskDecision,
            {
                "signal_id": signal["signal_id"],
                "outcome": "APPROVED",
                "reason_code": "OK",
                "reasons": [],
                "approved_quantity": None,
                "approved_notional": None,
                "decided_at": BASE_OPEN,
            },
        )
    session.rollback()


def test_rejected_risk_decision_needs_no_sizing(session: Session) -> None:
    signal = signal_row()
    insert(session, Signal, signal)
    insert(
        session,
        RiskDecision,
        {
            "signal_id": signal["signal_id"],
            "outcome": "REJECTED",
            "reason_code": "MAX_EXPOSURE",
            "reasons": ["existing open position"],
            "decided_at": BASE_OPEN,
        },
    )
    session.commit()


def test_snapshot_equity_identity_is_enforced(session: Session) -> None:
    with pytest.raises(IntegrityError):
        insert(session, PortfolioSnapshot, snapshot_row(equity=Decimal("999.00000000")))
    session.rollback()


# ------------------------------------------------------- type-level guards
def test_naive_datetime_is_refused_at_the_driver_boundary(session: Session) -> None:
    row = candle_row()
    row["received_at"] = datetime(2026, 1, 1)  # naive on purpose
    with pytest.raises(StatementError) as exc:
        insert(session, Candle, row)
    assert "naive datetime rejected" in str(exc.value)
    session.rollback()


def test_foreign_keys_are_enforced_by_the_pragma(session: Session) -> None:
    from sqlalchemy import text

    value = session.execute(text("PRAGMA foreign_keys")).scalar()
    assert value == 1, "PRAGMA foreign_keys must be ON (SQLite defaults to OFF)"
