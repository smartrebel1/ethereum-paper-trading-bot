"""FastAPI entrypoint.

Startup sequence (every step is observable and fails closed):

1. resolve settings (a non-paper config cannot even be constructed),
2. :func:`_startup_guard` — re-verify paper-only; on failure write to stderr and
   ``SystemExit(2)``. There is no "warn and continue" path.
3. best-effort bookkeeping: register the strategy version + configuration
   snapshot and append ``STARTUP_GUARD_PASSED`` to the event log.
   If the database has not been initialised yet, log a clear warning and keep
   serving ``/health`` (which does not touch the DB) while ``/ready`` reports
   not-ready. A monitoring system can therefore distinguish "misconfigured" (the
   process refuses to start) from "not yet initialised" (started, not ready).
"""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse

from app import SOFTWARE_VERSION
from app.api.dashboard import router as dashboard_router
from app.api.health import router as health_router
from app.api.metrics import router as metrics_router
from app.api.read_only import router as read_only_router
from app.common.errors import SafetyViolationError
from app.common.logging_setup import configure_logging, get_logger
from app.common.safety import SafetyReport, enforce_paper_only
from app.common.time_utils import utcnow
from app.config.settings import Settings, get_settings
from app.config.strategy_config import StrategyConfig, load_frozen_strategy_config
from app.database.engine import get_engine
from app.database.session import session_scope
from app.integrity.enums import EventType
from app.repositories import configuration_versions, strategy_versions, system_events

logger = get_logger("app.main")

GUARD_FAILURE_EXIT_CODE = 2

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_FILE = PROJECT_ROOT / "dashboard" / "static" / "index.html"


def _startup_guard(settings: Settings | None = None) -> SafetyReport:
    """Refuse to start unless every safety check passes.

    Raises ``SystemExit(2)`` for *any* unsafe configuration — including a config
    that pydantic itself refused to build (``TRADING_MODE=live`` never becomes a
    ``Settings`` object at all).
    """
    try:
        resolved = settings if settings is not None else get_settings()
    except Exception as exc:  # noqa: BLE001 - settings refused to construct
        sys.stderr.write(f"FATAL: invalid configuration: {exc}\n")
        raise SystemExit(GUARD_FAILURE_EXIT_CODE) from exc

    try:
        return enforce_paper_only(resolved)
    except SafetyViolationError as exc:
        sys.stderr.write(f"FATAL: {exc}\n")
        raise SystemExit(GUARD_FAILURE_EXIT_CODE) from exc


def _register_startup_records(settings: Settings, strategy: StrategyConfig) -> dict[str, Any]:
    """Write registry + audit rows. Returns a small status dict (never raises)."""
    result: dict[str, Any] = {"database": "OK", "strategy_registered": False, "config_registered": False}
    now = utcnow()
    # Events written during the same boot share one timestamp; an explicit
    # sequence makes their *order* deterministic for readers and replays.
    seq = 0
    try:
        with session_scope() as session:
            _, created = strategy_versions.ensure_registered(
                session, strategy, now=now, software_version=SOFTWARE_VERSION
            )
            result["strategy_registered"] = created

            _, config_created = configuration_versions.ensure_registered(
                session,
                config=settings.public_dict(),
                now=now,
                software_version=SOFTWARE_VERSION,
            )
            result["config_registered"] = config_created

            if created:
                seq += 1
                system_events.record_simple(
                    session,
                    event_type=EventType.STRATEGY_VERSION_REGISTERED.value,
                    timestamp=now,
                    software_version=SOFTWARE_VERSION,
                    entity_id=strategy.config_hash,
                    symbol=settings.symbol,
                    timeframe=settings.timeframe.value,
                    payload={
                        "name": strategy.name,
                        "version": strategy.version,
                        "config_hash": strategy.config_hash,
                    },
                    seq=seq,
                    strategy_version=strategy.version,
                )
            if config_created:
                seq += 1
                system_events.record_simple(
                    session,
                    event_type=EventType.CONFIGURATION_REGISTERED.value,
                    timestamp=now,
                    software_version=SOFTWARE_VERSION,
                    payload={"scope": "app"},
                    seq=seq,
                )
            seq += 1
            system_events.record_simple(
                session,
                event_type=EventType.STARTUP_GUARD_PASSED.value,
                timestamp=now,
                software_version=SOFTWARE_VERSION,
                symbol=settings.symbol,
                timeframe=settings.timeframe.value,
                payload={
                    "mode": settings.trading_mode.value,
                    "strategy_config_hash": strategy.config_hash,
                    "software_version": SOFTWARE_VERSION,
                },
                seq=seq,
                strategy_version=strategy.version,
            )
    except Exception as exc:  # noqa: BLE001 - DB may simply not be initialised yet
        result["database"] = "NOT_INITIALISED"
        result["error"] = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "startup bookkeeping skipped - database not usable yet",
            extra={"hint": "run: python scripts/init_db.py", "error": result["error"]},
        )
    return result


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    strategy: StrategyConfig = app.state.strategy_config

    report = _startup_guard(settings)  # raises SystemExit(2) if unsafe
    app.state.safety_report = report
    app.state.started_at = utcnow()

    startup_info = _register_startup_records(settings, strategy)
    app.state.startup_info = startup_info

    logger.info(
        "startup complete",
        extra={
            "mode": settings.trading_mode.value,
            "symbol": settings.symbol,
            "timeframe": settings.timeframe.value,
            "strategy_config_hash": strategy.config_hash,
            "software_version": SOFTWARE_VERSION,
            "database": startup_info["database"],
        },
    )
    yield
    logger.info("shutdown requested", extra={"timestamp": utcnow().isoformat()})


def create_app(settings: Settings | None = None) -> FastAPI:
    """Application factory (import-safe; used by tests with explicit settings)."""
    resolved = settings if settings is not None else get_settings()

    configure_logging(resolved.log_level)

    app = FastAPI(
        title="ETHUSDT Paper Trading Engine",
        version=SOFTWARE_VERSION,
        description=(
            "Paper-only trading research system with a real-time market-data paper path. "
            "There is still no live order provider, and the database refuses to store "
            "a non-paper execution."
        ),
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.state.strategy_config = load_frozen_strategy_config()
    app.state.engine = get_engine()
    app.state.started_at = utcnow()

    app.include_router(health_router)
    app.include_router(metrics_router)
    app.include_router(read_only_router)
    # Registered last: /dashboard renders the Arabic panel, and the technical
    # view stays available at /dashboard/technical for whoever wants the tables.
    app.include_router(dashboard_router)

    @app.get("/dashboard/technical", include_in_schema=False, response_class=FileResponse)
    def dashboard_technical() -> FileResponse:
        """Machine-shaped operational view, served same-origin so it can call /api/v1."""
        if not DASHBOARD_FILE.exists():  # pragma: no cover - packaging guard
            raise HTTPException(status_code=404, detail="dashboard asset is missing")
        return FileResponse(DASHBOARD_FILE, media_type="text/html")

    @app.get("/", tags=["meta"])
    def root(request: Request) -> Any:
        """Content-negotiated landing page.

        A browser (``Accept: text/html``) is redirected to the read-only
        dashboard; a tool or ``curl`` (``*/*`` / JSON) gets the machine-readable
        index. Same route, no duplicated endpoints, nothing that can mutate state.
        """
        accept = request.headers.get("accept", "")
        if "text/html" in accept:
            return RedirectResponse(url="/dashboard", status_code=307)
        return {
            "service": "ethusdt-paper-trading-engine",
            "version": SOFTWARE_VERSION,
            "mode": resolved.trading_mode.value,
            "paper_only": True,
            "notice": "PAPER ONLY - no live order path exists in this build",
            "endpoints": [
                "/health",
                "/ready",
                "/version",
                "/metrics",
                "/metrics/prometheus",
                "/api/v1/status",
                "/api/v1/config",
                "/api/v1/candles",
                "/api/v1/signals",
                "/api/v1/orders",
                "/api/v1/positions/open",
                "/api/v1/trades",
                "/api/v1/portfolio/latest",
                "/api/v1/portfolio/equity",
                "/api/v1/events",
                "/api/v1/integrity",
                "/api/v1/dashboard/summary",
                "/dashboard",
                "/dashboard/technical",
                "/docs",
            ],
        }

    return app


def _build_app_or_exit() -> FastAPI:
    """Build the ASGI app, or exit with code 2 on an unsafe configuration.

    ``uvicorn app.main:app`` imports this module: an invalid configuration must
    therefore fail *here*, at import, with a clear stderr message and the same
    exit code as the runtime guard - never boot into an unknown mode.
    """
    try:
        return create_app()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - includes pydantic ValidationError
        sys.stderr.write(f"FATAL: refusing to start with an invalid configuration: {exc}\n")
        raise SystemExit(GUARD_FAILURE_EXIT_CODE) from exc


app = _build_app_or_exit()


def main() -> None:  # pragma: no cover - process entrypoint
    import uvicorn

    settings = get_settings()
    _startup_guard(settings)
    uvicorn.run(
        "app.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=False,
        log_config=None,  # keep our JSON logging
    )


if __name__ == "__main__":  # pragma: no cover
    main()


def __getattr__(name: str) -> Any:  # pragma: no cover - typing only
    raise AttributeError(name)


_ = (Iterator, logging)  # keep imports used by type-checkers honest
