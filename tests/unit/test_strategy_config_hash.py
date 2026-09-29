"""Strategy config hashing: stability, sensitivity, and canonical form."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields

import pytest

from app.config.settings import Settings, clear_settings_cache
from app.config.strategy_config import (
    HASHED_FIELDS,
    StrategyConfig,
    load_frozen_strategy_config,
    strategy_config_from_settings,
)


def test_hash_is_stable_and_deterministic() -> None:
    a = load_frozen_strategy_config()
    b = load_frozen_strategy_config()
    assert a.config_hash == b.config_hash
    assert len(a.config_hash) == 64
    assert all(c in "0123456789abcdef" for c in a.config_hash)


def test_hash_is_reproducible_across_process_restart_like_reload() -> None:
    first = load_frozen_strategy_config().config_hash
    clear_settings_cache()
    second = load_frozen_strategy_config().config_hash
    assert first == second


def test_hash_changes_when_any_hashed_parameter_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    baseline = load_frozen_strategy_config().config_hash
    monkeypatch.setenv("EMA_PERIOD", "150")
    monkeypatch.setenv("WARMUP_CANDLES", "150")
    clear_settings_cache()
    assert load_frozen_strategy_config().config_hash != baseline


def test_version_bump_changes_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    baseline = load_frozen_strategy_config().config_hash
    monkeypatch.setenv("STRATEGY_VERSION", "1.0.1")
    clear_settings_cache()
    assert load_frozen_strategy_config().config_hash != baseline


def test_decimal_formatting_does_not_affect_hash() -> None:
    """Decimal('1.50') must hash exactly like Decimal('1.5')."""
    base = Settings()
    variant = Settings(
        atr_sl_multiplier="1.50",  # type: ignore[arg-type]
        fee_rate="0.001000",  # type: ignore[arg-type]
    )
    a = strategy_config_from_settings(base)
    b = strategy_config_from_settings(variant)
    assert a.as_dict()["atr_sl_multiplier"] == b.as_dict()["atr_sl_multiplier"] == "1.5"
    assert a.config_hash == b.config_hash


def test_symbol_change_changes_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    baseline = load_frozen_strategy_config().config_hash
    monkeypatch.setenv("SYMBOL", "BTCUSDT")
    clear_settings_cache()
    assert load_frozen_strategy_config().config_hash != baseline


def test_timeframe_change_changes_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    baseline = load_frozen_strategy_config().config_hash
    monkeypatch.setenv("TIMEFRAME", "1h")
    clear_settings_cache()
    assert load_frozen_strategy_config().config_hash != baseline


def test_hashed_fields_and_dataclass_agree() -> None:
    declared = {f.name for f in fields(StrategyConfig)}
    assert set(HASHED_FIELDS) == declared


def test_config_is_frozen() -> None:
    cfg = load_frozen_strategy_config()
    with pytest.raises(FrozenInstanceError):
        cfg.ema_period = 5  # type: ignore[misc]


def test_self_consistency_check_rejects_bad_config() -> None:
    cfg = StrategyConfig(
        name="X",
        version="1.0.0",
        symbol="ETHUSDT",
        timeframe="4h",
        ema_period=200,
        atr_period=14,
        atr_sl_multiplier="3.0",
        atr_tp_multiplier="1.0",
        max_position_pct="0.1",
        min_confidence="0.6",
        warmup_candles=200,
        fee_rate="0.001",
    )
    with pytest.raises(ValueError):
        cfg.assert_self_consistent()


def test_to_json_dict_is_complete() -> None:
    cfg = load_frozen_strategy_config()
    payload = cfg.to_json_dict()
    assert set(payload) == {f.name for f in fields(StrategyConfig)}
    assert payload["name"] == "EMA200_ATR_BASELINE"


def test_known_baseline_hash_is_pinned() -> None:
    """Guard against silent baseline drift.

    If this fails, a baseline parameter changed. That is allowed, but it must be
    a conscious decision: bump STRATEGY_VERSION first, then update the pin.
    """
    cfg = load_frozen_strategy_config()
    assert cfg.config_hash == "a7521277083fd94aca06a828f8374329e95892a94fe6ef23f238db62c70b4d13"
    assert cfg.as_dict() == {
        "name": "EMA200_ATR_BASELINE",
        "version": "1.0.0",
        "symbol": "ETHUSDT",
        "timeframe": "4h",
        "ema_period": 200,
        "atr_period": 14,
        "atr_sl_multiplier": "1.5",
        "atr_tp_multiplier": "3",
        "max_position_pct": "0.1",
        "min_confidence": "0.6",
        "warmup_candles": 200,
        "fee_rate": "0.001",
    }
