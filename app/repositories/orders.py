"""Order persistence — creation only (state changes go through the state machine)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.common.enums import OrderState
from app.common.ids import order_id as make_order_id
from app.common.rounding import money
from app.common.rounding import price as price_of
from app.common.rounding import qty as qty_of
from app.models.order import Order
from app.models.signal import Signal


def create_entry(
    session: Session,
    *,
    signal: Signal,
    quantity: Decimal,
    notional: Decimal,
    stop_loss: Decimal,
    take_profit: Decimal,
    now: datetime,
) -> Order:
    """Create the PENDING ENTRY order that targets the signal's next candle."""
    order = Order(
        order_id=make_order_id(signal.signal_id),
        signal_id=signal.signal_id,
        symbol=signal.symbol,
        timeframe=signal.timeframe,
        side=signal.action,  # BUY/SELL only: HOLD signals never become orders
        order_type="MARKET_ON_OPEN",
        purpose="ENTRY",
        target_execution_open_time=signal.target_execution_open_time,
        target_execution_candle_id=signal.target_execution_candle_id,
        intended_quantity=qty_of(quantity),
        intended_notional=money(notional),
        stop_loss_price=price_of(stop_loss),
        take_profit_price=price_of(take_profit),
        state=OrderState.PENDING.value,
        execution_provider="paper",
        strategy_version=signal.strategy_version,
        strategy_config_hash=signal.strategy_config_hash,
        created_at=now,
        state_updated_at=now,
        state_reason="created pending execution target",
    )
    session.add(order)
    session.flush()
    return order


def create_exit(
    session: Session,
    *,
    position: object,
    quantity: Decimal,
    notional: Decimal,
    target_open_time: datetime,
    target_candle_id: str,
    now: datetime,
) -> Order:
    """Create the EXIT order that closes a position at a known price level.

    Exits are modelled as orders too, so *every* fill in the ledger traces back
    to an order and a target candle — the same invariant as entries.
    """
    signal_id = getattr(position, "signal_id", None) or "unknown"
    exit_order = Order(
        order_id=f"ORD::EXIT::{make_order_id(position.position_id)[6:38]}",
        signal_id=signal_id,
        symbol=position.symbol,
        timeframe=position.timeframe,
        side="SELL" if position.side == "BUY" else "BUY",
        order_type="MARKET_ON_OPEN",
        purpose="EXIT",
        target_execution_open_time=target_open_time,
        target_execution_candle_id=target_candle_id,
        intended_quantity=qty_of(quantity),
        #: The exit level is known before the order is created, so the expected
        #: notional is exact; the fill recomputes it from the executed price.
        intended_notional=money(notional),
        stop_loss_price=None,
        take_profit_price=None,
        state=OrderState.PENDING.value,
        execution_provider="paper",
        strategy_version=getattr(position, "strategy_version", None),
        strategy_config_hash=getattr(position, "strategy_config_hash", None),
        created_at=now,
        state_updated_at=now,
        state_reason="exit created by position lifecycle",
    )
    session.add(exit_order)
    session.flush()
    return exit_order


def get(session: Session, order_id: str) -> Order | None:
    return session.get(Order, order_id)


def by_signal(session: Session, signal_id: str, *, purpose: str | None = None) -> list[Order]:
    """All orders belonging to a signal, oldest first.

    A signal normally yields an ENTRY order and, once the position closes, an
    EXIT order. Callers that only care about the entry (e.g. the risk engine's
    duplicate check) pass ``purpose="ENTRY"``.
    """
    stmt = select(Order).where(Order.signal_id == signal_id).order_by(Order.created_at)
    if purpose is not None:
        stmt = stmt.where(Order.purpose == purpose)
    return list(session.execute(stmt).scalars().all())


def entry_for_signal(session: Session, signal_id: str) -> Order | None:
    orders = by_signal(session, signal_id, purpose="ENTRY")
    return orders[0] if orders else None


def pending(session: Session, symbol: str, timeframe: str) -> list[Order]:
    stmt = (
        select(Order)
        .where(
            Order.symbol == symbol,
            Order.timeframe == timeframe,
            Order.state.in_([OrderState.CREATED.value, OrderState.PENDING.value]),
        )
        .order_by(Order.target_execution_open_time.asc())
    )
    return list(session.execute(stmt).scalars().all())


def count_by_state(session: Session, symbol: str, timeframe: str) -> dict[str, int]:
    rows = session.execute(
        select(Order.state, func.count())
        .where(Order.symbol == symbol, Order.timeframe == timeframe)
        .group_by(Order.state)
    ).all()
    return {state: int(total) for state, total in rows}


def count(session: Session, *, symbol: str | None = None, timeframe: str | None = None) -> int:
    stmt = select(func.count()).select_from(Order)
    if symbol:
        stmt = stmt.where(Order.symbol == symbol)
    if timeframe:
        stmt = stmt.where(Order.timeframe == timeframe)
    return int(session.execute(stmt).scalar() or 0)


__all__ = ["by_signal", "count", "count_by_state", "create_entry", "create_exit", "get", "pending"]
