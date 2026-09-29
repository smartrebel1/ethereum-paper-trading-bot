"""Shared repository helpers."""

from __future__ import annotations

from typing import Any, TypeVar

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

T = TypeVar("T")


def get_or_create(  # noqa: UP047
    session: Session,
    model: type[T],
    *,
    defaults: dict[str, Any] | None = None,
    **filters: Any,
) -> tuple[T, bool]:
    """Return ``(instance, created)`` for a unique-constrained lookup.

    Used for the registry tables (strategy_versions, configuration_versions,
    data_sources): re-running startup must be a no-op, not a duplicate-key
    error. ``created`` lets the caller decide whether to emit a system event.
    """
    stmt: Select = select(model).filter_by(**filters)
    existing = session.execute(stmt).scalars().first()
    if existing is not None:
        return existing, False
    instance = model(**filters, **(defaults or {}))  # type: ignore[call-arg]
    session.add(instance)
    session.flush()
    return instance, True


__all__ = ["get_or_create"]
