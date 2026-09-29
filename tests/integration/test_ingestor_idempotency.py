"""Ingestion must be deterministic, idempotent and fully accounted for (Phase 2).

The ingestor is the only writer of market data, and it is the place where a
"small" bug (a duplicate, a silent overwrite, a lost rejection) would quietly
poison every downstream number. These tests pin the four promises it makes:

1. the same batch ingested twice changes nothing (idempotency),
2. a genuinely changed candle is *restated* and the change is recorded,
3. out-of-order input is ordered, never appended blindly, and never rewrites
   history that is already stored,
4. rejected candles are counted, named, and written to the integrity log —
   nothing is dropped in silence.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app import SOFTWARE_VERSION
from app.candles.ingestor import DataIngestor
from app.candles.validator import CandleValidator
from app.common.clock import FrozenClock
from app.integrity.enums import EventType, IntegrityCode
from app.market_data.mock import MockProvider, make_candle
from app.models.candle import Candle
from app.models.system_event import SystemEvent
from app.repositories import candles as candle_repo
from app.repositories import data_sources, integrity_events

SYMBOL = "ETHUSDT"
TIMEFRAME = "4h"
BASE = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


def series(length: int, *, start: datetime = BASE, step_hours: int = 4) -> list:
    return [
        make_candle(start + timedelta(hours=step_hours * index), open_price=2500 + index)
        for index in range(length)
    ]


def ingestor(session, *, now: datetime) -> DataIngestor:  # noqa: ANN001
    """An ingestor whose clock is frozen: timestamps must come from the data."""
    return DataIngestor(
        session,
        symbol=SYMBOL,
        timeframe=TIMEFRAME,
        software_version=SOFTWARE_VERSION,
        clock=FrozenClock(now),
    )


def stored(session) -> list[Candle]:  # noqa: ANN001
    return candle_repo.ordered_closed_candles(session, SYMBOL, TIMEFRAME)


def snapshot(session) -> dict[datetime, tuple]:  # noqa: ANN001
    """The stored market data in a form that can be compared before/after."""
    return {
        row.open_time: (row.open, row.high, row.low, row.close, row.volume, row.is_complete)
        for row in stored(session)
    }


# ------------------------------------------------------------------ idempotency


def test_first_ingest_creates_every_candle(session) -> None:  # noqa: ANN001
    candles = series(10)
    report = ingestor(session, now=BASE + timedelta(hours=40)).ingest(candles)
    assert (report.created, report.unchanged, report.rejected) == (10, 0, 0)
    assert len(stored(session)) == 10
    assert report.first_open_time == candles[0].open_time
    assert report.last_open_time == candles[-1].open_time


def test_second_ingest_of_the_same_bytes_is_a_no_op(session) -> None:  # noqa: ANN001
    candles = series(10)
    now = BASE + timedelta(hours=40)
    ingestor(session, now=now).ingest(candles)
    before = snapshot(session)

    report = ingestor(session, now=now).ingest(candles)

    assert report.created == 0
    assert report.unchanged == 10
    assert report.duplicates == 10
    assert report.restated == 0
    after = snapshot(session)
    assert before == after, "re-ingesting the same data changed stored rows"


def test_repeated_ingestion_does_not_grow_the_table(session) -> None:  # noqa: ANN001
    candles = series(25)
    now = BASE + timedelta(hours=100)
    for _ in range(4):
        ingestor(session, now=now).ingest(candles)
    assert session.execute(select(func.count()).select_from(Candle)).scalar() == 25


def test_a_decimally_equal_volume_is_not_treated_as_a_change(session) -> None:  # noqa: ANN001
    """34331.8859000000 and 34331.88590000 are the same number.

    The archive writes a different number of trailing zeros than the database
    does; a naive string comparison would report every candle as restated (a
    real bug this test exists to prevent from returning).
    """
    now = BASE + timedelta(hours=4)
    ingestor(session, now=now).ingest(series(1))
    padded = dataclasses.replace(series(1)[0], volume=Decimal("100.000000000000"))
    report = ingestor(session, now=now).ingest([padded])
    assert report.restated == 0
    assert report.unchanged == 1


# ------------------------------------------------------------------ restatement


def test_a_corrected_candle_is_restated_and_recorded(session) -> None:  # noqa: ANN001
    now = BASE + timedelta(hours=4)
    ingestor(session, now=now).ingest(series(1))

    corrected = dataclasses.replace(series(1)[0], close=Decimal("2600"), high=Decimal("2605"))
    report = ingestor(session, now=now).ingest([corrected])

    assert report.restated == 1
    assert report.created == 0
    assert stored(session)[0].close == Decimal("2600")
    # A restatement is an integrity event, not just a counter: the operator must
    # be able to see *which* numbers changed and to what.
    assert integrity_events.count(session, code=IntegrityCode.DUPLICATE_CANDLE.value) == 1
    violation = integrity_events.recent(session, limit=1, code=IntegrityCode.DUPLICATE_CANDLE.value)[0]
    assert str(violation.severity) == "WARNING"
    # Payload values are canonical decimals: trailing zeros are not significant.
    assert Decimal(violation.details["stored"]["close"]) == Decimal("2500")
    assert Decimal(violation.details["incoming"]["close"]) == Decimal("2600")


def test_a_restatement_is_visible_in_the_system_event_log(session) -> None:  # noqa: ANN001
    """The event log keeps the first observation; the restatement is an alert."""
    now = BASE + timedelta(hours=4)
    ingestor(session, now=now).ingest(series(1))
    corrected = dataclasses.replace(series(1)[0], close=Decimal("2600"), high=Decimal("2605"))
    ingestor(session, now=now).ingest([corrected])
    received = (
        session.execute(select(SystemEvent).where(SystemEvent.event_type == EventType.CANDLE_RECEIVED.value))
        .scalars()
        .all()
    )
    assert [event.payload["open_time"] for event in received] == [BASE.isoformat()]


def test_restatement_leaves_untouched_candles_alone(session) -> None:  # noqa: ANN001
    now = BASE + timedelta(hours=40)
    candles = series(10)
    ingestor(session, now=now).ingest(candles)
    before = snapshot(session)

    changed = dataclasses.replace(candles[5], high=Decimal("9999"))
    ingestor(session, now=now).ingest([changed])

    after = snapshot(session)
    for open_time, values in before.items():
        if open_time != candles[5].open_time:
            assert after[open_time] == values


# ------------------------------------------------------------ ordering and gaps


def test_out_of_order_input_is_stored_in_order(session) -> None:  # noqa: ANN001
    candles = series(6)
    shuffled = [candles[3], candles[0], candles[5], candles[1], candles[4], candles[2]]
    now = BASE + timedelta(hours=24)
    provider = MockProvider(shuffled)
    report = ingestor(session, now=now).ingest_from_provider(provider)

    assert report.created == 6
    assert [row.open_time for row in stored(session)] == [c.open_time for c in candles]


def test_an_out_of_order_series_within_one_batch_is_refused_by_the_validator(
    session,
) -> None:  # noqa: ANN001
    """A candle that goes backwards relative to its own batch is a data error.

    The provider contract says "ascending, no duplicates"; the validator is what
    makes that contract a *fact* rather than a hope, and the rejection names the
    offending candle.
    """
    validator = CandleValidator(SYMBOL, TIMEFRAME)
    later = make_candle(BASE + timedelta(hours=4))
    earlier = make_candle(BASE)
    result = validator.validate(earlier, now=BASE + timedelta(hours=8), previous_open_time=later.open_time)
    assert not result.ok
    assert result.code is IntegrityCode.OUT_OF_ORDER_CANDLE


def test_a_backfill_older_than_stored_history_merges_in_order(session) -> None:  # noqa: ANN001
    """Late-arriving older data is allowed — appended *in order*, never on top.

    (The engine may be handed a month of history after a later month is already
    stored; what must never happen is a re-ordering of what is already there.)
    """
    now = BASE + timedelta(hours=100)
    ingestor(session, now=now).ingest(series(10, start=BASE))
    older = series(3, start=BASE - timedelta(hours=12))

    report = ingestor(session, now=now).ingest(older)

    assert report.created == 3
    rows = stored(session)
    assert len(rows) == 13
    assert [row.open_time for row in rows] == sorted(row.open_time for row in rows)
    assert len({row.open_time for row in rows}) == 13


def test_a_hole_in_the_middle_is_reported_with_its_length(session) -> None:  # noqa: ANN001
    candles = series(8)
    del candles[3:5]  # two missing candles
    now = BASE + timedelta(hours=32)
    report = ingestor(session, now=now).ingest(candles)

    assert len(report.gaps) == 1
    assert report.gaps[0].missing == 2
    assert report.ok is False
    assert candle_repo.count(session, symbol=SYMBOL, timeframe=TIMEFRAME) == 6


def test_a_gap_is_never_filled_in_silently(session) -> None:  # noqa: ANN001
    """After a gap is reported, the missing candles are still missing."""
    candles = series(6)
    missing_time = candles[2].open_time
    ingestor(session, now=BASE + timedelta(hours=24)).ingest(candles[:2] + candles[3:])
    assert missing_time not in candle_repo.open_times(session, SYMBOL, TIMEFRAME)


# -------------------------------------------------------------- rejections


def test_a_broken_candle_is_rejected_counted_and_logged(session) -> None:  # noqa: ANN001
    good = series(3)
    broken = dataclasses.replace(good[1], high=Decimal("1"))  # high below open
    now = BASE + timedelta(hours=12)
    report = ingestor(session, now=now).ingest([good[0], broken, good[2]])

    assert report.created == 2
    assert report.rejected == 1
    assert report.rejections[0]["code"] == IntegrityCode.OHLC_INVARIANT_BROKEN.value
    assert report.rejections[0]["open_time"] == broken.open_time.isoformat()
    assert candle_repo.count(session, symbol=SYMBOL, timeframe=TIMEFRAME) == 2
    assert integrity_events.count(session, code=IntegrityCode.OHLC_INVARIANT_BROKEN.value) == 1


def test_rejection_does_not_stop_the_rest_of_the_batch(session) -> None:  # noqa: ANN001
    candles = series(10)
    hostile = list(candles)
    hostile[4] = dataclasses.replace(candles[4], volume=Decimal("-5"))
    report = ingestor(session, now=BASE + timedelta(hours=40)).ingest(hostile)

    assert report.rejected == 1
    assert report.created == 9
    assert report.last_open_time == candles[-1].open_time


def test_duplicates_inside_one_batch_are_collapsed(session) -> None:  # noqa: ANN001
    candles = series(3)
    report = ingestor(session, now=BASE + timedelta(hours=12)).ingest([*candles, *candles])

    assert report.created == 3
    assert report.duplicates == 3
    assert candle_repo.count(session, symbol=SYMBOL, timeframe=TIMEFRAME) == 3


# ------------------------------------------------------------------ provenance


def test_dataset_provenance_is_recorded_once_per_hash(session) -> None:  # noqa: ANN001
    now = BASE + timedelta(hours=4)
    first, created_first = data_sources.ensure_registered(
        session,
        name="csv_archive",
        dataset_hash="f" * 64,
        symbol=SYMBOL,
        timeframe=TIMEFRAME,
        now=now,
        software_version=SOFTWARE_VERSION,
        candle_count=10,
    )
    second, created_second = data_sources.ensure_registered(
        session,
        name="csv_archive",
        dataset_hash="f" * 64,
        symbol=SYMBOL,
        timeframe=TIMEFRAME,
        now=now,
        software_version=SOFTWARE_VERSION,
        candle_count=10,
    )

    assert created_first is True
    assert created_second is False
    assert first.dataset_hash == second.dataset_hash
    assert data_sources.latest(session, symbol=SYMBOL, timeframe=TIMEFRAME).dataset_hash == "f" * 64


def test_a_different_hash_is_a_new_provenance_row(session) -> None:  # noqa: ANN001
    now = BASE + timedelta(hours=4)
    for digest in ("a" * 64, "b" * 64):
        data_sources.ensure_registered(
            session,
            name="csv_archive",
            dataset_hash=digest,
            symbol=SYMBOL,
            timeframe=TIMEFRAME,
            now=now,
            software_version=SOFTWARE_VERSION,
        )
    rows = data_sources.all_for(session, symbol=SYMBOL, timeframe=TIMEFRAME)
    assert {row.dataset_hash for row in rows} == {"a" * 64, "b" * 64}


@pytest.mark.parametrize("field", ["open", "high", "low", "close", "volume"])
def test_every_ohlcv_field_survives_the_round_trip(session, field: str) -> None:  # noqa: ANN001
    candle = make_candle(
        BASE,
        open_price="2500.12345678",
        high="2600.87654321",
        low="2400.11111111",
        close="2550.22222222",
        volume="1234.5678912345",
    )
    ingestor(session, now=BASE + timedelta(hours=4)).ingest([candle])
    row = stored(session)[0]
    assert getattr(row, field) == getattr(candle, field)
