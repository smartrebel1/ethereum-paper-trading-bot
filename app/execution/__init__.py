"""Paper execution engine — planned for phase 5.

Lands here: the order state machine, the hard invariant that an execution
candle's ``open_time`` must equal the order's ``target_execution_open_time``
(never "close enough", never "the next available candle"), and the
``MARK_EXECUTION_TARGET_MISSED`` path that fires instead of silently filling.

The package exists in phase 1 (empty) so that later phases add modules without
restructuring the tree, and so that "the directory is empty" is an explicit
statement rather than an oversight. Nothing in phase 1 imports it.
"""

from __future__ import annotations

__all__: list[str] = []
