"""Pydantic response schemas for the read-only API.

Money/quantity fields are typed as ``Decimal`` on purpose: pydantic serialises
Decimals as JSON strings, so the dashboard never sees a float-rounded price.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


# --------------------------------------------------------------------- health
class HealthResponse(BaseModel):
    status: str = "OK"
    mode: str
    paper_only: bool = True
    software_version: str
    strategy_version: str
    strategy_config_hash: str
    symbol: str
    timeframe: str
    started_at: datetime
    timestamp: datetime


class ReadinessCheck(BaseModel):
    name: str
    ok: bool
    detail: str = ""


class ReadyResponse(BaseModel):
    ready: bool
    checks: list[ReadinessCheck]
    timestamp: datetime


class MetricsResponse(BaseModel):
    counts: dict[str, int]
    open_position: dict[str, Any] | None = None
    last_trade: dict[str, Any] | None = None
    last_event: dict[str, Any] | None = None
    integrity_critical_count: int = 0


# --------------------------------------------------------------- read models
class CandleOut(ORMModel):
    candle_id: str
    symbol: str
    timeframe: str
    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    is_complete: bool
    source: str


class SignalOut(ORMModel):
    signal_id: str
    symbol: str
    timeframe: str
    action: str
    confidence: Decimal
    signal_candle_open_time: datetime
    signal_candle_close_time: datetime
    target_execution_open_time: datetime
    target_execution_candle_id: str
    strategy_name: str
    strategy_version: str
    strategy_config_hash: str
    indicator_context: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    created_at: datetime


class OrderOut(ORMModel):
    order_id: str
    signal_id: str
    symbol: str
    timeframe: str
    side: str
    purpose: str
    state: str
    target_execution_open_time: datetime
    target_execution_candle_id: str
    intended_quantity: Decimal
    intended_notional: Decimal
    stop_loss_price: Decimal | None = None
    take_profit_price: Decimal | None = None
    execution_provider: str
    state_reason: str = ""
    created_at: datetime
    state_updated_at: datetime


class ExecutionOut(ORMModel):
    execution_id: str
    order_id: str
    symbol: str
    timeframe: str
    side: str
    execution_candle_open_time: datetime
    raw_open_price: Decimal
    slippage_bps: Decimal
    actual_execution_price: Decimal
    quantity: Decimal
    notional: Decimal
    fee: Decimal
    provider: str
    execution_timestamp: datetime


class PositionOut(ORMModel):
    position_id: str
    symbol: str
    timeframe: str
    side: str
    state: str
    quantity: Decimal
    entry_price: Decimal
    entry_time: datetime
    stop_loss: Decimal
    take_profit: Decimal
    entry_fee: Decimal
    unrealized_pnl: Decimal
    realized_pnl: Decimal
    strategy_version: str
    strategy_config_hash: str


class TradeOut(ORMModel):
    trade_id: str
    symbol: str
    timeframe: str
    side: str
    quantity: Decimal
    entry_price: Decimal
    exit_price: Decimal
    entry_time: datetime
    exit_time: datetime
    holding_seconds: int
    entry_fee: Decimal
    exit_fee: Decimal
    gross_pnl: Decimal
    net_pnl: Decimal
    exit_reason: str
    strategy_config_hash: str


class PortfolioSnapshotOut(ORMModel):
    snapshot_id: str
    symbol: str
    timeframe: str
    at_candle_open_time: datetime
    cash: Decimal
    reserved: Decimal
    open_position_value: Decimal
    unrealized_pnl: Decimal
    realized_pnl_cum: Decimal
    fees_cum: Decimal
    equity: Decimal
    peak_equity: Decimal
    drawdown_pct: Decimal


class SystemEventOut(ORMModel):
    event_id: str
    event_type: str
    timestamp: datetime
    symbol: str | None = None
    timeframe: str | None = None
    entity_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    software_version: str
    strategy_version: str | None = None


class IntegrityEventOut(ORMModel):
    integrity_id: str
    code: str
    severity: str
    entity_id: str | None = None
    symbol: str | None = None
    timeframe: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    detected_at: datetime
    software_version: str


class ConfigOut(BaseModel):
    software_version: str
    strategy: dict[str, Any]
    strategy_config_hash: str
    coverage: dict[str, Any]
    settings: dict[str, Any]
    safety: dict[str, Any]


class StatusOut(BaseModel):
    symbol: str
    timeframe: str
    mode: str
    paper_only: bool
    data: dict[str, Any]
    pipeline: dict[str, Any]
    counts: dict[str, int]
    integrity_critical_count: int
    generated_at: datetime


class PageMeta(BaseModel):
    limit: int
    offset: int
    returned: int


class CandlePage(BaseModel):
    meta: PageMeta
    items: list[CandleOut]


class SignalPage(BaseModel):
    meta: PageMeta
    items: list[SignalOut]


class OrderPage(BaseModel):
    meta: PageMeta
    items: list[OrderOut]


class TradePage(BaseModel):
    meta: PageMeta
    items: list[TradeOut]


class EventPage(BaseModel):
    meta: PageMeta
    items: list[SystemEventOut]


class IntegrityPage(BaseModel):
    meta: PageMeta
    items: list[IntegrityEventOut]


__all__ = [
    "CandleOut",
    "CandlePage",
    "ConfigOut",
    "EventPage",
    "ExecutionOut",
    "HealthResponse",
    "IntegrityEventOut",
    "IntegrityPage",
    "MetricsResponse",
    "ORMModel",
    "OrderOut",
    "OrderPage",
    "PageMeta",
    "PortfolioSnapshotOut",
    "PositionOut",
    "ReadyResponse",
    "ReadinessCheck",
    "SignalOut",
    "SignalPage",
    "StatusOut",
    "SystemEventOut",
    "TradeOut",
    "TradePage",
]
