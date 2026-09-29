#!/usr/bin/env python
"""Create/upgrade the database schema (``alembic upgrade head``).

Run from the project root:

    python scripts/init_db.py

Prints the resulting table list so a failed/partial migration is obvious.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import inspect  # noqa: E402

from app import SOFTWARE_VERSION  # noqa: E402
from app.config.settings import get_settings  # noqa: E402
from app.database.engine import build_engine  # noqa: E402
from app.models import ALL_TABLES  # noqa: E402


def bootstrap_database(database_url: str | None = None) -> dict[str, object]:
    """Apply migrations and report which tables exist. Returns a status dict."""
    settings = get_settings()
    url = database_url or settings.database_url

    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)

    command.upgrade(cfg, "head")

    engine = build_engine(url)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    present = sorted(t for t in tables if t != "alembic_version")
    missing = sorted(ALL_TABLES - tables)
    return {
        "database_url": url,
        "software_version": SOFTWARE_VERSION,
        "tables": present,
        "missing": missing,
        "ok": not missing,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Initialise the trading bot database.")
    parser.add_argument("--database-url", default=None, help="override DATABASE_URL")
    args = parser.parse_args(argv)

    status = bootstrap_database(args.database_url)
    print(f"[init_db] database : {status['database_url']}")
    print(f"[init_db] tables   : {len(status['tables'])}")
    for table in status["tables"]:
        print(f"           - {table}")
    if status["missing"]:
        print(f"[init_db] MISSING  : {status['missing']}")
        return 1
    print("[init_db] OK - schema is at head revision")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
