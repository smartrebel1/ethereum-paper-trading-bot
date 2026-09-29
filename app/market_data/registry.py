"""Provider registry.

One place that maps a name to a provider, so the scheduler/scripts never
construct one directly and ``--provider`` stays a data-driven choice.
"""

from __future__ import annotations

from pathlib import Path

from app.market_data.base import MarketDataProvider
from app.market_data.binance import BinanceRESTProvider
from app.market_data.csv_archive import CSVArchiveProvider
from app.market_data.mock import MockProvider

PROVIDER_NAMES = ("archive", "binance", "mock")


def build_provider(
    name: str,
    *,
    archive_root: str | Path | None = None,
    verify_hash: bool = True,
) -> MarketDataProvider:
    """Instantiate a provider by name.

    ``archive`` (default) is offline and deterministic — the right choice for
    every replay, backtest and test. ``binance`` is for a live paper run.
    """
    key = name.strip().lower()
    if key == "archive":
        kwargs = {"verify_hash": verify_hash}
        if archive_root is not None:
            kwargs["root"] = archive_root  # type: ignore[assignment]
        return CSVArchiveProvider(**kwargs)  # type: ignore[arg-type]
    if key == "binance":
        return BinanceRESTProvider()
    if key == "mock":
        return MockProvider()
    raise ValueError(f"unknown provider {name!r}; expected one of {PROVIDER_NAMES}")


__all__ = ["PROVIDER_NAMES", "build_provider"]
