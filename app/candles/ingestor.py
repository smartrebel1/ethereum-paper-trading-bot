"""Data ingestor — the funnel through which every candle must pass.

Responsibilities:

1. **Validate** each candle (alignment, OHLC, completeness against the clock).
2. **Reject loudly**: an invalid candle never reaches the database; it produces a
   ``CANDLE_REJECTED`` system event plus an integrity row carrying the exact code
   and the offending values.
3. **Enforce chronological order**: candles are processed strictly ascending.
   Re-ingesting an already stored candle is counted as a duplicate — benign when
   byte-identical, an integrity warning when the content differs (that would mean
   the exchange restated history).
4. **Report gaps** over the requested span, without filling them.
5. **Stay idempotent and fast**: database work is batched (one read of known open
   times, one bulk insert), so replaying ~20k candles takes seconds rather than
   minutes, and re-running changes nothing.

The ingestor does not execute anything. It is the only writer of ``candles``.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.candles.gaps import Gap, detect_gaps
from app.candles.validator import CandleValidator
from app.common.clock import Clock, get_clock
from app.common.hashing import canonical_decimal
from app.common.time_utils import ensure_aware_utc, next_open_time
from app.events.bus import EventBus, EventEnvelope
from app.integrity.enums import EventType, IntegrityCode
from app.market_data.base import MarketDataProvider, RawCandle
from app.models.candle import Candle
from app.repositories import candles as candle_repo
from app.repositories import integrity_events, system_events

logger = logging.getLogger(__name__)


@dataclass
class IngestReport:
    """What one ingest call did (returned, logged, and surfaced by the API)."""

    fetched: int = 0
    created: int = 0
    unchanged: int = 0
    rejected: int = 0
    duplicates: int = 0
    restated: int = 0
    gaps: list[Gap] = field(default_factory=list)
    first_open_time: datetime | None = None
    last_open_time: datetime | None = None
    rejections: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.rejected == 0 and not self.gaps

    def summary(self) -> str:
        parts = [
            f"fetched={self.fetched}",
            f"created={self.created}",
            f"unchanged={self.unchanged}",
            f"rejected={self.rejected}",
        ]
        if self.duplicates:
            parts.append(f"duplicates={self.duplicates}")
        if self.restated:
            parts.append(f"restated={self.restated}")
        if self.gaps:
            parts.append(f"gaps={len(self.gaps)} (missing={sum(g.missing for g in self.gaps)})")
        return " ".join(parts)

    def as_dict(self) -> dict[str, Any]:
        return {
            "fetched": self.fetched,
            "created": self.created,
            "unchanged": self.unchanged,
            "rejected": self.rejected,
            "duplicates": self.duplicates,
            "restated": self.restated,
            "first_open_time": self.first_open_time.isoformat() if self.first_open_time else None,
            "last_open_time": self.last_open_time.isoformat() if self.last_open_time else None,
            "gaps": [gap.as_dict() for gap in self.gaps],
            "rejections": self.rejections,
            "ok": self.ok,
        }


class DataIngestor:
    """Validates and stores candles for one (symbol, timeframe)."""

    def __init__(
        self,
        session: Session,
        *,
        symbol: str,
        timeframe: str,
        software_version: str,
        clock: Clock | None = None,
        bus: EventBus | None = None,
    ) -> None:
        self.session = session
        self.symbol = symbol
        self.timeframe = timeframe
        self.software_version = software_version
        self.clock = clock or get_clock()
        self.bus = bus
        self.validator = CandleValidator(symbol, timeframe)

    # ------------------------------------------------------------------ public
    def ingest(
        self,
        candles: Sequence[RawCandle],
        *,
        start: datetime | None = None,
        end_exclusive: datetime | None = None,
        detect_data_gaps: bool = True,
    ) -> IngestReport:
        """Validate + persist a batch in strict chronological order."""
        report = IngestReport(fetched=len(candles))
        if not candles:
            return report

        now = ensure_aware_utc(self.clock.now())
        ordered = self._order(candles, report)

        # One read instead of one query per candle: what is already stored.
        span_start = ordered[0].open_time
        span_end = next_open_time(ordered[-1].open_time, self.timeframe)
        known_open_times = candle_repo.open_times(
            self.session, self.symbol, self.timeframe, start=span_start, end_exclusive=span_end
        )
        known_rows = self._known_rows(span_start, span_end)

        # Only constrain against previously stored history when the batch really
        # continues it. A full replay legitimately starts *before* the newest
        # stored candle, and must not be rejected for that.
        previous = candle_repo.last_open_time(self.session, self.symbol, self.timeframe)
        if previous is not None and previous >= ordered[0].open_time:
            previous = None

        new_rows: list[dict[str, Any]] = []
        updates: list[tuple[datetime, dict[str, Any]]] = []
        events: list[EventEnvelope] = []

        for candle in ordered:
            result = self.validator.validate(candle, now=now, previous_open_time=previous)
            if not result.ok:
                self._reject(candle, result.code, result.message, result.details, report, now)
                continue

            if candle.open_time in known_open_times:
                report.duplicates += 1
                stored = known_rows.get(candle.open_time)
                if stored is not None and self._content_differs(stored, candle):
                    report.restated += 1
                    self._integrity(
                        IntegrityCode.DUPLICATE_CANDLE,
                        "stored candle differs from the freshly fetched one",
                        candle,
                        now,
                        severity="WARNING",
                        stored=self._row_values(stored),
                        incoming={k: str(v) for k, v in candle.to_values().items()},
                    )
                    updates.append((candle.open_time, candle_repo.to_row(candle, now=now)))
                else:
                    report.unchanged += 1
            else:
                new_rows.append(candle_repo.to_row(candle, now=now))
                known_open_times.add(candle.open_time)
                report.created += 1
                events.append(self._envelope(EventType.CANDLE_RECEIVED, candle, now))
                events.append(self._envelope(EventType.CANDLE_VALIDATED, candle, now))

            previous = candle.open_time
            report.first_open_time = report.first_open_time or candle.open_time
            report.last_open_time = candle.open_time

        # Batched writes: one executemany insert, targeted updates, one event batch.
        if new_rows:
            self.session.execute(Candle.__table__.insert(), new_rows)
        for open_time, values in updates:
            self.session.execute(
                update(Candle)
                .where(
                    Candle.symbol == self.symbol,
                    Candle.timeframe == self.timeframe,
                    Candle.open_time == open_time,
                )
                .values(**values)
                # Without this, the ORM evaluates the WHERE clause in Python
                # against every object in the session: O(updates x session size).
                .execution_options(synchronize_session=False)
            )
        system_events.record_many(self.session, events, software_version=self.software_version)
        if self.bus is not None:
            for envelope in events:
                self.bus.publish(envelope)

        if detect_data_gaps:
            report.gaps = self._detect_gaps(start=start, end_exclusive=end_exclusive, now=now)
        return report

    def ingest_from_provider(
        self,
        provider: MarketDataProvider,
        *,
        start: datetime | None = None,
        end_exclusive: datetime | None = None,
        detect_data_gaps: bool = True,
    ) -> IngestReport:
        candles = provider.fetch_candles(self.symbol, self.timeframe, start=start, end=end_exclusive)
        return self.ingest(
            candles, start=start, end_exclusive=end_exclusive, detect_data_gaps=detect_data_gaps
        )

    # ----------------------------------------------------------------- helpers
    def _order(self, candles: Sequence[RawCandle], report: IngestReport) -> list[RawCandle]:
        """Sort ascending and drop exact repeats *within* the batch."""
        ordered = sorted(candles, key=lambda candle: candle.open_time)
        seen: set[datetime] = set()
        deduped: list[RawCandle] = []
        for candle in ordered:
            if candle.open_time in seen:
                report.duplicates += 1
                report.unchanged += 1
                continue
            seen.add(candle.open_time)
            deduped.append(candle)
        return deduped

    def _known_rows(self, start: datetime, end_exclusive: datetime) -> dict[datetime, Candle]:
        stmt = select(Candle).where(
            Candle.symbol == self.symbol,
            Candle.timeframe == self.timeframe,
            Candle.open_time >= start,
            Candle.open_time < end_exclusive,
        )
        return {row.open_time: row for row in self.session.execute(stmt).scalars().all()}

    #: OHLCV columns compared when a candle is re-ingested.
    VALUE_FIELDS = ("open", "high", "low", "close", "volume")

    @classmethod
    def _row_values(cls, row: Candle) -> dict[str, str]:
        """Canonical string form (scale-independent) - used in event payloads."""
        return {field: canonical_decimal(getattr(row, field)) for field in cls.VALUE_FIELDS}

    def _content_differs(self, row: Candle, candle: RawCandle) -> bool:
        """Numeric comparison: ``34331.8859`` and ``34331.8859000000`` are equal.

        Comparing strings here would flag every re-ingest as a "restatement"
        purely because the archive carries 8 decimals and the quantity column
        stores 10 - a false alarm that would train the operator to ignore the
        integrity panel.
        """
        incoming = candle.to_values()
        return any(getattr(row, field) != incoming[field] for field in self.VALUE_FIELDS)

    def _envelope(self, event_type: EventType, candle: RawCandle, now: datetime) -> EventEnvelope:
        return EventEnvelope(
            event_type=event_type.value,
            timestamp=now,
            symbol=candle.symbol,
            timeframe=candle.timeframe,
            entity_id=str(candle_repo.to_row(candle, now=now)["candle_id"]),
            payload={
                "open_time": candle.open_time.isoformat(),
                "close_time": candle.close_time.isoformat(),
                "close": str(candle.close),
                "source": candle.source,
                "is_complete": candle.is_complete,
            },
        )

    def _reject(
        self,
        candle: RawCandle,
        code: IntegrityCode | None,
        message: str,
        details: dict[str, Any],
        report: IngestReport,
        now: datetime,
    ) -> None:
        report.rejected += 1
        resolved = code or IntegrityCode.MISALIGNED_CANDLE
        rejection = {
            "open_time": candle.open_time.isoformat() if candle.open_time else None,
            "code": resolved.value,
            "message": message,
        }
        report.rejections.append(rejection)
        logger.warning(
            "candle rejected",
            extra={
                "code": resolved.value,
                "open_time": rejection["open_time"],
                "detail": message,
                "symbol": self.symbol,
                "timeframe": self.timeframe,
            },
        )
        self._integrity(resolved, message, candle, now, severity="WARNING", **details)
        envelope = self._envelope(EventType.CANDLE_REJECTED, candle, now)
        envelope = EventEnvelope(
            event_type=envelope.event_type,
            timestamp=envelope.timestamp,
            symbol=envelope.symbol,
            timeframe=envelope.timeframe,
            entity_id=envelope.entity_id,
            payload={**envelope.payload, "code": resolved.value, "detail": message, **details},
        )
        system_events.record_event(self.session, envelope, software_version=self.software_version)
        if self.bus is not None:
            self.bus.publish(envelope)

    def _detect_gaps(
        self,
        *,
        start: datetime | None,
        end_exclusive: datetime | None,
        now: datetime,
    ) -> list[Gap]:
        first = candle_repo.first_candle(self.session, self.symbol, self.timeframe)
        last = candle_repo.last_candle(self.session, self.symbol, self.timeframe)
        if first is None or last is None:
            return []

        span_start = start or first.open_time
        span_end = end_exclusive or last.close_time
        present = candle_repo.open_times(
            self.session, self.symbol, self.timeframe, start=span_start, end_exclusive=span_end
        )
        gaps = detect_gaps(present, span_start, span_end, self.timeframe)
        for gap in gaps:
            self._emit(
                EventEnvelope(
                    event_type=EventType.DATA_GAP_DETECTED.value,
                    timestamp=now,
                    symbol=self.symbol,
                    timeframe=self.timeframe,
                    entity_id=f"{gap.start.isoformat()}..{gap.end_exclusive.isoformat()}",
                    payload=gap.as_dict(),
                ),
                now,
                integrity=IntegrityCode.DATA_GAP,
            )
        return gaps

    def _emit(
        self, envelope: EventEnvelope, now: datetime, *, integrity: IntegrityCode | None = None
    ) -> None:
        if integrity is not None:
            integrity_events.record_violation(
                self.session,
                code=integrity,
                detected_at=now,
                software_version=self.software_version,
                entity_id=envelope.entity_id,
                symbol=self.symbol,
                timeframe=self.timeframe,
                severity="WARNING",
                details=envelope.payload,
            )
        system_events.record_event(self.session, envelope, software_version=self.software_version)
        if self.bus is not None:
            self.bus.publish(envelope)

    def _integrity(
        self,
        code: IntegrityCode,
        message: str,
        candle: RawCandle,
        now: datetime,
        *,
        severity: str | None = None,
        **details: Any,
    ) -> None:
        integrity_events.record_violation(
            self.session,
            code=code,
            detected_at=now,
            software_version=self.software_version,
            entity_id=candle.open_time.isoformat(),
            symbol=self.symbol,
            timeframe=self.timeframe,
            severity=severity,
            details={"message": message, **details},
        )


__all__ = ["DataIngestor", "IngestReport"]
