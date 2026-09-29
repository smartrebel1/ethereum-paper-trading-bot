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
from decimal import Decimal

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "status.db"
STATUS_PATH = ROOT / "BOT_STATUS.md"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["TRADING_MODE"] = "paper"
os.environ["ENABLE_LIVE_TRADING"] = "false"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ai.gemini import observe_latest  # noqa: E402
from app.candles.ingestor import DataIngestor  # noqa: E402
from app.common.safety import enforce_paper_only  # noqa: E402
from app.config.settings import get_settings  # noqa: E402
from app.config.strategy_config import load_frozen_strategy_config  # noqa: E402
from app.dashboard.summary import build_summary  # noqa: E402
from app.database.session import session_scope  # noqa: E402
from app.market_data.binance import BinanceError, BinanceRESTProvider  # noqa: E402
from app.market_data.csv_archive import CSVArchiveProvider  # noqa: E402
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
    candles = []
    rest_error: str | None = None
    current_price: Decimal | None = None
    current_price_at: datetime | None = None

    provider = BinanceRESTProvider()
    try:
        try:
            candles = provider.fetch_candles(
                settings.symbol,
                settings.timeframe,
                include_incomplete=False,
            )[-1000:]
            try:
                current_price, current_price_at = provider.fetch_current_price(settings.symbol)
                print(f"[status] current price={current_price} at={current_price_at.isoformat()}")
            except BinanceError as exc:
                print(f"[status] current price unavailable: {exc}")
        except BinanceError as exc:
            rest_error = str(exc)
    finally:
        provider.close()

    if not candles and rest_error:
        print(f"[status] REST unavailable: {rest_error}")
        print("[status] Falling back to official Binance Vision archive.")
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "fetch_binance_archive.py"),
                "--symbol",
                settings.symbol,
                "--interval",
                settings.timeframe.value,
                "--start-month",
                "2023-01",
            ],
            cwd=ROOT,
            check=True,
        )
        archive = CSVArchiveProvider(ROOT / "data" / "archive", verify_hash=True)
        candles = archive.fetch_candles(settings.symbol, settings.timeframe.value)[-1000:]

    if len(candles) < settings.warmup_candles:
        raise RuntimeError(
            f"Only {len(candles)} completed candles are available; "
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


def run_ai_shadow() -> None:
    settings = get_settings()
    if not settings.ai_enabled or settings.ai_provider != "gemini":
        print("[status] Gemini shadow disabled.")
        return
    try:
        with session_scope() as session:
            observation = observe_latest(session, settings, refresh=True)
            print(
                f"[status] Gemini shadow: bias="
                f"{observation.parsed_summary.get('bias', 'neutral')} "
                f"confidence={observation.parsed_summary.get('confidence', 0):.2f}"
            )
    except Exception as exc:
        # Gemini is observational only: a provider outage must never block
        # the baseline paper-trading status page.
        print(f"[status] Gemini shadow unavailable: {str(exc)[:256]}")


def render_status(current_price: Decimal | None, current_price_at: datetime | None) -> None:
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
    with session_scope() as ai_session:
        from sqlalchemy import desc, select

        from app.models.ai_observation import AIObservation

        ai_observation = ai_session.execute(
            select(AIObservation)
            .where(
                AIObservation.provider == "gemini",
                AIObservation.symbol == settings.symbol,
                AIObservation.timeframe == settings.timeframe.value,
            )
            .order_by(desc(AIObservation.at_candle_open_time))
            .limit(1)
        ).scalar_one_or_none()
    next_trade = summary.next_opportunity
    open_position = summary.open_position
    pending = summary.pending_order

    def value(item: object) -> str:
        return "—" if item is None else str(item)

    current_price_text = "غير متاح" if current_price is None else f"{current_price:,.2f} دولار"
    current_price_time_text = "غير متاح" if current_price_at is None else format_cairo(current_price_at)

    open_text = "لا توجد" if open_position is None else "نعم"
    pending_text = "لا توجد" if pending is None else "نعم"
    if open_position:
        open_text = (
            f"نعم — الدخول {open_position['entry_price']} دولار، "
            f"السعر الحالي {open_position['current_price']} دولار، "
            f"النتيجة الحالية {open_position['profit_now']}"
        )

    if ai_observation is None:
        ai_status = (
            "**الحالة:** غير مفعّل\n\n"
            "لا يوجد تحليل Gemini محفوظ في هذه الدورة."
        )
    elif ai_observation.error:
        ai_status = (
            "**الحالة:** فشل التحليل\n\n"
            f"**الخطأ:** {ai_observation.error}"
        )
    else:
        ai_status = (
            "**الحالة:** تم التحليل بنجاح\n\n"
            "| البند | القيمة |\n|---|---|\n"
            f"| الاتجاه | {str(ai_observation.parsed_summary.get('bias', 'neutral')).upper()} |\n"
            f"| Confidence | {float(ai_observation.parsed_summary.get('confidence', 0)):.0%} |\n"
            "| ملخص التحليل | "
            f"{str(ai_observation.parsed_summary.get('summary_ar', '—')).replace('|', '¦')} |\n"
            "| المخاطر | "
            f"{str(ai_observation.parsed_summary.get('risks_ar', '—')).replace('|', '¦')} |\n"
            f"| شمعة التحليل | {ai_observation.at_candle_open_time.isoformat()} |\n"
            f"| زمن الاستجابة | {ai_observation.latency_ms or '—'} ms |"
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
| **السعر الحالي (Live Snapshot)** | **{current_price_text}** |
| وقت جلب السعر الحالي | {current_price_time_text} |
| آخر شمعة 4H مكتملة | {value(d['to'])} |

> **مهم:** السعر الحالي Snapshot منفصل عن محرك الـBaseline. القرار والحسابات يستخدمان الشموع المكتملة فقط.

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

## 🤖 Gemini Shadow — مراقب مستقل

{ai_status}

> Gemini هنا **مراقب Shadow فقط**؛ لا يدخل في قرار الـBaseline ولا ينفذ أي أمر.
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
    current_price, current_price_at = fetch_and_ingest()
    replay()
    run_ai_shadow()
    render_status(current_price, current_price_at)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
