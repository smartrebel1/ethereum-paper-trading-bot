#!/usr/bin/env python
"""Write the Arabic dashboard to a standalone HTML file.

    python scripts/render_dashboard.py                 # dashboard/dashboard_ar.html
    python scripts/render_dashboard.py --out /tmp/x.html

The page the API serves is rendered from the live database on every request;
this script produces the same document as a *file*, so it can be opened,
mailed, or previewed without running the server. It contains no external
resources and no network calls, so it renders identically anywhere.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.api.dashboard import EMPTY_MESSAGE  # noqa: E402
from app.config.settings import get_settings  # noqa: E402
from app.dashboard.render import render_dashboard, render_empty  # noqa: E402
from app.dashboard.summary import build_summary  # noqa: E402
from app.database.session import session_scope  # noqa: E402
from app.repositories import candles as candle_repo  # noqa: E402

DEFAULT_OUT = PROJECT_ROOT / "dashboard" / "dashboard_ar.html"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render the Arabic dashboard to a file.")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--timeframe", default=None)
    args = parser.parse_args(argv)

    settings = get_settings()
    symbol = args.symbol or settings.symbol
    timeframe = args.timeframe or settings.timeframe.value

    with session_scope() as session:
        if candle_repo.count(session, symbol=symbol, timeframe=timeframe) == 0:
            html = render_empty(EMPTY_MESSAGE)
        else:
            summary = build_summary(
                session,
                symbol=symbol,
                timeframe=timeframe,
                starting_balance=settings.starting_balance,
            )
            html = render_dashboard(summary)

    path = Path(args.out)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    print(f"[dashboard] written to {path} ({len(html):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
