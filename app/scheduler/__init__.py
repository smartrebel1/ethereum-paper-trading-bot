"""Internal candle-driven scheduler for continuous paper trading.

The scheduler polls public market data, persists closed candles, and advances
the existing paper engine incrementally. It never submits exchange orders.
"""

from app.scheduler.runner import DEFAULT_POLL_SECONDS, PaperScheduler, SchedulerHaltedError, run_scheduler

__all__ = [
    "DEFAULT_POLL_SECONDS",
    "PaperScheduler",
    "SchedulerHaltedError",
    "run_scheduler",
]
