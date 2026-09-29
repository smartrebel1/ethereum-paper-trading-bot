"""Shared test fixtures.

Every test runs against a **fresh, isolated SQLite file** and a paper-only
environment. The autouse fixture also resets the process-wide caches
(settings, engine, schema probe, clock) so tests cannot leak state into each
other — the single most common source of flaky trading-engine tests.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # allow `pytest` from anywhere
    sys.path.insert(0, str(PROJECT_ROOT))

from app.common.clock import set_clock  # noqa: E402
from app.config.settings import clear_settings_cache  # noqa: E402
from app.database.engine import build_engine, reset_engine  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[dict[str, Any]]:
    """Paper-only environment + a private SQLite database per test."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "false")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.delenv("ENABLE_AI_SHADOW", raising=False)
    monkeypatch.delenv("AI_PROVIDER", raising=False)

    clear_settings_cache()
    reset_engine()
    set_clock(None)

    from app.api.deps import reset_schema_cache

    reset_schema_cache()

    yield {"db_path": db_path, "db_url": f"sqlite:///{db_path.as_posix()}", "tmp_path": tmp_path}

    reset_engine()
    reset_schema_cache()
    clear_settings_cache()
    set_clock(None)


@pytest.fixture
def db_url(isolated_env: dict[str, Any]) -> str:
    return str(isolated_env["db_url"])


def _alembic_config(db_url: str) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


@pytest.fixture
def alembic_config(db_url: str) -> Config:
    return _alembic_config(db_url)


@pytest.fixture
def migrated_db(db_url: str) -> Iterator[Engine]:
    """Run the real migrations, then hand back an engine bound to that DB."""
    command.upgrade(_alembic_config(db_url), "head")
    engine = build_engine(db_url)
    from app.api.deps import reset_schema_cache

    reset_schema_cache()
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def session(migrated_db: Engine):  # noqa: ANN201
    """SQLAlchemy session on the migrated database (auto-closed)."""
    from app.database.session import build_session_factory

    maker = build_session_factory(migrated_db)
    db = maker()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def api_client(migrated_db: Engine):  # noqa: ANN201
    """TestClient whose lifespan runs against the migrated temp database."""
    from fastapi.testclient import TestClient

    from app.config.settings import get_settings
    from app.main import create_app

    app = create_app(get_settings())
    with TestClient(app) as client:
        yield client


@pytest.fixture
def api_client_no_schema(db_url: str):  # noqa: ANN201
    """TestClient for a database that has NOT been migrated yet."""
    from fastapi.testclient import TestClient

    from app.config.settings import get_settings
    from app.main import create_app

    app = create_app(get_settings())
    with TestClient(app) as client:
        yield client
