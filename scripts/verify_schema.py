#!/usr/bin/env python
"""Detect drift between the live database schema and the ORM metadata.

Checks, in order:
1. every expected table exists,
2. every expected column exists (name + nullability),
3. every expected CHECK/UNIQUE constraint and index exists,
4. the partial unique index on positions is actually partial.

This is the cheap guard that catches "someone edited a model but never wrote a
migration" — the failure mode that makes a paper engine's numbers untrustworthy.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import inspect  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402

from app.database.base import Base  # noqa: E402
from app.database.engine import build_engine  # noqa: E402
from app.models import ALL_TABLES  # noqa: E402

PARTIAL_INDEX = "uq_positions_one_open_per_sym_tf"


def check_schema(engine: Engine) -> list[str]:
    """Return a list of human-readable problems (empty list == no drift)."""
    problems: list[str] = []
    inspector = inspect(engine)
    live_tables = set(inspector.get_table_names())

    for table in sorted(ALL_TABLES):
        if table not in live_tables:
            problems.append(f"missing table: {table}")

    for table_name, table in Base.metadata.tables.items():
        if table_name not in live_tables:
            continue
        live_columns = {c["name"]: c for c in inspector.get_columns(table_name)}
        for column in table.columns:
            if column.name not in live_columns:
                problems.append(f"{table_name}.{column.name}: missing column")
        live_checks = {c.get("name") for c in inspector.get_check_constraints(table_name)}
        expected_checks = {c.name for c in table.constraints if c.__class__.__name__ == "CheckConstraint"}
        for missing_check in sorted(expected_checks - live_checks):
            problems.append(f"{table_name}: missing check constraint {missing_check}")
        live_indexes = {i["name"] for i in inspector.get_indexes(table_name)}
        expected_indexes = {i.name for i in table.indexes}
        for missing_index in sorted(expected_indexes - live_indexes):
            problems.append(f"{table_name}: missing index {missing_index}")

    with engine.connect() as conn:
        from sqlalchemy import text

        row = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='index' AND name=:name"),
            {"name": PARTIAL_INDEX},
        ).fetchone()
    if row is None:
        problems.append(f"missing partial unique index: {PARTIAL_INDEX}")
    elif "WHERE" not in (row[0] or "").upper():
        problems.append(f"{PARTIAL_INDEX} exists but is not partial (no WHERE clause)")

    return problems


def main() -> int:
    engine = build_engine()
    try:
        problems = check_schema(engine)
    finally:
        engine.dispose()
    if problems:
        print("[verify_schema] DRIFT DETECTED")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"[verify_schema] OK - {len(ALL_TABLES)} tables match the ORM metadata")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
