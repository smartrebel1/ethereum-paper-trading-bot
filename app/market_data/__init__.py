"""Market data providers (phase 2).

* :class:`~app.market_data.csv_archive.CSVArchiveProvider` — the primary,
  offline, hash-verified source (official Binance dumps).
* :class:`~app.market_data.binance.BinanceRESTProvider` — opt-in live source.
* :class:`~app.market_data.mock.MockProvider` — scripted fixtures, including
  deliberately hostile ones.
"""

from __future__ import annotations

from app.market_data.base import MarketDataProvider, ProviderHealth, RawCandle
from app.market_data.binance import BinanceError, BinanceRESTProvider
from app.market_data.csv_archive import ArchiveError, CSVArchiveProvider
from app.market_data.mock import MockProvider, make_candle
from app.market_data.registry import PROVIDER_NAMES, build_provider

__all__ = [
    "PROVIDER_NAMES",
    "ArchiveError",
    "BinanceError",
    "BinanceRESTProvider",
    "CSVArchiveProvider",
    "MarketDataProvider",
    "MockProvider",
    "ProviderHealth",
    "RawCandle",
    "build_provider",
    "make_candle",
]
