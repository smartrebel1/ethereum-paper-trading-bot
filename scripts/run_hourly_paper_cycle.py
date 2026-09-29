#!/usr/bin/env python
"""Run one GitHub-hosted hourly live-paper trading cycle.

This is not a WebSocket session. Each invocation fetches the latest completed
4h candles plus the current public Binance price, resumes persistent paper
state, evaluates any newly closed 4h candle, manages an existing position at
the current snapshot, and exits. State is intentionally stored in a tracked
runtime file so the next scheduled GitHub Actions run resumes from it.

No Binance credentials and no real orders are used.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "runtime" / "hourly_paper_state.json"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.common.safety import enforce_paper_only
from app.config.settings import get_settings
from app.config.strategy_config import load_frozen_strategy_config
from app.live_paper.engine import LivePaperState
from app.market_data.binance import BinanceRESTProvider


def main() -> int:
    settings = get_settings()
    enforce_paper_only(settings)

    state = LivePaperState(
        STATE_PATH,
        settings.starting_balance,
        settings.symbol,
    )
    provider = BinanceRESTProvider()
    config = load_frozen_strategy_config()

    try:
        candles = provider.fetch_candles(
            settings.symbol,
            settings.timeframe,
            include_incomplete=False,
        )
        if len(candles) < settings.warmup_candles:
            raise RuntimeError(
                f"Only {len(candles)} completed candles are available; "
                f"{settings.warmup_candles} are required."
            )

        current_price, current_price_at = provider.fetch_current_price(settings.symbol)
        latest_candle = candles[-1]
        marker = latest_candle.open_time.isoformat()

        state.status = "RUNNING"
        state.last_error = None
        state.updated_at = current_price_at.isoformat()
        state.last_price = current_price
        state.last_price_at = current_price_at.isoformat()

        if state.last_closed_4h is None:
            state.last_closed_4h = marker
            print(f"[hourly] baseline initialized at {marker}")
        elif marker != state.last_closed_4h:
            from app.strategy.ema_atr import EMAAtrStrategy

            strategy = EMAAtrStrategy(config)
            decision = strategy.evaluate(candles[-strategy.warmup_candles():])
            state.last_closed_4h = marker
            state.last_signal = {
                "candle_open_time": marker,
                "action": decision.action.value,
                "confidence": (
                    str(decision.confidence)
                    if decision.confidence is not None
                    else None
                ),
                "reason": decision.reason,
                "atr": (
                    str(decision.snapshot.atr)
                    if decision.snapshot.atr is not None
                    else None
                ),
                "evaluated_at": current_price_at.isoformat(),
                "execution_mode": "hourly_snapshot",
            }
            state.armed = None

            if (
                decision.is_entry
                and decision.confidence is not None
                and decision.snapshot.atr is not None
            ):
                allowed, reason = state.can_open(current_price_at)
                if allowed:
                    state.armed = {
                        "signal_candle_open_time": marker,
                        "confidence": str(decision.confidence),
                        "atr": str(decision.snapshot.atr),
                        "expires_at": (
                            current_price_at + timedelta(seconds=1)
                        ).isoformat(),
                    }
                    state.last_signal["entry_note"] = (
                        "Filled at hourly current-price snapshot after the "
                        "completed 4H signal."
                    )
                else:
                    state.last_signal["risk_gate"] = reason

        # Reuse the same execution/risk logic as the WebSocket engine for this
        # single current-price observation.
        from app.live_paper.engine import LivePaperEngine

        engine = LivePaperEngine(
            state_path=STATE_PATH,
            rest_provider=provider,
        )
        engine.state = state
        asyncio.run(engine._on_trade(current_price, current_price_at))
        state.status = "IDLE"
        state.updated_at = current_price_at.isoformat()
        state.save()

        print(
            f"[hourly] price={current_price} "
            f"equity={state.equity} pnl={state.net_pnl} "
            f"position={'OPEN' if state.position else 'FLAT'}"
        )
        return 0
    except Exception as exc:
        state.status = "ERROR"
        state.last_error = f"{type(exc).__name__}: {exc}"
        state.updated_at = datetime.now(tz=UTC).isoformat()
        state.save()
        raise
    finally:
        provider.close()


if __name__ == "__main__":
    raise SystemExit(main())
