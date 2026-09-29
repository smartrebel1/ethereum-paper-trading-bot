"""ORM models.

Importing this package registers every table on ``Base.metadata`` — which is
what Alembic autogenerate, ``create_all`` (tests) and the schema tests inspect.
"""

from __future__ import annotations

from app.database.base import Base
from app.models.ai_observation import AIObservation
from app.models.candle import Candle
from app.models.configuration_version import ConfigurationVersion
from app.models.data_source import DataSource
from app.models.execution import Execution
from app.models.health_check import HealthCheck
from app.models.integrity_event import IntegrityEvent
from app.models.order import Order
from app.models.portfolio_snapshot import PortfolioSnapshot
from app.models.position import Position
from app.models.risk_decision import RiskDecision
from app.models.scheduler_run import SchedulerRun
from app.models.signal import Signal
from app.models.strategy_version import StrategyVersion
from app.models.system_event import SystemEvent
from app.models.trade import Trade

#: Every table the phase-1 schema must create (asserted by tests + migration test).
ALL_TABLES: frozenset[str] = frozenset(
    {
        "candles",
        "signals",
        "orders",
        "executions",
        "positions",
        "trades",
        "portfolio_snapshots",
        "strategy_versions",
        "risk_decisions",
        "system_events",
        "integrity_events",
        "ai_observations",
        "data_sources",
        "scheduler_runs",
        "health_checks",
        "configuration_versions",
    }
)

__all__ = [
    "ALL_TABLES",
    "AIObservation",
    "Base",
    "Candle",
    "ConfigurationVersion",
    "DataSource",
    "Execution",
    "HealthCheck",
    "IntegrityEvent",
    "Order",
    "PortfolioSnapshot",
    "Position",
    "RiskDecision",
    "SchedulerRun",
    "Signal",
    "StrategyVersion",
    "SystemEvent",
    "Trade",
]


# Import-time self check: models and the expected-table list must agree.
_registered = set(Base.metadata.tables)
if _registered != set(ALL_TABLES):  # pragma: no cover - import-time guard
    _missing = sorted(set(ALL_TABLES) - _registered)
    _extra = sorted(_registered - set(ALL_TABLES))
    raise RuntimeError(f"model/table mismatch. missing={_missing} unexpected={_extra}")
