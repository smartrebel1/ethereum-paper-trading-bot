"""Row factories shared by the integration tests.

Every factory returns *valid* data, so a test that expects a database error has
to mutate exactly one field — making the failing invariant unambiguous.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.common.ids import (
    candle_id as make_candle_id,
)
from app.common.ids import (
    execution_id as make_execution_id,
)
from app.common.ids import (
    order_id as make_order_id,
)
from app.common.ids import (
    position_id as make_position_id,
)
from app.common.ids import (
    signal_id as make_signal_id,
)
from app.common.ids import (
    snapshot_id as make_snapshot_id,
)
from app.common.ids import (
    trade_id as make_trade_id,
)

UTC = UTC
BASE_OPEN = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
SYMBOL = "ETHUSDT"
TIMEFRAME = "4h"
STRATEGY = "EMA200_ATR_BASELINE"
VERSION = "1.0.0"
CONFIG_HASH = "a" * 64


def next_open(open_time: datetime, hours: int = 4) -> datetime:
    return open_time + timedelta(hours=hours)


def candle_row(**overrides: Any) -> dict[str, Any]:
    open_time: datetime = overrides.pop("open_time", BASE_OPEN)
    tf = overrides.pop("timeframe", TIMEFRAME)
    symbol = overrides.pop("symbol", SYMBOL)
    hours = int(tf.rstrip("hdm")) if tf.endswith("h") else 4
    row: dict[str, Any] = {
        "candle_id": make_candle_id(symbol, tf, open_time),
        "symbol": symbol,
        "timeframe": tf,
        "open_time": open_time,
        "close_time": open_time + timedelta(hours=hours),
        "open": Decimal("2500.00000000"),
        "high": Decimal("2510.00000000"),
        "low": Decimal("2490.00000000"),
        "close": Decimal("2505.00000000"),
        "volume": Decimal("1234.5000000000"),
        "is_complete": True,
        "source": "test",
        "received_at": open_time + timedelta(hours=hours),
        "validated_at": open_time + timedelta(hours=hours),
    }
    row.update(overrides)
    return row


def signal_row(**overrides: Any) -> dict[str, Any]:
    open_time: datetime = overrides.pop("signal_candle_open_time", BASE_OPEN)
    target: datetime = overrides.pop("target_execution_open_time", next_open(open_time))
    tf = overrides.pop("timeframe", TIMEFRAME)
    symbol = overrides.pop("symbol", SYMBOL)
    version = overrides.pop("strategy_version", VERSION)
    config_hash = overrides.pop("strategy_config_hash", CONFIG_HASH)
    row: dict[str, Any] = {
        "signal_id": make_signal_id(symbol, tf, open_time, STRATEGY, version, config_hash),
        "symbol": symbol,
        "timeframe": tf,
        "action": "BUY",
        "confidence": Decimal("0.75000000"),
        "signal_candle_open_time": open_time,
        "signal_candle_close_time": target,
        "target_execution_open_time": target,
        "target_execution_candle_id": make_candle_id(symbol, tf, target),
        "strategy_name": STRATEGY,
        "strategy_version": version,
        "strategy_config_hash": config_hash,
        "decision_version": "1",
        "indicator_context": {"ema200": "2499.5", "atr14": "31.2"},
        "reason": "close above EMA200 with positive slope",
        "generation_timestamp": target,
        "created_at": target,
    }
    row.update(overrides)
    return row


def order_row(signal: dict[str, Any] | None = None, **overrides: Any) -> dict[str, Any]:
    sig = signal or signal_row()
    target = sig["target_execution_open_time"]
    row: dict[str, Any] = {
        "order_id": make_order_id(sig["signal_id"]),
        "signal_id": sig["signal_id"],
        "symbol": sig["symbol"],
        "timeframe": sig["timeframe"],
        "side": "BUY",
        "order_type": "MARKET_ON_OPEN",
        "purpose": "ENTRY",
        "target_execution_open_time": target,
        "target_execution_candle_id": sig["target_execution_candle_id"],
        "intended_quantity": Decimal("0.0050000000"),
        "intended_notional": Decimal("12.50000000"),
        "stop_loss_price": Decimal("2450.00000000"),
        "take_profit_price": Decimal("2620.00000000"),
        "state": "PENDING",
        "execution_provider": "paper",
        "created_at": target,
        "state_updated_at": target,
        "state_reason": "created",
    }
    row.update(overrides)
    return row


def execution_row(order: dict[str, Any] | None = None, **overrides: Any) -> dict[str, Any]:
    ordr = order or order_row()
    target = ordr["target_execution_open_time"]
    row: dict[str, Any] = {
        "execution_id": make_execution_id(ordr["order_id"], target),
        "order_id": ordr["order_id"],
        "symbol": ordr["symbol"],
        "timeframe": ordr["timeframe"],
        "side": "BUY",
        "execution_candle_open_time": target,
        "execution_candle_id": ordr["target_execution_candle_id"],
        "raw_open_price": Decimal("2505.00000000"),
        "slippage_bps": Decimal("0E-8"),
        "actual_execution_price": Decimal("2505.00000000"),
        "quantity": Decimal("0.0050000000"),
        "notional": Decimal("12.52500000"),
        "fee": Decimal("0.01252500"),
        "execution_timestamp": target,
        "provider": "paper",
    }
    row.update(overrides)
    return row


def position_row(order: dict[str, Any] | None = None, **overrides: Any) -> dict[str, Any]:
    ordr = order or order_row()
    row: dict[str, Any] = {
        "position_id": make_position_id(ordr["symbol"], ordr["timeframe"], ordr["order_id"]),
        "symbol": ordr["symbol"],
        "timeframe": ordr["timeframe"],
        "side": "BUY",
        "state": "OPEN",
        "quantity": Decimal("0.0050000000"),
        "entry_price": Decimal("2505.00000000"),
        "entry_time": ordr["target_execution_open_time"],
        "entry_candle_open_time": ordr["target_execution_open_time"],
        "entry_order_id": ordr["order_id"],
        "signal_id": ordr["signal_id"],
        "stop_loss": Decimal("2450.00000000"),
        "take_profit": Decimal("2620.00000000"),
        "entry_fee": Decimal("0.01252500"),
        "unrealized_pnl": Decimal("0.00000000"),
        "realized_pnl": Decimal("0.00000000"),
        "strategy_version": VERSION,
        "strategy_config_hash": CONFIG_HASH,
        "created_at": ordr["target_execution_open_time"],
        "updated_at": ordr["target_execution_open_time"],
        "closed_at": None,
        "close_reason": None,
    }
    row.update(overrides)
    return row


def trade_row(position: dict[str, Any] | None = None, **overrides: Any) -> dict[str, Any]:
    pos = position or position_row()
    exit_time = pos["entry_time"] + timedelta(hours=8)
    row: dict[str, Any] = {
        "trade_id": make_trade_id(pos["position_id"]),
        "position_id": pos["position_id"],
        "symbol": pos["symbol"],
        "timeframe": pos["timeframe"],
        "signal_id": pos["signal_id"],
        "entry_order_id": pos["entry_order_id"],
        "exit_order_id": None,
        "side": "BUY",
        "quantity": pos["quantity"],
        "entry_price": pos["entry_price"],
        "exit_price": Decimal("2620.00000000"),
        "entry_time": pos["entry_time"],
        "exit_time": exit_time,
        "holding_seconds": 8 * 3600,
        "entry_fee": pos["entry_fee"],
        "exit_fee": Decimal("0.01310000"),
        "gross_pnl": Decimal("0.57500000"),
        "net_pnl": Decimal("0.54937500"),
        "exit_reason": "TP_HIT",
        "strategy_version": pos["strategy_version"],
        "strategy_config_hash": pos["strategy_config_hash"],
        "created_at": exit_time,
    }
    row.update(overrides)
    return row


def snapshot_row(**overrides: Any) -> dict[str, Any]:
    at = overrides.pop("at_candle_open_time", BASE_OPEN)
    row: dict[str, Any] = {
        "snapshot_id": make_snapshot_id(SYMBOL, TIMEFRAME, at),
        "symbol": SYMBOL,
        "timeframe": TIMEFRAME,
        "at_candle_open_time": at,
        "cash": Decimal("187.47498000"),
        "reserved": Decimal("0.00000000"),
        "open_position_value": Decimal("12.52500000"),
        "unrealized_pnl": Decimal("0.00000000"),
        "realized_pnl_cum": Decimal("0.00000000"),
        "fees_cum": Decimal("0.01252500"),
        "equity": Decimal("200.00000000"),
        "peak_equity": Decimal("200.00000000"),
        "drawdown_pct": Decimal("0.00000000"),
        "created_at": at,
    }
    row.update(overrides)
    return row


__all__ = [
    "BASE_OPEN",
    "CONFIG_HASH",
    "SYMBOL",
    "TIMEFRAME",
    "candle_row",
    "execution_row",
    "next_open",
    "order_row",
    "position_row",
    "signal_row",
    "snapshot_row",
    "trade_row",
]
