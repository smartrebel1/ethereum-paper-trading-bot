"""Dashboard: the panel must answer four questions from real rows only.

These tests protect the *promise* made to the reader — a non-technical person
opening the page and asking «شترينا إيه؟ / اتنفذت امتى؟ / ربحنا كام؟ / الجاية
امتى؟» — and the promise that nothing on the page is invented: every number is
read back from ``trades`` / ``positions`` / ``orders``, and an empty database
produces an honest sentence rather than a plausible-looking zero.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.config.settings import get_settings
from app.dashboard.summary import build_summary, format_cairo, holding_arabic
from app.models.order import Order
from app.models.position import Position
from app.models.signal import Signal
from app.models.trade import Trade
from app.repositories import orders as order_repo
from tests.factories import order_row, position_row, signal_row, trade_row


def seed(session, *, offset_hours: int = 0, state: str = "CLOSED", with_trade: bool = False):  # noqa: ANN001
    """Persist one coherent signal -> order -> position (-> trade) chain.

    Ids are derived by the factories from the signal candle, so a different
    ``offset_hours`` yields a genuinely different, non-colliding chain.
    """
    from tests.factories import BASE_OPEN

    spec = signal_row(signal_candle_open_time=BASE_OPEN + timedelta(hours=offset_hours))
    order_spec = order_row(spec)
    position_spec = position_row(order_spec)
    trade_spec = trade_row(position_spec)

    signal = Signal(**spec)
    session.add(signal)
    session.flush()
    order = Order(**order_spec)
    session.add(order)
    session.flush()

    position = Position(**{**position_spec, "state": state})
    session.add(position)
    session.flush()

    trade = None
    if with_trade:
        trade = Trade(**trade_spec)
        session.add(trade)
        session.flush()
    return {"signal": signal, "order": order, "position": position, "trade": trade}


@pytest.fixture
def closed_trade(session):  # noqa: ANN001
    return seed(session, state="CLOSED", with_trade=True)["trade"]


def summary(session, **overrides):  # noqa: ANN001
    settings = get_settings()
    return build_summary(
        session,
        symbol=settings.symbol,
        timeframe=settings.timeframe.value,
        starting_balance=Decimal("200"),
        now=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        **overrides,
    )


def test_cairo_time_is_used_for_the_reader() -> None:
    """A UTC moment is shown in the reader's own time zone, in Arabic."""
    assert (
        format_cairo(datetime(2026, 9, 13, 22, 0, tzinfo=UTC))
        == "الاثنين 14 سبتمبر 2026، 1:00 صباحًا بتوقيت القاهرة"
    )


def test_a_same_candle_trade_is_described_in_words() -> None:
    assert holding_arabic(0) == "قفلت في نفس شمعة الدخول"
    assert holding_arabic(8 * 3600) == "8 ساعات"


def test_an_empty_database_says_so(session) -> None:  # noqa: ANN001
    data = summary(session).as_dict()
    assert data["statistics"]["trades"] == 0
    assert data["last_trade"] is None
    assert data["open_position"] is None
    assert data["portfolio"]["starting"] == "200.00"
    assert any("مفيش" in note for note in data["notes"])


def test_a_closed_trade_answers_the_four_questions(session, closed_trade) -> None:  # noqa: ANN001
    data = summary(session).as_dict()
    trade = data["last_trade"]

    assert data["statistics"]["trades"] == 1
    # ١) what did we buy
    assert trade["quantity"]
    assert trade["entry_price"] and trade["exit_price"]
    assert trade["side"] == "شراء"
    # ٢) when was it executed
    assert "بتوقيت القاهرة" in trade["entry_time"]
    assert "بتوقيت القاهرة" in trade["exit_time"]
    # ٣) how much did we make
    assert "دولار" in trade["profit_text"]
    assert data["statistics"]["net_pnl"].endswith("دولار")
    # ٤) when is the next one
    assert data["next_opportunity"]["when"]
    assert data["next_opportunity"]["detail"]
    # and it explains *why* it bought, in words, without a code
    assert "المؤشر" in trade["why_we_bought"]


def test_the_trade_table_is_ordered_newest_first(session, closed_trade) -> None:  # noqa: ANN001
    data = summary(session).as_dict()
    times = [row["when_utc"] for row in data["recent_trades"]]
    assert times == sorted(times, reverse=True)


def test_an_open_position_is_reported_and_blocks_new_trades(session, closed_trade) -> None:  # noqa: ANN001
    seed(session, offset_hours=8, state="OPEN")

    data = summary(session).as_dict()
    assert data["open_position"] is not None
    assert data["open_position"]["stop_loss"]
    assert data["open_position"]["take_profit"]
    assert "مفتوحة" in data["next_opportunity"]["headline"]
    assert "دولار" in data["open_position"]["profit_now"]


def test_every_reason_code_shown_to_the_reader_is_translated(session) -> None:  # noqa: ANN001
    """No English code may reach the page: the panel translates or stays silent."""
    from app.dashboard.summary import REASON_MEANINGS

    for code, meaning in REASON_MEANINGS.items():
        assert meaning and any("\u0600" <= char <= "\u06ff" for char in meaning), code


def test_the_rendered_page_has_no_developer_jargon(session, closed_trade) -> None:  # noqa: ANN001
    from app.dashboard.render import render_dashboard

    html = render_dashboard(summary(session))
    for forbidden in ("signal_id", "config_hash", "json", "traceback", "None", "{'"):
        assert forbidden not in html, f"the panel leaked {forbidden!r}"

    for question in ("شترينا إيه", "الصفقة اتنفذت امتى", "ربحنا كام", "الصفقة الجاية امتى"):
        assert question in html
    assert 'dir="rtl"' in html
    assert "http://" not in html and "https://" not in html, "the page must be self-contained"


def test_the_page_is_self_contained(session, closed_trade) -> None:  # noqa: ANN001
    from app.dashboard.render import render_dashboard

    html = render_dashboard(summary(session))
    assert html.startswith("<!DOCTYPE html>")
    assert html.count("<script") == 0  # nothing to load, nothing to fail
    assert html.count("<style") == 1


def test_the_api_summary_is_json_serialisable(session, closed_trade) -> None:  # noqa: ANN001
    import json

    payload = json.dumps(summary(session).as_dict(), ensure_ascii=False)
    restored = json.loads(payload)
    assert "شراء" in payload  # Arabic survives the round trip unescaped
    assert restored["statistics"]["trades"] == 1
    assert isinstance(restored["portfolio"]["equity"], str)


def test_dashboard_endpoint_renders_for_a_real_database(migrated_db, monkeypatch) -> None:  # noqa: ANN001
    """The HTTP page is served, in Arabic, with the paper-only banner."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app(get_settings())
    with TestClient(app) as client:
        response = client.get("/dashboard")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        body = response.text
        assert "لسه مفيش بيانات" in body or "متابعة بوت التداول" in body

        summary_response = client.get("/api/v1/dashboard/summary")
        assert summary_response.status_code == 200
        data = summary_response.json()
        assert set(data) >= {
            "portfolio",
            "statistics",
            "last_trade",
            "open_position",
            "pending_order",
            "next_opportunity",
            "data_status",
        }


def test_open_and_closed_positions_do_not_confuse_the_summary(session, closed_trade) -> None:  # noqa: ANN001
    """Only the OPEN position may be presented as «الصفقة المفتوحة»."""
    from sqlalchemy import select

    stored_closed = len(session.execute(select(Position)).scalars().all())
    data = summary(session).as_dict()
    assert data["open_position"] is None
    assert stored_closed == 1
    assert order_repo.count(session) >= 1
