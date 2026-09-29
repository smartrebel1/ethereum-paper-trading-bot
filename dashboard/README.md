# Dashboards

Two read-only views over the same database, both with no build step and no CDN.

## 1. `/dashboard` — the Arabic panel (plain language)

For the person who owns the money, not the code. It answers exactly four
questions, from real rows, in Egyptian Arabic, with Cairo times:

| Question | Where the answer comes from |
|---|---|
| **شترينا إيه؟** | last closed trade (or the open position): quantity, entry/exit price |
| **الصفقة اتنفذت امتى؟** | the fill moment = the open of the target candle, in Cairo time |
| **ربحنا كام؟** | `trades.net_pnl` summed, plus balance, fees, win/loss counts |
| **الصفقة الجاية امتى؟** | open position → after it closes; pending order → its target time; otherwise the next candle |

It also shows the engine's own reason for not trading (`REASON_MEANINGS` translates
every code), the data coverage including Binance's own holes, and a plain-language
description of the strategy rules.

* Live: `GET /dashboard` (rendered from the database on every request).
* Machine copy: `GET /api/v1/dashboard/summary`.
* Offline copy: `python scripts/render_dashboard.py` → `dashboard/dashboard_ar.html`
  (self-contained: no external CSS, fonts, images or scripts).

## 2. `/dashboard/technical` — the operational view

The single static file `dashboard/static/index.html`, for whoever wants counts,
the audit trail and the integrity panel. Served at **`/dashboard/technical`**.

```bash
python scripts/init_db.py
python scripts/ingest_archive.py
python scripts/replay.py
uvicorn app.main:app --host 127.0.0.1 --port 8000
# then open http://127.0.0.1:8000/dashboard
```

## What it shows

| Panel | Source | Notes |
|---|---|---|
| Mode / config hash badges | `/api/v1/config` | "PAPER ONLY" badge is hard-coded: it is not a claim, it is a build property |
| Counts (candles, signals, orders, executions, trades) | `/api/v1/status` | zero until phases 2-6 land |
| Pipeline (last candle, pending orders, last event) | `/api/v1/status` | "last event" is ordered by `(timestamp, sequence)` |
| Data coverage | `/api/v1/config` | first/last candle open time |
| Equity curve | `/api/v1/portfolio/equity` | inline SVG sparkline, no chart library |
| Open position | `/api/v1/positions/open` | at most one row, by database constraint |
| Recent trades | `/api/v1/trades` | immutable ledger rows |
| System events / integrity violations | `/api/v1/events`, `/api/v1/integrity` | the audit trail, and the "must stay empty" panel |

Everything degrades gracefully: with an empty database every panel renders an
explicit empty state, and if the API is unreachable the page shows exactly how
to start it instead of a blank screen.

## Why not Streamlit/Next.js yet

The phase-9 dashboard (interactive research UI, walk-forward comparison, AI
shadow panel) will replace this. Shipping the phase-9 stack now would add a
runtime (Node/Streamlit) and a build pipeline to a phase whose deliverable is a
validated schema and a paper-only guarantee.

This file has one dependency: a browser. It is also the reference implementation
of "what the read-only API can answer", which is why it doubles as the phase-1
smoke test for the HTTP layer.
