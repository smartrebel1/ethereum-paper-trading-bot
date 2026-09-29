"""Integrity package (phase 1: event catalog + codes)."""

from __future__ import annotations

from app.integrity.enums import (
    ALL_EVENT_TYPES,
    ALL_INTEGRITY_CODES,
    FATAL_INTEGRITY_CODES,
    EventType,
    IntegrityCode,
)

__all__ = [
    "ALL_EVENT_TYPES",
    "ALL_INTEGRITY_CODES",
    "FATAL_INTEGRITY_CODES",
    "EventType",
    "IntegrityCode",
]
