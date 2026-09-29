"""HTTP API surface (read-only + health)."""

from __future__ import annotations

from app import SOFTWARE_VERSION
from app.config.strategy_config import load_frozen_strategy_config


def test_root_advertises_paper_only(api_client) -> None:
    payload = api_client.get("/").json()
    assert payload["paper_only"] is True
    assert "PAPER ONLY" in payload["notice"]
    assert "/health" in payload["endpoints"]


def test_root_redirects_browsers_to_the_dashboard(api_client) -> None:
    browser = api_client.get(
        "/", headers={"accept": "text/html,application/xhtml+xml"}, follow_redirects=False
    )
    assert browser.status_code == 307
    assert browser.headers["location"] == "/dashboard"
    # And the redirect target really is the dashboard.
    assert api_client.get("/dashboard").status_code == 200
    tool = api_client.get("/", headers={"accept": "*/*"})
    assert tool.status_code == 200
    assert tool.json()["paper_only"] is True


def test_health(api_client) -> None:
    response = api_client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "OK"
    assert payload["mode"] == "paper"
    assert payload["paper_only"] is True
    assert payload["software_version"] == SOFTWARE_VERSION
    assert payload["symbol"] == "ETHUSDT"
    assert payload["timeframe"] == "4h"
    assert payload["strategy_config_hash"] == load_frozen_strategy_config().config_hash


def test_ready_after_migration(api_client) -> None:
    payload = api_client.get("/ready").json()
    assert payload["ready"] is True
    checks = {c["name"]: c for c in payload["checks"]}
    assert set(checks) == {"safety_guard", "database", "schema"}
    assert all(c["ok"] for c in checks.values())


def test_version(api_client) -> None:
    payload = api_client.get("/version").json()
    assert payload["strategy_name"] == "EMA200_ATR_BASELINE"
    assert payload["mode"] == "paper"


def test_metrics_json(api_client) -> None:
    payload = api_client.get("/metrics").json()
    assert payload["mode"] == "paper"
    assert payload["paper_only"] is True
    # startup wrote one event; ledger tables are empty in phase 1.
    assert payload["counts"]["system_events"] >= 1
    assert payload["counts"]["candles"] == 0
    assert payload["integrity_critical_count"] == 0


def test_prometheus_metrics(api_client) -> None:
    response = api_client.get("/metrics/prometheus")
    assert response.status_code == 200
    body = response.text
    assert "text/plain" in response.headers["content-type"]
    assert "trading_bot_candles_total 0" in body
    assert "trading_bot_paper_only 1.0" in body
    assert "# TYPE trading_bot_open_position gauge" in body


def test_status_endpoint(api_client) -> None:
    payload = api_client.get("/api/v1/status").json()
    assert payload["symbol"] == "ETHUSDT"
    assert payload["paper_only"] is True
    assert payload["data"]["last_candle_open_time"] is None
    assert payload["pipeline"]["last_event_type"] == "STARTUP_GUARD_PASSED"
    assert payload["integrity_critical_count"] == 0


def test_config_endpoint_matches_the_frozen_hash(api_client) -> None:
    payload = api_client.get("/api/v1/config").json()
    strategy = load_frozen_strategy_config()
    assert payload["strategy_config_hash"] == strategy.config_hash
    assert payload["strategy"]["ema_period"] == 200
    assert payload["safety"]["ok"] is True
    assert "gemini_api_key" not in payload["settings"]
    assert payload["coverage"]["candles"] == 0


def test_read_endpoints_are_empty_but_valid(api_client) -> None:
    assert api_client.get("/api/v1/candles").json()["items"] == []
    assert api_client.get("/api/v1/signals").json()["items"] == []
    assert api_client.get("/api/v1/orders").json()["items"] == []
    assert api_client.get("/api/v1/trades").json()["items"] == []
    assert api_client.get("/api/v1/positions/open").json() == []
    assert api_client.get("/api/v1/portfolio/latest").json() is None
    assert api_client.get("/api/v1/portfolio/equity").json() == []
    assert api_client.get("/api/v1/integrity").json()["items"] == []


def test_events_endpoint_exposes_the_audit_trail(api_client) -> None:
    payload = api_client.get("/api/v1/events", params={"event_type": "STARTUP_GUARD_PASSED"}).json()
    assert payload["meta"]["returned"] == 1
    event = payload["items"][0]
    assert event["event_type"] == "STARTUP_GUARD_PASSED"
    assert event["payload"]["mode"] == "paper"
    assert event["software_version"] == SOFTWARE_VERSION


def test_dashboard_is_served_same_origin(api_client) -> None:
    """Two dashboards: the Arabic panel and the technical view, both offline-safe."""
    # Same-origin relative calls only (no CORS needed), and no external assets:
    # the pages must render even in an offline/sandboxed viewer.
    for path, marker in (
        ("/dashboard", "أموال وهمية"),
        ("/dashboard/technical", "Paper only"),
    ):
        response = api_client.get(path)
        assert response.status_code == 200, path
        assert response.headers["content-type"].startswith("text/html")
        body = response.text
        assert marker in body, f"{path} does not state the paper-only mode"
        assert 'fetch("http' not in body
        assert "fetch('http" not in body
        assert 'src="http' not in body and "src='http" not in body
        assert 'href="http' not in body
        assert "cdn." not in body


def test_openapi_schema_is_generated(api_client) -> None:
    schema = api_client.get("/openapi.json").json()
    assert schema["info"]["title"] == "ETHUSDT Paper Trading Engine"
    paths = set(schema["paths"])
    assert {"/health", "/ready", "/metrics", "/api/v1/status"} <= paths
    # No write endpoints exist in phase 1.
    assert all(method in {"get", "head", "options"} for path in schema["paths"].values() for method in path)


def test_candles_pagination_validation(api_client) -> None:
    assert api_client.get("/api/v1/candles", params={"limit": 0}).status_code == 422
    assert api_client.get("/api/v1/candles", params={"limit": 10_000}).status_code == 422
