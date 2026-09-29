#!/usr/bin/env python
"""Run the real-time ETHUSDT paper trader.

This process connects to Binance public market-data WebSocket streams only.
It never sends orders and never needs Binance API keys.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.common.safety import enforce_paper_only  # noqa: E402
from app.config.settings import get_settings  # noqa: E402
from app.live_paper.engine import DEFAULT_WS_URL, LivePaperEngine  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run real-time ETHUSDT paper trading.")
    parser.add_argument("--state", default=None, help="persistent JSON state path")
    parser.add_argument("--ws-url", default=DEFAULT_WS_URL, help="Binance public WebSocket base URL")
    parser.add_argument("--reset", action="store_true", help="delete the paper state before starting")
    args = parser.parse_args(argv)

    settings = get_settings()
    enforce_paper_only(settings)

    state_path = Path(args.state or "data/live_paper_state.json")
    if args.reset and state_path.exists():
        state_path.unlink()

    engine = LivePaperEngine(state_path=state_path, websocket_url=args.ws_url)
    try:
        asyncio.run(engine.run_forever())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
