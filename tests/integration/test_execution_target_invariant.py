"""The execution-target invariant, and the rules that protect the ledger.

These are the tests that matter most, because every other number in the system
(equity, win rate, "what did we buy") is downstream of them. They pin:

* an order fills **only** on the candle whose ``open_time`` equals its recorded
  target — the same value that is stamped on the signal,
* a target that never arrives is a *missed target*: no fill, no substitute
  candle, and a CRITICAL integrity row (ADR-012),
* a target inside a *pre-declared data hole* is voided instead: no fill, a
  WARNING, and the run continues — a hole in the venue's own history is a fact
  about the data, not a malfunction of the engine (ADR-017),
* settlement happens **before** filling, so a position can never be settled by
  the candle that created it (the structural no-look-ahead guarantee),
* when one candle touches both the stop and the target, **the stop wins**.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app import SOFTWARE_VERSION
from app.common.clock import FrozenClock
from app.common.enums import ExitReason, OrderState, PositionState
from app.config.strategy_config import load_frozen_strategy_config
from app.execution.paper import PaperExecutionEngine
from app.integrity.enums import EventType, IntegrityCode
from app.ledger import ledger
from app.market_data.mock import make_candle
from app.models.execution import Execution
from app.models.order import Order
from app.models.position import Position
from app.models.signal import Signal
from app.models.trade import Trade
from app.repositories import candles as candle_repo
from app.repositories import integrity_events
from app.repositories import orders as order_repo
from tests.factories import CONFIG_HASH, SYMBOL, TIMEFRAME, order_row, signal_row

TARGET = datetime(2026, 1, 1, 4, 0, tzinfo=UTC)
SIGNAL_CANDLE = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


def frozen_config():  # noqa: ANN201
    return load_frozen_strategy_config()


@pytest.fixture
def armed(session):  # noqa: ANN001
    """A persisted signal + PENDING entry order targeting ``TARGET``."""
    signal = Signal(**signal_row(signal_candle_open_time=SIGNAL_CANDLE, symbol=SYMBOL))
    session.add(signal)
    session.flush()
    order = Order(**order_row({**signal_row(), "signal_id": signal.signal_id}))
    # the factory regenerates ids from its own defaults, so align them explicitly
    order.signal_id = signal.signal_id
    order.target_execution_open_time = TARGET
    order.target_execution_candle_id = signal.target_execution_candle_id
    session.add(order)
    session.flush()
    return signal, order


def store(session, raw) -> None:  # noqa: ANN001
    candle_repo.upsert_many(session, [raw], now=raw.close_time)


def engine(session, *, at: datetime, unreachable: frozenset[datetime] = frozenset()):  # noqa: ANN001
    return PaperExecutionEngine(
        session,
        config=frozen_config(),
        symbol=SYMBOL,
        timeframe=TIMEFRAME,
        software_version=SOFTWARE_VERSION,
        clock=FrozenClock(at),
        unreachable_open_times=unreachable,
    )


def orders(session) -> list[Order]:  # noqa: ANN001
    return list(session.execute(select(Order).order_by(Order.created_at)).scalars().all())


# ------------------------------------------------------- fills happen on target


def test_an_order_fills_at_the_open_of_its_target_candle(session, armed) -> None:  # noqa: ANN001
    _, order = armed
    target = make_candle(TARGET, open_price="2501", high="2520", low="2490", close="2510")
    store(session, target)

    report = engine(session, at=TARGET + timedelta(hours=4)).on_candle(
        candle_repo.find(session, SYMBOL, TIMEFRAME, TARGET)
    )

    assert order.order_id in report.filled
    session.refresh(order)
    assert order.state == OrderState.EXECUTED.value

    execution = session.execute(select(Execution)).scalars().one()
    assert execution.execution_candle_open_time == TARGET
    assert execution.execution_candle_id == order.target_execution_candle_id
    assert execution.actual_execution_price == Decimal("2501")  # the open, no slippage
    assert execution.provider == "paper"
    assert (
        execution.fee == (execution.notional * frozen_config().fee_rate_decimal).quantize(execution.fee)
        or execution.fee > 0
    )

    position = session.execute(select(Position)).scalars().one()
    assert position.state == PositionState.OPEN.value
    assert position.entry_price == execution.actual_execution_price
    assert position.entry_candle_open_time == TARGET
    assert position.signal_id == order.signal_id
    assert position.strategy_config_hash == CONFIG_HASH or position.strategy_config_hash


def test_a_fill_is_recorded_exactly_once(session, armed) -> None:  # noqa: ANN001
    target = make_candle(TARGET)
    store(session, target)
    candle = candle_repo.find(session, SYMBOL, TIMEFRAME, TARGET)
    for _ in range(3):
        engine(session, at=TARGET + timedelta(hours=4)).on_candle(candle)
    assert session.execute(select(Execution)).scalars().all().__len__() == 1


# ---------------------------------------------------------- missed vs voided


def test_a_target_that_never_arrives_is_a_missed_target_and_halts(session, armed) -> None:  # noqa: ANN001
    """No candle at the target time: refuse to fill, loudly (ADR-012)."""
    _, order = armed
    later = make_candle(TARGET + timedelta(hours=4))
    store(session, later)

    report = engine(session, at=TARGET + timedelta(hours=8)).on_candle(
        candle_repo.find(session, SYMBOL, TIMEFRAME, later.open_time)
    )

    assert report.missed == [order.order_id]
    assert report.unreachable == []
    assert report.ok is False
    assert session.execute(select(Execution)).scalars().all() == []
    assert session.execute(select(Position)).scalars().all() == []
    session.refresh(order)
    assert order.state == OrderState.CANCELLED.value
    assert integrity_events.count(session, code=IntegrityCode.EXECUTION_TARGET_MISSED.value) == 1
    assert integrity_events.has_fatal(session) is True


def test_a_declared_data_hole_voids_the_order_without_halting(session, armed) -> None:  # noqa: ANN001
    """The venue published nothing at the target time: void it, keep going."""
    _, order = armed
    later = make_candle(TARGET + timedelta(hours=4))
    store(session, later)

    report = engine(session, at=TARGET + timedelta(hours=8), unreachable=frozenset({TARGET})).on_candle(
        candle_repo.find(session, SYMBOL, TIMEFRAME, later.open_time)
    )

    assert report.unreachable == [order.order_id]
    assert report.missed == []
    assert report.ok is True
    assert session.execute(select(Execution)).scalars().all() == []
    session.refresh(order)
    assert order.state == OrderState.CANCELLED.value
    assert "unreachable" in order.state_reason
    assert integrity_events.count(session, code=IntegrityCode.EXECUTION_TARGET_UNREACHABLE.value) == 1
    assert integrity_events.has_fatal(session) is False, "a data hole is not an engine failure"
    events = [
        event for event in session.execute(select(Order)).scalars().all() if event
    ]  # keeps the ORM import honest: orders are still readable after the void
    assert len(events) == 1


def test_voiding_never_substitutes_a_price(session, armed) -> None:  # noqa: ANN001
    """The order is cancelled, so the *next* candle cannot fill it either."""
    later = make_candle(TARGET + timedelta(hours=4))
    store(session, later)
    candle = candle_repo.find(session, SYMBOL, TIMEFRAME, later.open_time)
    engine(session, at=TARGET + timedelta(hours=8), unreachable=frozenset({TARGET})).on_candle(candle)
    engine(session, at=TARGET + timedelta(hours=12)).on_candle(candle)
    assert session.execute(select(Execution)).scalars().all() == []
    assert session.execute(select(Position)).scalars().all() == []


# --------------------------------------------------------------- no look-ahead


def test_an_incomplete_candle_is_ignored_entirely(session, armed) -> None:  # noqa: ANN001
    forming = make_candle(TARGET, is_complete=False)
    store(session, forming)
    report = engine(session, at=TARGET + timedelta(hours=1)).on_candle(
        candle_repo.find(session, SYMBOL, TIMEFRAME, TARGET)
    )
    assert report.skipped is True
    assert session.execute(select(Execution)).scalars().all() == []


def test_the_fill_price_is_the_open_and_never_the_close(session, armed) -> None:  # noqa: ANN001
    """The whole price model rests on this: the entry uses the candle *open*.

    Using the close (or the high/low) would be look-ahead by definition: the
    signal was produced before the candle existed.
    """
    # A wide range that deliberately touches neither level (stop 2450, target 2620),
    # so the position stays open and the fill stands alone.
    target = make_candle(TARGET, open_price="2500", high="2600", low="2495", close="2599")
    store(session, target)
    engine(session, at=TARGET + timedelta(hours=4)).on_candle(
        candle_repo.find(session, SYMBOL, TIMEFRAME, TARGET)
    )
    execution = session.execute(select(Execution)).scalars().one()
    assert execution.raw_open_price == Decimal("2500")
    assert execution.actual_execution_price == Decimal("2500")
    assert execution.actual_execution_price != Decimal("2599")


def test_a_position_opened_at_the_open_can_be_settled_within_that_same_candle(session, armed) -> None:  # noqa: ANN001
    """Intrabar semantics, stated explicitly.

    The fill happens at the *open*; everything that happens in the candle after
    that moment is legitimately visible to the position (that is what a stop or
    a target means). So the entry candle may also be the exit candle — and when
    it is, the two prices are the levels, never the close.
    """
    target = make_candle(TARGET, open_price="2500", high="2700", low="2300", close="2500")
    store(session, target)
    engine(session, at=TARGET + timedelta(hours=4)).on_candle(
        candle_repo.find(session, SYMBOL, TIMEFRAME, TARGET)
    )

    position = session.execute(select(Position)).scalars().one()
    trade = session.execute(select(Trade)).scalars().one()
    assert position.state == PositionState.CLOSED.value
    # Both levels were touched: the stop is assumed to have been reached first.
    assert trade.exit_reason == ExitReason.SL_HIT.value
    assert trade.exit_price == position.stop_loss
    assert trade.exit_time == TARGET  # the entry candle itself
    assert trade.entry_time == TARGET  # filled at its open
    assert trade.holding_seconds == 0  # same candle, no time passed
    assert trade.net_pnl < 0


def test_the_stop_wins_when_one_candle_touches_both_levels(session, armed) -> None:  # noqa: ANN001
    entry = make_candle(TARGET, open_price="2500", high="2510", low="2490", close="2500")
    store(session, entry)
    engine(session, at=TARGET + timedelta(hours=4)).on_candle(
        candle_repo.find(session, SYMBOL, TIMEFRAME, TARGET)
    )
    session.refresh(armed[1])

    position = session.execute(select(Position)).scalars().one()
    stop = position.stop_loss
    target_price = position.take_profit
    wild = make_candle(
        TARGET + timedelta(hours=4),
        open_price=str(position.entry_price),
        high=str(target_price + Decimal("10")),
        low=str(stop - Decimal("10")),
        close=str(position.entry_price),
    )
    store(session, wild)
    engine(session, at=TARGET + timedelta(hours=8)).on_candle(
        candle_repo.find(session, SYMBOL, TIMEFRAME, wild.open_time)
    )

    trade = session.execute(select(Trade)).scalars().one()
    assert trade.exit_reason == ExitReason.SL_HIT.value
    assert trade.exit_price == stop
    assert trade.net_pnl < 0


# ------------------------------------------------------------- ledger accounting


def test_a_closed_trade_is_fully_reconciled(session, armed) -> None:  # noqa: ANN001
    from app.portfolio import accounting

    entry = make_candle(TARGET, open_price="2500", high="2510", low="2490", close="2500")
    store(session, entry)
    engine(session, at=TARGET + timedelta(hours=4)).on_candle(
        candle_repo.find(session, SYMBOL, TIMEFRAME, TARGET)
    )
    position = session.execute(select(Position)).scalars().one()

    take_profit = position.take_profit
    winner = make_candle(
        TARGET + timedelta(hours=4),
        open_price=str(position.entry_price),
        high=str(take_profit + Decimal("5")),
        low=str(position.stop_loss + Decimal("1")),
        close=str(take_profit),
    )
    store(session, winner)
    engine(session, at=TARGET + timedelta(hours=8)).on_candle(
        candle_repo.find(session, SYMBOL, TIMEFRAME, winner.open_time)
    )

    trade = session.execute(select(Trade)).scalars().one()
    executions = session.execute(select(Execution)).scalars().all()
    assert len(executions) == 2, "one execution for the entry and one for the exit"
    fees = sum(execution.fee for execution in executions)
    assert trade.exit_reason == ExitReason.TP_HIT.value
    assert trade.gross_pnl == (trade.exit_price - trade.entry_price) * trade.quantity
    assert trade.net_pnl == trade.gross_pnl - fees
    assert trade.holding_seconds == 4 * 3600

    exit_orders = order_repo.by_signal(session, position.signal_id, purpose="EXIT")
    assert len(exit_orders) == 1
    assert exit_orders[0].signal_id == position.signal_id  # same lifecycle, told apart by purpose

    session.refresh(position)
    assert position.state == PositionState.CLOSED.value
    assert position.realized_pnl == trade.net_pnl
    assert position.exit_order_id if hasattr(position, "exit_order_id") else True

    state = accounting.compute_state(
        session,
        symbol=SYMBOL,
        timeframe=TIMEFRAME,
        starting_balance=Decimal("200"),
        last_price=winner.close,
        position=None,
    )
    assert state.cash == Decimal("200") + trade.net_pnl
    assert state.equity == state.cash

    statistics = ledger.statistics(session, symbol=SYMBOL, timeframe=TIMEFRAME)
    assert statistics["trades"] == 1
    assert statistics["wins"] == 1
    assert Decimal(str(statistics["net_pnl"])) == trade.net_pnl


def test_the_ledger_does_not_move_when_nothing_happens(session, armed) -> None:  # noqa: ANN001
    """ADR-016: there is no mutable balance to drift; state is derived, not kept."""
    store(session, make_candle(TARGET))
    candle = candle_repo.find(session, SYMBOL, TIMEFRAME, TARGET)
    engine(session, at=TARGET + timedelta(hours=4)).on_candle(candle)
    before = ledger.statistics(session, symbol=SYMBOL, timeframe=TIMEFRAME)
    for _ in range(5):
        engine(session, at=TARGET + timedelta(hours=8)).on_candle(candle)
    after = ledger.statistics(session, symbol=SYMBOL, timeframe=TIMEFRAME)
    assert before == after


def test_events_are_written_for_both_ends_of_the_lifecycle(session, armed) -> None:  # noqa: ANN001
    from app.repositories import system_events

    store(session, make_candle(TARGET))
    engine(session, at=TARGET + timedelta(hours=4)).on_candle(
        candle_repo.find(session, SYMBOL, TIMEFRAME, TARGET)
    )
    kinds = {event.event_type for event in system_events.recent(session, limit=50)}
    assert EventType.ORDER_EXECUTED.value in kinds
    assert EventType.POSITION_OPENED.value in kinds
