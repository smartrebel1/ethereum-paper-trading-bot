"""Strategy engine — planned for phase 3.

Lands here: the EMA(200) + ATR(14) baseline, immutable Signal emission carrying
an explicit ``target_execution_open_time``, and property tests that prove no
future information can reach a decision.

The package exists in phase 1 (empty) so that later phases add modules without
restructuring the tree, and so that "the directory is empty" is an explicit
statement rather than an oversight. Nothing in phase 1 imports it.
"""

from __future__ import annotations

__all__: list[str] = []
