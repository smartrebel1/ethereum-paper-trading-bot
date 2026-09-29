"""Settings validation + the safety guard."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.common.enums import ExecutionProvider, Timeframe, TradingMode
from app.config.settings import Settings, get_settings


def test_defaults_are_paper_and_ethusdt() -> None:
    s = Settings()
    assert s.trading_mode is TradingMode.PAPER
    assert s.enable_live_trading is False
    assert s.symbol == "ETHUSDT"
    assert s.timeframe is Timeframe.H4
    assert s.is_paper is True


def test_refuses_live_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_MODE", "live")
    with pytest.raises(ValidationError):
        Settings()


def test_refuses_live_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "true")
    with pytest.raises(ValidationError) as exc:
        Settings()
    assert "ENABLE_LIVE_TRADING" in str(exc.value)


def test_refuses_live_mode_even_with_valid_enum_spelling(monkeypatch: pytest.MonkeyPatch) -> None:
    # Defence in depth: even a *spelled* live mode is rejected.
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "1")
    monkeypatch.setenv("TRADING_MODE", "PAPER")
    with pytest.raises(ValidationError):
        Settings()


def test_tp_must_exceed_sl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATR_SL_MULTIPLIER", "3.0")
    monkeypatch.setenv("ATR_TP_MULTIPLIER", "1.5")
    with pytest.raises(ValidationError) as exc:
        Settings()
    assert "ATR_TP_MULTIPLIER" in str(exc.value)


def test_warmup_must_cover_indicators(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMA_PERIOD", "200")
    monkeypatch.setenv("WARMUP_CANDLES", "100")
    with pytest.raises(ValidationError) as exc:
        Settings()
    assert "WARMUP_CANDLES" in str(exc.value)


def test_symbol_is_normalised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYMBOL", " ethusdt ")
    assert Settings().symbol == "ETHUSDT"


@pytest.mark.parametrize("bad", ["ETH-USDT", "", "ETH"])
def test_symbol_rejects_invalid(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    monkeypatch.setenv("SYMBOL", bad)
    with pytest.raises(ValidationError):
        Settings()


def test_log_level_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "loud")
    with pytest.raises(ValidationError):
        Settings()
    monkeypatch.setenv("LOG_LEVEL", "debug")
    assert Settings().log_level == "DEBUG"


def test_database_url_scheme_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "mysql://nope")
    with pytest.raises(ValidationError):
        Settings()
    monkeypatch.setenv("DATABASE_URL", "postgresql://user@localhost/trading")
    assert Settings().database_url.startswith("postgresql://")


def test_ai_shadow_requires_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_AI_SHADOW", "true")
    monkeypatch.setenv("AI_PROVIDER", "disabled")
    with pytest.raises(ValidationError):
        Settings()


def test_gemini_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_AI_SHADOW", "true")
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    with pytest.raises(ValidationError):
        Settings()
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    assert Settings().ai_enabled is True


def test_bounds_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_POSITION_PCT", "1.5")
    with pytest.raises(ValidationError):
        Settings()
    monkeypatch.setenv("MAX_POSITION_PCT", "0.10")
    monkeypatch.setenv("FEE_RATE", "-1")
    with pytest.raises(ValidationError):
        Settings()


def test_decimals_stay_decimal() -> None:
    s = Settings()
    assert isinstance(s.fee_rate, Decimal)
    assert isinstance(s.starting_balance, Decimal)
    assert s.fee_rate == Decimal("0.001")


def test_public_dict_excludes_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "supersecret")
    payload = Settings().public_dict()
    assert "gemini_api_key" not in payload
    assert "supersecret" not in str(payload)


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()


def test_trading_mode_enum_has_no_live() -> None:
    assert {m.value for m in TradingMode} == {"paper"}
    assert {p.value for p in ExecutionProvider} == {"paper"}
