"""Metrics: JSON (for the dashboard) and Prometheus text (for scraping).

Phase 1 exposes counts from the ledger tables. Phase 9 adds the trading
metrics that matter (equity, drawdown, hit rate, fee drag) once positions and
snapshots are being written.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response
from sqlalchemy import func, select

from app.common.safety import enforce_paper_only
from app.config.settings import get_settings
from app.database.session import session_scope
from app.models import (
    AIObservation,
    Candle,
    Execution,
    IntegrityEvent,
    Order,
    PortfolioSnapshot,
    Position,
    RiskDecision,
    Signal,
    SystemEvent,
    Trade,
)

router = APIRouter(tags=["metrics"])

COUNTED_MODELS: dict[str, Any] = {
    "candles": Candle,
    "signals": Signal,
    "risk_decisions": RiskDecision,
    "orders": Order,
    "executions": Execution,
    "positions": Position,
    "trades": Trade,
    "portfolio_snapshots": PortfolioSnapshot,
    "system_events": SystemEvent,
    "integrity_events": IntegrityEvent,
    "ai_observations": AIObservation,
}


def _counts() -> dict[str, int]:
    with session_scope() as session:
        return {
            name: int(session.execute(select(func.count()).select_from(model)).scalar() or 0)
            for name, model in COUNTED_MODELS.items()
        }


def _last_open_position() -> dict[str, Any] | None:
    with session_scope() as session:
        row = (
            session.execute(
                select(Position).where(Position.state == "OPEN").order_by(Position.created_at.desc()).limit(1)
            )
            .scalars()
            .first()
        )
        if row is None:
            return None
        return {
            "position_id": row.position_id,
            "side": row.side,
            "quantity": str(row.quantity),
            "entry_price": str(row.entry_price),
            "stop_loss": str(row.stop_loss),
            "take_profit": str(row.take_profit),
            "unrealized_pnl": str(row.unrealized_pnl),
        }


def _last_trade() -> dict[str, Any] | None:
    with session_scope() as session:
        row = session.execute(select(Trade).order_by(Trade.exit_time.desc()).limit(1)).scalars().first()
        if row is None:
            return None
        return {
            "trade_id": row.trade_id,
            "exit_reason": row.exit_reason,
            "net_pnl": str(row.net_pnl),
            "exit_time": row.exit_time.isoformat(),
        }


def _last_event() -> dict[str, Any] | None:
    with session_scope() as session:
        row = (
            session.execute(
                select(SystemEvent)
                .order_by(SystemEvent.timestamp.desc(), SystemEvent.sequence.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        if row is None:
            return None
        return {
            "event_type": row.event_type,
            "timestamp": row.timestamp.isoformat(),
            "entity_id": row.entity_id,
        }


def _critical_integrity_count() -> int:
    with session_scope() as session:
        return int(
            session.execute(
                select(func.count()).select_from(IntegrityEvent).where(IntegrityEvent.severity == "CRITICAL")
            ).scalar()
            or 0
        )


@router.get("/metrics")
def metrics() -> dict[str, Any]:
    """JSON metrics (used by the dashboard)."""
    settings = get_settings()
    return {
        "counts": _counts(),
        "open_position": _last_open_position(),
        "last_trade": _last_trade(),
        "last_event": _last_event(),
        "integrity_critical_count": _critical_integrity_count(),
        "mode": settings.trading_mode.value,
        "paper_only": settings.is_paper,
    }


@router.get("/metrics/prometheus", response_class=Response)
def prometheus() -> Response:
    """Prometheus exposition format (text/plain; version=0.0.4).

    A missing external library cannot break this endpoint: the format is
    generated directly, which is more than enough for the handful of gauges
    phase 1 tracks.
    """
    settings = get_settings()
    report = enforce_paper_only(settings)
    lines: list[str] = []

    def gauge(name: str, value: float, help_text: str, labels: str = "") -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name}{labels} {value}")

    for name, value in _counts().items():
        gauge(f"trading_bot_{name}_total", float(value), f"row count of {name}")

    gauge(
        "trading_bot_integrity_critical_total",
        float(_critical_integrity_count()),
        "critical integrity violations recorded",
    )
    gauge(
        "trading_bot_paper_only",
        1.0 if report.ok else 0.0,
        "1 when every paper-only safety check passes",
    )
    position = _last_open_position()
    gauge(
        "trading_bot_open_position",
        1.0 if position else 0.0,
        "1 when a position is OPEN",
        labels=f'{{symbol="{settings.symbol}",timeframe="{settings.timeframe.value}"}}',
    )
    lines.append("")
    return Response(content="\n".join(lines), media_type="text/plain; version=0.0.4; charset=utf-8")


__all__ = ["router"]
