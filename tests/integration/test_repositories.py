"""Repository behaviour: idempotency, severity policy, payload hygiene."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.config.strategy_config import load_frozen_strategy_config
from app.events.bus import EventEnvelope
from app.integrity.enums import EventType, IntegrityCode
from app.models import ConfigurationVersion, StrategyVersion
from app.repositories import configuration_versions, integrity_events, strategy_versions, system_events

UTC = UTC
TS = datetime(2026, 1, 1, 4, tzinfo=UTC)


def test_strategy_registration_is_idempotent(session: Session) -> None:
    config = load_frozen_strategy_config()
    first, created_first = strategy_versions.ensure_registered(
        session, config, now=TS, software_version="0.1.0"
    )
    session.commit()
    second, created_second = strategy_versions.ensure_registered(
        session, config, now=TS, software_version="0.1.0"
    )
    session.commit()

    assert created_first is True
    assert created_second is False
    assert first.id == second.id
    assert session.query(StrategyVersion).count() == 1
    assert first.config_hash == config.config_hash


def test_strategy_registration_records_the_full_config(session: Session) -> None:
    config = load_frozen_strategy_config()
    row, _ = strategy_versions.ensure_registered(session, config, now=TS, software_version="0.1.0")
    session.commit()
    assert row.config["ema_period"] == 200
    assert row.config["atr_sl_multiplier"] == "1.5"
    assert strategy_versions.by_hash(session, config.config_hash) is not None
    assert strategy_versions.by_hash(session, "deadbeef") is None


def test_config_registration_strips_secrets(session: Session) -> None:
    payload = {"symbol": "ETHUSDT", "gemini_api_key": "super-secret", "log_level": "INFO"}
    row, created = configuration_versions.ensure_registered(
        session, config=payload, now=TS, software_version="0.1.0"
    )
    session.commit()
    assert created is True
    assert "gemini_api_key" not in row.config
    assert row.config["symbol"] == "ETHUSDT"
    latest = configuration_versions.latest(session)
    assert latest is not None and latest.config_hash == row.config_hash
    assert session.query(ConfigurationVersion).count() == 1


def test_event_recording_is_idempotent_on_event_id(session: Session) -> None:
    envelope = EventEnvelope(
        event_type=EventType.CANDLE_VALIDATED.value,
        timestamp=TS,
        entity_id="CDL::ETHUSDT::4h::2026-01-01T00:00:00Z",
        symbol="ETHUSDT",
        timeframe="4h",
        payload={"close": "2505.0"},
    )
    first = system_events.record_event(session, envelope, software_version="0.1.0")
    session.commit()
    second = system_events.record_event(session, envelope, software_version="0.1.0")
    session.commit()
    assert first.event_id == second.event_id
    assert system_events.count(session) == 1


def test_large_event_payload_is_truncated(session: Session) -> None:
    envelope = EventEnvelope(
        event_type=EventType.CANDLE_RECEIVED.value,
        timestamp=TS,
        payload={"blob": "x" * 50_000},
    )
    row = system_events.record_event(session, envelope, software_version="0.1.0")
    session.commit()
    assert row.payload.get("_truncated") is True
    assert row.payload["_original_bytes"] > 16_384


def test_integrity_severity_policy(session: Session) -> None:
    fatal = integrity_events.record_violation(
        session,
        code=IntegrityCode.LOOKAHEAD_DETECTED,
        detected_at=TS,
        software_version="0.1.0",
        entity_id="SIG::x",
    )
    benign = integrity_events.record_violation(
        session,
        code=IntegrityCode.DUPLICATE_CANDLE,
        detected_at=TS,
        software_version="0.1.0",
        entity_id="CDL::x",
    )
    session.commit()
    assert fatal.severity == "CRITICAL"
    assert benign.severity == "ERROR"
    assert integrity_events.has_fatal(session) is True
    assert integrity_events.count(session) == 2
    assert integrity_events.count(session, code=IntegrityCode.DUPLICATE_CANDLE.value) == 1
    assert len(integrity_events.recent(session)) == 2


def test_event_payload_can_hold_decimals_and_dates(session: Session) -> None:
    row = system_events.record_simple(
        session,
        event_type=EventType.ORDER_CREATED.value,
        timestamp=TS,
        software_version="0.1.0",
        entity_id="ORD::x",
        payload={"quantity": str(Decimal("0.005")), "when": TS.isoformat()},
    )
    session.commit()
    assert row.payload["quantity"] == "0.005"
