"""Immutable trade ledger.

One row per closed round-trip, written once, inside the same transaction that
closes the position. ``net_pnl = gross_pnl - entry_fee - exit_fee`` is computed
here and stored, so a report never has to re-derive PnL (and can never disagree
with the ledger about it).

``trade_id`` is derived from the position id, so replaying the same history
reproduces the same ledger rows byte for byte.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.common.enums import ExitReason, Side
from app.common.ids import trade_id as make_trade_id
from app.common.rounding import money
from app.common.rounding import price as price_of
from app.common.rounding import qty as qty_of
from app.models.execution import Execution
from app.models.order import Order
from app.models.position import Position
from app.models.trade import Trade

ZERO = Decimal(0)


def gross_pnl(side: Side, entry_price: Decimal, exit_price: Decimal, quantity: Decimal) -> Decimal:
    direction = Decimal(1) if side is Side.BUY else Decimal(-1)
    return money((exit_price - entry_price) * quantity * direction)


def record(
    session: Session,
    *,
    position: Position,
    exit_price: Decimal,
    exit_fee: Decimal,
    exit_reason: ExitReason,
    exit_order_id: str | None,
    exit_time: datetime,
    strategy_version: str,
    strategy_config_hash: str,
) -> Trade:
    """Append the immutable round-trip row."""
    side = Side(position.side)
    entry_price = price_of(position.entry_price)
    exit_price_value = price_of(exit_price)
    quantity = qty_of(position.quantity)
    gross = gross_pnl(side, entry_price, exit_price_value, quantity)
    fee_total = money(position.entry_fee + exit_fee)
    net = money(gross - fee_total)

    holding = int((exit_time - position.entry_time).total_seconds())
    trade = Trade(
        trade_id=make_trade_id(position.position_id),
        position_id=position.position_id,
        symbol=position.symbol,
        timeframe=position.timeframe,
        signal_id=position.signal_id or "unknown",
        entry_order_id=position.entry_order_id or "unknown",
        exit_order_id=exit_order_id,
        side=side.value,
        quantity=quantity,
        entry_price=entry_price,
        exit_price=exit_price_value,
        entry_time=position.entry_time,
        exit_time=exit_time,
        holding_seconds=max(0, holding),
        entry_fee=money(position.entry_fee),
        exit_fee=money(exit_fee),
        gross_pnl=gross,
        net_pnl=net,
        exit_reason=exit_reason.value,
        strategy_version=strategy_version,
        strategy_config_hash=strategy_config_hash,
        created_at=exit_time,
    )
    session.add(trade)
    session.flush()
    return trade


def existing_trade(session: Session, position_id: str) -> Trade | None:
    stmt = select(Trade).where(Trade.position_id == position_id)
    return session.execute(stmt).scalars().first()


def count(session: Session, *, symbol: str | None = None, timeframe: str | None = None) -> int:
    stmt = select(func.count()).select_from(Trade)
    if symbol:
        stmt = stmt.where(Trade.symbol == symbol)
    if timeframe:
        stmt = stmt.where(Trade.timeframe == timeframe)
    return int(session.execute(stmt).scalar() or 0)


def recent(session: Session, *, symbol: str, timeframe: str, limit: int = 20) -> list[Trade]:
    stmt = (
        select(Trade)
        .where(Trade.symbol == symbol, Trade.timeframe == timeframe)
        .order_by(Trade.exit_time.desc())
        .limit(limit)
    )
    return list(session.execute(stmt).scalars().all())


def statistics(session: Session, *, symbol: str, timeframe: str) -> dict[str, object]:
    """Performance summary computed from the ledger alone (dashboard input)."""
    trades = list(
        session.execute(
            select(Trade)
            .where(Trade.symbol == symbol, Trade.timeframe == timeframe)
            .order_by(Trade.exit_time.asc())
        )
        .scalars()
        .all()
    )
    if not trades:
        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": None,
            "net_pnl": "0",
            "gross_profit": "0",
            "gross_loss": "0",
            "profit_factor": None,
            "expectancy": None,
            "avg_win": None,
            "avg_loss": None,
            "best": None,
            "worst": None,
            "max_drawdown": None,
            "avg_holding_hours": None,
            "by_exit_reason": {},
        }

    wins = [t for t in trades if t.net_pnl > ZERO]
    losses = [t for t in trades if t.net_pnl < ZERO]
    gross_profit = money(sum(t.net_pnl for t in wins)) if wins else ZERO
    gross_loss = money(sum(t.net_pnl for t in losses)) if losses else ZERO
    net = money(sum(t.net_pnl for t in trades))
    equity = ZERO
    peak = ZERO
    max_dd = ZERO
    for trade in trades:
        equity += trade.net_pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    by_reason: dict[str, int] = {}
    for trade in trades:
        by_reason[trade.exit_reason] = by_reason.get(trade.exit_reason, 0) + 1

    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(trades), 4),
        "net_pnl": str(net),
        "gross_profit": str(gross_profit),
        "gross_loss": str(gross_loss),
        "profit_factor": (
            None if gross_loss == ZERO else round(float(gross_profit) / abs(float(gross_loss)), 4)
        ),
        "expectancy": str(money(net / len(trades))),
        "avg_win": str(money(gross_profit / len(wins))) if wins else None,
        "avg_loss": str(money(gross_loss / len(losses))) if losses else None,
        "best": str(max(t.net_pnl for t in trades)),
        "worst": str(min(t.net_pnl for t in trades)),
        "max_drawdown": str(money(max_dd)),
        "avg_holding_hours": round(sum(t.holding_seconds for t in trades) / len(trades) / 3600, 2),
        "by_exit_reason": by_reason,
    }


__all__ = [
    "count",
    "existing_trade",
    "gross_pnl",
    "recent",
    "record",
    "statistics",
]

_ = (Execution, Order)
