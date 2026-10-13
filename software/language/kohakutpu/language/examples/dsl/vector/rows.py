"""The shape every vector example shares: [R, W] rows split evenly over the
vector cores, each core's stream loop taking `rows` rows a trip as GT4
bands (4 rows x 32 columns a register). How wide a row the core holds is the
compiler's: it refuses a band its registers or L1 cannot take."""

import kohakutpu.language.dsl as kt


def per_core(R: int, W: int, rows: int) -> int:
    """The rows each core takes; refuses a shape the examples' loops do not
    cover, by name, at trace time."""
    cores = kt.machine().vc
    kt.require(W % 32 == 0, f"rows of whole 32-column blocks, not {W}")
    kt.require(rows % 4 == 0, f"trips of whole 4-row bands, not {rows}")
    kt.require(
        R % (cores * rows) == 0,
        f"{R} rows do not split into {cores} cores x trips of {rows}",
    )
    return R // cores


__all__ = ["per_core"]
