#!/usr/bin/env python
"""Ingest the static archive into the database (idempotent).

    python scripts/ingest_archive.py                      # full 4h archive
    python scripts/ingest_archive.py --since 2023-01-01   # recent slice
    python scripts/ingest_archive.py --reject-report      # list rejected candles

Safe to run repeatedly: deterministic ids + the (symbol, timeframe, open_time)
unique constraint make re-ingestion a no-op, and the ``data_sources`` row
records the dataset hash that proves which bytes were ingested.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import SOFTWARE_VERSION  # noqa: E402
from app.candles.ingestor import DataIngestor  # noqa: E402
from app.common.time_utils import parse_iso_z  # noqa: E402
from app.config.settings import get_settings  # noqa: E402
from app.database.session import session_scope  # noqa: E402
from app.market_data.csv_archive import CSVArchiveProvider  # noqa: E402
from app.repositories import data_sources  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest the CSV archive into the database.")
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--timeframe", default=None)
    parser.add_argument("--archive-root", default=str(PROJECT_ROOT / "data" / "archive"))
    parser.add_argument("--since", default=None, help="ISO date/time, e.g. 2023-01-01")
    parser.add_argument("--until", default=None, help="exclusive upper bound")
    parser.add_argument("--limit", type=int, default=None, help="ingest only the last N candles")
    parser.add_argument("--skip-hash-check", action="store_true")
    parser.add_argument("--reject-report", action="store_true")
    args = parser.parse_args(argv)

    settings = get_settings()
    symbol = args.symbol or settings.symbol
    timeframe = args.timeframe or settings.timeframe.value

    provider = CSVArchiveProvider(args.archive_root, verify_hash=not args.skip_hash_check)
    start = parse_iso_z(args.since) if args.since else None
    if args.since and "T" not in args.since:
        start = parse_iso_z(f"{args.since}T00:00:00Z")
    end = parse_iso_z(args.until) if args.until else None

    candles = provider.fetch_candles(symbol, timeframe, start=start, end=end)
    if args.limit:
        candles = candles[-args.limit :]
    print(f"[ingest] fetched {len(candles)} candles from {provider.name}")

    manifest = provider.manifest(symbol, timeframe)
    if manifest.get("dataset_hash"):
        print(f"[ingest] dataset hash: {manifest['dataset_hash']}")

    with session_scope() as session:
        ingestor = DataIngestor(
            session, symbol=symbol, timeframe=timeframe, software_version=SOFTWARE_VERSION
        )
        report = ingestor.ingest(candles, start=start, end_exclusive=end)
        data_sources.ensure_registered(
            session,
            name=provider.name,
            kind="archive",
            dataset_hash=manifest.get("dataset_hash", "unknown"),
            symbol=symbol,
            timeframe=timeframe,
            start_time=report.first_open_time,
            end_time=report.last_open_time,
            candle_count=report.created + report.unchanged,
            now=datetime.now(tz=provider_cutoff()),
            software_version=SOFTWARE_VERSION,
            metadata={"csv_file": manifest.get("csv_file"), "rows": manifest.get("rows")},
        )

    print(f"[ingest] {report.summary()}")
    if report.rejections and args.reject_report:
        print("[ingest] rejections:")
        for rejection in report.rejections[:50]:
            print(f"  - {rejection['open_time']} {rejection['code']}: {rejection['message']}")
    print("[ingest] OK" if report.ok else "[ingest] completed with gaps")
    return 0


def provider_cutoff():  # noqa: ANN201

    return UTC


if __name__ == "__main__":
    raise SystemExit(main())
