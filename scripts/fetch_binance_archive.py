#!/usr/bin/env python
"""Build the static Binance archive that the engine replays.

Downloads the **official** Binance public data dumps
(``data.binance.vision``, monthly klines ZIPs) and normalises them into one
canonical CSV per (symbol, timeframe) plus a manifest with a dataset hash.

Why the monthly dumps instead of ``api.binance.com``:

* the REST API is geo-restricted in many environments (HTTP 451), and
* a *static* archive makes every test, replay and backtest runnable with no
  network at all - which is the whole point of a reproducible research engine.

Canonical CSV format (the only format the engine reads)::

    open_time,close_time,open,high,low,close,volume
    2017-08-17T04:00:00Z,2017-08-17T08:00:00Z,299.00,301.00,298.50,300.10,1234.567

Notes handled by this script (all observed in the real dumps):

* files published before ~2025 have **no header row**, newer ones do;
* timestamps switched from **milliseconds to microseconds**;
* the last month may not be published yet (404) or may be partial.

Usage::

    python scripts/fetch_binance_archive.py --symbol ETHUSDT --interval 4h
    python scripts/fetch_binance_archive.py --symbol ETHUSDT --interval 1d
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sys
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_ROOT = PROJECT_ROOT / "data" / "archive"
BASE_URL = "https://data.binance.vision/data/spot/monthly/klines"

INTERVAL_SECONDS = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
ZERO = Decimal(0)
#: Raw dump column layout (12 columns) - documented in the Binance data repo.
RAW_COLUMNS = 12


@dataclass(frozen=True, slots=True)
class CanonicalRow:
    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def to_csv_row(self) -> list[str]:
        return [
            self.open_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            self.close_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            f"{self.open:f}",
            f"{self.high:f}",
            f"{self.low:f}",
            f"{self.close:f}",
            f"{self.volume:f}",
        ]


def months_between(start: datetime, end: datetime) -> list[str]:
    out: list[str] = []
    cursor = datetime(start.year, start.month, 1, tzinfo=UTC)
    while cursor <= end:
        out.append(f"{cursor.year:04d}-{cursor.month:02d}")
        cursor = (cursor + timedelta(days=32)).replace(day=1)
    return out


def _timestamp_to_utc(raw: str) -> datetime:
    """Binance switched from ms to us; detect by magnitude."""
    value = int(raw.strip())
    if value > 10**14:  # microseconds
        seconds = value / 1_000_000
    elif value > 10**11:  # milliseconds
        seconds = value / 1_000
    else:  # seconds (defensive)
        seconds = float(value)
    return datetime.fromtimestamp(seconds, tz=UTC)


def parse_dump_csv(payload: bytes, interval: str) -> list[CanonicalRow]:
    """Parse one monthly dump (header or no header, ms or us timestamps)."""
    text = payload.decode("utf-8").strip()
    if not text:
        return []
    reader = csv.reader(io.StringIO(text))
    rows: list[CanonicalRow] = []
    for raw in reader:
        if not raw or not raw[0].strip():
            continue
        if not raw[0].strip()[0].isdigit():
            continue  # header row
        if len(raw) < 7:
            continue
        try:
            open_time = _timestamp_to_utc(raw[0])
            open_p, high, low, close, volume = (Decimal(str(v)) for v in raw[1:6])
        except Exception:  # noqa: BLE001 - malformed row: skip, never guess
            continue
        rows.append(
            CanonicalRow(
                open_time=open_time,
                close_time=open_time + timedelta(seconds=INTERVAL_SECONDS[interval]),
                open=open_p,
                high=high,
                low=low,
                close=close,
                volume=volume,
            )
        )
    return rows


def download_month(symbol: str, interval: str, month: str, *, retries: int = 3) -> bytes | None:
    url = f"{BASE_URL}/{symbol}/{interval}/{symbol}-{interval}-{month}.zip"
    last_error: Exception | None = None
    for _ in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=45) as response:  # noqa: S310 - fixed host
                archive = response.read()
            break
        except Exception as exc:  # noqa: BLE001 - 404 = month not published
            last_error = exc
    else:
        if last_error is not None and "404" not in str(last_error):
            print(f"  ! {month}: {last_error}", file=sys.stderr)
        return None
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        name = zf.namelist()[0]
        return zf.read(name)


def build_archive(
    symbol: str,
    interval: str,
    *,
    start_month: str,
    end_month: str,
    workers: int = 8,
) -> dict[str, object]:
    start = datetime.strptime(start_month, "%Y-%m").replace(tzinfo=UTC)
    end = datetime.strptime(end_month, "%Y-%m").replace(tzinfo=UTC)
    months = months_between(start, end)

    fetched: dict[str, bytes] = {}
    print(f"[archive] {symbol} {interval}: {len(months)} monthly dumps {start_month}..{end_month}")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(download_month, symbol, interval, month): month for month in months}
        for future in as_completed(futures):
            month = futures[future]
            payload = future.result()
            if payload:
                fetched[month] = payload

    rows: list[CanonicalRow] = []
    for month in sorted(fetched):
        rows.extend(parse_dump_csv(fetched[month], interval))
    rows.sort(key=lambda row: row.open_time)

    deduped: list[CanonicalRow] = []
    seen: set[datetime] = set()
    duplicates = 0
    for row in rows:
        if row.open_time in seen:
            duplicates += 1
            continue
        seen.add(row.open_time)
        deduped.append(row)

    out_dir = ARCHIVE_ROOT / symbol
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"{symbol}-{interval}.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["open_time", "close_time", "open", "high", "low", "close", "volume"])
        for row in deduped:
            writer.writerow(row.to_csv_row())

    digest = hashlib.sha256()
    for row in deduped:
        digest.update(",".join(row.to_csv_row()).encode("utf-8"))
        digest.update(b"\n")

    manifest = {
        "symbol": symbol,
        "interval": interval,
        "source": "data.binance.vision (spot monthly klines)",
        "months_requested": len(months),
        "months_downloaded": len(fetched),
        "missing_months": [m for m in months if m not in fetched],
        "rows": len(deduped),
        "duplicate_rows_removed": duplicates,
        "first_open_time": deduped[0].open_time.isoformat() if deduped else None,
        "last_open_time": deduped[-1].open_time.isoformat() if deduped else None,
        "dataset_hash": digest.hexdigest(),
        "csv_file": str(csv_path.relative_to(PROJECT_ROOT)),
        "csv_bytes": csv_path.stat().st_size,
        "built_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    manifest_path = out_dir / f"{symbol}-{interval}.manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    print(f"[archive] rows        : {len(deduped)} (dedup removed {duplicates})")
    print(f"[archive] coverage    : {manifest['first_open_time']} .. {manifest['last_open_time']}")
    print(f"[archive] csv         : {manifest['csv_file']} ({manifest['csv_bytes'] / 1024:.1f} KiB)")
    print(f"[archive] dataset hash: {manifest['dataset_hash']}")
    if manifest["missing_months"]:
        print(f"[archive] not published: {manifest['missing_months']}")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the static Binance candle archive.")
    parser.add_argument("--symbol", default="ETHUSDT")
    parser.add_argument("--interval", default="4h", choices=sorted(INTERVAL_SECONDS))
    parser.add_argument("--start-month", default="2017-08")
    parser.add_argument("--end-month", default=datetime.now(UTC).strftime("%Y-%m"))
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args(argv)

    manifest = build_archive(
        args.symbol,
        args.interval,
        start_month=args.start_month,
        end_month=args.end_month,
        workers=args.workers,
    )
    return 0 if manifest["rows"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
