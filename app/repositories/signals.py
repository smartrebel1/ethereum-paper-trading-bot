"""Signal persistence — append-only.

A signal that already exists is returned unchanged: re-evaluating the same
closed candle is idempotent (the deterministic ``signal_id`` makes it so), which
is what allows the replay to be re-run without duplicating decisions.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.signal import Signal
from app.strategy.ema_atr import StrategySignal


def persist(session: Session, signal: StrategySignal, *, now: datetime) -> tuple[Signal, bool]:
    """Insert the signal if unseen. Returns ``(row, created)``."""
    existing = session.get(Signal, signal.signal_id)
    if existing is not None:
        return existing, False
    row = Signal(
        signal_id=signal.signal_id,
        symbol=signal.symbol,
        timeframe=signal.timeframe,
        action=signal.action.value,
        confidence=signal.confidence,
        signal_candle_open_time=signal.signal_candle_open_time,
        signal_candle_close_time=signal.signal_candle_close_time,
        target_execution_open_time=signal.target_execution_open_time,
        target_execution_candle_id=signal.target_execution_candle_id,
        strategy_name=signal.strategy_name,
        strategy_version=signal.strategy_version,
        strategy_config_hash=signal.strategy_config_hash,
        decision_version="1",
        indicator_context=dict(signal.indicator_context),
        reason=signal.reason[:128],
        generation_timestamp=now,
        created_at=now,
    )
    session.add(row)
    session.flush()
    return row, True


def by_target(session: Session, symbol: str, timeframe: str, target_open_time: datetime) -> list[Signal]:
    stmt = select(Signal).where(
        Signal.symbol == symbol,
        Signal.timeframe == timeframe,
        Signal.target_execution_open_time == target_open_time,
    )
    return list(session.execute(stmt).scalars().all())


def latest(session: Session, symbol: str, timeframe: str) -> Signal | None:
    stmt = (
        select(Signal)
        .where(Signal.symbol == symbol, Signal.timeframe == timeframe)
        .order_by(Signal.signal_candle_open_time.desc())
        .limit(1)
    )
    return session.execute(stmt).scalars().first()


def count(session: Session, *, symbol: str | None = None, timeframe: str | None = None) -> int:
    stmt = select(func.count()).select_from(Signal)
    if symbol:
        stmt = stmt.where(Signal.symbol == symbol)
    if timeframe:
        stmt = stmt.where(Signal.timeframe == timeframe)
    return int(session.execute(stmt).scalar() or 0)


__all__ = ["by_target", "count", "latest", "persist"]
