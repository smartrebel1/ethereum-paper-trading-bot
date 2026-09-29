"""Binance REST provider (opt-in, live paper runs only).

The engine does **not** need this provider: the CSV archive is the primary,
offline source, and every test runs without network access.

It exists for a live paper run, where new closed candles must be fetched as they
appear. Two properties are enforced here because they are the difference between
"paper trading" and "a backtest with extra steps":

* the in-progress candle is **never** returned unless explicitly requested
  (``/api/v3/klines`` includes it, and using it is textbook look-ahead);
* ``close_time`` is normalised to the exclusive boundary the rest of the engine
  uses (Binance returns ``openTime + tf - 1ms``).

Network tests are marked ``network`` and skipped by default
(``pytest -m network`` to run them).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal

import httpx

from app.common.enums import Timeframe
from app.common.time_utils import next_open_time
from app.market_data.base import MarketDataProvider, ProviderHealth, RawCandle

logger = logging.getLogger(__name__)

BINANCE_BASE_URL = "https://data-api.binance.vision"
KLINES_PATH = "/api/v3/klines"
TICKER_PRICE_PATH = "/api/v3/ticker/price"
MAX_LIMIT = 1000


class BinanceError(RuntimeError):
    """REST call failed or returned an unusable payload."""


class BinanceRESTProvider(MarketDataProvider):
    name = "binance_rest"
    is_offline = False

    def __init__(
        self,
        *,
        base_url: str = BINANCE_BASE_URL,
        timeout_seconds: float = 15.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._client = client
        self._owns_client = client is None

    # ---------------------------------------------------------------- plumbing
    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(base_url=self.base_url, timeout=self.timeout_seconds)
        return self._client

    def close(self) -> None:
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None

    def __enter__(self) -> BinanceRESTProvider:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

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
        now = datetime.now(tz=UTC)
        collected: list[RawCandle] = []
        cursor = int(start.timestamp() * 1000) if start else None
        end_ms = int(end.timestamp() * 1000) if end else None

        while True:
            params: dict[str, object] = {"symbol": symbol, "interval": tf, "limit": MAX_LIMIT}
            if cursor is not None:
                params["startTime"] = cursor
            if end_ms is not None:
                params["endTime"] = end_ms

            payload = self._request(KLINES_PATH, params)
            if not payload:
                break

            batch = [self._parse_row(symbol, tf, row, now) for row in payload]
            batch = [candle for candle in batch if candle is not None]
            if not include_incomplete:
                batch = [candle for candle in batch if candle.is_complete]
            if end is not None:
                batch = [candle for candle in batch if candle.open_time < end]
            collected.extend(batch)

            next_cursor = int(payload[-1][0]) + 1
            if cursor is not None and next_cursor <= cursor:  # pragma: no cover - defensive
                break
            cursor = next_cursor
            if len(payload) < MAX_LIMIT:
                break
            if end_ms is not None and cursor >= end_ms:
                break

        # Deduplicate defensively: pagination can overlap on an exact boundary.
        unique: dict[datetime, RawCandle] = {candle.open_time: candle for candle in collected}
        return [unique[key] for key in sorted(unique)]

    def fetch_current_price(self, symbol: str) -> tuple[Decimal, datetime]:
        """Fetch the latest public spot price without an API key."""
        payload = self._request(TICKER_PRICE_PATH, {"symbol": symbol})
        if not isinstance(payload, dict):
            raise BinanceError("ticker price response was not an object")
        try:
            price = Decimal(str(payload["price"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise BinanceError("ticker price response missing a valid price") from exc
        return price, datetime.now(tz=UTC)

    def health(self) -> ProviderHealth:
        try:
            response = self.client.get("/api/v3/ping")
            return ProviderHealth(
                name=self.name,
                reachable=response.status_code == 200,
                detail=f"HTTP {response.status_code}",
            )
        except Exception as exc:  # noqa: BLE001 - health must never raise
            return ProviderHealth(name=self.name, reachable=False, detail=f"{type(exc).__name__}: {exc}")

    # ----------------------------------------------------------------- helpers
    def _request(
        self, path: str, params: dict[str, object]
    ) -> list[list[object]] | dict[str, object]:
        try:
            response = self.client.get(path, params=params)
        except Exception as exc:  # noqa: BLE001 - network layer
            raise BinanceError(f"request failed: {type(exc).__name__}: {exc}") from exc
        if response.status_code == 451:
            raise BinanceError(
                "Binance market-data endpoint returned HTTP 451 (restricted location). "
                "Use the static archive instead: python scripts/fetch_binance_archive.py"
            )
        if response.status_code != 200:
            raise BinanceError(f"HTTP {response.status_code}: {response.text[:200]}")
        try:
            return response.json()
        except ValueError as exc:  # pragma: no cover - non-JSON body
            raise BinanceError(f"non-JSON response: {response.text[:200]}") from exc

    @staticmethod
    def _parse_row(symbol: str, timeframe: str, row: list[object], now: datetime) -> RawCandle | None:
        if not isinstance(row, list) or len(row) < 7:
            return None
        open_time = datetime.fromtimestamp(int(row[0]) / 1000, tz=UTC)
        close_time = next_open_time(open_time, timeframe)
        # Binance sends closeTime = openTime + tf - 1ms; re-derive the exclusive
        # boundary ourselves so the engine has exactly one definition of it.
        del row[6]
        return RawCandle(
            symbol=symbol,
            timeframe=timeframe,
            open_time=open_time,
            close_time=close_time,
            open=Decimal(str(row[1])),
            high=Decimal(str(row[2])),
            low=Decimal(str(row[3])),
            close=Decimal(str(row[4])),
            volume=Decimal(str(row[5])),
            is_complete=now >= close_time,
            source="binance_rest",
        )


def kline_interval(timeframe: Timeframe | str) -> str:
    """Binance interval string for a timeframe (identity today, kept explicit)."""
    tf = timeframe.value if isinstance(timeframe, Timeframe) else str(timeframe)
    if tf not in {"15m", "1h", "4h", "1d"}:
        raise ValueError(f"unsupported Binance interval: {tf}")
    return tf


__all__ = [
    "BINANCE_BASE_URL",
    "KLINES_PATH",
    "TICKER_PRICE_PATH",
    "MAX_LIMIT",
    "BinanceError",
    "BinanceRESTProvider",
    "kline_interval",
]
