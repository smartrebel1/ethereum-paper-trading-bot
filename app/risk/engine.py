"""Risk engine — the gate between "the strategy wants to trade" and "an order exists".

Checks, in order (the first failure wins and is recorded):

1. **Warm-up**          — the signal's indicator context must be complete.
2. **Actionable**       — HOLD/None never reaches here, but be explicit.
3. **Config match**     — the signal must carry the *current* config hash, so a
                          signal produced by a different build can never be traded.
4. **One position**     — refuse if a position is already open for (symbol, timeframe).
5. **No pending order** — refuse if an order is already queued for this symbol.
6. **Funding**          — ``available_cash`` must cover notional + expected fees.
7. **Sizing sanity**    — quantity must be > 0 and notional ≤ MAX_POSITION_PCT of equity.

Sizing (frozen):

    notional = min(equity * MAX_POSITION_PCT, available_cash / (1 + fee_rate))
    quantity = notional / entry_reference_price

Every rejection is persisted with a machine-readable ``reason_code`` and a list
of reasons, so "why didn't it trade?" is a SQL query rather than an opinion.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Session

from app.common.clock import Clock, get_clock
from app.common.enums import Action, RiskOutcome
from app.common.rounding import money
from app.common.rounding import price as price_of
from app.common.rounding import qty as qty_of
from app.common.time_utils import ensure_aware_utc
from app.config.strategy_config import StrategyConfig
from app.events.bus import EventBus, EventEnvelope
from app.integrity.enums import EventType
from app.models.order import Order
from app.models.risk_decision import RiskDecision
from app.models.signal import Signal
from app.portfolio.accounting import PortfolioState, available_cash
from app.repositories import orders as order_repo
from app.repositories import positions as position_repo
from app.repositories import system_events

ZERO = Decimal(0)
ONE = Decimal(1)

REASON_OK = "OK"
REASON_WARMUP = "WARMUP_INCOMPLETE"
REASON_NOT_ACTIONABLE = "NOT_ACTIONABLE"
REASON_CONFIG_MISMATCH = "STRATEGY_CONFIG_MISMATCH"
REASON_POSITION_OPEN = "POSITION_ALREADY_OPEN"
REASON_ORDER_PENDING = "ORDER_ALREADY_PENDING"
REASON_INSUFFICIENT_FUNDS = "INSUFFICIENT_AVAILABLE_CASH"
REASON_ZERO_QUANTITY = "COMPUTED_QUANTITY_ZERO"
REASON_EXPOSURE_CAP = "EXPOSURE_CAP_EXCEEDED"


class RiskEngine:
    """Deterministic, side-effect-explicit risk gate."""

    def __init__(
        self,
        session: Session,
        *,
        config: StrategyConfig,
        symbol: str,
        timeframe: str,
        software_version: str,
        clock: Clock | None = None,
        bus: EventBus | None = None,
    ) -> None:
        self.session = session
        self.config = config
        self.symbol = symbol
        self.timeframe = timeframe
        self.software_version = software_version
        self.clock = clock or get_clock()
        self.bus = bus

    # ------------------------------------------------------------------ decide
    def decide(
        self,
        signal: Signal,
        *,
        state: PortfolioState,
        reference_price: Decimal,
    ) -> tuple[RiskDecision, Order | None]:
        """Evaluate one signal exactly once (the row is keyed by signal_id)."""
        existing = self.session.get(RiskDecision, signal.signal_id)
        if existing is not None:
            return existing, order_repo.entry_for_signal(self.session, signal.signal_id)

        reasons: list[str] = []
        code = REASON_OK

        context = signal.indicator_context or {}
        if not context.get("warm"):
            code = REASON_WARMUP
            reasons.append("indicator context is not warm")
        elif signal.action not in (Action.BUY.value, Action.SELL.value):
            code = REASON_NOT_ACTIONABLE
            reasons.append(f"action {signal.action} is not actionable")
        elif signal.strategy_config_hash != self.config.config_hash:
            code = REASON_CONFIG_MISMATCH
            reasons.append("signal was produced by a different strategy config")

        if code == REASON_OK:
            open_pos = position_repo.open_position(self.session, signal.symbol, signal.timeframe)
            if open_pos is not None:
                code = REASON_POSITION_OPEN
                reasons.append(f"position {open_pos.position_id} is already open")
            elif order_repo.pending(self.session, signal.symbol, signal.timeframe):
                code = REASON_ORDER_PENDING
                reasons.append("an order for this series is already pending")

        quantity = ZERO
        notional = ZERO
        stop_loss = None
        take_profit = None
        atr_value = _decimal_from_context(context.get("atr"))

        if code == REASON_OK and atr_value is None:
            code = REASON_WARMUP
            reasons.append("ATR missing from indicator context")

        if code == REASON_OK:
            quantity, notional, stop_loss, take_profit, code, reasons = self._size(
                signal=signal,
                state=state,
                reference_price=reference_price,
                atr_value=atr_value,  # type: ignore[arg-type]
            )

        approved = code == REASON_OK
        decision = RiskDecision(
            signal_id=signal.signal_id,
            outcome=RiskOutcome.APPROVED.value if approved else RiskOutcome.REJECTED.value,
            reason_code=code,
            reasons=reasons,
            approved_quantity=qty_of(quantity) if approved else None,
            approved_notional=money(notional) if approved else None,
            stop_loss_price=stop_loss if approved else None,
            take_profit_price=take_profit if approved else None,
            decided_at=ensure_aware_utc(self.clock.now()),
        )
        self.session.add(decision)
        self.session.flush()

        order = None
        if approved:
            now = ensure_aware_utc(self.clock.now())
            order = order_repo.create_entry(
                self.session,
                signal=signal,
                quantity=quantity,
                notional=notional,
                stop_loss=stop_loss,  # type: ignore[arg-type]
                take_profit=take_profit,  # type: ignore[arg-type]
                now=now,
            )

        self._emit(signal, decision, order)
        return decision, order

    # -------------------------------------------------------------------- size
    def _size(
        self,
        *,
        signal: Signal,
        state: PortfolioState,
        reference_price: Decimal,
        atr_value: Decimal,
    ) -> tuple[Decimal, Decimal, Decimal | None, Decimal | None, str, list[str]]:
        reasons: list[str] = []
        price = price_of(reference_price)
        if price <= ZERO:
            return ZERO, ZERO, None, None, REASON_ZERO_QUANTITY, ["reference price is not positive"]

        equity = state.equity if state.equity > ZERO else state.starting_balance
        cap = money(equity * self.config.max_position_pct_decimal)
        spendable = money(max(ZERO, available_cash(state)))
        budget = money(min(cap, spendable / (ONE + self.config.fee_rate_decimal)))

        if budget <= ZERO:
            return (
                ZERO,
                ZERO,
                None,
                None,
                REASON_INSUFFICIENT_FUNDS,
                ["no spendable cash after entry/exit notional and fees"],
            )

        quantity = qty_of(budget / price)
        if quantity <= ZERO:
            return ZERO, ZERO, None, None, REASON_ZERO_QUANTITY, ["computed quantity rounds to zero"]

        notional = money(quantity * price)
        if notional > cap:
            return (
                ZERO,
                ZERO,
                None,
                None,
                REASON_EXPOSURE_CAP,
                [f"notional {notional} exceeds cap {cap} ({self.config.max_position_pct} of equity)"],
            )

        sl_distance = self.config.sl_multiplier * atr_value
        tp_distance = self.config.tp_multiplier * atr_value
        if signal.action == Action.BUY.value:
            stop_loss = price_of(price - sl_distance)
            take_profit = price_of(price + tp_distance)
        else:
            stop_loss = price_of(price + sl_distance)
            take_profit = price_of(price - tp_distance)
        if stop_loss <= ZERO or take_profit <= ZERO:
            return ZERO, ZERO, None, None, REASON_ZERO_QUANTITY, ["stop/target not positive"]

        return quantity, notional, stop_loss, take_profit, REASON_OK, reasons

    # ------------------------------------------------------------------ events
    def _emit(self, signal: Signal, decision: RiskDecision, order: Order | None) -> None:
        event_type = (
            EventType.RISK_APPROVED
            if decision.outcome == RiskOutcome.APPROVED.value
            else EventType.RISK_REJECTED
        )
        payload: dict[str, object] = {
            "signal_id": signal.signal_id,
            "outcome": decision.outcome,
            "reason_code": decision.reason_code,
            "reasons": list(decision.reasons or []),
        }
        if decision.approved_quantity is not None:
            payload["approved_quantity"] = str(decision.approved_quantity)
            payload["approved_notional"] = str(decision.approved_notional)
            payload["stop_loss"] = str(decision.stop_loss_price)
            payload["take_profit"] = str(decision.take_profit_price)
        if order is not None:
            payload["order_id"] = order.order_id

        envelope = EventEnvelope(
            event_type=event_type.value,
            timestamp=ensure_aware_utc(self.clock.now()),
            symbol=signal.symbol,
            timeframe=signal.timeframe,
            entity_id=signal.signal_id,
            payload=payload,
        )
        system_events.record_event(self.session, envelope, software_version=self.software_version)
        if self.bus is not None:
            self.bus.publish(envelope)


def _decimal_from_context(value: object) -> Decimal | None:
    if value in (None, "", "None"):
        return None
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 - malformed context is a data problem, not a crash
        return None


__all__ = [
    "REASON_CONFIG_MISMATCH",
    "REASON_EXPOSURE_CAP",
    "REASON_INSUFFICIENT_FUNDS",
    "REASON_NOT_ACTIONABLE",
    "REASON_OK",
    "REASON_ORDER_PENDING",
    "REASON_POSITION_OPEN",
    "REASON_WARMUP",
    "REASON_ZERO_QUANTITY",
    "RiskEngine",
]
