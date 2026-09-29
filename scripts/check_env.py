#!/usr/bin/env python
"""Print the effective configuration and run the paper-only safety checks.

Use before starting the engine (and before any replay/backtest run):

    python scripts/check_env.py

Exit code is non-zero when any safety check fails.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import SOFTWARE_VERSION  # noqa: E402
from app.common.safety import enforce_paper_only  # noqa: E402
from app.config.settings import get_settings  # noqa: E402
from app.config.strategy_config import load_frozen_strategy_config  # noqa: E402


def main() -> int:
    settings = get_settings()
    strategy = load_frozen_strategy_config()

    print("=" * 68)
    print(f"ETHUSDT Paper Trading Engine  (software {SOFTWARE_VERSION})")
    print("=" * 68)
    print(f"Trading mode        : {settings.trading_mode.value}")
    print(f"Live trading flag   : {settings.enable_live_trading}  (must be False)")
    print(f"Symbol / timeframe  : {settings.symbol} / {settings.timeframe.value}")
    print(f"Strategy            : {strategy.name} v{strategy.version}")
    print(f"Strategy hash       : {strategy.config_hash}")
    print("-" * 68)
    print("Strategy parameters")
    for key, value in strategy.as_dict().items():
        print(f"  {key:<20}: {value}")
    print("-" * 68)
    print(f"Starting balance    : {settings.starting_balance}")
    print(f"Fee rate            : {settings.fee_rate}")
    print(f"AI shadow           : {settings.enable_ai_shadow} (provider={settings.ai_provider})")
    print(f"Database URL        : {settings.database_url}")
    print(f"API bind            : {settings.api_host}:{settings.api_port}")
    print(f"Log level           : {settings.log_level}")
    print("-" * 68)

    report = enforce_paper_only(settings)
    for check in report.to_dict()["checks"]:
        mark = "PASS" if check["passed"] else "FAIL"
        print(f"[{mark}] {check['name']:<34} {check['detail']}")
    print("=" * 68)
    print("ALL SAFETY CHECKS PASSED - paper only" if report.ok else "SAFETY CHECKS FAILED")
    return 0 if report.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
