"""The replay loop.

Given an ordered list of *closed* candles, run them through the same engines a
live paper run uses, and report what happened. Determinism requirements, all
enforced here:

* the clock is frozen to the candle's close time (``FrozenClock``), so every
  timestamp written during processing a candle is a function of that candle —
  never of wall-clock time. Re-running months later reproduces the same rows.
* candles are processed strictly in ascending order, exactly once.
* a candle that is not complete is refused with an integrity event rather than
  silently used.

Halt policy: by default the run **stops** at the first CRITICAL integrity event
(a missed execution target means the series is not what the engine believes).
``on_integrity="continue"`` is available for diagnostics and is recorded in the
result, so a "continue" run can never be mistaken for a clean one.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.candles.gaps import Gap, find_missing_open_times
from app.common.clock import FrozenClock
from app.common.enums import Action
from app.common.time_utils import next_open_time, timeframe_delta
from app.config.strategy_config import StrategyConfig
from app.database.session import session_scope
from app.events.bus import EventBus
from app.execution.paper import ExecutionReport, PaperExecutionEngine
from app.integrity.enums import EventType
from app.models.candle import Candle
from app.portfolio import accounting
from app.repositories import candles as candle_repo
from app.repositories import integrity_events, system_events
from app.repositories import positions as position_repo
from app.repositories import signals as signal_repo
from app.risk.engine import RiskEngine
from app.strategy.ema_atr import Decision, EMAAtrStrategy

logger = logging.getLogger(__name__)


@dataclass
class ReplayStep:
    """Per-candle trace (used by the dashboard and by failure diagnosis)."""

    open_time: datetime
    close: str
    equity: str
    decision: str
    detail: str = ""
    filled: list[str] = field(default_factory=list)
    missed: list[str] = field(default_factory=list)
    voided: list[str] = field(default_factory=list)
    opened: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)


@dataclass
class ReplayResult:
    """Summary of a replay run."""

    processed: int = 0
    signals: int = 0
    approved: int = 0
    rejected: int = 0
    fills: int = 0
    trades: int = 0
    missed_targets: int = 0
    #: Orders voided because their target fell in a pre-declared data hole.
    voided_targets: int = 0
    gaps: list[Gap] = field(default_factory=list)
    halted: bool = False
    halt_reason: str = ""
    first_open_time: datetime | None = None
    last_open_time: datetime | None = None
    starting_equity: str = "0"
    ending_equity: str = "0"
    next_expected_open_time: datetime | None = None
    timeline: list[ReplayStep] = field(default_factory=list)
    suppressed: dict[str, int] = field(default_factory=dict)
    rejections: dict[str, int] = field(default_factory=dict)

    def as_dict(self, *, timeline_limit: int = 0) -> dict[str, Any]:
        return {
            "processed": self.processed,
            "signals": self.signals,
            "approved": self.approved,
            "rejected": self.rejected,
            "fills": self.fills,
            "trades": self.trades,
            "missed_targets": self.missed_targets,
            "voided_targets": self.voided_targets,
            "gaps": [gap.as_dict() for gap in self.gaps],
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "first_open_time": self.first_open_time.isoformat() if self.first_open_time else None,
            "last_open_time": self.last_open_time.isoformat() if self.last_open_time else None,
            "starting_equity": self.starting_equity,
            "ending_equity": self.ending_equity,
            "next_expected_open_time": (
                self.next_expected_open_time.isoformat() if self.next_expected_open_time else None
            ),
            "suppressed": dict(self.suppressed),
            "rejections": dict(self.rejections),
            "timeline": [
                {
                    "open_time": step.open_time.isoformat(),
                    "close": step.close,
                    "equity": step.equity,
                    "decision": step.decision,
                    "detail": step.detail,
                    "filled": step.filled,
                    "missed": step.missed,
                    "voided": step.voided,
                    "opened": step.opened,
                    "closed": step.closed,
                }
                for step in (self.timeline[-timeline_limit:] if timeline_limit else self.timeline)
            ],
        }


class ReplayEngine:
    """Candle-driven replay over one (symbol, timeframe) series."""

    def __init__(
        self,
        *,
        config: StrategyConfig,
        symbol: str,
        timeframe: str,
        software_version: str,
        starting_balance: str | float = "200",
        slippage_bps: str | float = 0,
        on_integrity: str = "halt",
        bus: EventBus | None = None,
        timeline_limit: int | None = None,
        progress: Callable[[int, int, ReplayStep], None] | None = None,
    ) -> None:
        if on_integrity not in {"halt", "continue"}:
            raise ValueError("on_integrity must be 'halt' or 'continue'")
        self.config = config
        self.symbol = symbol
        self.timeframe = timeframe
        self.software_version = software_version
        self.starting_balance = accounting.money(starting_balance)
        self.slippage_bps = slippage_bps
        self.on_integrity = on_integrity
        self.bus = bus
        self.timeline_limit = timeline_limit
        self.progress = progress

    # -------------------------------------------------------------------- run
    def run(
        self,
        candles: Sequence[Candle],
        *,
        session: Session | None = None,
        process_from: int = 0,
    ) -> ReplayResult:
        """Replay closed candles.

        ``process_from`` supports the live paper scheduler: the leading candles
        are used only to rebuild the strategy warm-up window, while execution
        and decision-making start at that zero-based index. This keeps the
        incremental path on the exact same strategy/risk/execution code as
        historical replay.
        """
        if process_from < 0 or process_from > len(candles):
            raise ValueError("process_from must be between 0 and len(candles)")
        if session is not None:
            return self._run(candles, session, process_from=process_from)
        with session_scope() as scoped:
            return self._run(candles, scoped, process_from=process_from)

    # ------------------------------------------------------------------- internals
    def _run(
        self,
        candles: Sequence[Candle],
        session: Session,
        *,
        process_from: int = 0,
    ) -> ReplayResult:
        result = ReplayResult()
        strategy = EMAAtrStrategy(self.config)
        window: list[Candle] = []

        starting_state = accounting.compute_state(
            session,
            symbol=self.symbol,
            timeframe=self.timeframe,
            starting_balance=self.starting_balance,
            last_price=candles[0].open if candles else accounting.money(0),
            position=None,
        )
        result.starting_equity = str(starting_state.equity)

        # Declare, before any execution happens, which execution targets this
        # series can never satisfy. Derived from the series itself, so the run
        # is still a pure function of its inputs.
        unreachable = self._unreachable_open_times(candles)

        total = len(candles)
        for index, candle in enumerate(candles, start=1):
            # The leading warm-up window is intentionally read-only. It rebuilds
            # strategy state without replaying historical side effects.
            if index <= process_from:
                window.append(candle)
                continue

            # Deterministic time: every timestamp written while processing this            # candle is derived from the candle itself, never from the wall clock.
            clock = FrozenClock(candle.close_time)
            execution = PaperExecutionEngine(
                session,
                config=self.config,
                symbol=self.symbol,
                timeframe=self.timeframe,
                software_version=self.software_version,
                slippage_bps=self.slippage_bps,
                clock=clock,
                bus=self.bus,
                unreachable_open_times=unreachable,
            )
            risk = RiskEngine(
                session,
                config=self.config,
                symbol=self.symbol,
                timeframe=self.timeframe,
                software_version=self.software_version,
                clock=clock,
                bus=self.bus,
            )

            # 1. settle what the previous candles committed to
            execution_report: ExecutionReport = execution.on_candle(candle)
            result.voided_targets += len(execution_report.unreachable)
            result.fills += len(execution_report.filled)
            result.trades += len(execution_report.closed)
            if execution_report.missed:
                result.missed_targets += len(execution_report.missed)
                result.halted = True
                result.halt_reason = "execution target missed"
                self._halt(candle, execution_report)
                if self.on_integrity == "halt":
                    break
                result.halted = False

            # 2. decide on this closed candle
            window.append(candle)
            armed = window[-strategy.warmup_candles() :]
            decision = strategy.evaluate(armed)
            step = self._step(candle, decision, execution_report, session)

            if decision.is_entry:
                signal, created = signal_repo.persist(
                    session, strategy.build_signal(decision), now=clock.now()
                )
                if created:
                    result.signals += 1
                    self._emit(
                        session,
                        clock,
                        EventType.SIGNAL_CREATED,
                        signal.signal_id,
                        {
                            "action": signal.action,
                            "confidence": str(signal.confidence),
                            "signal_candle_open_time": signal.signal_candle_open_time.isoformat(),
                            "target_execution_open_time": signal.target_execution_open_time.isoformat(),
                            "target_execution_candle_id": signal.target_execution_candle_id,
                            "strategy_config_hash": signal.strategy_config_hash,
                            "indicator_context": signal.indicator_context,
                        },
                    )
                    state = accounting.compute_state(
                        session,
                        symbol=self.symbol,
                        timeframe=self.timeframe,
                        starting_balance=self.starting_balance,
                        last_price=candle.close,
                        position=position_repo.open_position(session, self.symbol, self.timeframe),
                    )
                    decision_row, order = risk.decide(signal, state=state, reference_price=candle.close)
                    if decision_row.outcome == "APPROVED":
                        result.approved += 1
                        step.detail = f"order {order.order_id if order else '-'} queued"
                    else:
                        result.rejected += 1
                        result.rejections[decision_row.reason_code] = (
                            result.rejections.get(decision_row.reason_code, 0) + 1
                        )
                        step.detail = f"risk rejected: {decision_row.reason_code}"
            else:
                if decision.suppressed_reason:
                    result.suppressed[decision.suppressed_reason] = (
                        result.suppressed.get(decision.suppressed_reason, 0) + 1
                    )
                self._emit(
                    session,
                    clock,
                    EventType.SIGNAL_SUPPRESSED,
                    f"{self.symbol}:{self.timeframe}:{candle.open_time.isoformat()}",
                    {
                        "reason": decision.suppressed_reason or "NO_SETUP",
                        "detail": decision.reason,
                        "indicator_context": decision.snapshot.as_dict(),
                    },
                )

            # 3. equity point
            snapshot = accounting.snapshot(
                session,
                symbol=self.symbol,
                timeframe=self.timeframe,
                candle_open_time=candle.open_time,
                starting_balance=self.starting_balance,
                last_price=candle.close,
                position=position_repo.open_position(session, self.symbol, self.timeframe),
                now=clock.now(),
            )
            step.equity = str(getattr(snapshot, "equity", "0"))
            self._emit(
                session,
                clock,
                EventType.PORTFOLIO_SNAPSHOT_TAKEN,
                getattr(snapshot, "snapshot_id", "snapshot"),
                {"equity": step.equity, "at": candle.open_time.isoformat()},
            )

            result.processed += 1
            result.first_open_time = result.first_open_time or candle.open_time
            result.last_open_time = candle.open_time
            result.next_expected_open_time = next_open_time(candle.open_time, self.timeframe)
            if self.timeline_limit is None or len(result.timeline) < self.timeline_limit:
                result.timeline.append(step)
            if self.progress is not None:
                self.progress(index, total, step)

        final_state = accounting.compute_state(
            session,
            symbol=self.symbol,
            timeframe=self.timeframe,
            starting_balance=self.starting_balance,
            last_price=candles[-1].close if candles else accounting.money(0),
            position=position_repo.open_position(session, self.symbol, self.timeframe),
        )
        result.ending_equity = str(final_state.equity)
        return result

    # ----------------------------------------------------------------- helpers
    def _step(
        self,
        candle: Candle,
        decision: Decision,
        report: ExecutionReport,
        session: Session,
    ) -> ReplayStep:
        state = accounting.compute_state(
            session,
            symbol=self.symbol,
            timeframe=self.timeframe,
            starting_balance=self.starting_balance,
            last_price=candle.close,
            position=position_repo.open_position(session, self.symbol, self.timeframe),
        )
        return ReplayStep(
            open_time=candle.open_time,
            close=str(candle.close),
            equity=str(state.equity),
            decision=decision.action.value if isinstance(decision.action, Action) else str(decision.action),
            detail=decision.reason,
            filled=list(report.filled),
            missed=list(report.missed),
            voided=list(report.unreachable),
            opened=list(report.opened),
            closed=list(report.closed),
        )

    def _unreachable_open_times(self, candles: Sequence[Candle]) -> frozenset[datetime]:
        """Open times this series provably cannot supply (venue data holes).

        Uses the same generator the ingestor uses, so the engine and the
        ingest-time gap report can never disagree about where the holes are.
        """
        if not candles:
            return frozenset()
        present = {candle.open_time for candle in candles}
        last = candles[-1].open_time
        step = timeframe_delta(self.timeframe)
        missing = find_missing_open_times(present, candles[0].open_time, last + step, self.timeframe)
        if missing:
            logger.warning(
                "replay series has declared data holes",
                extra={"detail": f"{len(missing)} unreachable execution target(s)"},
            )
        return frozenset(missing)

    def _halt(self, candle: Candle, report: ExecutionReport) -> None:
        logger.error(
            "replay halted on a missed execution target",
            extra={
                "candle_open_time": candle.open_time.isoformat(),
                "missed_orders": report.missed,
            },
        )

    def _emit(
        self,
        session: Session,
        clock: FrozenClock,
        event_type: EventType,
        entity_id: str,
        payload: dict[str, Any],
    ) -> None:
        from app.events.bus import EventEnvelope

        envelope = EventEnvelope(
            event_type=event_type.value,
            timestamp=clock.now(),
            symbol=self.symbol,
            timeframe=self.timeframe,
            entity_id=entity_id,
            payload=payload,
        )
        system_events.record_event(session, envelope, software_version=self.software_version)
        if self.bus is not None:
            self.bus.publish(envelope)


# --------------------------------------------------------------------- helpers
def load_series(
    session: Session,
    *,
    symbol: str,
    timeframe: str,
    start: datetime | None = None,
    end_exclusive: datetime | None = None,
    limit: int | None = None,
) -> list[Candle]:
    """Closed candles in strict ascending order (the replay input)."""
    candles = candle_repo.ordered_closed_candles(
        session, symbol, timeframe, start=start, end_exclusive=end_exclusive
    )
    if limit is not None:
        candles = candles[-limit:]
    return candles


def critical_integrity_count(session: Session) -> int:
    return integrity_events.count(session)


def assert_no_critical_integrity(session: Session) -> None:
    if integrity_events.has_fatal(session):
        raise RuntimeError("critical integrity events present - refusing to continue")
