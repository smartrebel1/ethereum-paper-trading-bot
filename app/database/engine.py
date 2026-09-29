"""Engine construction + SQLite pragmas.

SQLite is the phase-1 store. The pragmas below are the ones that matter for a
single-writer trading process:

* ``foreign_keys=ON``  — off by default in SQLite (!), which would silently
  orphan executions/trades.
* ``journal_mode=WAL`` — readers (dashboard) never block the writer.
* ``busy_timeout``     — the scheduler + API can briefly contend; wait instead
  of raising "database is locked".
* ``synchronous=NORMAL`` — WAL + NORMAL is the accepted durability/speed point
  for a paper ledger that is rebuildable from candles.
"""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from app.config.settings import get_settings

SQLITE_PREFIXES = ("sqlite:///", "sqlite+pysqlite:///", "sqlite://")


def sqlite_path_from_url(url: str) -> Path | None:
    """Filesystem path of a sqlite URL (``None`` for memory databases)."""
    for prefix in ("sqlite+pysqlite:///", "sqlite:///"):
        if url.startswith(prefix):
            raw = url[len(prefix) :]
            if raw in ("", ":memory:"):
                return None
            return Path(raw)
    return None


def ensure_sqlite_dir(url: str) -> None:
    """Create the parent directory of a file-backed sqlite DB."""
    path = sqlite_path_from_url(url)
    if path is not None and path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def _is_memory_sqlite(url: str) -> bool:
    return _is_sqlite(url) and (":memory:" in url or url.endswith("sqlite://"))


def build_engine(url: str | None = None, *, echo: bool = False) -> Engine:
    """Create an :class:`Engine` for *url* (defaults to ``DATABASE_URL``)."""
    settings = get_settings()
    db_url = url or settings.database_url
    ensure_sqlite_dir(db_url)

    kwargs: dict[str, Any] = {"future": True, "echo": echo, "pool_pre_ping": True}
    if _is_memory_sqlite(db_url):
        # One shared in-memory DB across connections (tests).
        kwargs["connect_args"] = {"check_same_thread": False}
        kwargs["poolclass"] = StaticPool
    elif _is_sqlite(db_url):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    engine = create_engine(db_url, **kwargs)

    if _is_sqlite(db_url):
        _install_sqlite_pragmas(engine)
    return engine


def _install_sqlite_pragmas(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection: Any, connection_record: Any) -> None:  # noqa: ANN401, ARG001
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON;")
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA synchronous=NORMAL;")
            cursor.execute("PRAGMA busy_timeout=30000;")
        finally:
            cursor.close()

    @event.listens_for(engine, "connect")
    def _register_decimal_adapters(dbapi_connection: Any, connection_record: Any) -> None:  # noqa: ANN401, ARG001
        # Defensive: nothing should reach sqlite as a Decimal (FixedPoint converts
        # to int), but a raw str() is preferable to driver-dependent behaviour.
        # sqlite3.register_adapter is deprecated in 3.12+; failures are ignored.
        with suppress(Exception):
            import sqlite3

            sqlite3.register_adapter(Decimal, str)


_engine: Engine | None = None


def get_engine() -> Engine:
    """Process-wide engine (lazily built from settings)."""
    global _engine
    if _engine is None:
        _engine = build_engine()
    return _engine


def set_engine(engine: Engine | None) -> Engine | None:
    """Install an engine (tests / replay).

    ``None`` clears the cache and returns ``None`` — it deliberately does *not*
    rebuild, so "forget the engine" never requires a valid configuration (the
    reset path must keep working while configuration is being swapped).
    """
    global _engine
    if _engine is not None and engine is not _engine:
        with suppress(Exception):  # disposal is best effort
            _engine.dispose()
    _engine = engine
    return _engine


def reset_engine() -> None:
    """Dispose and forget the cached engine (test hook). Nothing is rebuilt."""
    set_engine(None)


__all__ = [
    "SQLITE_PREFIXES",
    "build_engine",
    "ensure_sqlite_dir",
    "get_engine",
    "reset_engine",
    "set_engine",
    "sqlite_path_from_url",
]

from decimal import Decimal  # noqa: E402  (imported late on purpose: used by adapters)
