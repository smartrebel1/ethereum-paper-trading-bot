"""Replay engine — the deterministic candle-driven pipeline.

This is the component that answers "what actually happens, candle by candle".
It is used by:

* ``scripts/replay.py``      — the operational replay (writes the database),
* ``scripts/demo_replay.py`` — the narrated demonstration,
* the backtest/walk-forward phases, which reuse the exact same code path.

Per closed candle, in this order (the order *is* the no-look-ahead guarantee)::

    1. execution.on_candle(C)      fills orders targeting C.open_time,
                                   settles SL/TP against C's range
    2. strategy.evaluate(...C)     decides using data up to C.close only
    3. risk.decide(signal)         sizing, caps, funding
    4. persistence                 signal -> order (target = next candle open)
    5. portfolio.snapshot(C)       equity point

Nothing in step 2 or 3 can see C+1: the target is computed as a *time*, and the
order can only be filled when a candle with exactly that open time arrives.
"""

from __future__ import annotations

from app.replay_engine.engine import ReplayEngine, ReplayResult, ReplayStep

__all__ = ["ReplayEngine", "ReplayResult", "ReplayStep"]
