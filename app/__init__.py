"""ETHUSDT deterministic paper trading engine (phase 1).

Paper-only by construction: there is no code path that can reach a real venue.
See docs/ARCHITECTURE.md and app/config/settings.py (safety guard).
"""

from __future__ import annotations

__version__ = "0.1.0"

#: Human-readable software version recorded on every event/integrity row.
#: Bump this whenever persisted semantics change (payload shape, state machine).
SOFTWARE_VERSION = __version__

__all__ = ["__version__", "SOFTWARE_VERSION"]
