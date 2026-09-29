#!/usr/bin/env python3
"""Run the Gemini shadow observer against stored market candles.

Usage:
  python scripts/ai_shadow.py --check
  python scripts/ai_shadow.py --dry-run --limit 2
  python scripts/ai_shadow.py
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import desc, select  # noqa: E402
from app.config.settings import get_settings  # noqa: E402
from app.database.session import session_scope  # noqa: E402
from app.models.candle import Candle  # noqa: E402
from app.ai.gemini import GEMINI_URL, observe_latest  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Gemini market-only shadow observer")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    if settings.ai_provider != "gemini" or not settings.ai_enabled:
        print("❌ Gemini shadow is disabled. Set ENABLE_AI_SHADOW=true and AI_PROVIDER=gemini.")
        return 2
    if not settings.gemini_api_key.strip():
        print("❌ GEMINI_API_KEY is missing.")
        return 2

    if args.check:
        import httpx
        import time
        import random
        url = GEMINI_URL.format(model=settings.gemini_model)
        payload = {
            "contents": [{"parts": [{"text": "Return JSON: {\\\"ok\\\":true}"}]}],
            "generationConfig": {"temperature": 0},
        }
        for attempt in range(4):
            response = httpx.post(
                url,
                headers={"x-goog-api-key": settings.gemini_api_key},
                json=payload,
                timeout=settings.ai_timeout_seconds,
            )
            if response.is_success:
                print(f"✅ Gemini API reachable; model={settings.gemini_model}")
                return 0
            if response.status_code not in {408, 429, 500, 502, 503, 504} or attempt == 3:
                break
            delay = min(8.0, 1.0 * (2 ** attempt)) + random.uniform(0, 0.5)
            print(f"Gemini transient HTTP {response.status_code}; retrying in {delay:.1f}s ({attempt + 1}/3)")
            time.sleep(delay)
        try:
            detail = response.json().get("error", {}).get("message", "")
        except Exception:
            detail = ""
        suffix = f" - {detail}" if detail else ""
        print(f"❌ Gemini API rejected the key/request: HTTP {response.status_code}{suffix}")
        return 1

    with session_scope() as session:
        candles = list(session.execute(
            select(Candle)
            .where(Candle.symbol == settings.symbol,
                   Candle.timeframe == settings.timeframe.value,
                   Candle.is_complete.is_(True))
            .order_by(desc(Candle.open_time))
            .limit(max(8, settings.ema_period + 2, settings.atr_period + 2))
        ).scalars())[::-1]
        if not candles:
            print("❌ No completed candles found.")
            return 4

        if args.dry_run:
            context = {
                "symbol": settings.symbol,
                "timeframe": settings.timeframe.value,
                "candles_sent": min(args.limit, len(candles)),
                "latest_candle": candles[-1].open_time.isoformat(),
            }
            print("DRY RUN — nothing is sent.")
            print(json.dumps(context, ensure_ascii=False, indent=2))
            return 0

        for _ in range(max(1, args.limit)):
            observation = observe_latest(session, settings, refresh=args.refresh)
            print(
                f"{observation.at_candle_open_time.isoformat()} | "
                f"{observation.parsed_summary.get('bias', 'neutral')} | "
                f"confidence={observation.parsed_summary.get('confidence', 0):.2f} | "
                f"{observation.parsed_summary.get('summary_ar', observation.error or 'no result')}"
            )
            if observation.error:
                break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
