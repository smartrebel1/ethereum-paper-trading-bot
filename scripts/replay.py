#!/usr/bin/env python
"""Deterministic replay of the frozen baseline over the archived series.

    python scripts/replay.py                          # replay everything already in the DB
    python scripts/replay.py --limit 2000             # last 2000 candles only
    python scripts/replay.py --rebuild-db             # wipe, migrate, ingest, replay (one command)
    python scripts/replay.py --report data/replay_report.json

The replay is a pure function of its inputs: candles come from the archive, time
comes from the candles (``FrozenClock``), and the strategy config hash is
stamped on every signal, order and position. Running it twice on the same series
produces byte-identical rows.

Exit codes
    0  replay completed over the whole series
    3  replay halted on a missed execution target (an integrity failure)
    4  no candles available (ingest the archive first)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import SOFTWARE_VERSION  # noqa: E402
from app.config.settings import get_settings  # noqa: E402
from app.config.strategy_config import load_frozen_strategy_config  # noqa: E402
from app.database.session import session_scope  # noqa: E402
from app.ledger import ledger  # noqa: E402
from app.portfolio import accounting  # noqa: E402
from app.replay_engine.engine import ReplayEngine, load_series  # noqa: E402
from app.repositories import candles as candle_repo  # noqa: E402
from app.repositories import orders as order_repo  # noqa: E402
from app.repositories import positions as position_repo  # noqa: E402
from app.repositories import signals as signal_repo  # noqa: E402


def _rebuild_db(project_root: Path) -> int:
    """Delete the SQLite file, migrate it, then ingest the archive."""
    db_path = Path(get_settings().database_url.removeprefix("sqlite:///"))
    if not db_path.is_absolute():
        db_path = project_root / db_path
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(db_path) + suffix)
        if candidate.exists():
            candidate.unlink()
            print(f"[rebuild] removed {candidate}")
    for argv, label in (
        ([sys.executable, "-m", "alembic", "upgrade", "head"], "migrate"),
        ([sys.executable, str(project_root / "scripts" / "ingest_archive.py")], "ingest"),
    ):
        result = subprocess.run(argv, cwd=project_root, check=False)
        if result.returncode != 0:
            print(f"[rebuild] {label} failed with exit code {result.returncode}")
            return result.returncode
    return 0


def _trade_rows(session, *, symbol: str, timeframe: str, limit: int = 10) -> list[dict]:
    """The most recent closed trades, as plain numbers (dashboard-friendly)."""
    from sqlalchemy import select

    from app.models.trade import Trade

    stmt = (
        select(Trade)
        .where(Trade.symbol == symbol, Trade.timeframe == timeframe)
        .order_by(Trade.exit_time.desc())
        .limit(limit)
    )
    rows = []
    for trade in session.execute(stmt).scalars().all():
        rows.append(
            {
                "trade_id": trade.trade_id,
                "side": trade.side,
                "quantity": str(trade.quantity),
                "entry_price": str(trade.entry_price),
                "exit_price": str(trade.exit_price),
                "entry_time": trade.entry_time.isoformat(),
                "exit_time": trade.exit_time.isoformat(),
                "gross_pnl": str(trade.gross_pnl),
                "net_pnl": str(trade.net_pnl),
                "fees": str(trade.entry_fee + trade.exit_fee),
                "exit_reason": trade.exit_reason,
                "holding_hours": round(trade.holding_seconds / 3600, 2),
            }
        )
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay the archive through the paper engine.")
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--timeframe", default=None)
    parser.add_argument("--limit", type=int, default=None, help="replay only the last N candles")
    parser.add_argument("--rebuild-db", action="store_true", help="wipe + migrate + ingest first")
    parser.add_argument("--on-integrity", choices=("halt", "continue"), default="halt")
    parser.add_argument("--report", default=None, help="write a JSON report to this path")
    parser.add_argument("--trades", type=int, default=10, help="how many recent trades to include")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    settings = get_settings()
    symbol = args.symbol or settings.symbol
    timeframe = args.timeframe or settings.timeframe

    if args.rebuild_db:
        code = _rebuild_db(PROJECT_ROOT)
        if code:
            return code

    config = load_frozen_strategy_config()
    with session_scope() as session:
        series = load_series(session, symbol=symbol, timeframe=timeframe)
        if args.limit:
            series = series[-args.limit :]
        if not series:
            print("[replay] no candles in the database - run scripts/ingest_archive.py first")
            return 4

        if not args.quiet:
            print(
                f"[replay] series: {len(series)} candles "
                f"{series[0].open_time.isoformat()} -> {series[-1].open_time.isoformat()}"
            )

        engine = ReplayEngine(
            config=config,
            symbol=symbol,
            timeframe=timeframe,
            software_version=SOFTWARE_VERSION,
            on_integrity=args.on_integrity,
            timeline_limit=0,
        )
        started = time.perf_counter()
        result = engine.run(series, session=session)
        elapsed = time.perf_counter() - started

        statistics = ledger.statistics(session, symbol=symbol, timeframe=timeframe)
        state = accounting.compute_state(
            session,
            symbol=symbol,
            timeframe=timeframe,
            starting_balance=result.starting_equity,
            last_price=series[-1].close,
            position=position_repo.open_position(session, symbol, timeframe),
        )
        payload = {
            "software_version": SOFTWARE_VERSION,
            "strategy_config_hash": config.config_hash,
            "symbol": symbol,
            "timeframe": timeframe,
            "elapsed_seconds": round(elapsed, 2),
            "replay": result.as_dict(timeline_limit=0),
            "statistics": {
                key: (value if isinstance(value, dict) else str(value)) for key, value in statistics.items()
            },
            "portfolio": {
                "starting_balance": str(state.starting_balance),
                "cash": str(state.cash),
                "reserved": str(state.reserved),
                "equity": str(state.equity),
                "realized_pnl": str(state.realized_pnl_cum),
                "unrealized_pnl": str(state.unrealized_pnl),
                "fees_paid": str(state.fees_cum),
                "peak_equity": str(state.peak_equity),
                "drawdown_pct": str(state.drawdown_pct),
            },
            "counts": {
                "candles": candle_repo.count(session, symbol=symbol, timeframe=timeframe),
                "signals": signal_repo.count(session, symbol=symbol, timeframe=timeframe),
                "orders": order_repo.count(session, symbol=symbol, timeframe=timeframe),
            },
            "recent_trades": _trade_rows(session, symbol=symbol, timeframe=timeframe, limit=args.trades),
        }
        if args.report:
            report_path = Path(args.report)
            if not report_path.is_absolute():
                report_path = PROJECT_ROOT / report_path
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
            print(f"[replay] report written to {report_path}")

        if args.quiet:
            return 3 if result.halted else 0

        print(f"[replay] processed={result.processed} in {elapsed:.1f}s")
        print(f"[replay] signals={result.signals} approved={result.approved} rejected={result.rejected}")
        print(
            f"[replay] fills={result.fills} closed_positions={result.trades} "
            f"voided_targets={result.voided_targets} missed_targets={result.missed_targets}"
        )
        print(f"[replay] equity {result.starting_equity} -> {result.ending_equity}")
        print(f"[replay] next expected candle: {result.next_expected_open_time}")
        print(f"[replay] suppressed: {result.suppressed}")
        print(f"[replay] rejections: {result.rejections}")
        print("[replay] ledger statistics:")
        for key, value in statistics.items():
            print(f"    {key:>18}: {value}")
        if result.halted:
            print(f"[replay] HALTED: {result.halt_reason}")
            return 3
        print("[replay] completed over the whole series")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
