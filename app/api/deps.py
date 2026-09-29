"""FastAPI dependencies.

Read endpoints must degrade gracefully before the DB is initialised: a fresh
checkout should show an empty dashboard with a clear message, not a 500 and a
stack trace. :func:`require_schema` turns "tables missing" into a 503 whose
body tells you to run ``python scripts/init_db.py``.
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import HTTPException, status
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.database.engine import get_engine
from app.database.session import build_session_factory

INIT_HINT = "database schema missing - run: python scripts/init_db.py"

_schema_ready: bool | None = None


def schema_is_ready(*, refresh: bool = False) -> bool:
    """Cheap, cached check that the phase-1 schema is present."""
    global _schema_ready
    if _schema_ready is None or refresh:
        try:
            _schema_ready = inspect(get_engine()).has_table("candles")
        except Exception:  # noqa: BLE001 - any failure means "not ready"
            _schema_ready = False
    return _schema_ready


def reset_schema_cache() -> None:
    global _schema_ready
    _schema_ready = None


def get_db() -> Iterator[Session]:
    """Read-only session dependency (no implicit commit)."""
    session = build_session_factory()()
    try:
        yield session
    finally:
        session.close()


def require_schema() -> None:
    if not schema_is_ready(refresh=True):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=INIT_HINT,
        )


__all__ = ["INIT_HINT", "get_db", "require_schema", "reset_schema_cache", "schema_is_ready"]
