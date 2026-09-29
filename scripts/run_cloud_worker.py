#!/usr/bin/env python3
"""Cloud entrypoint: initialize historical paper state once, then run continuously."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def run(*args: str) -> None:
    subprocess.run([sys.executable, *args], cwd=PROJECT_ROOT, check=True)


def main() -> int:
    from app.common.safety import enforce_paper_only
    from app.config.settings import get_settings

    settings = get_settings()
    enforce_paper_only(settings)

    run("-m", "alembic", "upgrade", "head")

    from sqlalchemy import func, select

    from app.database.session import session_scope
    from app.models import Candle, Trade

    with session_scope() as session:
        candles = int(session.execute(select(func.count()).select_from(Candle)).scalar() or 0)
        trades = int(session.execute(select(func.count()).select_from(Trade)).scalar() or 0)

    if candles == 0:
        run("scripts/ingest_archive.py")

    if trades == 0:
        run("scripts/replay.py")

    run("scripts/run_paper_scheduler.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
