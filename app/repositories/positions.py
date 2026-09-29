"""Position persistence.

The invariant "at most one OPEN position per (symbol, timeframe)" is enforced by
the database (partial unique index). These helpers never try to work around it:
they ask first, and the engine treats a surprise second position as an integrity
failure rather than something to reconcile.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.common.clock import Clock, get_clock
from app.common.enums import ExitReason, PositionState
from app.common.ids import position_id as make_position_id
from app.common.rounding import money
from app.common.rounding import price as price_of
from app.common.rounding import qty as qty_of
from app.models.execution import Execution
from app.models.order import Order
from app.models.position import Position


def open_position(session: Session, symbol: str, timeframe: str) -> Position | None:
    stmt = (
        select(Position)
        .where(
            Position.symbol == symbol,
            Position.timeframe == timeframe,
            Position.state.in_([PositionState.OPEN.value, PositionState.CLOSING.value]),
        )
        .order_by(Position.created_at.asc())
    )
    return session.execute(stmt).scalars().first()


def get(session: Session, position_id: str) -> Position | None:
    return session.get(Position, position_id)


def create(
    session: Session,
    *,
    order: Order,
    execution: Execution,
    stop_loss: Decimal,
    take_profit: Decimal,
    clock: Clock | None = None,
    strategy_version: str | None = None,
    strategy_config_hash: str | None = None,
) -> Position:
    now = (clock or get_clock()).now()
    position = Position(
        position_id=make_position_id(order.symbol, order.timeframe, order.order_id),
        symbol=order.symbol,
        timeframe=order.timeframe,
        side=order.side,
        state=PositionState.OPEN.value,
        quantity=qty_of(execution.quantity),
        entry_price=price_of(execution.actual_execution_price),
        # ``entry_time`` is the *market* moment of the fill: the open of the
        # target candle, i.e. the same clock the exit uses (its candle's open).
        # Holding time is therefore always a whole number of candles, and it is
        # a pure function of the data. The wall-clock moment at which the fill
        # was recorded lives in ``execution_timestamp``.
        entry_time=execution.execution_candle_open_time,
        entry_candle_open_time=execution.execution_candle_open_time,
        entry_order_id=order.order_id,
        signal_id=order.signal_id,
        stop_loss=price_of(stop_loss),
        take_profit=price_of(take_profit),
        entry_fee=money(execution.fee),
        unrealized_pnl=money(Decimal(0)),
        realized_pnl=money(Decimal(0)),
        strategy_version=strategy_version or order.strategy_version or "unknown",
        strategy_config_hash=strategy_config_hash or order.strategy_config_hash or "unknown",
        created_at=now,
        updated_at=now,
    )
    session.add(position)
    session.flush()
    return position


def position_for_signal(session: Session, signal_id: str) -> Position | None:
    """The most recent position opened for a signal (there is at most one)."""
    stmt = (
        select(Position).where(Position.signal_id == signal_id).order_by(Position.opened_at.desc()).limit(1)
    )
    return session.execute(stmt).scalars().first()


def reopen(session: Session, position: Position, *, clock: Clock | None = None) -> Position:
    """CLOSING -> OPEN, used when an exit order is voided by a data hole.

    An exit level is a price condition that survives a gap in the series, so the
    position must keep living until the level can actually be observed. This is
    the only sanctioned way back out of CLOSING.
    """
    now = (clock or get_clock()).now()
    position.state = PositionState.OPEN.value
    position.updated_at = now
    session.flush()
    return position


def mark_closing(session: Session, position: Position, *, clock: Clock | None = None) -> Position:
    """OPEN -> CLOSING (the crash-recoverable intermediate state)."""
    now = (clock or get_clock()).now()
    position.state = PositionState.CLOSING.value
    position.updated_at = now
    session.flush()
    return position


def close(
    session: Session,
    position: Position,
    *,
    exit_reason: ExitReason,
    realized_pnl: Decimal,
    closed_at: datetime,
) -> Position:
    """OPEN/CLOSING -> CLOSED. Exit price and fee are not stored here on purpose:
    they live in the exit execution and the trade row, so there is exactly one
    place where money is recorded (ADR-016)."""
    position.state = PositionState.CLOSED.value
    position.realized_pnl = money(realized_pnl)
    position.unrealized_pnl = money(Decimal(0))
    position.closed_at = closed_at
    position.updated_at = closed_at
    position.close_reason = exit_reason.value
    session.flush()
    return position


__all__ = [
    "close",
    "create",
    "get",
    "mark_closing",
    "open_position",
    "position_for_signal",
    "reopen",
]
