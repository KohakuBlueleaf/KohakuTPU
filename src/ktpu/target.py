"""Machine descriptions. A level-1 graph is valid against a family of machines;
one of these picks a member."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Target:
    """A machine configuration: engine counts, capacities, dispatch limits.

    Describes what the elaborated mesh HAS, not what a program chooses to use;
    a schedule may use fewer clusters than `clusters`, never more.

    Capacities are in hardware units, not elements. `tiles` is output sub-tiles
    per accumulator, where a sub-tile is `lanes x lanes`. `l1_a`/`l1_b` are L1
    entries, where an entry is `lanes x kblock` elements. `l1_a_banks` is how
    many chunks L1 A is divided into, so one chunk gets `l1_a // l1_a_banks`.

    `stage_flits` bounds one dispatch round: passes are admitted to a round
    until their flits exceed it, so many small passes serialise into many
    rounds. `ncmd` is the per-round command RAM depth.
    """

    name: str = "vu13p-8cu"

    clusters: int = 8
    tiles: int = 512  # output sub-tiles per accumulator, mx_acu_fp DEPTH
    l1_a: int = 128  # entries; an entry is lanes x kblock elements
    l1_b: int = 256
    l1_a_banks: int = 2  # A is double-buffered, so a chunk gets half

    lanes: int = 4  # elements per sub-tile edge
    kblock: int = 32  # elements sharing one MXFP7 scale

    vector_cores: int = 8
    vector_lanes: int = 16
    vlmax: int = 128

    stage_flits: int = 128  # what admits passes to a round
    ncmd: int = 128

    mhz: float = 300.0

    def block(self, gm: int, gn: int) -> tuple[int, int]:
        """Convert a sub-tile count to an output block in ELEMENTS.

        `gm`/`gn` count sub-tiles of `lanes` elements per edge, so at lanes=4
        `gm=16, gn=32` is a 64 x 128 element block. Returns `(rows, cols)`.
        """
        return gm * self.lanes, gn * self.lanes


VU13P_8CU = Target()

# vector bring-up: four cores, four MAG ports, no matmul at all
VU13P_VEC4 = Target(name="vu13p-vec4", clusters=0, vector_cores=4)

# matmul only, for comparing against the legacy driver's numbers
VU13P_MM8 = Target(name="vu13p-mm8", vector_cores=0)
