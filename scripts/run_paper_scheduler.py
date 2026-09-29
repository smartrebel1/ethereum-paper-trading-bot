#!/usr/bin/env python
"""Run the internal continuous paper-trading scheduler.

Examples:
    python scripts/run_paper_scheduler.py
    python scripts/run_paper_scheduler.py --once
    python scripts/run_paper_scheduler.py --poll-seconds 30

The process reads public Binance market data only. It never sends an order to
Binance and does not require API credentials.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.common.safety import enforce_paper_only  # noqa: E402
from app.config.settings import get_settings  # noqa: E402
from app.scheduler.runner import DEFAULT_POLL_SECONDS, run_scheduler  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the continuous ETHUSDT paper scheduler.")
    parser.add_argument("--once", action="store_true", help="run one tick and exit")
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=DEFAULT_POLL_SECONDS,
        help="seconds between provider polls (minimum 5)",
    )
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--timeframe", default=None)
    args = parser.parse_args(argv)

    enforce_paper_only(get_settings())

    try:
        run_scheduler(
            poll_seconds=args.poll_seconds,
            once=args.once,
            symbol=args.symbol,
            timeframe=args.timeframe,
        )
    except KeyboardInterrupt:
        print("\n[scheduler] stopped by operator")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
