"""Application settings (pydantic-settings) — the single source of truth.

Every value that changes behaviour is validated here, and the *safety* values
cannot even be constructed in an unsafe state:

* ``trading_mode`` is typed as :class:`TradingMode`, whose only member is
  ``paper`` — so ``TRADING_MODE=live`` fails at *parse* time.
* ``enable_live_trading=true`` fails at *validation* time.
* :func:`app.common.safety.enforce_paper_only` re-checks the built object at
  startup and in tests.

Cross-field invariants (all enforced, all tested):
* ``atr_tp_multiplier > atr_sl_multiplier`` — a strategy whose reward is below
  its risk cannot be profitable after fees; refuse it up front.
* ``warmup_candles >= max(ema_period, atr_period)`` — the strategy must be
  numerically warm before its first signal (no NaNs leaking into a decision).
* ``enable_ai_shadow`` requires a non-disabled provider, and the gemini
  provider requires an API key.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.common.enums import Timeframe, TradingMode

LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
DB_SCHEMES = ("sqlite://", "postgresql://", "postgresql+psycopg://")


class Settings(BaseSettings):
    """Validated runtime configuration. Construct once via :func:`get_settings`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        validate_default=True,
    )

    # ------------------------------------------------------------------ safety
    trading_mode: TradingMode = TradingMode.PAPER
    enable_live_trading: bool = False

    # -------------------------------------------------------------- instrument
    symbol: str = "ETHUSDT"
    timeframe: Timeframe = Timeframe.H4

    # ------------------------------------------------------ strategy (frozen)
    strategy_name: str = "EMA200_ATR_BASELINE"
    strategy_version: str = "1.0.0"
    ema_period: int = Field(default=200, ge=2, le=2000)
    atr_period: int = Field(default=14, ge=2, le=500)
    atr_sl_multiplier: Decimal = Field(default=Decimal("1.5"), gt=Decimal("0"))
    atr_tp_multiplier: Decimal = Field(default=Decimal("3.0"), gt=Decimal("0"))
    max_position_pct: Decimal = Field(default=Decimal("0.10"), gt=Decimal("0"), le=Decimal("1"))
    min_confidence: Decimal = Field(default=Decimal("0.60"), ge=Decimal("0"), le=Decimal("1"))
    warmup_candles: int = Field(default=200, ge=1, le=100_000)
    fee_rate: Decimal = Field(default=Decimal("0.001"), ge=Decimal("0"), le=Decimal("0.05"))
    starting_balance: Decimal = Field(default=Decimal("200"), gt=Decimal("0"))

    # -------------------------------------------------------------------- ai
    enable_ai_shadow: bool = False
    ai_provider: Literal["disabled", "gemini", "local"] = "disabled"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    ai_timeout_seconds: float = Field(default=15.0, gt=0, le=120)

    # --------------------------------------------------------------- storage
    database_url: str = "sqlite:///./data/trading.db"

    # ------------------------------------------------------------------- api
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)
    log_level: str = "INFO"

    # ------------------------------------------------------------ validators
    @field_validator("symbol")
    @classmethod
    def _validate_symbol(cls, v: str) -> str:
        v = v.strip().upper()
        if not v.isalnum():
            raise ValueError("symbol must be alphanumeric (e.g. ETHUSDT)")
        if len(v) < 6:
            raise ValueError("symbol looks too short for a spot pair (e.g. ETHUSDT)")
        return v

    @field_validator("strategy_name", "strategy_version")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be empty")
        return v

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        v = v.strip().upper()
        if v not in LOG_LEVELS:
            raise ValueError(f"log_level must be one of {sorted(LOG_LEVELS)}")
        return v

    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith(DB_SCHEMES):
            raise ValueError(f"database_url must start with one of {DB_SCHEMES}")
        return v

    @model_validator(mode="after")
    def _safety_guard(self) -> Settings:
        """HARD SAFETY GUARD (master prompt §39/§40) — refuse to exist when unsafe."""
        if self.trading_mode != TradingMode.PAPER:
            raise ValueError(
                f"TRADING_MODE must be 'paper' (got {self.trading_mode!r}); "
                "live mode is not implemented in this build."
            )
        if self.enable_live_trading:
            raise ValueError("ENABLE_LIVE_TRADING must be false; live trading is not implemented.")
        return self

    @model_validator(mode="after")
    def _strategy_coherence(self) -> Settings:
        if self.atr_tp_multiplier <= self.atr_sl_multiplier:
            raise ValueError(
                "ATR_TP_MULTIPLIER must be greater than ATR_SL_MULTIPLIER "
                f"(got tp={self.atr_tp_multiplier}, sl={self.atr_sl_multiplier})"
            )
        required_warmup = max(self.ema_period, self.atr_period)
        if self.warmup_candles < required_warmup:
            raise ValueError(
                f"WARMUP_CANDLES must be >= max(EMA_PERIOD, ATR_PERIOD)={required_warmup} "
                f"(got {self.warmup_candles}); otherwise indicators emit NaN into signals"
            )
        return self

    @model_validator(mode="after")
    def _ai_coherence(self) -> Settings:
        if self.enable_ai_shadow and self.ai_provider == "disabled":
            raise ValueError("ENABLE_AI_SHADOW=true requires AI_PROVIDER != 'disabled'")
        if self.enable_ai_shadow and self.ai_provider == "gemini" and not self.gemini_api_key.strip():
            raise ValueError("AI_PROVIDER=gemini requires GEMINI_API_KEY")
        return self

    # ------------------------------------------------------------ projections
    @property
    def is_paper(self) -> bool:
        return self.trading_mode is TradingMode.PAPER and not self.enable_live_trading

    @property
    def ai_enabled(self) -> bool:
        return self.enable_ai_shadow and self.ai_provider != "disabled"

    def public_dict(self) -> dict[str, Any]:
        """Settings safe to expose over HTTP / write into logs (no secrets)."""
        return {
            "trading_mode": self.trading_mode.value,
            "enable_live_trading": self.enable_live_trading,
            "symbol": self.symbol,
            "timeframe": self.timeframe.value,
            "strategy_name": self.strategy_name,
            "strategy_version": self.strategy_version,
            "ema_period": self.ema_period,
            "atr_period": self.atr_period,
            "atr_sl_multiplier": str(self.atr_sl_multiplier),
            "atr_tp_multiplier": str(self.atr_tp_multiplier),
            "max_position_pct": str(self.max_position_pct),
            "min_confidence": str(self.min_confidence),
            "warmup_candles": self.warmup_candles,
            "fee_rate": str(self.fee_rate),
            "starting_balance": str(self.starting_balance),
            "enable_ai_shadow": self.enable_ai_shadow,
            "ai_provider": self.ai_provider,
            "gemini_model": self.gemini_model,
            "database_url": self.database_url,
            "log_level": self.log_level,
        }


_settings: Settings | None = None


def get_settings() -> Settings:
    """Cached settings accessor."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings_cache() -> Settings:
    """Drop the cache and rebuild from the current environment (test hook)."""
    global _settings
    _settings = None
    return get_settings()


def clear_settings_cache() -> None:
    """Drop the cache without rebuilding (used by test fixtures)."""
    global _settings
    _settings = None


# Backwards-compatible alias used by earlier drafts / external scripts.
reload_settings = reset_settings_cache

__all__ = [
    "DB_SCHEMES",
    "LOG_LEVELS",
    "Settings",
    "clear_settings_cache",
    "get_settings",
    "reload_settings",
    "reset_settings_cache",
]
