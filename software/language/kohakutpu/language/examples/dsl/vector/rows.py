"""The shape every vector example shares: [R, W] rows split evenly over the
vector cores, each core's stream loop taking `rows` rows a trip as GT4
bands (4 rows x 32 columns a register)."""

import kohakutpu.language.dsl as kt

#: Widest row one band's registers hold (8 registers of 32 columns).
MAX_W = 256


def per_core(R: int, W: int, rows: int) -> int:
    """The rows each core takes; refuses a shape the examples' loops do not
    cover, by name, at trace time."""
    cores = kt.machine().vc
    kt.require(
        W % 32 == 0 and W <= MAX_W,
        f"rows of whole 32-column blocks up to {MAX_W}, not {W}",
    )
    kt.require(rows % 4 == 0, f"trips of whole 4-row bands, not {rows}")
    kt.require(
        R % (cores * rows) == 0,
        f"{R} rows do not split into {cores} cores x trips of {rows}",
    )
    return R // cores


__all__ = ["MAX_W", "per_core"]
