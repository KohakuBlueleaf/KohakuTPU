"""Graph -> schedule. The decisions, one pass per file.

The tile chooser is here rather than in `ir/` because a tile is a DECISION about
a program on a machine, not a property of either. Conflating the tile with the
cross-cluster split is what cost the legacy driver a factor of seven.
"""

from ktpu.passes.lower import engine_for, fusion_report, lower
from ktpu.passes.tile import TileChoice, choose_tile, grid_for, occupancy

__all__ = [
    "TileChoice",
    "choose_tile",
    "engine_for",
    "fusion_report",
    "grid_for",
    "lower",
    "occupancy",
]
