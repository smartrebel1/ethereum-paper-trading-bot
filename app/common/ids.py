"""Deterministic id helpers.

Every persisted entity gets an id that is a pure function of its semantic
inputs. Consequences (all desirable):

* Re-ingesting the same candle produces the same ``candle_id`` -> the unique
  index turns a duplicate into a no-op instead of a second row.
* Replaying history from scratch reproduces byte-identical rows, so a backtest
  and a live paper run can be diffed row by row.
* No UUIDs, no autoincrement for anything that matters. Autoincrement ids are
  only used for pure log/bookkeeping tables (see models).

The id format is ``PREFIX::payload`` where payload is either a readable
composite (candle ids) or a truncated sha256 digest (everything else).
"""

from __future__ import annotations

from datetime import datetime

from app.common.enums import Timeframe
from app.common.hashing import ID_DIGEST_LEN, short_digest
from app.common.time_utils import to_utc_iso_z

PREFIX_CANDLE = "CDL"
PREFIX_SIGNAL = "SIG"
PREFIX_ORDER = "ORD"
PREFIX_EXECUTION = "EXE"
PREFIX_POSITION = "POS"
PREFIX_TRADE = "TRD"
PREFIX_EVENT = "EVT"
PREFIX_INTEGRITY = "INT"
PREFIX_OBSERVATION = "OBS"
PREFIX_SNAPSHOT = "SNP"
PREFIX_RUN = "RUN"


def det_id(prefix: str, *parts: object) -> str:
    """``PREFIX::<first 32 hex of sha256(joined parts)>``.

    ``parts`` are stringified with the canonical rules from
    :mod:`app.common.hashing` (datetimes must already be ISO-UTC strings).
    """
    payload = "|".join(str(part) for part in parts)
    return f"{prefix}::{short_digest(payload)}"


def candle_id(symbol: str, timeframe: Timeframe | str, open_time: datetime) -> str:
    """Readable, deterministic candle identity (matches candles.candle_id PK)."""
    tf = timeframe.value if isinstance(timeframe, Timeframe) else timeframe
    return f"{PREFIX_CANDLE}::{symbol}::{tf}::{to_utc_iso_z(open_time)}"


def signal_id(
    symbol: str,
    timeframe: Timeframe | str,
    candle_open_time: datetime,
    strategy_name: str,
    strategy_version: str,
    strategy_config_hash: str,
) -> str:
    """Signal identity = (what, where, when, which strategy build).

    Two runs of the same strategy over the same closed candle are the *same*
    signal, so the second insert is rejected by the primary key instead of
    double-trading. A config change (hash) yields a different id by design.
    """
    tf = timeframe.value if isinstance(timeframe, Timeframe) else timeframe
    return det_id(
        PREFIX_SIGNAL,
        symbol,
        tf,
        to_utc_iso_z(candle_open_time),
        strategy_name,
        strategy_version,
        strategy_config_hash,
    )


def order_id(sig_id: str) -> str:
    """One order per signal (enforced by ``orders.signal_id`` unique index)."""
    return det_id(PREFIX_ORDER, sig_id)


def execution_id(ord_id: str, target_execution_open_time: datetime) -> str:
    """Execution identity includes the target candle: a missed target can never
    be "retried" under the same execution id."""
    return det_id(PREFIX_EXECUTION, ord_id, to_utc_iso_z(target_execution_open_time))


def position_id(symbol: str, timeframe: Timeframe | str, entry_order_id: str) -> str:
    tf = timeframe.value if isinstance(timeframe, Timeframe) else timeframe
    return det_id(PREFIX_POSITION, symbol, tf, entry_order_id)


def trade_id(position_id_: str) -> str:
    """One closed trade per position round-trip."""
    return det_id(PREFIX_TRADE, position_id_)


def snapshot_id(symbol: str, timeframe: Timeframe | str, at_candle_open_time: datetime) -> str:
    tf = timeframe.value if isinstance(timeframe, Timeframe) else timeframe
    return det_id(PREFIX_SNAPSHOT, symbol, tf, to_utc_iso_z(at_candle_open_time))


def event_id(kind: str, entity_id: str, ts: datetime, seq: int = 0) -> str:
    """Event identity. ``seq`` disambiguates identical events in the same
    microsecond (e.g. two CANDLE_RECEIVED for the same candle in one tick)."""
    return det_id(PREFIX_EVENT, kind, entity_id, to_utc_iso_z(ts), seq)


def integrity_event_id(code: str, entity_id: str, ts: datetime, seq: int = 0) -> str:
    return det_id(PREFIX_INTEGRITY, code, entity_id or "-", to_utc_iso_z(ts), seq)


def observation_id(provider: str, symbol: str, timeframe: str, at: datetime, seq: int = 0) -> str:
    return det_id(PREFIX_OBSERVATION, provider, symbol, timeframe, to_utc_iso_z(at), seq)


def scheduler_run_id(name: str, started_at: datetime) -> str:
    return det_id(PREFIX_RUN, name, to_utc_iso_z(started_at))


__all__ = [
    "ID_DIGEST_LEN",
    "PREFIX_CANDLE",
    "PREFIX_EVENT",
    "PREFIX_EXECUTION",
    "PREFIX_INTEGRITY",
    "PREFIX_OBSERVATION",
    "PREFIX_ORDER",
    "PREFIX_POSITION",
    "PREFIX_RUN",
    "PREFIX_SIGNAL",
    "PREFIX_SNAPSHOT",
    "PREFIX_TRADE",
    "candle_id",
    "det_id",
    "event_id",
    "execution_id",
    "integrity_event_id",
    "observation_id",
    "order_id",
    "position_id",
    "scheduler_run_id",
    "signal_id",
    "snapshot_id",
    "trade_id",
]
