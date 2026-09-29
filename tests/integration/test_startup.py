"""Startup lifecycle: guard, registry writes, idempotency, graceful degradation."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.engine import Engine

from app.integrity.enums import EventType
from app.models import ConfigurationVersion, StrategyVersion, SystemEvent
from app.repositories import system_events


def test_lifespan_registers_strategy_and_config(api_client, migrated_db: Engine) -> None:
    from app.database.session import build_session_factory

    with build_session_factory(migrated_db)() as session:
        strategies = list(session.execute(select(StrategyVersion)).scalars())
        configs = list(session.execute(select(ConfigurationVersion)).scalars())
        events = {e.event_type for e in session.execute(select(SystemEvent)).scalars()}

    assert len(strategies) == 1
    assert len(configs) == 1
    assert strategies[0].name == "EMA200_ATR_BASELINE"
    assert strategies[0].config_hash
    assert EventType.STARTUP_GUARD_PASSED.value in events
    assert EventType.STRATEGY_VERSION_REGISTERED.value in events
    assert EventType.CONFIGURATION_REGISTERED.value in events


def test_startup_records_are_idempotent_across_restarts(api_client, migrated_db: Engine, db_url: str) -> None:
    """A second boot must not duplicate registry rows or guard events."""
    from fastapi.testclient import TestClient

    from app.config.settings import get_settings
    from app.database.session import build_session_factory
    from app.main import create_app

    app = create_app(get_settings())
    with TestClient(app) as second_client:
        assert second_client.get("/health").status_code == 200

    with build_session_factory(migrated_db)() as session:
        assert session.query(StrategyVersion).count() == 1
        assert session.query(ConfigurationVersion).count() == 1
        guard_events = [
            e
            for e in session.execute(select(SystemEvent)).scalars()
            if e.event_type == EventType.STARTUP_GUARD_PASSED.value
        ]

    # One per boot: the *event log* records every start (that is intentional and
    # desirable), while the registry tables stay deduplicated.
    assert len(guard_events) == 2
    assert len({e.event_id for e in guard_events}) == 2


def test_startup_guard_passed_event_carries_the_config_hash(api_client, migrated_db: Engine) -> None:
    from app.config.strategy_config import load_frozen_strategy_config
    from app.database.session import build_session_factory

    expected = load_frozen_strategy_config().config_hash
    with build_session_factory(migrated_db)() as session:
        rows = [
            e
            for e in session.execute(select(SystemEvent)).scalars()
            if e.event_type == EventType.STARTUP_GUARD_PASSED.value
        ]
    assert rows and rows[0].payload["strategy_config_hash"] == expected
    assert rows[0].payload["mode"] == "paper"


def test_startup_degrades_gracefully_without_schema(api_client_no_schema) -> None:
    """No DB yet: /health still answers, /ready is honest, /api/v1 is 503."""
    assert api_client_no_schema.get("/health").status_code == 200
    ready = api_client_no_schema.get("/ready").json()
    assert ready["ready"] is False
    checks = {c["name"]: c for c in ready["checks"]}
    assert checks["schema"]["ok"] is False
    assert "init_db" in checks["schema"]["detail"]

    blocked = api_client_no_schema.get("/api/v1/status")
    assert blocked.status_code == 503
    assert "init_db" in blocked.json()["detail"]


def test_unknown_event_type_is_not_in_the_catalog() -> None:
    from app.integrity.enums import ALL_EVENT_TYPES

    assert "LIVE_ORDER_SENT" not in ALL_EVENT_TYPES
    assert system_events.count  # module import sanity
