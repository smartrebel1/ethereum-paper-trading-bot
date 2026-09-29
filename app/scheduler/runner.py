"""Continuous paper-trading scheduler.

The scheduler is intentionally internal and candle-driven. It polls a public
market-data provider, stores only validated closed candles, then advances the
same ReplayEngine incrementally. No exchange trading endpoint is ever called.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from app import SOFTWARE_VERSION
from app.candles.ingestor import DataIngestor
from app.common.time_utils import timeframe_delta
from app.config.settings import get_settings
from app.config.strategy_config import load_frozen_strategy_config
from app.database.session import session_scope
from app.market_data.base import MarketDataProvider
from app.market_data.registry import build_provider
from app.models.scheduler_run import SchedulerRun
from app.repositories import candles as candle_repo
from app.replay_engine.engine import ReplayEngine

logger = logging.getLogger(__name__)

DEFAULT_POLL_SECONDS = 60
WARMUP_CANDLES = 200
RUN_NAME = "ethusdt_paper"


class SchedulerHaltedError(RuntimeError):
    """Raised when the trading engine hits a critical integrity failure."""


class PaperScheduler:
    """Poll closed candles and advance the paper engine exactly once per candle."""

    def __init__(
        self,
        *,
        symbol: str | None = None,
        timeframe: str | None = None,
        provider: MarketDataProvider | None = None,
        poll_seconds: int = DEFAULT_POLL_SECONDS,
        once: bool = False,
    ) -> None:
        if poll_seconds < 5:
            raise ValueError("poll_seconds must be >= 5")
        settings = get_settings()
        self.symbol = symbol or settings.symbol
        self.timeframe = timeframe or settings.timeframe.value
        self.poll_seconds = poll_seconds
        self.once = once
        self.provider = provider or build_provider("binance")
        self._owns_provider = provider is None

    def close(self) -> None:
        if self._owns_provider:
            close = getattr(self.provider, "close", None)
            if close is not None:
                close()

    def run_forever(self) -> None:
        """Run until interrupted or a critical integrity halt occurs."""
        try:
            while True:
                try:
                    summary = self.tick()
                    logger.info("scheduler tick: %s", summary)
                except SchedulerHaltedError:
                    logger.exception("scheduler halted on critical integrity failure")
                    raise
                except Exception:
                    logger.exception("scheduler tick failed; will retry")
                if self.once:
                    return
                time.sleep(self.poll_seconds)
        finally:
            self.close()

    def tick(self) -> dict[str, Any]:
        """Fetch, ingest and process all closed candles not yet processed."""
        started = datetime.now(tz=UTC)
        run_id = uuid.uuid4().hex
        self._create_run(run_id, started)

        try:
            result = self._tick_transaction(run_id, started)
            self._finish_run(run_id, "SUCCESS", result)
            return result
        except SchedulerHaltedError as exc:
            self._finish_run(run_id, "FAILED", {"error": str(exc)})
            raise
        except Exception as exc:
            self._finish_run(
                run_id,
                "FAILED",
                {"error": f"{type(exc).__name__}: {exc}"},
            )
            raise

    def _create_run(self, run_id: str, started: datetime) -> None:
        with session_scope() as session:
            session.add(
                SchedulerRun(
                    run_id=run_id,
                    name=RUN_NAME,
                    started_at=started,
                    status="RUNNING",
                    details={"symbol": self.symbol, "timeframe": self.timeframe},
                )
            )

    def _finish_run(self, run_id: str, status: str, details: dict[str, Any]) -> None:
        with session_scope() as session:
            row = session.execute(
                select(SchedulerRun).where(SchedulerRun.run_id == run_id)
            ).scalar_one()
            row.finished_at = datetime.now(tz=UTC)
            row.status = status
            row.candles_processed = int(details.get("candles_processed", 0))
            row.details = details

    def _cursor(self, session) -> datetime | None:
        """Return the last successfully processed candle.

        On the first scheduler run, the existing database tail is the baseline.
        This is important when the database was already populated by the
        historical replay: the scheduler must not replay that history again.
        """
        row = session.execute(
            select(SchedulerRun)
            .where(
                SchedulerRun.name == RUN_NAME,
                SchedulerRun.symbol if False else True,  # kept out of SQL; see filter below
            )
        ).scalars().all()
        successful = [r for r in row if r.status == "SUCCESS"]
        for candidate in reversed(successful):
            value = candidate.details.get("last_processed_open_time")
            if value:
                return datetime.fromisoformat(value)
        return candle_repo.last_open_time(session, self.symbol, self.timeframe)

    def _tick_transaction(self, run_id: str, started: datetime) -> dict[str, Any]:
        config = load_frozen_strategy_config()
        with session_scope() as session:
            cursor = self._cursor(session)

            start = None
            if cursor is not None:
                start = cursor

            raw = self.provider.fetch_candles(
                self.symbol,
                self.timeframe,
                start=start,
                include_incomplete=False,
            )
            ingestor = DataIngestor(
                session,
                symbol=self.symbol,
                timeframe=self.timeframe,
                software_version=SOFTWARE_VERSION,
            )
            ingest_report = ingestor.ingest(raw, start=start, detect_data_gaps=False)

            stored = candle_repo.ordered_closed_candles(
                session,
                self.symbol,
                self.timeframe,
                start=(cursor + timeframe_delta(self.timeframe)) if cursor else None,
            )
            if not stored:
                return {
                    "run_id": run_id,
                    "status": "IDLE",
                    "candles_processed": 0,
                    "cursor": cursor.isoformat() if cursor else None,
                    "provider": self.provider.name,
                    "ingested_created": ingest_report.created,
                }

            history = candle_repo.ordered_closed_candles(
                session,
                self.symbol,
                self.timeframe,
                limit=WARMUP_CANDLES + len(stored),
            )
            process_from = max(0, len(history) - len(stored))

            engine = ReplayEngine(
                config=config,
                symbol=self.symbol,
                timeframe=self.timeframe,
                software_version=SOFTWARE_VERSION,
                on_integrity="halt",
                timeline_limit=0,
            )
            replay = engine.run(
                history,
                session=session,
                process_from=process_from,
            )
            if replay.halted:
                raise SchedulerHaltedError(replay.halt_reason)

            last = stored[-1]
            details = {
                "run_id": run_id,
                "status": "PROCESSED",
                "candles_processed": len(stored),
                "last_processed_open_time": last.open_time.isoformat(),
                "first_processed_open_time": stored[0].open_time.isoformat(),
                "ending_equity": replay.ending_equity,
                "signals": replay.signals,
                "approved": replay.approved,
                "fills": replay.fills,
                "trades": replay.trades,
                "provider": self.provider.name,
                "ingested_created": ingest_report.created,
                "ingested_unchanged": ingest_report.unchanged,
                "strategy_config_hash": config.config_hash,
                "started_at": started.isoformat(),
            }
            return details


def run_scheduler(
    *,
    poll_seconds: int = DEFAULT_POLL_SECONDS,
    once: bool = False,
    symbol: str | None = None,
    timeframe: str | None = None,
) -> None:
    scheduler = PaperScheduler(
        poll_seconds=poll_seconds,
        once=once,
        symbol=symbol,
        timeframe=timeframe,
    )
    scheduler.run_forever()


__all__ = [
    "DEFAULT_POLL_SECONDS",
    "PaperScheduler",
    "SchedulerHaltedError",
    "run_scheduler",
]
