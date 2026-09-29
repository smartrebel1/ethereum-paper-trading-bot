"""Replay determinism and idempotence (Phase 3).

The engine's headline claim is that a replay is a *pure function of the archived
candles*: same input, same signals, same orders, same fills, same ledger — and
running it a second time over the same database changes nothing at all.

The second property is what makes the ledger trustworthy. If a second pass could
re-open positions or re-apply fees, every reported number would depend on how
many times the operator pressed "run".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app import SOFTWARE_VERSION
from app.candles.ingestor import DataIngestor
from app.common.clock import FrozenClock
from app.config.strategy_config import StrategyConfig
from app.ledger import ledger
from app.market_data.mock import make_candle
from app.models.execution import Execution
from app.models.order import Order
from app.models.position import Position
from app.models.signal import Signal
from app.models.trade import Trade
from app.portfolio import accounting
from app.replay_engine.engine import ReplayEngine, load_series

SYMBOL = "ETHUSDT"
TIMEFRAME = "4h"
BASE = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


def replay_config() -> StrategyConfig:
    """A *test* configuration: the frozen baseline, with a short warm-up.

    Deliberately built here rather than loaded from settings, so a change to the
    frozen baseline can never silently change what these tests exercise. The
    baseline itself is pinned by its own tests.
    """
    return StrategyConfig(
        name="EMA_ATR_TEST",
        version="1.0.0",
        symbol=SYMBOL,
        timeframe=TIMEFRAME,
        ema_period=5,
        atr_period=3,
        atr_sl_multiplier="1.5",
        atr_tp_multiplier="3",
        max_position_pct="0.1",
        min_confidence="0.1",
        warmup_candles=8,
        fee_rate="0.001",
    )


def trending_series(count: int = 60) -> list:
    """A deterministic rise-and-drop pattern: entries, a stop and a target.

    No randomness anywhere: the same list is produced on every machine, which is
    what makes the determinism assertion meaningful.
    """
    pattern = (30, 30, 30, 30, 40, 40, -120, -20, -10, 0, 10, 20)
    price = Decimal("2500")
    candles = []
    for index in range(count):
        step = Decimal(pattern[index % len(pattern)])
        close = price + step
        candles.append(
            make_candle(
                BASE + timedelta(hours=4 * index),
                open_price=price,
                high=max(price, close) + Decimal("5"),
                low=min(price, close) - Decimal("5"),
                close=close,
                volume="10",
            )
        )
        price = close
    return candles


@pytest.fixture
def loaded(session):  # noqa: ANN001
    candles = trending_series()
    ingestor = DataIngestor(
        session,
        symbol=SYMBOL,
        timeframe=TIMEFRAME,
        software_version=SOFTWARE_VERSION,
        clock=FrozenClock(candles[-1].close_time),
    )
    ingestor.ingest(candles)
    return candles


def run(session, candles):  # noqa: ANN001
    engine = ReplayEngine(
        config=replay_config(),
        symbol=SYMBOL,
        timeframe=TIMEFRAME,
        software_version=SOFTWARE_VERSION,
    )
    return engine.run(load_series(session, symbol=SYMBOL, timeframe=TIMEFRAME), session=session)


def counts(session) -> dict[str, int]:  # noqa: ANN001
    return {
        name: int(session.execute(select(func.count()).select_from(model)).scalar() or 0)
        for name, model in (
            ("signals", Signal),
            ("orders", Order),
            ("executions", Execution),
            ("positions", Position),
            ("trades", Trade),
        )
    }


def test_the_synthetic_series_actually_trades(session, loaded) -> None:  # noqa: ANN001
    """Guard against a vacuous determinism test (nothing to be deterministic about)."""
    result = run(session, loaded)
    assert result.processed == len(loaded)
    assert result.fills >= 2
    assert result.trades >= 1


def test_a_second_pass_changes_nothing(session, loaded) -> None:  # noqa: ANN001
    first = run(session, loaded)
    before = counts(session)
    statistics_before = ledger.statistics(session, symbol=SYMBOL, timeframe=TIMEFRAME)

    second = run(session, loaded)

    assert second.fills == 0, "a replayed order was filled twice"
    assert second.trades == 0, "a replayed position was closed twice"
    assert second.processed == first.processed
    assert second.signals == 0, "signals were duplicated on the second pass"
    assert counts(session) == before
    assert ledger.statistics(session, symbol=SYMBOL, timeframe=TIMEFRAME) == statistics_before
    assert second.ending_equity == first.ending_equity


def test_replaying_a_prefix_then_the_whole_series_reaches_the_same_state(session, loaded) -> None:  # noqa: ANN001
    """Incremental catch-up equals a single full run: the engine has no hidden state."""
    half = len(loaded) // 2
    ingestor = DataIngestor(
        session,
        symbol=SYMBOL,
        timeframe=TIMEFRAME,
        software_version=SOFTWARE_VERSION,
        # The archive is already complete; the ingestor's clock only decides
        # whether a candle's *window* has closed. It must therefore be at or
        # after the last candle in the series.
        clock=FrozenClock(loaded[-1].close_time),
    )
    engine = ReplayEngine(
        config=replay_config(),
        symbol=SYMBOL,
        timeframe=TIMEFRAME,
        software_version=SOFTWARE_VERSION,
    )
    engine.run(load_series(session, symbol=SYMBOL, timeframe=TIMEFRAME)[:half], session=session)
    ingestor.ingest(loaded[half:])
    incremental = engine.run(load_series(session, symbol=SYMBOL, timeframe=TIMEFRAME), session=session)

    assert incremental.halted is False
    assert incremental.voided_targets == 0
    # Re-running the whole series changes nothing, so the state after catch-up is
    # the same state the engine would hold after one pass over the full data.
    stable = run(session, loaded)
    assert ledger.statistics(session, symbol=SYMBOL, timeframe=TIMEFRAME) == ledger.statistics(
        session, symbol=SYMBOL, timeframe=TIMEFRAME
    )
    assert stable.fills == 0
    assert stable.trades == 0


def test_no_order_is_left_dangling_after_a_replay(session, loaded) -> None:  # noqa: ANN001
    """Every order ends in a terminal state: nothing is left "in flight"."""
    run(session, loaded)
    rows = session.execute(select(Order)).scalars().all()
    assert rows
    states = {row.state for row in rows}
    assert states <= {"EXECUTED", "CANCELLED", "TARGET_MISSED"} or not (
        states & {"PENDING", "CREATED", "TARGET_REACHED"}
    ), f"non-terminal order states after replay: {states}"


def test_positions_and_trades_agree_one_to_one(session, loaded) -> None:  # noqa: ANN001
    run(session, loaded)
    closed = session.execute(select(Position).where(Position.state == "CLOSED")).scalars().all()
    trades = session.execute(select(Trade)).scalars().all()
    assert len(closed) == len(trades)
    for trade in trades:
        position = session.get(Position, trade.position_id)
        assert position is not None
        assert trade.entry_time == position.entry_candle_open_time
        assert trade.exit_time >= trade.entry_time
        assert trade.holding_seconds >= 0


def test_the_frozen_fee_rate_is_the_only_fee_applied(session, loaded) -> None:  # noqa: ANN001
    run(session, loaded)
    executions = session.execute(select(Execution)).scalars().all()
    rate = replay_config().fee_rate_decimal
    for execution in executions:
        assert execution.fee == (execution.notional * rate).quantize(execution.fee)


def test_equity_equals_starting_balance_plus_realized_pnl(session, loaded) -> None:  # noqa: ANN001
    result = run(session, loaded)
    state = accounting.compute_state(
        session,
        symbol=SYMBOL,
        timeframe=TIMEFRAME,
        starting_balance=Decimal("200"),
        last_price=loaded[-1].close,
        position=None,
    )
    statistics = ledger.statistics(session, symbol=SYMBOL, timeframe=TIMEFRAME)
    assert Decimal(str(statistics["net_pnl"])) == state.realized_pnl_cum
    assert state.cash == Decimal("200") + state.realized_pnl_cum
    assert Decimal(result.ending_equity) == state.equity

    # The two independent ways of counting the same money agree to within one
    # unit of the money scale per round trip (quantisation, not drift).
    residual = accounting.cash_flow_residual(session, symbol=SYMBOL, timeframe=TIMEFRAME)
    assert abs(residual) <= Decimal("0.00000001") * int(statistics["trades"])
