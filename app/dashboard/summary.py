"""Plain-language Arabic summary of what the engine actually did.

The audience for the dashboard is not a developer: it must answer four questions
without a single code, table or JSON key in sight —

1. **شترينا إيه؟**      what did we buy (and at what price, and how much),
2. **الصفقة اتنفذت امتى؟** when the trade was executed, in Cairo time,
3. **ربحنا كام؟**       how much was made or lost, per trade and in total,
4. **الصفقة الجاية امتى؟** when the next trade is expected, and *why*.

Everything here is derived from the same immutable rows the engine writes
(``trades``, ``positions``, ``orders``, ``signals``, ``candles``). Nothing is
recomputed from a different formula and nothing is hard-coded: if the database
says zero trades, the dashboard says "no trades yet" rather than showing a
pretty mock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.candles.gaps import detect_gaps
from app.common.enums import Side, Timeframe
from app.common.time_utils import next_open_time, timeframe_delta
from app.config.strategy_config import StrategyConfig, load_frozen_strategy_config
from app.integrity.enums import EventType, IntegrityCode
from app.ledger import ledger
from app.models.candle import Candle
from app.models.order import Order
from app.models.position import Position
from app.models.signal import Signal
from app.models.system_event import SystemEvent
from app.models.trade import Trade
from app.portfolio import accounting
from app.repositories import candles as candle_repo
from app.repositories import integrity_events
from app.repositories import orders as order_repo

CAIRO = ZoneInfo("Africa/Cairo")

ARABIC_MONTHS = (
    "يناير",
    "فبراير",
    "مارس",
    "أبريل",
    "مايو",
    "يونيو",
    "يوليو",
    "أغسطس",
    "سبتمبر",
    "أكتوبر",
    "نوفمبر",
    "ديسمبر",
)
ARABIC_WEEKDAYS = {
    0: "الاثنين",
    1: "الثلاثاء",
    2: "الأربعاء",
    3: "الخميس",
    4: "الجمعة",
    5: "السبت",
    6: "الأحد",
}
#: Plain-Arabic meaning of every reason code the engine can produce.
REASON_MEANINGS: dict[str, str] = {
    "WARMUP": "المحرك لسه بيجهّز حساباته الأولية (محتاج ٢٠٠ شمعة قبل ما يبدأ يقرر).",
    "TREND_FILTER": "السعر تحت الخط المتوسط (EMA 200)، والاستراتيجية بتشتري بس والسعر فوقه.",
    "SLOPE_FILTER": "الخط المتوسط مش طالع لفوق، فالإشارة اتلغت.",
    "MIN_CONFIDENCE": "الإشارة كانت ضعيفة (نسبة التأكد أقل من الحد المسموح ٦٠٪).",
    "ATR_UNAVAILABLE": "مفيش بيانات كفاية لحساب مقياس التذبذب (ATR).",
    "POSITION_ALREADY_OPEN": "عندنا صفقة مفتوحة بالفعل، والقاعدة: صفقة واحدة في المرة.",
    "ORDER_ALREADY_PENDING": "فيه أمر شراء مستني يتنفذ على الشمعة الجاية.",
    "EXPOSURE_CAP_EXCEEDED": "حجم الصفقة أكبر من الحد المسموح (١٠٪ من رأس المال).",
    "INSUFFICIENT_AVAILABLE_CASH": "الرصيد المتاح مش كفاية لفتح الصفقة بعد الرسوم.",
    "COMPUTED_QUANTITY_ZERO": "حساب حجم الصفقة طلع صفر (الرصيد أو الحد صغير أوي).",
    "WARMUP_INCOMPLETE": "المحرك لسه في مرحلة التجهيز، مبيدخلش صفقات.",
    "STRATEGY_CONFIG_MISMATCH": "إعدادات الاستراتيجية اتغيرت، والأمر ده وقف التنفيذ للسلامة.",
    "NOT_ACTIONABLE": "القرار مش إشارة شراء (HOLD).",
    "NO_SETUP": "السعر مش فوق الخط المتوسط، فمفيش إشارة شراء.",
    "SL_HIT": "الصفقة قفلت على وقف الخسارة.",
    "TP_HIT": "الصفقة قفلت على هدف الربح.",
    "UNKNOWN": "المحرك مستني إشارة جديدة.",
}
SIDE_ARABIC = {Side.BUY.value: "شراء", Side.SELL.value: "بيع"}


def format_cairo(moment: datetime | None) -> str:
    """A human Cairo-time stamp: «الأحد 13 سبتمبر 2026، 4:00 عصرًا»."""
    if moment is None:
        return "—"
    local = moment.astimezone(CAIRO)
    hour = local.hour % 12 or 12
    period = "صباحًا" if local.hour < 12 else ("عصرًا" if local.hour < 17 else "مساءً")
    return (
        f"{ARABIC_WEEKDAYS[local.weekday()]} {local.day} {ARABIC_MONTHS[local.month - 1]} "
        f"{local.year}، {hour}:{local.minute:02d} {period} بتوقيت القاهرة"
    )


def count_arabic(count: int, one: str, two: str, few: str) -> str:
    """Arabic counting: 1 singular, 2 dual, 3-10 plural, 11+ singular again."""
    if count == 1:
        return one
    if count == 2:
        return two
    if count <= 10:
        return f"{count} {few}"
    return f"{count} {one}"


def humanize_arabic(delta: timedelta) -> str:
    """A duration in words: «٣ أيام و٥ ساعات»."""
    total = int(delta.total_seconds())
    if total <= 0:
        return "الوقت ده عدّى بالفعل"
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    parts: list[str] = []

    def plural(count: int, one: str, two: str, few: str) -> str:
        return count_arabic(count, one, two, few)

    if days:
        parts.append(plural(days, "يوم", "يومين", "أيام"))
    if hours:
        parts.append(plural(hours, "ساعة", "ساعتين", "ساعات"))
    if minutes and not days:
        parts.append(plural(minutes, "دقيقة", "دقيقتين", "دقائق"))
    return " و".join(parts) if parts else "أقل من دقيقة"


def _dec(value: object, default: Decimal = Decimal(0)) -> Decimal:
    """Coerce a ledger statistic (Decimal, str or None) without inventing numbers."""
    if value is None:
        return default
    return Decimal(str(value))


def money_arabic(value: Decimal | str) -> str:
    """A money string with an explicit sign and currency, in western digits."""
    amount = Decimal(str(value))
    sign = "+" if amount > 0 else ("−" if amount < 0 else "")
    return f"{sign}{abs(amount):.2f} دولار"


def amount_arabic(value: Decimal | str) -> str:
    """An unsigned amount: for quantities where a "+" would read as a gain."""
    return f"{abs(Decimal(str(value))):.2f} دولار"


def holding_arabic(seconds: int) -> str:
    """How long a position was held, in words — including the same-candle case."""
    if seconds <= 0:
        return "قفلت في نفس شمعة الدخول"
    return humanize_arabic(timedelta(seconds=seconds))


def _signal_for(session: Session, signal_id: str | None) -> Signal | None:
    if not signal_id:
        return None
    return session.get(Signal, signal_id)


def _last_event(session: Session, event_type: EventType) -> SystemEvent | None:
    stmt = (
        select(SystemEvent)
        .where(SystemEvent.event_type == event_type.value)
        .order_by(desc(SystemEvent.timestamp), desc(SystemEvent.sequence))
        .limit(1)
    )
    return session.execute(stmt).scalars().first()


def _trade_row(session: Session, trade: Trade) -> dict[str, Any]:
    position = session.get(Position, trade.position_id)
    signal = _signal_for(session, trade.signal_id)
    profit = Decimal(str(trade.net_pnl))
    return {
        "when_utc": trade.exit_time,
        "when": format_cairo(trade.exit_time),
        "entry_time": format_cairo(trade.entry_time),
        "exit_time": format_cairo(trade.exit_time),
        "side": SIDE_ARABIC.get(trade.side, trade.side),
        "quantity": f"{Decimal(str(trade.quantity)):.6f}",
        "entry_price": f"{Decimal(str(trade.entry_price)):.2f}",
        "exit_price": f"{Decimal(str(trade.exit_price)):.2f}",
        "profit": profit,
        "profit_text": money_arabic(profit),
        "won": profit > 0,
        "reason": REASON_MEANINGS.get(trade.exit_reason, trade.exit_reason),
        "holding": holding_arabic(trade.holding_seconds),
        "stop_loss": None if position is None else f"{Decimal(str(position.stop_loss)):.2f}",
        "take_profit": (None if position is None else f"{Decimal(str(position.take_profit)):.2f}"),
        "why_we_bought": _why_we_bought(signal),
    }


def _why_we_bought(signal: Signal | None) -> str:
    """Explain the entry in words. Codes and English never reach the reader."""
    if signal is None:
        return "الإشارة الأصلية غير متاحة في قاعدة البيانات، لكن التنفيذ محفوظ بالكامل."
    context = signal.indicator_context or {}
    close = context.get("close")
    ema = context.get("ema", context.get("ema200"))
    if close is None or ema is None:
        return "إشارة شراء من الاستراتيجية: السعر كان أعلى من المؤشر (متوسط 200 شمعة) والمؤشر كان صاعد."
    return (
        f"السعر ({float(close):,.2f}) كان أعلى من المؤشر ({float(ema):,.2f}) "
        "والمؤشر كان صاعد، ونسبة التأكد "
        f"{Decimal(str(signal.confidence)) * 100:.0f}٪."
    )


def _open_position_row(session: Session, position: Position, last_price: Decimal) -> dict[str, Any]:
    unrealized = accounting.unrealized_pnl(position, last_price)
    signal = _signal_for(session, position.signal_id)
    entry_order = order_repo.get(session, position.entry_order_id or "")
    return {
        "side": SIDE_ARABIC.get(position.side, position.side),
        "quantity": f"{Decimal(str(position.quantity)):.6f}",
        "entry_price": f"{Decimal(str(position.entry_price)):.2f}",
        "entry_time": format_cairo(position.entry_candle_open_time),
        "stop_loss": f"{Decimal(str(position.stop_loss)):.2f}",
        "take_profit": f"{Decimal(str(position.take_profit)):.2f}",
        "current_price": f"{last_price:,.2f}",
        "profit_now": money_arabic(unrealized),
        "profit_now_value": unrealized,
        "fee_paid": money_arabic(Decimal(str(position.entry_fee))),
        "why_we_bought": _why_we_bought(signal),
        "target_candle": (
            "—" if entry_order is None else format_cairo(entry_order.target_execution_open_time)
        ),
    }


def _pending_row(session: Session, order: Order) -> dict[str, Any]:
    signal = _signal_for(session, order.signal_id)
    return {
        "side": SIDE_ARABIC.get(order.side, order.side),
        "quantity": f"{Decimal(str(order.intended_quantity)):.6f}",
        "notional": money_arabic(Decimal(str(order.intended_notional))),
        "stop_loss": None if order.stop_loss_price is None else f"{Decimal(str(order.stop_loss_price)):.2f}",
        "take_profit": (
            None if order.take_profit_price is None else f"{Decimal(str(order.take_profit_price)):.2f}"
        ),
        "target_time": format_cairo(order.target_execution_open_time),
        "target_time_utc": order.target_execution_open_time,
        "why_we_bought": _why_we_bought(signal),
    }


def _next_opportunity(
    *,
    last_candle: Candle | None,
    timeframe: str,
    open_position: Position | None,
    pending: Order | None,
    now: datetime,
) -> dict[str, Any]:
    """Answer «الصفقة الجاية امتى؟» as honestly as the data allows."""
    if open_position is not None:
        return {
            "headline": "مفيش صفقة جديدة دلوقتي — عندنا صفقة مفتوحة",
            "detail": (
                "القاعدة عندنا: صفقة واحدة في الوقت الواحد. لما الصفقة الحالية تقفل "
                "(يا إما عند وقف الخسارة يا إما عند هدف الربح) المحرك يقدر يفتح صفقة جديدة."
            ),
            "when": "بعد ما الصفقة الحالية تقفل",
        }
    if pending is not None:
        return {
            "headline": "فيه صفقة مستنية تتنفذ",
            "detail": (
                "المحرك لقى إشارة شراء، وهينفذها عند فتح الشمعة المحددة بالظبط. "
                "الوقت ده ثابت ومتسجّل، ومش بيتنفذ قبلها ولا بعدها."
            ),
            "when": format_cairo(pending.target_execution_open_time),
        }
    if last_candle is None:
        return {
            "headline": "مفيش بيانات كفاية",
            "detail": "لازم نستورد بيانات الأسعار الأول.",
            "when": "—",
        }
    following = next_open_time(last_candle.open_time, timeframe)
    delta = following - now
    return {
        "headline": "المحرك مراقب كل شمعة جديدة",
        "detail": (
            f"آخر شمعة عندنا بدأت {format_cairo(last_candle.open_time)} وقفلت "
            f"{format_cairo(last_candle.close_time)}. المحرك بيقرر بعد إغلاق كل شمعة "
            f"({timeframe})، وأول فرصة جديدة هتكون عند الشمعة اللي بعدها."
        ),
        "when": format_cairo(following),
        "from_now": humanize_arabic(delta),
    }


@dataclass(frozen=True, slots=True)
class DashboardSummary:
    """Everything the panel shows, already in Arabic, ready to render."""

    generated_at_utc: datetime
    generated_at_cairo: str
    symbol: str
    timeframe: str
    strategy: dict[str, Any]
    portfolio: dict[str, Any]
    statistics: dict[str, Any]
    last_trade: dict[str, Any] | None
    open_position: dict[str, Any] | None
    pending_order: dict[str, Any] | None
    next_opportunity: dict[str, Any]
    data_status: dict[str, Any]
    engine_decision: dict[str, Any]
    recent_trades: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        def convert(value: Any) -> Any:
            if isinstance(value, Decimal):
                return str(value)
            if isinstance(value, datetime):
                return value.isoformat()
            if isinstance(value, dict):
                return {key: convert(item) for key, item in value.items()}
            if isinstance(value, list):
                return [convert(item) for item in value]
            return value

        return convert(
            {
                "generated_at_utc": self.generated_at_utc,
                "generated_at_cairo": self.generated_at_cairo,
                "symbol": self.symbol,
                "timeframe": self.timeframe,
                "strategy": self.strategy,
                "portfolio": self.portfolio,
                "statistics": self.statistics,
                "last_trade": self.last_trade,
                "open_position": self.open_position,
                "pending_order": self.pending_order,
                "next_opportunity": self.next_opportunity,
                "data_status": self.data_status,
                "engine_decision": self.engine_decision,
                "recent_trades": self.recent_trades,
                "notes": self.notes,
            }
        )


def build_summary(
    session: Session,
    *,
    symbol: str,
    timeframe: str,
    starting_balance: Decimal | str,
    config: StrategyConfig | None = None,
    now: datetime | None = None,
    recent_limit: int = 12,
) -> DashboardSummary:
    """Read the ledger and describe it in plain Arabic."""
    config = config or load_frozen_strategy_config()
    now = now or datetime.now(tz=UTC)
    starting = Decimal(str(starting_balance))

    last_candle = candle_repo.last_candle(session, symbol, timeframe)
    open_position = (
        session.execute(
            select(Position)
            .where(Position.symbol == symbol, Position.timeframe == timeframe, Position.state == "OPEN")
            .order_by(desc(Position.entry_time))
            .limit(1)
        )
        .scalars()
        .first()
    )
    last_price = (
        Decimal(str(last_candle.close))
        if last_candle is not None
        else (Decimal(str(open_position.entry_price)) if open_position is not None else Decimal(0))
    )
    state = accounting.compute_state(
        session,
        symbol=symbol,
        timeframe=timeframe,
        starting_balance=starting,
        last_price=last_price,
        position=open_position,
    )
    stats = ledger.statistics(session, symbol=symbol, timeframe=timeframe)
    trades_total = int(stats["trades"])

    pending_entry = None
    pending = order_repo.pending(session, symbol, timeframe)
    if pending:
        pending_entry = next((order for order in pending if order.purpose == "ENTRY"), None)

    recent = (
        session.execute(
            select(Trade)
            .where(Trade.symbol == symbol, Trade.timeframe == timeframe)
            .order_by(desc(Trade.exit_time))
            .limit(recent_limit)
        )
        .scalars()
        .all()
    )
    last_trade = _trade_row(session, recent[0]) if recent else None

    present = candle_repo.open_times(session, symbol, timeframe)
    gaps: list[dict[str, Any]] = []
    if present:
        first = min(present)
        last = max(present)
        for gap in detect_gaps(present, first, last + timeframe_delta(timeframe), timeframe):
            gaps.append(
                {
                    "from": format_cairo(gap.start),
                    "missing": gap.missing,
                }
            )

    fatal = integrity_events.has_fatal(session)
    suppressed_event = _last_event(session, EventType.SIGNAL_SUPPRESSED)
    rejected_event = _last_event(session, EventType.RISK_REJECTED)
    decision_reason = "UNKNOWN"
    decision_time = None
    if suppressed_event is not None:
        decision_reason = str(suppressed_event.payload.get("reason", "UNKNOWN"))
        decision_time = suppressed_event.timestamp
    if rejected_event is not None and (decision_time is None or rejected_event.timestamp >= decision_time):
        decision_reason = str(rejected_event.payload.get("reason_code", decision_reason))
        decision_time = rejected_event.timestamp

    profit = state.equity - starting
    profit_pct = (profit / starting * 100) if starting else Decimal(0)
    win_rate = _dec(stats["win_rate"])
    avg_holding = (
        "—"
        if stats["avg_holding_hours"] is None
        else humanize_arabic(timedelta(hours=float(stats["avg_holding_hours"])))
    )

    return DashboardSummary(
        generated_at_utc=now,
        generated_at_cairo=format_cairo(now),
        symbol=symbol,
        timeframe=timeframe,
        strategy={
            "name": config.name,
            "version": config.version,
            "config_hash_short": config.config_hash[:10],
            "rules": [
                "نشوف كل شمعة 4 ساعات بعد ما تقفل (مش قبل كده ولا في وسطها).",
                "الشرط الأول: سعر الإغلاق يكون أعلى من المؤشر (متوسط 200 شمعة).",
                "الشرط الثاني: المؤشر نفسه يكون طالع لفوق.",
                "الشرط الثالث: نسبة التأكد 60٪ أو أكتر.",
                "لو الثلاثة اتفقوا: نشتري عند فتح الشمعة الجاية بس، ومش بنستنى.",
                "بنحدد وقف الخسارة والهدف من مقياس التذبذب (ATR): خسارة 1.5×، ربح 3×.",
                "أقصى مبلغ في الصفقة الواحدة 10٪ من رأس المال، ومفيش رفع مالي.",
                "لما وقف الخسارة والهدف يتلمسوا في نفس الشمعة، بنحسب إن وقف الخسارة اللي ضرب.",
            ],
        },
        portfolio={
            "starting": f"{starting:,.2f}",
            "equity": f"{state.equity:,.2f}",
            "cash": f"{state.cash:,.2f}",
            "profit": money_arabic(profit),
            "profit_value": profit,
            "profit_pct": f"{profit_pct:+.2f}٪",
            "fees_paid": amount_arabic(state.fees_cum),
            "open_value": f"{state.open_position_value:,.2f}",
            "drawdown_pct": f"{state.drawdown_pct * 100:.2f}٪",
            "worst_drawdown": (
                "—" if stats["max_drawdown"] is None else amount_arabic(_dec(stats["max_drawdown"]))
            ),
            "peak_equity": f"{state.peak_equity:,.2f}",
            "up": profit > 0,
        },
        statistics={
            "trades": trades_total,
            "wins": int(stats["wins"]),
            "losses": int(stats["losses"]),
            "win_rate": f"{win_rate * 100:.0f}٪",
            "net_pnl": money_arabic(_dec(stats["net_pnl"])),
            "best": "—" if stats["best"] is None else money_arabic(_dec(stats["best"])),
            "worst": "—" if stats["worst"] is None else money_arabic(_dec(stats["worst"])),
            "avg_win": "—" if stats["avg_win"] is None else money_arabic(_dec(stats["avg_win"])),
            "avg_loss": "—" if stats["avg_loss"] is None else money_arabic(_dec(stats["avg_loss"])),
            "avg_holding": avg_holding,
            "by_reason": stats["by_exit_reason"],
        },
        last_trade=last_trade,
        open_position=(
            None if open_position is None else _open_position_row(session, open_position, last_price)
        ),
        pending_order=None if pending_entry is None else _pending_row(session, pending_entry),
        next_opportunity=_next_opportunity(
            last_candle=last_candle,
            timeframe=timeframe,
            open_position=open_position,
            pending=pending_entry,
            now=now,
        ),
        data_status={
            "candles": candle_repo.count(session, symbol=symbol, timeframe=timeframe),
            "gaps_text": count_arabic(len(gaps), "فجوة", "فجوتين", "فجوات"),
            "from": format_cairo(min(present)) if present else "—",
            "to": format_cairo(max(present)) if present else "—",
            "gaps": len(gaps),
            "missing": sum(int(gap["missing"]) for gap in gaps),
            "missing_text": count_arabic(sum(int(gap["missing"]) for gap in gaps), "شمعة", "شمعتين", "شمعات"),
            "gap_examples": gaps[:3],
            "integrity_critical": integrity_events.count(
                session, code=IntegrityCode.EXECUTION_TARGET_MISSED.value
            ),
            "has_fatal": fatal,
        },
        engine_decision={
            "reason_code": decision_reason,
            "meaning": REASON_MEANINGS.get(decision_reason, REASON_MEANINGS["UNKNOWN"]),
            "when": format_cairo(decision_time),
        },
        recent_trades=[_trade_row(session, trade) for trade in recent],
        notes=_notes(trades_total, gaps, fatal),
    )


def _notes(trades_total: int, gaps: list[dict[str, Any]], fatal: bool) -> list[str]:
    notes = [
        "كل الأرقام دي من نتائج تجربة على بيانات حقيقية من منصة بينانس، "
        "مفيش أي فلوس حقيقية ومفيش أي أوامر حقيقية.",
        "الرصيد الابتدائي 200 دولار، وكل صفقة بتدفع رسوم 0.1٪ (بتتخصم من الربح).",
    ]
    if trades_total == 0:
        notes.append(
            "لسه مفيش صفقات مقفولة. شغّل الأمر: python scripts/replay.py --rebuild-db "
            "وهيقرأ التاريخ كله ويبني الصفقات."
        )
    if gaps:
        missing = sum(int(gap["missing"]) for gap in gaps)
        notes.append(
            f"لقينا {count_arabic(len(gaps), 'فجوة', 'فجوتين', 'فجوات')} في بيانات بينانس "
            f"نفسها ({count_arabic(missing, 'شمعة', 'شمعتين', 'شمعات')} ناقصة). "
            "مبنخترعش بيانات مكانها أبدًا: الشمعة الناقصة تتسجّل، والصفقة اللي كان هدفها "
            "الشمعة الناقصة بتتلغي (مش بتتنفذ بسعر بديل)."
        )
    if fatal:
        notes.append("فيه تسجيل خطأ جوهري (شرطة حمرا) محتاج مراجعة.")
    return notes


def default_timeframe() -> str:
    return Timeframe.H4.value


__all__ = [
    "CAIRO",
    "DashboardSummary",
    "REASON_MEANINGS",
    "amount_arabic",
    "build_summary",
    "count_arabic",
    "format_cairo",
    "holding_arabic",
    "humanize_arabic",
    "money_arabic",
]
