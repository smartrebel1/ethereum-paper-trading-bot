"""Order state machine — legal transitions, enforced in one place.

    CREATED ──▶ PENDING ──▶ TARGET_REACHED ──▶ EXECUTED      (terminal)
                   │
                   ├──▶ TARGET_MISSED ──▶ CANCELLED          (terminal)
                   └──▶ CANCELLED                            (terminal)

Anything else raises :class:`IllegalStateTransitionError` and writes an
``ORDER_STATE_ILLEGAL_TRANSITION`` integrity row. Terminal states are terminal:
``EXECUTED`` never becomes anything, and a ``TARGET_MISSED`` order is never
"retried" — a missed target is a data-integrity event, not a scheduling hiccup.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.common.clock import Clock, get_clock
from app.common.enums import OrderState
from app.common.errors import IllegalStateTransitionError
from app.integrity.enums import IntegrityCode
from app.models.order import Order
from app.repositories import integrity_events

#: Explicit transition table. Adding an edge is a deliberate design change.
LEGAL_TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    OrderState.CREATED: frozenset({OrderState.PENDING, OrderState.CANCELLED}),
    OrderState.PENDING: frozenset(
        {OrderState.TARGET_REACHED, OrderState.TARGET_MISSED, OrderState.CANCELLED}
    ),
    OrderState.TARGET_REACHED: frozenset({OrderState.EXECUTED, OrderState.CANCELLED}),
    OrderState.TARGET_MISSED: frozenset({OrderState.CANCELLED}),
    OrderState.EXECUTED: frozenset(),
    OrderState.CANCELLED: frozenset(),
}

TERMINAL_STATES = frozenset({OrderState.EXECUTED, OrderState.CANCELLED})


def can_transition(current: OrderState | str, target: OrderState | str) -> bool:
    source = current if isinstance(current, OrderState) else OrderState(current)
    destination = target if isinstance(target, OrderState) else OrderState(target)
    return destination in LEGAL_TRANSITIONS[source]


def transition(
    session: Session,
    order: Order,
    target: OrderState,
    *,
    reason: str = "",
    software_version: str,
    clock: Clock | None = None,
    entity_id: str | None = None,
) -> Order:
    """Apply a state transition, or fail loudly (never silently)."""
    now: datetime = (clock or get_clock()).now()
    current = OrderState(order.state)

    if not can_transition(current, target):
        integrity_events.record_violation(
            session,
            code=IntegrityCode.ORDER_STATE_ILLEGAL_TRANSITION,
            detected_at=now,
            software_version=software_version,
            entity_id=entity_id or order.order_id,
            symbol=order.symbol,
            timeframe=order.timeframe,
            details={
                "order_id": order.order_id,
                "from": current.value,
                "to": (target.value if isinstance(target, OrderState) else str(target)),
                "reason": reason,
            },
        )
        raise IllegalStateTransitionError("order", current.value, str(target))

    order.state = target.value
    order.state_updated_at = now
    if reason:
        order.state_reason = reason[:128]
    session.flush()
    return order


def is_terminal(order: Order) -> bool:
    return OrderState(order.state) in TERMINAL_STATES


__all__ = [
    "LEGAL_TRANSITIONS",
    "TERMINAL_STATES",
    "can_transition",
    "is_terminal",
    "transition",
]
