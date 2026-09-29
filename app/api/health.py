"""Health / readiness / version endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from sqlalchemy import text

from app import SOFTWARE_VERSION
from app.api.schemas import HealthResponse, ReadinessCheck, ReadyResponse
from app.common.safety import enforce_paper_only
from app.common.time_utils import utcnow
from app.config.settings import get_settings
from app.config.strategy_config import load_frozen_strategy_config
from app.database.engine import get_engine

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    """Liveness: the process is up and paper-only. No DB access on purpose."""
    settings = get_settings()
    config = load_frozen_strategy_config()
    started_at = getattr(request.app.state, "started_at", utcnow())
    return HealthResponse(
        status="OK",
        mode=settings.trading_mode.value,
        paper_only=settings.is_paper,
        software_version=SOFTWARE_VERSION,
        strategy_version=config.version,
        strategy_config_hash=config.config_hash,
        symbol=settings.symbol,
        timeframe=settings.timeframe.value,
        started_at=started_at,
        timestamp=utcnow(),
    )


@router.get("/ready", response_model=ReadyResponse)
def ready() -> ReadyResponse:
    """Readiness: can this process actually do its job right now?"""
    checks: list[ReadinessCheck] = []

    # 1. safety
    try:
        report = enforce_paper_only(get_settings())
        checks.append(ReadinessCheck(name="safety_guard", ok=report.ok, detail="paper-only"))
    except Exception as exc:  # noqa: BLE001
        checks.append(ReadinessCheck(name="safety_guard", ok=False, detail=str(exc)))

    # 2. database connectivity
    db_ok = True
    detail = "SELECT 1 ok"
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        db_ok, detail = False, f"{type(exc).__name__}: {exc}"
    checks.append(ReadinessCheck(name="database", ok=db_ok, detail=detail))

    # 3. schema present
    from app.api.deps import schema_is_ready

    ready_schema = schema_is_ready(refresh=True)
    checks.append(
        ReadinessCheck(
            name="schema",
            ok=ready_schema,
            detail="16 tables present" if ready_schema else "run scripts/init_db.py",
        )
    )

    return ReadyResponse(ready=all(c.ok for c in checks), checks=checks, timestamp=utcnow())


@router.get("/version")
def version() -> dict[str, Any]:
    settings = get_settings()
    return {
        "software_version": SOFTWARE_VERSION,
        "strategy_version": settings.strategy_version,
        "strategy_name": settings.strategy_name,
        "symbol": settings.symbol,
        "timeframe": settings.timeframe.value,
        "mode": settings.trading_mode.value,
    }


__all__ = ["router"]
