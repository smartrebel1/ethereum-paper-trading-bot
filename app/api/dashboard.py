"""Dashboard HTTP surface: one human page, one machine-readable summary.

``GET /dashboard``          → the Arabic panel (rendered server-side, live data)
``GET /api/v1/dashboard/summary`` → the same numbers as JSON, for the panel, for
``curl`` and for tests.

Both are strictly read-only, and both degrade honestly before any data exists.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.config.settings import get_settings
from app.dashboard.render import render_dashboard, render_empty
from app.dashboard.summary import build_summary
from app.repositories import candles as candle_repo

router = APIRouter(tags=["dashboard"])

EMPTY_MESSAGE = "لسه مفيش بيانات أسعار في قاعدة البيانات، فاللوحة فاضية."


def _summary(session: Session) -> Any:
    settings = get_settings()
    return build_summary(
        session,
        symbol=settings.symbol,
        timeframe=settings.timeframe.value,
        starting_balance=settings.starting_balance,
    )




LIVE_PAPER_STATE = Path(__file__).resolve().parents[2] / "data" / "live_paper_state.json"


@router.get("/api/v1/live-paper/status")
def live_paper_status() -> dict[str, Any]:
    """Read-only snapshot of the persistent real-time paper trader."""
    if not LIVE_PAPER_STATE.exists():
        return {
            "available": False,
            "mode": "live-paper",
            "message": "Real-time paper trader has not created its state file yet.",
        }
    try:
        payload = json.loads(LIVE_PAPER_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "available": False,
            "mode": "live-paper",
            "message": f"State file unavailable: {type(exc).__name__}: {exc}",
        }
    payload["available"] = True
    payload["mode"] = "live-paper"
    return payload

@router.get("/dashboard", include_in_schema=False, response_class=HTMLResponse)
def dashboard(session: Session = Depends(get_db)) -> HTMLResponse:
    """The panel a non-technical reader opens first."""
    settings = get_settings()
    if candle_repo.count(session, symbol=settings.symbol, timeframe=settings.timeframe.value) == 0:
        return HTMLResponse(render_empty(EMPTY_MESSAGE))
    return HTMLResponse(render_dashboard(_summary(session)))


@router.get("/api/v1/dashboard/summary")
def dashboard_summary(session: Session = Depends(get_db)) -> dict[str, Any]:
    """The same content as JSON: every number traceable to a stored row."""
    return _summary(session).as_dict()


__all__ = ["router"]
