"""Structured JSON logging (one JSON object per line).

The original snippet emitted ``"message":%(message)r`` which produces a
*Python literal* (``'foo'``) instead of a JSON string — invalid for log
processors. This module renders the record through :mod:`json` instead, and
promotes useful ``extra=`` fields into the JSON object.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from app.common.clock import get_clock
from app.common.time_utils import to_iso_z

#: Standard LogRecord attributes; anything else on the record is a user field.
_RESERVED: frozenset[str] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


class JsonFormatter(logging.Formatter):
    """Renders ``record`` as a single-line JSON object."""

    def __init__(self, *, component: str | None = None, include_extra: bool = True) -> None:
        super().__init__()
        self._component = component
        self._include_extra = include_extra

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        payload: dict[str, Any] = {
            "ts": to_iso_z(get_clock().now()),
            "level": record.levelname,
            "component": record.name if self._component is None else self._component,
            "message": record.getMessage(),
        }
        if self._include_extra:
            for key, value in record.__dict__.items():
                if key not in _RESERVED and not key.startswith("_"):
                    payload[key] = _safe(value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":"))


def _safe(value: Any) -> Any:
    if isinstance(value, dict | list | tuple | str | int | float | bool | type(None)):
        return value
    return str(value)


def configure_logging(level: str = "INFO", *, stream: Any = None) -> None:
    """Install the JSON formatter on the root logger (idempotent)."""
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    # uvicorn installs its own handlers; make them use ours.
    for noisy in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(noisy)
        logger.handlers = []
        logger.propagate = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


__all__ = ["JsonFormatter", "configure_logging", "get_logger"]
