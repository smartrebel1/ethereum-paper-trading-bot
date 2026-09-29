"""Safety enforcement — the last line of defence before "live".

Two independent layers (defence in depth):

1. ``app.config.settings.Settings`` refuses to *validate* a non-paper config,
   so an invalid config can never be constructed.
2. :func:`enforce_paper_only` re-checks an already-built settings object and is
   called from the app startup hook, from ``scripts`` and from tests.
3. The database enforces it a third time: ``ck_order_paper_only`` /
   ``ck_exec_paper_only`` CHECK constraints reject any row whose provider is
   not ``'paper'``.

A live provider literally does not exist in the enum, and the DB would reject
its rows even if someone added one.
"""

from __future__ import annotations

from typing import Any

from app.common.enums import ExecutionProvider, TradingMode
from app.common.errors import SafetyViolationError, StartupGuardError


class SafetyReport:
    """Result of a pre-flight check (kept tiny and JSON-serialisable)."""

    __slots__ = ("checks", "ok")

    def __init__(self, checks: list[tuple[str, bool, str]]) -> None:
        self.checks = checks
        self.ok = all(passed for _, passed, _ in checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checks": [{"name": n, "passed": p, "detail": d} for n, p, d in self.checks],
        }


def enforce_paper_only(settings: Any) -> SafetyReport:
    """Raise :class:`StartupGuardError` unless *everything* says paper.

    Never returns False — either returns an all-green report or raises. A guard
    that can "warn" is a guard that will be ignored.
    """
    checks: list[tuple[str, bool, str]] = []

    mode = getattr(settings, "trading_mode", None)
    mode_value = getattr(mode, "value", mode)
    checks.append(
        ("trading_mode_is_paper", mode_value == TradingMode.PAPER.value, f"TRADING_MODE={mode_value!r}")
    )

    live_flag = bool(getattr(settings, "enable_live_trading", False))
    checks.append(("live_flag_disabled", not live_flag, f"ENABLE_LIVE_TRADING={live_flag}"))

    checks.append(
        (
            "live_trading_mode_absent_from_enum",
            "live" not in {m.value for m in TradingMode},
            "TradingMode enum contains only 'paper'",
        )
    )
    checks.append(
        (
            "live_provider_absent_from_enum",
            "live" not in {p.value for p in ExecutionProvider},
            "ExecutionProvider enum contains only 'paper'",
        )
    )

    report = SafetyReport(checks)
    if not report.ok:
        failures = [f"{name}: {detail}" for name, passed, detail in checks if not passed]
        raise StartupGuardError("refusing to start (paper-only guarantee): " + "; ".join(failures))
    return report


def assert_paper_provider(provider: str) -> None:
    """Reject any execution provider that is not paper (used by the exec engine)."""
    if provider != ExecutionProvider.PAPER.value:
        raise SafetyViolationError(
            f"execution provider {provider!r} is not allowed; only "
            f"{ExecutionProvider.PAPER.value!r} exists in this build"
        )


__all__ = ["SafetyReport", "assert_paper_provider", "enforce_paper_only"]
