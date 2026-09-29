#!/usr/bin/env python
"""Build the human-readable GitHub BOT_STATUS.md snapshot.

This job is deliberately stateless: every run creates a fresh small SQLite
database from recent completed Binance candles, replays the paper strategy,
and publishes only the resulting human-readable status page.

No Binance credentials are used and no orders are ever sent.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "status.db"
STATUS_PATH = ROOT / "BOT_STATUS.md"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["TRADING_MODE"] = "paper"
os.environ["ENABLE_LIVE_TRADING"] = "false"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.candles.ingestor import DataIngestor  # noqa: E402
from app.common.safety import enforce_paper_only  # noqa: E402
from app.config.settings import get_settings  # noqa: E402
from app.config.strategy_config import load_frozen_strategy_config  # noqa: E402
from app.dashboard.summary import build_summary  # noqa: E402
from app.database.session import session_scope  # noqa: E402
from app.market_data.binance import BinanceRESTProvider  # noqa: E402
from app.replay_engine.engine import ReplayEngine  # noqa: E402


def reset_database() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(DB_PATH) + suffix)
        if path.exists():
            path.unlink()
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        check=True,
    )


def fetch_and_ingest() -> None:
    settings = get_settings()
    provider = BinanceRESTProvider()
    try:
        candles = provider.fetch_candles(
            settings.symbol,
            settings.timeframe,
            include_incomplete=False,
        )[-1000:]
    finally:
        provider.close()

    if len(candles) < settings.warmup_candles:
        raise RuntimeError(
            f"Binance returned only {len(candles)} completed candles; "
            f"{settings.warmup_candles} are required for warmup."
        )

    with session_scope() as session:
        ingestor = DataIngestor(
            session,
            symbol=settings.symbol,
            timeframe=settings.timeframe.value,
            software_version="github-status",
        )
        report = ingestor.ingest(candles)
        print(f"[status] ingested {report.created} new candles")


def replay() -> None:
    settings = get_settings()
    config = load_frozen_strategy_config()
    with session_scope() as session:
        from app.replay_engine.engine import load_series

        series = load_series(
            session,
            symbol=settings.symbol,
            timeframe=settings.timeframe.value,
        )
        engine = ReplayEngine(
            config=config,
            symbol=settings.symbol,
            timeframe=settings.timeframe.value,
            software_version="github-status",
            on_integrity="halt",
            timeline_limit=0,
        )
        result = engine.run(series, session=session)
        if result.halted:
            raise RuntimeError(f"Replay halted: {result.halt_reason}")
        print(
            f"[status] replay processed={result.processed} "
            f"trades={result.trades} fills={result.fills}"
        )


def render_status() -> None:
    settings = get_settings()
    with session_scope() as session:
        summary = build_summary(
            session,
            symbol=settings.symbol,
            timeframe=settings.timeframe.value,
            starting_balance=settings.starting_balance,
            now=datetime.now(tz=UTC),
        )

    p = summary.portfolio
    s = summary.statistics
    d = summary.data_status
    decision = summary.engine_decision
    next_trade = summary.next_opportunity
    open_position = summary.open_position
    pending = summary.pending_order

    def value(item: object) -> str:
        return "—" if item is None else str(item)

    open_text = "لا توجد" if open_position is None else "نعم"
    pending_text = "لا توجد" if pending is None else "نعم"
    if open_position:
        open_text = (
            f"نعم — الدخول {open_position['entry_price']} دولار، "
            f"السعر الحالي {open_position['current_price']} دولار، "
            f"النتيجة الحالية {open_position['profit_now']}"
        )

    text = f"""# 🤖 Ethereum Paper Trading — BOT STATUS

> صفحة متابعة بسيطة للبوت — البيانات تتحدث تلقائيًا من GitHub Actions.

## 🟢 آخر حالة محفوظة

| البند | القيمة |
|---|---|
| السوق | {summary.symbol} |
| الفريم | {summary.timeframe} |
| نوع التشغيل | Paper Trading — فلوس افتراضية فقط |
| الرصيد الابتدائي | {p['starting']} دولار |
| Equity الحالية | {p['equity']} دولار |
| الصفقات المغلقة | {s['trades']} |
| الصفقات الرابحة | {s['wins']} |
| الصفقات الخاسرة | {s['losses']} |
| الصفقة المفتوحة الآن | {open_text} |
| الإشارة المعلقة | {pending_text} |
| آخر تحديث | {summary.generated_at_cairo} |
| آخر بيانات سعر | {value(d['to'])} |

## 💰 المحفظة

| البند | القيمة |
|---|---:|
| Equity | {p['equity']} دولار |
| Cash | {p['cash']} دولار |
| الربح / الخسارة | {p['profit']} |
| نسبة الربح / الخسارة | {p['profit_pct']} |
| الرسوم المدفوعة | {p['fees_paid']} |
| أقصى Drawdown | {p['worst_drawdown']} |

## 📊 الأداء

| البند | القيمة |
|---|---:|
| Win Rate | {s['win_rate']} |
| Net P&L | {s['net_pnl']} |
| أفضل صفقة | {s['best']} |
| أسوأ صفقة | {s['worst']} |
| متوسط مدة الصفقة | {s['avg_holding']} |

## 🧠 قرار الـ Baseline

**القرار/الحالة:** {decision['reason_code']}

**الشرح:** {decision['meaning']}

**وقت القرار:** {decision['when']}

## 📅 الصفقة القادمة

**{next_trade['headline']}**

{next_trade['detail']}

**الموعد:** {next_trade['when']}

## 🗃️ البيانات

- الشموع المستخدمة: **{d['candles']}**
- الفترة: **{value(d['from'])} → {value(d['to'])}**
- فجوات البيانات: **{d['gaps_text']}**
- الشموع الناقصة: **{d['missing_text']}**
- أخطاء Integrity الحرجة: **{d['integrity_critical']}**

## ⚙️ الاستراتيجية الثابتة

- ETHUSDT / 4H
- EMA 200
- ATR 14
- Stop Loss = 1.5 × ATR
- Take Profit = 3.0 × ATR
- Maximum Position = 10%
- Minimum Confidence = 60%
- Warmup = 200 candles
- Fee = 0.1%
- Starting Balance = $200
- **Live Trading = OFF**

## 🔒 الأمان

- **Paper Trading فقط.**
- لا توجد Binance API Keys.
- لا يتم إرسال أي Buy/Sell حقيقي.
- Live Trading غير موجود في هذا الإصدار.
- بيانات السوق من Binance Public API.
- GitHub Actions تحدث صفحة الحالة فقط.

## 🔄 التحديث

الصفحة يتم تحديثها تلقائيًا تقريبًا كل ساعة بواسطة GitHub Actions.

> **مهم:** الأرقام دي نتائج محاكاة وليست أموالًا أو أرباحًا حقيقية.
"""
    STATUS_PATH.write_text(text, encoding="utf-8")
    print(f"[status] wrote {STATUS_PATH}")


def main() -> int:
    enforce_paper_only(get_settings())
    reset_database()
    fetch_and_ingest()
    replay()
    render_status()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
