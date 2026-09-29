"""Renders the Arabic dashboard: one self-contained HTML document.

Constraints that shaped this file:

* **Arabic, right to left, no developer jargon.** The reader is not a programmer:
  no JSON, no English field names, no codes — every reason code is translated in
  :mod:`app.dashboard.summary` before it gets here.
* **No external resources.** All CSS is inline and there are no scripts, images,
  fonts or CDN links, so the same document renders identically in a browser, in
  a sandboxed preview and saved to disk.
* **Real numbers or an honest blank.** When a figure does not exist yet the
  panel says so ("لسه مفيش صفقات") instead of showing a placeholder number.
"""

from __future__ import annotations

from html import escape

from app.dashboard.summary import DashboardSummary

STYLE = """
:root {
  --bg: #0f1720; --card: #17222e; --card-2: #1e2c3a; --line: #2b3d4f;
  --ink: #eaf2f8; --muted: #9fb3c8; --green: #35c98a; --red: #ef6b6b;
  --gold: #f2c14e; --blue: #63b3ed;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 0 0 48px; background: var(--bg); color: var(--ink);
  font-family: "Segoe UI", "Noto Naskh Arabic", "Traditional Arabic", Tahoma, Arial, sans-serif;
  direction: rtl; line-height: 1.9; font-size: 17px;
}
.wrap { max-width: 1100px; margin: 0 auto; padding: 20px; }
header.top { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; justify-content: space-between; }
h1 { font-size: 28px; margin: 0 0 6px; }
h2 { font-size: 21px; margin: 0 0 12px; }
h3 { font-size: 18px; margin: 0 0 8px; }
.sub { color: var(--muted); font-size: 15px; margin: 0; }
.pill {
  background: #24405a; color: #cfe8ff; border: 1px solid #35618a;
  border-radius: 999px; padding: 6px 14px; font-size: 14px; white-space: nowrap;
}
.pill.paper { background: #2a4030; color: #b7f0cf; border-color: #3d6b4c; }
.grid { display: grid; gap: 16px; }
.answers { grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); margin-top: 18px; }
.two { grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); margin-top: 16px; }
.card {
  background: var(--card); border: 1px solid var(--line); border-radius: 16px;
  padding: 18px 20px; box-shadow: 0 8px 22px rgba(0,0,0,.18);
}
.card .label { color: var(--muted); font-size: 15px; margin: 0 0 6px; }
.card .big { font-size: 26px; font-weight: 700; margin: 0 0 6px; }
.card .small { color: var(--muted); font-size: 14px; margin: 0; }
.green { color: var(--green); }
.red { color: var(--red); }
.gold { color: var(--gold); }
.blue { color: var(--blue); }
.muted { color: var(--muted); }
table { width: 100%; border-collapse: collapse; font-size: 16px; margin-top: 10px; }
th, td { padding: 10px 8px; text-align: right; border-bottom: 1px solid var(--line); }
th { color: var(--muted); font-weight: 600; font-size: 15px; }
tr:last-child td { border-bottom: none; }
.kv {
  display: flex; justify-content: space-between; gap: 12px;
  padding: 6px 0; border-bottom: 1px dashed var(--line);
}
.kv:last-child { border-bottom: none; }
.kv span:first-child { color: var(--muted); }
ul.rules { margin: 8px 0 0; padding-inline-start: 22px; }
ul.rules li { margin-bottom: 8px; }
ul.notes { margin: 8px 0 0; padding-inline-start: 22px; color: var(--muted); font-size: 15px; }
.banner {
  border-radius: 14px; padding: 14px 18px; margin: 18px 0 6px; font-size: 16px;
  background: #1c3326; border: 1px solid #35594048; color: #cfeeff;
}
.banner.warn { background: #3a2c1c; border-color: #6b4d2a; color: #ffe6bf; }
.banner.bad { background: #3a1f1f; border-color: #7a3333; color: #ffd9d9; }
button {
  background: #24405a; color: #eaf2f8; border: 1px solid #35618a; border-radius: 10px;
  padding: 8px 16px; font-size: 15px; cursor: pointer; font-family: inherit;
}
.foot { margin-top: 26px; color: #6f8399; font-size: 13px; text-align: center; }
.refresh-bar { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-top:12px; color:var(--muted); font-size:14px; }
.chart-wrap { margin-top: 14px; overflow: hidden; border: 1px solid var(--line); border-radius: 12px; background: var(--card-2); }\n.chart-wrap svg { display:block; width:100%; height:auto; }\n.chart-caption { color:var(--muted); font-size:13px; margin-top:6px; }\n.refresh-dot { width:9px; height:9px; border-radius:50%; display:inline-block; background:var(--green); margin-left:6px; }
code { background: var(--card-2); padding: 2px 6px; border-radius: 6px; font-size: 14px; }
"""


def _rows(pairs: list[tuple[str, str, str]]) -> str:
    """``(label, value, css-class)`` triples as a card's key/value list."""
    return "".join(
        f'<div class="kv"><span>{escape(label)}</span><span class="{css}">{escape(value)}</span></div>'
        for label, value, css in pairs
    )


def _card(label: str, big: str, small: str = "", tone: str = "") -> str:
    cls = f"big {tone}".strip()
    small_html = f'<p class="small">{escape(small)}</p>' if small else ""
    return (
        f'<div class="card"><p class="label">{escape(label)}</p>'
        f'<p class="{cls}">{escape(big)}</p>{small_html}</div>'
    )


def _answers(summary: DashboardSummary) -> str:
    """The four questions the panel exists to answer."""
    trade = summary.last_trade
    position = summary.open_position
    pending = summary.pending_order

    if position is not None:
        what = _card(
            "١) شترينا إيه؟",
            f"{position['quantity']} إيثيريوم",
            f"سعر الدخول {position['entry_price']} دولار — الصفقة لسه مفتوحة",
            "blue",
        )
        when = _card(
            "٢) الصفقة اتنفذت امتى؟",
            position["entry_time"].split("،")[0],
            f"ساعة التنفيذ: {position['entry_time'].split('،')[-1]}",
            "blue",
        )
        profit = _card(
            "٣) ربحنا كام؟",
            position["profit_now"],
            f"على السعر الحالي {position['current_price']} دولار — لسه مش مقفولة",
            "green" if position["profit_now_value"] > 0 else "red",
        )
    elif trade is not None:
        what = _card(
            "١) شترينا إيه؟",
            f"{trade['quantity']} إيثيريوم",
            f"دخلنا عند {trade['entry_price']} دولار وخرجنا عند {trade['exit_price']} دولار",
            "blue",
        )
        when = _card(
            "٢) الصفقة اتنفذت امتى؟",
            trade["entry_time"].split("،")[0],
            f"ساعة التنفيذ: {trade['entry_time'].split('،')[-1].strip()} "
            f"— والقفل كان {trade['exit_time'].split('،')[0]} بعد {trade['holding']}",
            "blue",
        )
        profit = _card(
            "٣) ربحنا كام؟",
            trade["profit_text"],
            f"{trade['reason']} — الرصيد الحالي {summary.portfolio['equity']} دولار",
            "green" if trade["won"] else "red",
        )
    else:
        what = _card("١) شترينا إيه؟", "لسه مفيش صفقات", "المحرك لسه بيجهّز حساباته الأولية.")
        when = _card("٢) الصفقة اتنفذت امتى؟", "لسه مفيش تنفيذ", "")
        profit = _card("٣) ربحنا كام؟", summary.portfolio["profit"], "من الرصيد الابتدائي 200 دولار")

    next_opp = summary.next_opportunity
    if pending is not None:
        next_small = f"{pending['notional']} — صفقة شراء مستنية التنفيذ عند فتح الشمعة"
    else:
        next_small = f"آخر شمعة وصلتنا: {summary.data_status['to'].split('،')[0]}"
    next_card = _card("٤) الصفقة الجاية امتى؟", next_opp["when"], next_small, "gold")

    return '<div class="grid answers">' + what + when + profit + next_card + "</div>"


def _banner(summary: DashboardSummary) -> str:
    status = summary.data_status
    if status["has_fatal"]:
        css, text = (
            "banner bad",
            "فيه مشكلة جوهري اتسجلت — المحرك وقف عند هدف تنفيذ مش موجود في البيانات. "
            "الرقام المعروضة لحد آخر صفقة سليمة.",
        )
    elif summary.portfolio["up"]:
        css, text = (
            "banner",
            f"البوت شغال على بيانات حقيقية من بينانس، ومعاه أرباح "
            f"{summary.portfolio['profit']} من أصل 200 دولار.",
        )
    else:
        css, text = (
            "banner warn",
            f"البوت شغال على بيانات حقيقية، والنتيجة حاليًا {summary.portfolio['profit']} "
            "من أصل 200 دولار. الخسارة جزء طبيعي من التجربة التجريبية.",
        )
    return f'<div class="{css}">{escape(text)}</div>'


def _portfolio(summary: DashboardSummary) -> str:
    rows = [
        ("الرصيد الابتدائي", f"{summary.portfolio['starting']} دولار"),
        ("الرصيد الحالي (الكاش)", f"{summary.portfolio['cash']} دولار"),
        ("قيمة الصفقة المفتوحة", f"{summary.portfolio['open_value']} دولار"),
        ("إجمالي الربح/الخسارة", summary.portfolio["profit"]),
        ("النسبة من رأس المال", summary.portfolio["profit_pct"]),
        ("أعلى رصيد وصلناه", f"{summary.portfolio['peak_equity']} دولار"),
        ("أكبر تراجع من القمة", summary.portfolio["worst_drawdown"]),
        ("الرسوم المدفوعة", summary.portfolio["fees_paid"]),
    ]
    body = "".join(
        f"<div class='kv'><span>{escape(label)}</span><span>{escape(value)}</span></div>"
        for label, value in rows
    )
    return f'<div class="card"><h2>الرصيد والحساب</h2>{body}</div>'


def _statistics(summary: DashboardSummary) -> str:
    stats = summary.statistics
    rows = [
        ("عدد الصفقات المقفولة", f"{stats['trades']}"),
        ("صفقات كسبانة", f"{stats['wins']}"),
        ("صفقات خسرانة", f"{stats['losses']}"),
        ("نسبة الصفقات الكسبانة", stats["win_rate"]),
        ("متوسط الصفقة الكسبانة", stats["avg_win"]),
        ("متوسط الصفقة الخسرانة", stats["avg_loss"]),
        ("أحسن صفقة", stats["best"]),
        ("أسوأ صفقة", stats["worst"]),
        ("متوسط مدة الصفقة", stats["avg_holding"]),
    ]
    body = "".join(
        f"<div class='kv'><span>{escape(label)}</span><span>{escape(value)}</span></div>"
        for label, value in rows
    )
    reasons = stats["by_reason"] or {}
    reasons_html = ""
    if reasons:
        parts = [
            f"{int(count)} صفقة قفلت على {'هدف الربح' if code == 'TP_HIT' else 'وقف الخسارة'}"
            for code, count in sorted(reasons.items(), key=lambda item: -item[1])
        ]
        reasons_html = f'<p class="small">{"، و".join(parts)}.</p>'
    return f'<div class="card"><h2>النتائج بالأرقام</h2>{body}{reasons_html}</div>'


def _current(summary: DashboardSummary) -> str:
    """What is happening right now: open position, pending order, or a reason."""
    if summary.open_position is not None:
        position = summary.open_position
        rows = _rows(
            [
                ("الكمية", f"{position['quantity']} إيثيريوم", ""),
                ("سعر الدخول", f"{position['entry_price']} دولار", ""),
                ("وقت التنفيذ", position["entry_time"], ""),
                ("وقف الخسارة", f"{position['stop_loss']} دولار", "red"),
                ("هدف الربح", f"{position['take_profit']} دولار", "green"),
                ("السعر الحالي", f"{position['current_price']} دولار", ""),
                ("الربح لو قفلنا الآن", position["profit_now"], ""),
            ]
        )
        return (
            '<div class="card"><h2>الصفقة المفتوحة دلوقتي</h2>'
            f"<p>{escape(position['why_we_bought'])}</p>{rows}</div>"
        )
    if summary.pending_order is not None:
        pending = summary.pending_order
        rows = _rows(
            [
                ("الكمية المطلوبة", f"{pending['quantity']} إيثيريوم", ""),
                ("المبلغ", pending["notional"], ""),
                ("وقت التنفيذ", pending["target_time"], "gold"),
                ("وقف الخسارة", f"{pending['stop_loss']} دولار", "red"),
                ("هدف الربح", f"{pending['take_profit']} دولار", "green"),
            ]
        )
        return (
            '<div class="card"><h2>صفقة مستنية التنفيذ</h2>'
            f"<p>{escape(pending['why_we_bought'])}</p>{rows}</div>"
        )
    return (
        '<div class="card"><h2>ليه مفيش صفقة جديدة دلوقتي؟</h2>'
        f"<p>{escape(summary.engine_decision['meaning'])}</p>"
        f"<p class='small'>آخر مرة اتفحص فيها السعر: {escape(summary.engine_decision['when'])}</p>"
        f"<p class='small'>{escape(summary.next_opportunity['detail'])}</p>"
        "</div>"
    )


def _recent_trades(summary: DashboardSummary) -> str:
    if not summary.recent_trades:
        return (
            '<div class="card"><h2>آخر الصفقات</h2>'
            "<p class='muted'>لسه مفيش صفقات. شغّل <code>python scripts/replay.py --rebuild-db</code> "
            "وهيبني كل الصفقات من تاريخ بينانس الحقيقي.</p></div>"
        )
    rows = "".join(
        f"<tr><td>{escape(trade['when'])}</td>"
        f"<td>{escape(trade['side'])} {escape(trade['quantity'])}</td>"
        f"<td>{escape(trade['entry_price'])} ← {escape(trade['exit_price'])}</td>"
        f"<td class='{'green' if trade['won'] else 'red'}'>{escape(trade['profit_text'])}</td>"
        f"<td>{escape(trade['holding'])}</td>"
        f"<td class='muted'>{escape(trade['reason'])}</td></tr>"
        for trade in summary.recent_trades
    )
    return (
        '<div class="card"><h2>آخر الصفقات (الأحدث فوق)</h2>'
        "<table><thead><tr><th>قفلت امتى</th><th>الكمية</th><th>سعر الدخول ← الخروج</th>"
        "<th>الربح/الخسارة</th><th>مدة الصفقة</th><th>ليه قفلت</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></div>"
    )


def _data_status(summary: DashboardSummary) -> str:
    status = summary.data_status
    examples = ""
    if status["gap_examples"]:
        first = status["gap_examples"][0]
        examples = (
            f"<p class='small'>أول فجوة مثلًا: {escape(first['from'])} ({int(first['missing'])} ناقصة).</p>"
        )
    rows = _rows(
        [
            ("نوع البيانات", "أسعار إيثيريوم حقيقية لشمعة 4 ساعات (بينانس)", ""),
            ("عدد الشمعات", f"{status['candles']:,}", ""),
            ("من", status["from"], ""),
            ("إلى", status["to"], ""),
            (
                "فجوات في بيانات بينانس نفسها",
                f"{status['gaps_text']} ({status['missing_text']} ناقصة)",
                "gold",
            ),
        ]
    )
    return f'<div class="card"><h2>البيانات اللي البوت بيقرا منها</h2>{rows}{examples}</div>'


def _monitor(summary: DashboardSummary) -> str:
    status = summary.scheduler_status
    status_class = "green" if status["status"] in {"RUNNING", "SUCCESS"} else "red"
    points = summary.equity_curve
    chart = ""
    if len(points) >= 2:
        values = [float(point["equity"]) for point in points]
        low, high = min(values), max(values)
        span = max(high - low, 0.01)
        coords = []
        width, height = 900, 230
        for index, value in enumerate(values):
            x = 20 + (index / (len(values) - 1)) * (width - 40)
            y = 20 + (1 - ((value - low) / span)) * (height - 40)
            coords.append(f"{x:.1f},{y:.1f}")
        chart = (
            '<div class="chart-wrap"><svg viewBox="0 0 900 230" role="img" '
            'aria-label="منحنى تطور رأس المال">'
            '<polyline fill="none" stroke="var(--blue)" stroke-width="3" points="'
            + " ".join(coords)
            + '"/></svg></div>'
            f'<div class="chart-caption">من {values[0]:,.2f} دولار إلى {values[-1]:,.2f} دولار</div>'
        )
    else:
        chart = '<p class="muted">لسه مفيش صفقات كفاية لعرض منحنى رأس المال.</p>'
    rows = _rows(
        [
            ("حالة الـ Scheduler", status["status_ar"], status_class),
            ("آخر دورة", status["last_run"], ""),
            ("الشمعات المعالجة في آخر دورة", str(status["candles_processed"]), ""),
        ]
    )
    return (
        '<div class="card"><h2>حالة البوت ومنحنى رأس المال</h2>'
        + rows
        + chart
        + "</div>"
    )


def _strategy(summary: DashboardSummary) -> str:
    rules = "".join(f"<li>{escape(rule)}</li>" for rule in summary.strategy["rules"])
    return (
        '<div class="card"><h2>إزاي البوت بياخد قراره؟ (بالعربي المفهوم)</h2>'
        f"<ul class='rules'>{rules}</ul>"
        "<p class='small'>الإعدادات مربوطة ومقفولة (نسخة "
        f"{escape(summary.strategy['version'])}). أي تغيير فيها بيتسجّل، والقرارات القديمة "
        "تفضل مربوطة بنسختها الأصلية.</p></div>"
    )


def _notes(summary: DashboardSummary) -> str:
    items = "".join(f"<li>{escape(note)}</li>" for note in summary.notes)
    return f'<div class="card"><h2>ملاحظات مهمة</h2><ul class="notes">{items}</ul></div>'


def render_dashboard(summary: DashboardSummary) -> str:
    """Return the complete HTML document for the dashboard."""
    title = "متابعة بوت التداول — إيثيريوم"
    outcome = "ربح" if summary.portfolio["up"] else "خسارة"
    return f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<style>{STYLE}</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <div>
      <h1>{escape(title)}</h1>
      <p class="sub">بيانات حقيقية من بينانس — شمعة كل 4 ساعات — من
        {escape(summary.data_status["from"].split("،")[0])} إلى
        {escape(summary.data_status["to"].split("،")[0])}</p>
    </div>
    <div>
      <span class="pill paper">وضع تجريبي: أموال وهمية، مفيش أوامر حقيقية</span>
      <span class="pill">آخر تحديث: {escape(summary.generated_at_cairo)}</span>
    </div>
  </header>

  {_banner(summary)}
  {_answers(summary)}

  <div class="refresh-bar">
    <span><span class="refresh-dot"></span>التحديث التلقائي كل 60 ثانية</span>
    <button type="button" onclick="location.reload()">تحديث الآن</button>
  </div>

  <div class="grid two">
    {_current(summary)}
    {_portfolio(summary)}
  </div>

  <div class="grid two">
    {_monitor(summary)}
    {_statistics(summary)}
    {_data_status(summary)}
  </div>

  {_recent_trades(summary)}
  <div class="grid two">
    {_strategy(summary)}
    {_notes(summary)}
  </div>

  <p class="foot">
    هذه اللوحة تعرض نتائج تشغيل حقيقي للمحرك على بيانات تاريخية.
    الرصيد النهائي {escape(summary.portfolio["equity"])} دولار
    ({escape(outcome)} {escape(summary.portfolio["profit_pct"])} مقارنة برأس المال الأصلي).
    بلا شبكة، بلا مفاتيح API، بلا تنفيذ حقيقي.
  </p>
</div>
<script>
  // The dashboard is read-only. Reloading only asks the server for fresh stored data.
  window.setInterval(function () {{ window.location.reload(); }}, 60000);
</script>
</body>
</html>
"""


def render_empty(message: str) -> str:
    """A tiny, honest page for when there is nothing to show yet."""
    return f"""<!DOCTYPE html>
<html lang="ar" dir="rtl"><head><meta charset="utf-8">
<title>متابعة بوت التداول</title><style>{STYLE}</style></head>
<body><div class="wrap">
<header class="top"><h1>متابعة بوت التداول</h1>
<span class="pill paper">وضع تجريبي: أموال وهمية، مفيش أوامر حقيقية</span></header>
<div class="banner warn">{escape(message)}</div>
<p class="muted">لتحضير البيانات: <code>python scripts/ingest_archive.py</code>
ثم <code>python scripts/replay.py</code></p>
</div></body></html>
"""


__all__ = ["render_dashboard", "render_empty", "STYLE"]
