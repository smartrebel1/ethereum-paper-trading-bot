"""AI shadow observer — planned for phase 10.

Isolated context observer; output is stored and never read by the trading path

The package exists in phase 1 (empty) so that later phases add modules without
restructuring the tree, and so that "the directory is empty" is an explicit
statement rather than an oversight. Nothing in phase 1 imports it.
"""

from __future__ import annotations

__all__: list[str] = []
