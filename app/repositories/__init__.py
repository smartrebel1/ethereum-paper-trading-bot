"""Repositories: the only place that writes to the database.

Phase 1 ships the registry/audit repositories needed by startup and the API.
Business repositories (candles, signals, orders, executions, positions,
trades, snapshots) arrive with their engines in phases 2-6 — putting them here
early would mean writing SQL for state machines that do not exist yet.
"""

from __future__ import annotations

from app.repositories import (
    configuration_versions,
    integrity_events,
    strategy_versions,
    system_events,
)
from app.repositories.base import get_or_create

__all__ = [
    "configuration_versions",
    "get_or_create",
    "integrity_events",
    "strategy_versions",
    "system_events",
]
