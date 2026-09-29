"""Custom SQLAlchemy column types.

Two problems are solved here, both of which would silently corrupt a
"deterministic" engine on SQLite:

1. **Timestamps.** SQLite's DATETIME stores a naive string; SQLAlchemy returns
   naive datetimes on read and *silently ignores* tzinfo on write. Naive
   datetimes are how look-ahead bugs hide. :class:`UTCDateTime` refuses naive
   input and always returns aware UTC on read, on every dialect.

2. **Money.** SQLite has no DECIMAL; ``Numeric`` there is REAL (float64) and
   SQLAlchemy emits a rounding warning because Decimal->float->Decimal is lossy.
   :class:`FixedPoint` stores exact scaled integers instead, so:
     * round-trips are exact (no 2500.1234567800002),
     * SQL comparisons in CHECK constraints are exact integer comparisons,
     * a 200 USDT account never accumulates float drift.

   1 unit of ``scale`` is stored as 1. ``PRICE = FixedPoint(8)`` means a price
   of 2500.12 is stored as 250012000000. Python code should quantize explicitly
   with :mod:`app.common.rounding` before assigning (the type quantizes as a
   safety net using ROUND_HALF_EVEN).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, DateTime
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from app.common.rounding import MONEY_SCALE, PRICE_SCALE, QTY_SCALE, RATIO_SCALE, to_decimal

UTC = UTC


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC datetime column.

    * Binds: reject naive datetimes, store the UTC instant.
    * Reads: attach UTC if the driver returned a naive value.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:  # noqa: ARG002
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError(f"UTCDateTime expects datetime, got {type(value).__name__}")
        if value.tzinfo is None:
            raise ValueError("naive datetime rejected: all persisted timestamps must be timezone-aware UTC")
        return value.astimezone(UTC)

    def process_result_value(self, value: Any, dialect: Dialect) -> Any:  # noqa: ARG002
        if value is None:
            return None
        if not isinstance(value, datetime):
            return value
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class FixedPoint(TypeDecorator[Decimal]):
    """Exact fixed-point decimal stored as a scaled INTEGER.

    ``FixedPoint(8)`` -> 8 decimal places, values up to ~9.2e10.
    """

    impl = BigInteger
    cache_ok = True

    def __init__(self, scale: int) -> None:
        if not 0 <= scale <= 18:
            raise ValueError("scale must be within [0, 18]")
        super().__init__()
        self.scale = scale
        self._quantum = Decimal(1).scaleb(-scale)

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:  # noqa: ARG002
        if value is None:
            return None
        decimal_value = to_decimal(value)
        if not decimal_value.is_finite():
            raise ValueError(f"non-finite decimal rejected: {decimal_value!r}")
        quantized = decimal_value.quantize(self._quantum)
        return int(quantized.scaleb(self.scale).to_integral_value())

    def process_result_value(self, value: Any, dialect: Dialect) -> Any:  # noqa: ARG002
        if value is None:
            return None
        return Decimal(int(value)).scaleb(-self.scale)

    @property
    def python_type(self) -> type[Decimal]:  # pragma: no cover - typing hook
        return Decimal


#: Shared type instances (type objects are stateless and reusable).
TS = UTCDateTime()
PRICE = FixedPoint(PRICE_SCALE)
QTY = FixedPoint(QTY_SCALE)
MONEY = FixedPoint(MONEY_SCALE)
RATIO = FixedPoint(RATIO_SCALE)


__all__ = [
    "MONEY",
    "PRICE",
    "QTY",
    "RATIO",
    "TS",
    "FixedPoint",
    "UTCDateTime",
]
