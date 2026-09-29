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

@router.get("/live-paper", include_in_schema=False, response_class=HTMLResponse)
def live_paper_page() -> HTMLResponse:
    """Mobile-friendly live-paper monitor; it never mutates trading state."""
    html = """
    <!doctype html><html lang="ar" dir="rtl"><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Live Paper Trading</title>
    <style>
      body{font-family:system-ui,sans-serif;background:#111;color:#eee;margin:0;padding:16px}
      .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px}
      .card{background:#1b1b1b;border:1px solid #333;border-radius:12px;padding:14px}
      .label{color:#aaa;font-size:13px}.value{font-size:22px;font-weight:700;margin-top:6px}
      pre{white-space:pre-wrap;word-break:break-word}
    </style>
    <h2>🤖 ETHUSDT — Live Paper</h2>
    <div id="status">جاري التحميل...</div>
    <script>
      async function refresh(){
        const el=document.getElementById("status");
        try{
          const r=await fetch("/api/v1/live-paper/status",{cache:"no-store"});
          const s=await r.json();
          if(!s.available){el.innerHTML="<div class='card'>"+s.message+"</div>";return}
          const p=s.position;
          const cards=[
            ["الحالة",s.status],["السعر الحالي",s.last_price||"—"],
            ["Equity",s.equity||"—"],["P&L",s.net_pnl||"—"],
            ["Unrealized",s.unrealized_pnl||"—"],
            ["الصفقة",p?"OPEN":"لا توجد"],
            ["آخر شمعة 4H",s.last_closed_4h||"—"],
            ["آخر تحديث",s.updated_at||"—"]
          ];
          el.innerHTML="<div class='grid'>"+cards.map(x=>"<div class='card'><div class='label'>"+x[0]+"</div><div class='value'>"+x[1]+"</div></div>").join("")+"</div>"+
            "<div class='card' style='margin-top:10px'><pre>"+JSON.stringify(s.last_signal||{},null,2)+"</pre></div>";
        }catch(e){el.innerHTML="<div class='card'>تعذر قراءة الحالة: "+e+"</div>"}
      }
      refresh(); setInterval(refresh,2000);
    </script></html>
    """
    return HTMLResponse(html)

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
