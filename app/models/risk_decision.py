"""Risk verdicts — one row per signal (``signal_id`` is the PK).

Exactly one verdict per signal is a deliberate constraint: re-running the risk
engine on the same signal must be idempotent, and a REJECTED signal can never
be "re-risked" into an approval by a later tick.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, CheckConstraint, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.database.types import MONEY, TS


class RiskDecision(Base):
    __tablename__ = "risk_decisions"

    signal_id: Mapped[str] = mapped_column(String(64), ForeignKey("signals.signal_id"), primary_key=True)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)  # APPROVED/REJECTED
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False, default="OK")
    reasons: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    approved_quantity: Mapped[Decimal | None] = mapped_column(MONEY)
    approved_notional: Mapped[Decimal | None] = mapped_column(MONEY)
    stop_loss_price: Mapped[Decimal | None] = mapped_column(MONEY)
    take_profit_price: Mapped[Decimal | None] = mapped_column(MONEY)

    decided_at: Mapped[datetime] = mapped_column(TS, nullable=False)

    __table_args__ = (
        CheckConstraint("outcome IN ('APPROVED','REJECTED')", name="ck_risk_decisions_outcome_valid"),
        CheckConstraint(
            "outcome = 'REJECTED' OR (approved_quantity IS NOT NULL AND approved_notional IS NOT NULL)",
            name="ck_risk_decisions_approved_has_sizing",
        ),
        CheckConstraint(
            "approved_quantity IS NULL OR approved_quantity > 0",
            name="ck_risk_decisions_quantity_positive",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<RiskDecision {self.signal_id} {self.outcome} {self.reason_code}>"


__all__ = ["RiskDecision"]
