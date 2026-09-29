"""Frozen strategy configuration + config hashing.

The hash of this dataclass is the identity of "the strategy build". It is
stored on every signal, order, position and trade, and registered in
``strategy_versions``. If any parameter changes, the hash changes, and the
resulting signals/orders/trades are a *different* dataset — which is exactly
what makes walk-forward and A/B comparison auditable.

Hashing rules (why the original draft was right to store Decimals as strings):
* Decimals are canonicalised to their normalised form (``1.500`` -> ``"1.5"``)
  so ``Decimal("1.5")`` and ``Decimal("1.50")`` hash identically — the config
  hash must depend on *value*, not on how someone typed it.
* Keys are sorted and the JSON is whitespace-free: adding a field changes the
  hash deterministically and reproducibly on any machine/Python version.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from decimal import Decimal
from typing import Any

from app.common.hashing import canonical_decimal
from app.common.hashing import config_hash as _config_hash
from app.config.settings import Settings, get_settings

#: Fields that participate in the hash. Ordering here is irrelevant (keys are
#: sorted) but the *set* is frozen: adding a field is a breaking change and
#: must be accompanied by a strategy_version bump.
HASHED_FIELDS: tuple[str, ...] = (
    "name",
    "version",
    "symbol",
    "timeframe",
    "ema_period",
    "atr_period",
    "atr_sl_multiplier",
    "atr_tp_multiplier",
    "max_position_pct",
    "min_confidence",
    "warmup_candles",
    "fee_rate",
)


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    """Immutable, hashable baseline strategy parameters."""

    name: str
    version: str
    symbol: str
    timeframe: str
    ema_period: int
    atr_period: int
    # Decimals are stored as canonical strings so hashing is stable across
    # Python/pydantic versions and immune to float repr differences.
    atr_sl_multiplier: str
    atr_tp_multiplier: str
    max_position_pct: str
    min_confidence: str
    warmup_candles: int
    fee_rate: str

    def __post_init__(self) -> None:
        """Normalise decimal strings so the hash depends on value, not spelling."""
        for field_name in (
            "atr_sl_multiplier",
            "atr_tp_multiplier",
            "max_position_pct",
            "min_confidence",
            "fee_rate",
        ):
            object.__setattr__(self, field_name, canonical_decimal(getattr(self, field_name)))

    def as_dict(self) -> dict[str, Any]:
        """Dict used for hashing (only :data:`HASHED_FIELDS`, sorted keys later)."""
        raw = asdict(self)
        return {field_name: raw[field_name] for field_name in HASHED_FIELDS}

    def to_json_dict(self) -> dict[str, Any]:
        """Full record persisted into ``strategy_versions.config``."""
        return asdict(self)

    @property
    def config_hash(self) -> str:
        return _config_hash(self.as_dict())

    @property
    def sl_multiplier(self) -> Decimal:
        return Decimal(self.atr_sl_multiplier)

    @property
    def tp_multiplier(self) -> Decimal:
        return Decimal(self.atr_tp_multiplier)

    @property
    def fee_rate_decimal(self) -> Decimal:
        return Decimal(self.fee_rate)

    @property
    def min_confidence_decimal(self) -> Decimal:
        return Decimal(self.min_confidence)

    @property
    def max_position_pct_decimal(self) -> Decimal:
        return Decimal(self.max_position_pct)

    def assert_self_consistent(self) -> None:
        """Guard against a hand-built config that violates the invariants."""
        if self.tp_multiplier <= self.sl_multiplier:
            raise ValueError("tp multiplier must exceed sl multiplier")
        if self.warmup_candles < max(self.ema_period, self.atr_period):
            raise ValueError("warmup_candles must cover the longest indicator")


def strategy_config_from_settings(settings: Settings) -> StrategyConfig:
    return StrategyConfig(
        name=settings.strategy_name,
        version=settings.strategy_version,
        symbol=settings.symbol,
        timeframe=settings.timeframe.value,
        ema_period=settings.ema_period,
        atr_period=settings.atr_period,
        atr_sl_multiplier=canonical_decimal(settings.atr_sl_multiplier),
        atr_tp_multiplier=canonical_decimal(settings.atr_tp_multiplier),
        max_position_pct=canonical_decimal(settings.max_position_pct),
        min_confidence=canonical_decimal(settings.min_confidence),
        warmup_candles=settings.warmup_candles,
        fee_rate=canonical_decimal(settings.fee_rate),
    )


def load_frozen_strategy_config() -> StrategyConfig:
    """Build the frozen baseline config from current settings."""
    config = strategy_config_from_settings(get_settings())
    config.assert_self_consistent()
    return config


def hash_matches_previous(config: StrategyConfig, expected_hash: str) -> bool:
    """Used by startup to detect an accidental parameter change."""
    return config.config_hash == expected_hash


__all__ = [
    "HASHED_FIELDS",
    "StrategyConfig",
    "hash_matches_previous",
    "load_frozen_strategy_config",
    "strategy_config_from_settings",
]

# Fail loudly at import time if the field set and the dataclass drift apart.
_declared = {f.name for f in fields(StrategyConfig)}
_missing = set(HASHED_FIELDS) - _declared
if _missing:  # pragma: no cover - import-time guard
    raise RuntimeError(f"HASHED_FIELDS references unknown fields: {sorted(_missing)}")
