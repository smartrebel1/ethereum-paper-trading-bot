"""Read-only HTTP surface for the dashboard.

Everything under ``/api/v1`` is a read. There is deliberately no write endpoint
in phase 1 (and there may never be a *trading* write endpoint: the engine is
driven by candle arrival, not by HTTP). This keeps the API surface auditable:
if it is mounted, it cannot change state.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app import SOFTWARE_VERSION
from app.api.deps import get_db, require_schema
from app.api.schemas import (
    CandleOut,
    CandlePage,
    ConfigOut,
    EventPage,
    IntegrityEventOut,
    IntegrityPage,
    OrderOut,
    OrderPage,
    PageMeta,
    PortfolioSnapshotOut,
    PositionOut,
    SignalOut,
    SignalPage,
    StatusOut,
    SystemEventOut,
    TradeOut,
    TradePage,
)
from app.common.safety import enforce_paper_only
from app.common.time_utils import utcnow
from app.config.settings import get_settings
from app.config.strategy_config import load_frozen_strategy_config
from app.models import (
    Candle,
    Execution,
    IntegrityEvent,
    Order,
    PortfolioSnapshot,
    Position,
    Signal,
    SystemEvent,
    Trade,
)

router = APIRouter(prefix="/api/v1", tags=["read-only"], dependencies=[Depends(require_schema)])

LimitQuery = Query(default=100, ge=1, le=1000)
OffsetQuery = Query(default=0, ge=0)


def _meta(items: list[Any], limit: int, offset: int) -> PageMeta:
    return PageMeta(limit=limit, offset=offset, returned=len(items))


@router.get("/config", response_model=ConfigOut)
def config(session: Session = Depends(get_db)) -> ConfigOut:
    """Frozen strategy config + hash + coverage facts. Fully reproducible."""
    settings = get_settings()
    strategy = load_frozen_strategy_config()
    first = session.execute(select(func.min(Candle.open_time))).scalar()
    last = session.execute(select(func.max(Candle.open_time))).scalar()
    candles = int(session.execute(select(func.count()).select_from(Candle)).scalar() or 0)
    complete = int(
        session.execute(select(func.count()).select_from(Candle).where(Candle.is_complete.is_(True))).scalar()
        or 0
    )
    return ConfigOut(
        software_version=SOFTWARE_VERSION,
        strategy=strategy.to_json_dict(),
        strategy_config_hash=strategy.config_hash,
        coverage={
            "candles": candles,
            "complete_candles": complete,
            "first_open_time": first.isoformat() if isinstance(first, datetime) else None,
            "last_open_time": last.isoformat() if isinstance(last, datetime) else None,
        },
        settings=settings.public_dict(),
        safety=enforce_paper_only(settings).to_dict(),
    )


@router.get("/status", response_model=StatusOut)
def status(session: Session = Depends(get_db)) -> StatusOut:
    settings = get_settings()
    last_candle = session.execute(select(Candle).order_by(desc(Candle.open_time)).limit(1)).scalars().first()
    last_event = (
        session.execute(
            select(SystemEvent).order_by(desc(SystemEvent.timestamp), desc(SystemEvent.sequence)).limit(1)
        )
        .scalars()
        .first()
    )
    open_position = (
        session.execute(select(Position).where(Position.state == "OPEN").limit(1)).scalars().first()
    )
    pending_orders = int(
        session.execute(select(func.count()).select_from(Order).where(Order.state == "PENDING")).scalar() or 0
    )
    counts = {
        "candles": int(session.execute(select(func.count()).select_from(Candle)).scalar() or 0),
        "signals": int(session.execute(select(func.count()).select_from(Signal)).scalar() or 0),
        "orders": int(session.execute(select(func.count()).select_from(Order)).scalar() or 0),
        "executions": int(session.execute(select(func.count()).select_from(Execution)).scalar() or 0),
        "trades": int(session.execute(select(func.count()).select_from(Trade)).scalar() or 0),
    }
    criticals = int(
        session.execute(
            select(func.count()).select_from(IntegrityEvent).where(IntegrityEvent.severity == "CRITICAL")
        ).scalar()
        or 0
    )
    return StatusOut(
        symbol=settings.symbol,
        timeframe=settings.timeframe.value,
        mode=settings.trading_mode.value,
        paper_only=settings.is_paper,
        data={
            "last_candle_open_time": last_candle.open_time.isoformat() if last_candle else None,
            "last_candle_complete": bool(last_candle.is_complete) if last_candle else None,
            "last_source": last_candle.source if last_candle else None,
        },
        pipeline={
            "open_position_id": open_position.position_id if open_position else None,
            "pending_orders": pending_orders,
            "last_event_type": last_event.event_type if last_event else None,
            "last_event_at": last_event.timestamp.isoformat() if last_event else None,
        },
        counts=counts,
        integrity_critical_count=criticals,
        generated_at=utcnow(),
    )


@router.get("/candles", response_model=CandlePage)
def candles(
    limit: int = LimitQuery,
    offset: int = OffsetQuery,
    only_complete: bool = Query(default=True),
    session: Session = Depends(get_db),
) -> CandlePage:
    stmt = select(Candle).order_by(desc(Candle.open_time)).limit(limit).offset(offset)
    if only_complete:
        stmt = stmt.where(Candle.is_complete.is_(True))
    rows = list(session.execute(stmt).scalars().all())
    return CandlePage(meta=_meta(rows, limit, offset), items=[CandleOut.model_validate(r) for r in rows])


@router.get("/signals", response_model=SignalPage)
def signals(
    limit: int = LimitQuery,
    offset: int = OffsetQuery,
    session: Session = Depends(get_db),
) -> SignalPage:
    stmt = select(Signal).order_by(desc(Signal.created_at)).limit(limit).offset(offset)
    rows = list(session.execute(stmt).scalars().all())
    return SignalPage(meta=_meta(rows, limit, offset), items=[SignalOut.model_validate(r) for r in rows])


@router.get("/orders", response_model=OrderPage)
def orders(
    limit: int = LimitQuery,
    offset: int = OffsetQuery,
    state: str | None = Query(default=None),
    session: Session = Depends(get_db),
) -> OrderPage:
    stmt = select(Order).order_by(desc(Order.created_at)).limit(limit).offset(offset)
    if state:
        stmt = stmt.where(Order.state == state.upper())
    rows = list(session.execute(stmt).scalars().all())
    return OrderPage(meta=_meta(rows, limit, offset), items=[OrderOut.model_validate(r) for r in rows])


@router.get("/positions/open", response_model=list[PositionOut])
def open_positions(session: Session = Depends(get_db)) -> list[PositionOut]:
    stmt = select(Position).where(Position.state == "OPEN").order_by(desc(Position.created_at))
    rows = list(session.execute(stmt).scalars().all())
    return [PositionOut.model_validate(r) for r in rows]


@router.get("/trades", response_model=TradePage)
def trades(
    limit: int = LimitQuery,
    offset: int = OffsetQuery,
    session: Session = Depends(get_db),
) -> TradePage:
    stmt = select(Trade).order_by(desc(Trade.exit_time)).limit(limit).offset(offset)
    rows = list(session.execute(stmt).scalars().all())
    return TradePage(meta=_meta(rows, limit, offset), items=[TradeOut.model_validate(r) for r in rows])


@router.get("/portfolio/latest", response_model=PortfolioSnapshotOut | None)
def portfolio_latest(session: Session = Depends(get_db)) -> PortfolioSnapshotOut | None:
    row = (
        session.execute(
            select(PortfolioSnapshot).order_by(desc(PortfolioSnapshot.at_candle_open_time)).limit(1)
        )
        .scalars()
        .first()
    )
    return PortfolioSnapshotOut.model_validate(row) if row else None


@router.get("/portfolio/equity", response_model=list[PortfolioSnapshotOut])
def portfolio_equity(
    limit: int = Query(default=500, ge=1, le=5000),
    session: Session = Depends(get_db),
) -> list[PortfolioSnapshotOut]:
    stmt = select(PortfolioSnapshot).order_by(desc(PortfolioSnapshot.at_candle_open_time)).limit(limit)
    rows = list(session.execute(stmt).scalars().all())
    return [PortfolioSnapshotOut.model_validate(r) for r in rows]


@router.get("/events", response_model=EventPage)
def events(
    limit: int = LimitQuery,
    offset: int = OffsetQuery,
    event_type: str | None = Query(default=None),
    session: Session = Depends(get_db),
) -> EventPage:
    stmt = select(SystemEvent).order_by(desc(SystemEvent.timestamp)).limit(limit).offset(offset)
    if event_type:
        stmt = stmt.where(SystemEvent.event_type == event_type.upper())
    rows = list(session.execute(stmt).scalars().all())
    return EventPage(meta=_meta(rows, limit, offset), items=[SystemEventOut.model_validate(r) for r in rows])


@router.get("/integrity", response_model=IntegrityPage)
def integrity(
    limit: int = LimitQuery,
    offset: int = OffsetQuery,
    code: str | None = Query(default=None),
    session: Session = Depends(get_db),
) -> IntegrityPage:
    stmt = select(IntegrityEvent).order_by(desc(IntegrityEvent.detected_at)).limit(limit).offset(offset)
    if code:
        stmt = stmt.where(IntegrityEvent.code == code.upper())
    rows = list(session.execute(stmt).scalars().all())
    return IntegrityPage(
        meta=_meta(rows, limit, offset), items=[IntegrityEventOut.model_validate(r) for r in rows]
    )


__all__ = ["router"]
