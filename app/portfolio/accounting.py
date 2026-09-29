"""Portfolio accounting — derived, never mutated.

**Design decision (ADR-016): there is no mutable cash balance.**

The engine never does ``cash -= notional``. Instead the portfolio is a pure
function of the immutable ledger::

    cash    = starting_balance + Σ net_pnl of closed trades   (the ledger)
    residual= (exit notional - entry notional - fees) - Σ net_pnl
    reserved= Σ notional of ENTRY orders still pending   (never double-counted:
              cash is only debited when a fill actually exists)
    position_value = quantity * last_close
    equity  = cash + reserved + open_position_value
    unrealized_pnl = quantity * (last_close - entry)  for BUY

Consequences:

* the balance invariant cannot drift, because nothing is stored twice;
* replaying history reproduces the equity curve exactly;
* recovery (phase 7) has nothing to repair - it recomputes.

Funding rule: entries are rejected (by the risk engine) when
``cash - reserved`` cannot cover the notional plus fees. There is no leverage and
no margin call in phase 1, which is why ``equity`` can never go negative from a
single trade.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.common.enums import OrderState, Side
from app.common.rounding import money, ratio
from app.common.time_utils import ensure_aware_utc
from app.models.execution import Execution
from app.models.order import Order
from app.models.position import Position

ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class PortfolioState:
    """Derived portfolio snapshot at a point in time."""

    starting_balance: Decimal
    cash: Decimal
    reserved: Decimal
    open_position_value: Decimal
    unrealized_pnl: Decimal
    realized_pnl_cum: Decimal
    fees_cum: Decimal
    equity: Decimal
    peak_equity: Decimal
    drawdown_pct: Decimal

    def as_dict(self) -> dict[str, str]:
        return {
            "cash": str(self.cash),
            "reserved": str(self.reserved),
            "open_position_value": str(self.open_position_value),
            "unrealized_pnl": str(self.unrealized_pnl),
            "realized_pnl_cum": str(self.realized_pnl_cum),
            "fees_cum": str(self.fees_cum),
            "equity": str(self.equity),
            "peak_equity": str(self.peak_equity),
            "drawdown_pct": str(self.drawdown_pct),
        }


def cash_flow_residual(session: Session, *, symbol: str, timeframe: str) -> Decimal:
    """Rounding artefact between the two ways of counting the same money.

    ``cash`` is taken from the ledger (the sum of the stored, immutable
    ``trade.net_pnl``). Summing the raw cash flows instead gives a *very* close
    but not bit-identical number, because quantisation happens at different
    points (per trade vs per execution): at most one unit of the money scale per
    round trip. Reporting it (and asserting its bound in tests) turns a rounding
    difference into a *checked* quantity instead of a hidden one.
    """
    flows = (
        exit_notional_cum(session, symbol=symbol, timeframe=timeframe)
        - entry_notional_cum(session, symbol=symbol, timeframe=timeframe)
        - fees_cum(session, symbol=symbol, timeframe=timeframe)
    )
    return money(flows - realized_pnl_cum(session, symbol=symbol, timeframe=timeframe))


def realized_pnl_cum(session: Session, *, symbol: str, timeframe: str) -> Decimal:
    from app.models.trade import Trade

    total = session.execute(
        select(func.coalesce(func.sum(Trade.net_pnl), 0)).where(
            Trade.symbol == symbol, Trade.timeframe == timeframe
        )
    ).scalar()
    return money(total or ZERO)


def fees_cum(session: Session, *, symbol: str, timeframe: str) -> Decimal:
    total = session.execute(
        select(func.coalesce(func.sum(Execution.fee), 0)).where(
            Execution.symbol == symbol, Execution.timeframe == timeframe
        )
    ).scalar()
    return money(total or ZERO)


def entry_notional_cum(session: Session, *, symbol: str, timeframe: str) -> Decimal:
    """Notional of every ENTRY fill (money that left the account)."""
    total = session.execute(
        select(func.coalesce(func.sum(Execution.notional), 0))
        .join(Order, Order.order_id == Execution.order_id)
        .where(
            Execution.symbol == symbol,
            Execution.timeframe == timeframe,
            Order.purpose == "ENTRY",
        )
    ).scalar()
    return money(total or ZERO)


def exit_notional_cum(session: Session, *, symbol: str, timeframe: str) -> Decimal:
    """Notional of every EXIT fill (money that came back)."""
    total = session.execute(
        select(func.coalesce(func.sum(Execution.notional), 0))
        .join(Order, Order.order_id == Execution.order_id)
        .where(
            Execution.symbol == symbol,
            Execution.timeframe == timeframe,
            Order.purpose == "EXIT",
        )
    ).scalar()
    return money(total or ZERO)


def reserved_cash(session: Session, *, symbol: str, timeframe: str) -> Decimal:
    """Notional committed to pending ENTRY orders (no fill exists yet)."""
    total = session.execute(
        select(func.coalesce(func.sum(Order.intended_notional), 0)).where(
            Order.symbol == symbol,
            Order.timeframe == timeframe,
            Order.purpose == "ENTRY",
            Order.state.in_([OrderState.CREATED.value, OrderState.PENDING.value]),
        )
    ).scalar()
    return money(total or ZERO)


def unrealized_pnl(position: Position, last_price: Decimal) -> Decimal:
    """Mark-to-market PnL of an open position (long-only in the baseline)."""
    direction = Decimal(1) if Side(position.side) is Side.BUY else Decimal(-1)
    return money((last_price - position.entry_price) * position.quantity * direction)


def position_value(position: Position, last_price: Decimal) -> Decimal:
    return money(last_price * position.quantity)


def available_cash(state: PortfolioState) -> Decimal:
    """Cash that may be committed to a new order."""
    return money(state.cash - state.reserved)


def compute_state(
    session: Session,
    *,
    symbol: str,
    timeframe: str,
    starting_balance: Decimal,
    last_price: Decimal,
    position: Position | None,
    peak_equity: Decimal | None = None,
) -> PortfolioState:
    """Recompute the portfolio from the ledger. Nothing is cached or mutated."""
    starting = money(starting_balance)
    realized = realized_pnl_cum(session, symbol=symbol, timeframe=timeframe)
    # cash is defined *by the ledger*, so "cash == starting + realized PnL" holds
    # by construction and not merely by coincidence. The cash-flow view is kept
    # as a cross-check (see cash_flow_residual).
    cash = money(starting + realized)
    reserved = reserved_cash(session, symbol=symbol, timeframe=timeframe)
    value = position_value(position, last_price) if position is not None else ZERO
    unrealized = unrealized_pnl(position, last_price) if position is not None else ZERO
    fees = fees_cum(session, symbol=symbol, timeframe=timeframe)

    equity = money(cash + reserved + value)
    peak = money(max(peak_equity or starting, equity))
    drawdown = ratio(ZERO) if peak <= ZERO else ratio((peak - equity) / peak)

    return PortfolioState(
        starting_balance=starting,
        cash=cash,
        reserved=reserved,
        open_position_value=money(value),
        unrealized_pnl=unrealized,
        realized_pnl_cum=realized,
        fees_cum=fees,
        equity=equity,
        peak_equity=peak,
        drawdown_pct=drawdown,
    )


def previous_peak_equity(session: Session, *, symbol: str, timeframe: str) -> Decimal | None:
    from app.models.portfolio_snapshot import PortfolioSnapshot

    value = session.execute(
        select(func.max(PortfolioSnapshot.peak_equity)).where(
            PortfolioSnapshot.symbol == symbol, PortfolioSnapshot.timeframe == timeframe
        )
    ).scalar()
    return money(value) if value is not None else None


def snapshot(
    session: Session,
    *,
    symbol: str,
    timeframe: str,
    candle_open_time: datetime,
    starting_balance: Decimal,
    last_price: Decimal,
    position: Position | None,
    now: datetime,
) -> object:
    """Persist one equity-curve point (idempotent by deterministic id)."""
    from app.common.ids import snapshot_id as make_snapshot_id
    from app.models.portfolio_snapshot import PortfolioSnapshot

    state = compute_state(
        session,
        symbol=symbol,
        timeframe=timeframe,
        starting_balance=starting_balance,
        last_price=last_price,
        position=position,
        peak_equity=previous_peak_equity(session, symbol=symbol, timeframe=timeframe),
    )
    snapshot_identifier = make_snapshot_id(symbol, timeframe, candle_open_time)
    existing = session.get(PortfolioSnapshot, snapshot_identifier)
    if existing is not None:
        existing.cash = state.cash
        existing.reserved = state.reserved
        existing.open_position_value = state.open_position_value
        existing.unrealized_pnl = state.unrealized_pnl
        existing.realized_pnl_cum = state.realized_pnl_cum
        existing.fees_cum = state.fees_cum
        existing.equity = state.equity
        existing.peak_equity = state.peak_equity
        existing.drawdown_pct = state.drawdown_pct
        session.flush()
        return existing

    row = PortfolioSnapshot(
        snapshot_id=snapshot_identifier,
        symbol=symbol,
        timeframe=timeframe,
        at_candle_open_time=ensure_aware_utc(candle_open_time),
        cash=state.cash,
        reserved=state.reserved,
        open_position_value=state.open_position_value,
        unrealized_pnl=state.unrealized_pnl,
        realized_pnl_cum=state.realized_pnl_cum,
        fees_cum=state.fees_cum,
        equity=state.equity,
        peak_equity=state.peak_equity,
        drawdown_pct=state.drawdown_pct,
        created_at=ensure_aware_utc(now),
    )
    session.add(row)
    session.flush()
    return row


#: Re-exported so callers can quantise money without importing rounding directly.
money = money

__all__ = [
    "PortfolioState",
    "available_cash",
    "cash_flow_residual",
    "compute_state",
    "entry_notional_cum",
    "exit_notional_cum",
    "fees_cum",
    "position_value",
    "previous_peak_equity",
    "realized_pnl_cum",
    "reserved_cash",
    "snapshot",
    "money",
    "unrealized_pnl",
]
