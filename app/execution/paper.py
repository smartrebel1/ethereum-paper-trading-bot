"""Paper execution engine — where the central invariant lives.

The engine is **driven by candle arrival**, never by a timer::

    for each closed candle C (strictly ascending):
        1. settle open positions against C     (SL/TP, stop-first intrabar policy)
        2. execute pending orders whose target is exactly C.open_time
        3. flag any pending order whose target was skipped -> TARGET_MISSED

The invariant, checked immediately before any fill:

    C.open_time == order.target_execution_open_time
    C.candle_id == order.target_execution_candle_id
    C.is_complete is True

If a candle arrives *past* a pending order's target, the order becomes
``TARGET_MISSED`` and a CRITICAL integrity event is written — no fill, no
"next best candle". Inventing a fill would make the ``strategy_config_hash``
stamped on the signal a lie (docs/DECISIONS.md, ADR-012).

Price model (frozen; changing it changes the config hash):

    entry actual = candle.open  * (1 + slippage_bps/10000)     for BUY
    exit  actual = level        * (1 - slippage_bps/10000)     for a SELL exit
    fee          = notional * fee_rate                         (entry and exit)

Baseline ``slippage_bps = 0``: archive klines carry no book depth, so inventing
slippage would be fiction. The hook exists so a later phase can plug in a
*deterministic* model instead of an ad-hoc fudge factor.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.common.clock import Clock, get_clock
from app.common.enums import CloseCause, ExitReason, OrderState, PositionState, Side
from app.common.errors import ExecutionTargetMissedError
from app.common.ids import execution_id as make_execution_id
from app.common.rounding import money, to_decimal
from app.common.rounding import price as price_of
from app.common.rounding import qty as qty_of
from app.config.strategy_config import StrategyConfig
from app.events.bus import EventBus, EventEnvelope
from app.execution.state import transition
from app.integrity.enums import EventType, IntegrityCode
from app.ledger import ledger
from app.models.candle import Candle
from app.models.execution import Execution
from app.models.order import Order
from app.models.position import Position
from app.repositories import integrity_events, system_events
from app.repositories import orders as order_repo
from app.repositories import positions as position_repo

logger = logging.getLogger(__name__)

ZERO = Decimal(0)
BPS_DENOMINATOR = Decimal(10_000)
OPEN_STATES = (OrderState.CREATED.value, OrderState.PENDING.value)


@dataclass
class ExecutionReport:
    """What one candle did to the order book."""

    candle_open_time: datetime | None = None
    filled: list[str] = field(default_factory=list)
    missed: list[str] = field(default_factory=list)
    #: Orders whose target candle is *declared absent* by the series itself
    #: (a venue data hole known before the run). They are voided, not missed:
    #: no fill, no substitute candle, but also no halt — the absence is a
    #: property of the published data, not evidence that the engine is confused.
    unreachable: list[str] = field(default_factory=list)
    opened: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    skipped: bool = False
    detail: str = ""

    @property
    def ok(self) -> bool:
        return not self.missed

    def as_dict(self) -> dict[str, object]:
        return {
            "candle_open_time": self.candle_open_time.isoformat() if self.candle_open_time else None,
            "filled": list(self.filled),
            "missed": list(self.missed),
            "opened": list(self.opened),
            "closed": list(self.closed),
            "skipped": self.skipped,
            "detail": self.detail,
            "ok": self.ok,
        }


class PaperExecutionEngine:
    """Deterministic paper fills with an explicit execution target."""

    def __init__(
        self,
        session: Session,
        *,
        config: StrategyConfig,
        symbol: str,
        timeframe: str,
        software_version: str,
        slippage_bps: Decimal | int | str = 0,
        clock: Clock | None = None,
        bus: EventBus | None = None,
        fee_rate: Decimal | None = None,
        unreachable_open_times: Iterable[datetime] | None = None,
    ) -> None:
        self.session = session
        self.config = config
        self.symbol = symbol
        self.timeframe = timeframe
        self.software_version = software_version
        self.slippage_bps = to_decimal(slippage_bps)
        self.fee_rate = to_decimal(fee_rate) if fee_rate is not None else config.fee_rate_decimal
        self.clock = clock or get_clock()
        self.bus = bus
        #: Execution targets that the series provably cannot satisfy. Declared
        #: up front (from the same gap detection the ingestor uses) so that a
        #: data hole is a *decision*, never a surprise at fill time.
        self.unreachable_open_times: frozenset[datetime] = frozenset(unreachable_open_times or ())

    # ------------------------------------------------------------------ driven
    def on_candle(self, candle: Candle) -> ExecutionReport:
        """Advance the engine by one closed candle (its only clock)."""
        report = ExecutionReport(candle_open_time=candle.open_time)
        if not candle.is_complete:
            report.skipped = True
            report.detail = "candle is not complete"
            return report

        self._settle_positions(candle, report)
        self._execute_due_orders(candle, report)
        self._detect_missed_targets(candle, report)
        return report

    # ------------------------------------------------------------------ queries
    def _due_orders(self, candle_open_time: datetime) -> list[Order]:
        stmt = (
            select(Order)
            .where(
                Order.symbol == self.symbol,
                Order.timeframe == self.timeframe,
                Order.state.in_(OPEN_STATES),
                Order.target_execution_open_time == candle_open_time,
            )
            .order_by(Order.created_at.asc())
        )
        return list(self.session.execute(stmt).scalars().all())

    def _overdue_orders(self, candle_open_time: datetime) -> list[Order]:
        stmt = (
            select(Order)
            .where(
                Order.symbol == self.symbol,
                Order.timeframe == self.timeframe,
                Order.state.in_(OPEN_STATES),
                Order.target_execution_open_time < candle_open_time,
            )
            .order_by(Order.target_execution_open_time.asc())
        )
        return list(self.session.execute(stmt).scalars().all())

    def _execute_due_orders(self, candle: Candle, report: ExecutionReport) -> None:
        for order in self._due_orders(candle.open_time):
            self._fill_entry(order, candle, report)

    def _detect_missed_targets(self, candle: Candle, report: ExecutionReport) -> None:
        for order in self._overdue_orders(candle.open_time):
            if order.target_execution_open_time in self.unreachable_open_times:
                self._void_unreachable(order, candle, report)
            else:
                self._mark_missed(order, candle, report)

    # -------------------------------------------------------------------- fills
    def _fill_entry(self, order: Order, candle: Candle, report: ExecutionReport) -> None:
        """Execute a pending ENTRY order at the open of its target candle."""
        self._assert_target(order, candle)

        transition(
            self.session,
            order,
            OrderState.TARGET_REACHED,
            reason=f"candle {candle.open_time.isoformat()} matches target",
            software_version=self.software_version,
            clock=self.clock,
        )
        self._emit(
            EventType.ORDER_EXECUTION_TARGET_REACHED,
            entity_id=order.order_id,
            payload={
                "order_id": order.order_id,
                "candle_id": candle.candle_id,
                "candle_open_time": candle.open_time.isoformat(),
            },
        )

        raw_price = price_of(candle.open)
        actual_price = self._apply_slippage(raw_price, Side(order.side))
        quantity = qty_of(order.intended_quantity)
        notional = money(quantity * actual_price)
        fee = money(notional * self.fee_rate)

        execution = self._write_execution(
            order=order,
            candle=candle,
            raw_price=raw_price,
            actual_price=actual_price,
            quantity=quantity,
            notional=notional,
            fee=fee,
        )
        transition(
            self.session,
            order,
            OrderState.EXECUTED,
            reason="filled at target candle open",
            software_version=self.software_version,
            clock=self.clock,
        )
        report.filled.append(order.order_id)
        self._emit(
            EventType.ORDER_EXECUTED,
            entity_id=execution.execution_id,
            payload={
                "order_id": order.order_id,
                "execution_id": execution.execution_id,
                "raw_price": str(raw_price),
                "price": str(actual_price),
                "quantity": str(quantity),
                "notional": str(notional),
                "fee": str(fee),
                "slippage_bps": str(self.slippage_bps),
                "purpose": order.purpose,
            },
        )

        if order.purpose == "ENTRY":
            position = self._open_position(order, execution)
            if position is not None:
                report.opened.append(position.position_id)
                # The entry candle itself may already contain the stop/target:
                # we know the entry happened at the open, so this is not
                # look-ahead, and the stop-first policy keeps it conservative.
                self._settle_positions(candle, report, position=position)

    def _write_execution(
        self,
        *,
        order: Order,
        candle: Candle,
        raw_price: Decimal,
        actual_price: Decimal,
        quantity: Decimal,
        notional: Decimal,
        fee: Decimal,
    ) -> Execution:
        execution = Execution(
            execution_id=make_execution_id(order.order_id, candle.open_time),
            order_id=order.order_id,
            symbol=order.symbol,
            timeframe=order.timeframe,
            side=order.side,
            execution_candle_open_time=candle.open_time,
            execution_candle_id=candle.candle_id,
            raw_open_price=price_of(raw_price),
            slippage_bps=self.slippage_bps,
            actual_execution_price=price_of(actual_price),
            quantity=qty_of(quantity),
            notional=money(notional),
            fee=money(fee),
            execution_timestamp=self.clock.now(),
            provider="paper",
        )
        self.session.add(execution)
        self.session.flush()
        return execution

    def _apply_slippage(self, raw_price: Decimal, side: Side) -> Decimal:
        if self.slippage_bps == ZERO:
            return price_of(raw_price)
        factor = self.slippage_bps / BPS_DENOMINATOR
        adjusted = (
            raw_price * (Decimal(1) + factor) if side is Side.BUY else raw_price * (Decimal(1) - factor)
        )
        return price_of(adjusted)

    def _assert_target(self, order: Order, candle: Candle) -> None:
        """The hard invariant. Raising here is the *correct* outcome."""
        if candle.open_time != order.target_execution_open_time:
            self._integrity(
                IntegrityCode.EXECUTION_TARGET_MISMATCH,
                "fill attempted against a candle that is not the order's target",
                entity_id=order.order_id,
                severity="CRITICAL",
                order_target=order.target_execution_open_time.isoformat(),
                candle_open_time=candle.open_time.isoformat(),
            )
            raise ExecutionTargetMissedError(
                IntegrityCode.EXECUTION_TARGET_MISMATCH.value,
                "candle open_time does not equal the order's execution target",
            )
        if candle.candle_id != order.target_execution_candle_id:
            self._integrity(
                IntegrityCode.EXECUTION_TARGET_MISMATCH,
                "candle id does not equal the order's target candle id",
                entity_id=order.order_id,
                severity="CRITICAL",
                expected=order.target_execution_candle_id,
                actual=candle.candle_id,
            )
            raise ExecutionTargetMissedError(
                IntegrityCode.EXECUTION_TARGET_MISMATCH.value, "candle id mismatch"
            )
        if not candle.is_complete:
            self._integrity(
                IntegrityCode.INCOMPLETE_CANDLE_USED,
                "fill attempted against an incomplete candle",
                entity_id=order.order_id,
                severity="CRITICAL",
                candle_open_time=candle.open_time.isoformat(),
            )
            raise ExecutionTargetMissedError(
                IntegrityCode.INCOMPLETE_CANDLE_USED.value, "candle is not complete"
            )

    def _void_unreachable(self, order: Order, candle: Candle, report: ExecutionReport) -> None:
        """Void an order whose target candle does not exist in the archive.

        The target time is in the pre-declared missing set, so there is nothing
        to wait for: no candle will ever carry it. The order is cancelled with a
        named reason and a WARNING (not CRITICAL) integrity observation. A
        position held by the order is re-opened so the next candle can re-arm
        the exit — an exit level is a price condition, not a time target, so it
        survives the hole. Nothing is ever filled at a substitute candle.
        """
        transition(
            self.session,
            order,
            OrderState.CANCELLED,
            reason=f"target unreachable: no candle exists at {order.target_execution_open_time.isoformat()}",
            software_version=self.software_version,
            clock=self.clock,
        )
        self._emit(
            EventType.ORDER_TARGET_UNREACHABLE,
            entity_id=order.order_id,
            payload={
                "order_id": order.order_id,
                "purpose": order.purpose,
                "target_execution_open_time": order.target_execution_open_time.isoformat(),
                "voided_at_candle": candle.open_time.isoformat(),
                "reason": "TARGET_UNREACHABLE_DATA_GAP",
                "fill": "none - the venue published no candle at this open time",
            },
        )
        self._integrity(
            IntegrityCode.EXECUTION_TARGET_UNREACHABLE,
            "execution target falls inside a known data hole; order voided, no fill",
            entity_id=order.order_id,
            severity="WARNING",
            target_execution_open_time=order.target_execution_open_time.isoformat(),
        )
        if order.purpose == "EXIT":
            position = position_repo.position_for_signal(self.session, order.signal_id)
            if position is not None:
                position_repo.reopen(self.session, position, clock=self.clock)
                logger.warning(
                    "exit order voided by a data hole; position re-opened to re-arm the exit",
                    extra={"detail": order.order_id, "position_id": position.position_id},
                )
        report.unreachable.append(order.order_id)

    def _mark_missed(self, order: Order, candle: Candle, report: ExecutionReport) -> None:
        """The target candle never arrived: no fill, CRITICAL integrity event."""
        transition(
            self.session,
            order,
            OrderState.TARGET_MISSED,
            reason=f"candle {candle.open_time.isoformat()} passed the target",
            software_version=self.software_version,
            clock=self.clock,
        )
        self._emit(
            EventType.ORDER_TARGET_MISSED,
            entity_id=order.order_id,
            payload={
                "order_id": order.order_id,
                "target_execution_open_time": order.target_execution_open_time.isoformat(),
                "saw_open_time": candle.open_time.isoformat(),
                "reason": "target candle missing from the series (gap or reorder)",
                "fill": "none - no substitute candle is ever used",
            },
        )
        self._integrity(
            IntegrityCode.EXECUTION_TARGET_MISSED,
            "execution target candle never arrived; order marked TARGET_MISSED, no fill was made",
            entity_id=order.order_id,
            severity="CRITICAL",
            target_execution_open_time=order.target_execution_open_time.isoformat(),
            saw_open_time=candle.open_time.isoformat(),
        )
        transition(
            self.session,
            order,
            OrderState.CANCELLED,
            reason="closed after a missed execution target",
            software_version=self.software_version,
            clock=self.clock,
        )
        report.missed.append(order.order_id)

    # ---------------------------------------------------------------- positions
    def _open_position(self, order: Order, execution: Execution) -> Position | None:
        existing = position_repo.open_position(self.session, self.symbol, self.timeframe)
        if existing is not None:
            self._integrity(
                IntegrityCode.BALANCE_INVARIANT_BROKEN,
                "entry fill arrived while a position was already open; fill recorded, "
                "position not duplicated",
                entity_id=order.order_id,
                severity="CRITICAL",
                existing_position_id=existing.position_id,
            )
            return None

        position = position_repo.create(
            self.session,
            order=order,
            execution=execution,
            stop_loss=order.stop_loss_price or execution.actual_execution_price,
            take_profit=order.take_profit_price or execution.actual_execution_price,
            clock=self.clock,
            strategy_version=order.strategy_version or self.config.version,
            strategy_config_hash=order.strategy_config_hash or self.config.config_hash,
        )
        self._emit(
            EventType.POSITION_OPENED,
            entity_id=position.position_id,
            payload={
                "position_id": position.position_id,
                "side": position.side,
                "quantity": str(position.quantity),
                "entry_price": str(position.entry_price),
                "stop_loss": str(position.stop_loss),
                "take_profit": str(position.take_profit),
                "entry_candle_open_time": position.entry_candle_open_time.isoformat(),
                "strategy_config_hash": position.strategy_config_hash,
            },
        )
        return position

    def _settle_positions(
        self, candle: Candle, report: ExecutionReport, *, position: Position | None = None
    ) -> None:
        """Check SL/TP for the open position against this candle's range."""
        position = position or position_repo.open_position(self.session, self.symbol, self.timeframe)
        if position is None or PositionState(position.state) is not PositionState.OPEN:
            return

        side = Side(position.side)
        stop_hit = candle.low <= position.stop_loss if side is Side.BUY else candle.high >= position.stop_loss
        target_hit = (
            candle.high >= position.take_profit if side is Side.BUY else candle.low <= position.take_profit
        )

        # Intrabar policy (frozen): if one candle touches both levels we assume
        # the stop was reached first. Conservative by construction - it can never
        # manufacture a win from information the candle does not contain.
        if stop_hit:
            self._close_position(
                position,
                candle,
                exit_price=price_of(position.stop_loss),
                exit_reason=ExitReason.SL_HIT,
                cause=CloseCause.STOP_LOSS,
                event_type=EventType.STOP_LOSS_HIT,
                report=report,
            )
        elif target_hit:
            self._close_position(
                position,
                candle,
                exit_price=price_of(position.take_profit),
                exit_reason=ExitReason.TP_HIT,
                cause=CloseCause.TAKE_PROFIT,
                event_type=EventType.TAKE_PROFIT_HIT,
                report=report,
            )
        else:
            from app.portfolio import accounting

            position.unrealized_pnl = accounting.unrealized_pnl(position, candle.close)
            position.updated_at = self.clock.now()
            self.session.flush()

    def _close_position(
        self,
        position: Position,
        candle: Candle,
        *,
        exit_price: Decimal,
        exit_reason: ExitReason,
        cause: CloseCause,
        event_type: EventType,
        report: ExecutionReport,
    ) -> None:
        """Close a position: exit order + exit execution + trade, one transaction."""
        self._emit(
            event_type,
            entity_id=position.position_id,
            payload={
                "position_id": position.position_id,
                "candle_open_time": candle.open_time.isoformat(),
                "level": str(exit_price),
                "cause": cause.value,
                "policy": "stop_first" if cause is CloseCause.STOP_LOSS else "target_hit",
            },
        )

        position_repo.mark_closing(self.session, position, clock=self.clock)

        exit_order = order_repo.create_exit(
            self.session,
            position=position,
            quantity=position.quantity,
            notional=money(position.quantity * exit_price),
            target_open_time=candle.open_time,
            target_candle_id=candle.candle_id,
            now=self.clock.now(),
        )
        transition(
            self.session,
            exit_order,
            OrderState.TARGET_REACHED,
            reason="exit level reached inside the target candle",
            software_version=self.software_version,
            clock=self.clock,
        )
        actual_price = self._apply_slippage(exit_price, Side(exit_order.side))
        notional = money(position.quantity * actual_price)
        fee = money(notional * self.fee_rate)
        execution = self._write_execution(
            order=exit_order,
            candle=candle,
            raw_price=price_of(exit_price),
            actual_price=actual_price,
            quantity=position.quantity,
            notional=notional,
            fee=fee,
        )
        transition(
            self.session,
            exit_order,
            OrderState.EXECUTED,
            reason="exit filled",
            software_version=self.software_version,
            clock=self.clock,
        )

        trade = ledger.record(
            self.session,
            position=position,
            exit_price=actual_price,
            exit_fee=fee,
            exit_reason=exit_reason,
            exit_order_id=exit_order.order_id,
            exit_time=candle.open_time,
            strategy_version=position.strategy_version,
            strategy_config_hash=position.strategy_config_hash,
        )
        position_repo.close(
            self.session,
            position,
            exit_reason=exit_reason,
            realized_pnl=trade.net_pnl,
            closed_at=candle.open_time,
        )

        report.closed.append(position.position_id)
        self._emit(
            EventType.POSITION_CLOSED,
            entity_id=position.position_id,
            payload={
                "position_id": position.position_id,
                "execution_id": execution.execution_id,
                "exit_order_id": exit_order.order_id,
                "exit_price": str(actual_price),
                "exit_reason": exit_reason.value,
                "net_pnl": str(trade.net_pnl),
                "holding_seconds": trade.holding_seconds,
            },
        )
        self._emit(
            EventType.TRADE_RECORDED,
            entity_id=trade.trade_id,
            payload={
                "trade_id": trade.trade_id,
                "net_pnl": str(trade.net_pnl),
                "gross_pnl": str(trade.gross_pnl),
                "exit_reason": trade.exit_reason,
            },
        )

    # ------------------------------------------------------------------ events
    def _emit(self, event_type: EventType, *, entity_id: str, payload: dict[str, object]) -> None:
        envelope = EventEnvelope(
            event_type=event_type.value,
            timestamp=self.clock.now(),
            symbol=self.symbol,
            timeframe=self.timeframe,
            entity_id=entity_id,
            payload=payload,
        )
        system_events.record_event(self.session, envelope, software_version=self.software_version)
        if self.bus is not None:
            self.bus.publish(envelope)

    def _integrity(
        self,
        code: IntegrityCode,
        message: str,
        *,
        entity_id: str,
        severity: str,
        **details: object,
    ) -> None:
        integrity_events.record_violation(
            self.session,
            code=code,
            detected_at=self.clock.now(),
            software_version=self.software_version,
            entity_id=entity_id,
            symbol=self.symbol,
            timeframe=self.timeframe,
            severity=severity,
            details={"message": message, **details},
        )


__all__ = ["BPS_DENOMINATOR", "ExecutionReport", "PaperExecutionEngine"]
