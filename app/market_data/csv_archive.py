"""CSV archive provider — the engine's primary data source.

Reads the canonical CSV produced by ``scripts/fetch_binance_archive.py``
(official Binance monthly dumps, normalised and hashed).

Properties that matter:

* **Offline and deterministic.** No network, no rate limits, no clock: a replay
  run today produces exactly the bytes it produced last month.
* **Self-verifying.** The sibling ``*.manifest.json`` records a ``dataset_hash``
  over the canonical rows; ``verify_hash=True`` recomputes it on load and
  refuses to serve a series that does not match. Silent data corruption becomes
  an exception instead of a subtly different backtest.
* **Bounded memory.** History is loaded once, cached, and sliced; 20k candles is
  ~2 MB of CSV, so loading it whole is faster and simpler than seeking.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.common.enums import Timeframe
from app.common.time_utils import next_open_time, parse_iso_z
from app.market_data.base import MarketDataProvider, ProviderHealth, RawCandle

logger = logging.getLogger(__name__)

REQUIRED_COLUMNS = ("open_time", "open", "high", "low", "close", "volume")
DEFAULT_ARCHIVE_ROOT = Path("data") / "archive"


class ArchiveError(RuntimeError):
    """Archive file missing, malformed, or failing its own hash check."""


class CSVArchiveProvider(MarketDataProvider):
    """Serves candles from ``<root>/<SYMBOL>/<SYMBOL>-<interval>.csv``."""

    name = "csv_archive"
    is_offline = True

    def __init__(self, root: str | Path = DEFAULT_ARCHIVE_ROOT, *, verify_hash: bool = True) -> None:
        self.root = Path(root)
        self.verify_hash = verify_hash
        self._cache: dict[tuple[str, str], tuple[RawCandle, ...]] = {}
        self._verified: dict[tuple[str, str], str] = {}
        self._manifests: dict[tuple[str, str], dict] = {}

    # ------------------------------------------------------------------ paths
    def csv_path(self, symbol: str, timeframe: str) -> Path:
        return self.root / symbol / f"{symbol}-{timeframe}.csv"

    def manifest_path(self, symbol: str, timeframe: str) -> Path:
        return self.root / symbol / f"{symbol}-{timeframe}.manifest.json"

    def manifest(self, symbol: str, timeframe: str) -> dict:
        key = (symbol, timeframe)
        if key not in self._manifests:
            path = self.manifest_path(symbol, timeframe)
            self._manifests[key] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return self._manifests[key]

    # ------------------------------------------------------------------ fetch
    def fetch_candles(
        self,
        symbol: str,
        timeframe: Timeframe | str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        include_incomplete: bool = False,
    ) -> list[RawCandle]:
        tf = timeframe.value if isinstance(timeframe, Timeframe) else str(timeframe)
        candles = self._load(symbol, tf)
        selected = [
            candle
            for candle in candles
            if (start is None or candle.open_time >= start) and (end is None or candle.open_time < end)
        ]
        if not include_incomplete:
            # Archive rows are historical; ``is_complete`` is True by construction.
            selected = [candle for candle in selected if candle.is_complete]
        return selected

    def health(self) -> ProviderHealth:
        detail = "offline archive"
        return ProviderHealth(name=self.name, reachable=self.root.exists(), detail=detail)

    # ------------------------------------------------------------------- load
    def _load(self, symbol: str, timeframe: str) -> tuple[RawCandle, ...]:
        key = (symbol, timeframe)
        if key in self._cache:
            return self._cache[key]

        path = self.csv_path(symbol, timeframe)
        if not path.exists():
            raise ArchiveError(
                f"archive not found: {path}. Build it with: "
                f"python scripts/fetch_binance_archive.py --symbol {symbol} --interval {timeframe}"
            )

        rows: list[RawCandle] = []
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            missing = [column for column in REQUIRED_COLUMNS if column not in (reader.fieldnames or [])]
            if missing:
                raise ArchiveError(f"{path}: missing columns {missing}")

            for line_no, row in enumerate(reader, start=2):
                try:
                    open_time = parse_iso_z(row["open_time"])
                    close_time = (
                        parse_iso_z(row["close_time"])
                        if row.get("close_time")
                        else next_open_time(open_time, timeframe)
                    )
                    values = {
                        name: Decimal(str(row[name]).strip())
                        for name in ("open", "high", "low", "close", "volume")
                    }
                except (KeyError, InvalidOperation, ValueError) as exc:
                    raise ArchiveError(f"{path}:{line_no}: malformed row ({exc})") from exc

                rows.append(
                    RawCandle(
                        symbol=symbol,
                        timeframe=timeframe,
                        open_time=open_time,
                        close_time=close_time,
                        open=values["open"],
                        high=values["high"],
                        low=values["low"],
                        close=values["close"],
                        volume=values["volume"],
                        is_complete=True,
                        source=self.name,
                    )
                )

        rows.sort(key=lambda candle: candle.open_time)
        if self.verify_hash:
            self._verify(symbol, timeframe, rows)
        self._cache[key] = tuple(rows)
        return self._cache[key]

    def _verify(self, symbol: str, timeframe: str, rows: list[RawCandle]) -> None:
        manifest = self.manifest(symbol, timeframe)
        expected = manifest.get("dataset_hash")
        if not expected:
            logger.warning(
                "archive manifest missing dataset_hash - hash check skipped",
                extra={"symbol": symbol, "timeframe": timeframe},
            )
            return

        digest = hashlib.sha256()
        for row in rows:
            digest.update(
                ",".join(
                    [
                        row.open_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        row.close_time.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        f"{row.open:f}",
                        f"{row.high:f}",
                        f"{row.low:f}",
                        f"{row.close:f}",
                        f"{row.volume:f}",
                    ]
                ).encode("utf-8")
            )
            digest.update(b"\n")
        actual = digest.hexdigest()
        if actual != expected:
            raise ArchiveError(
                f"{symbol}-{timeframe}: dataset hash mismatch "
                f"(manifest {expected[:16]}..., file {actual[:16]}...). "
                "The archive was modified after it was built; rebuild it and re-run."
            )
        self._verified[(symbol, timeframe)] = actual

    @property
    def verified_hashes(self) -> dict[str, str]:
        """Dataset hashes verified during this process (for ``data_sources``)."""
        return {f"{symbol}-{tf}": digest for (symbol, tf), digest in self._verified.items()}


__all__ = ["DEFAULT_ARCHIVE_ROOT", "REQUIRED_COLUMNS", "ArchiveError", "CSVArchiveProvider"]
