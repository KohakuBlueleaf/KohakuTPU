"""KohakuTPU's machine descriptions.

``ktpu.target.Target`` carried the framework's dispatch limits and eleven of this
project's capacities in one class. The limits and the feature MECHANISM are now
:class:`kohakuaccel.machine.Target`; everything below is what was left when they
were taken out, which is the whole point of the exercise.
"""

from dataclasses import dataclass
from typing import ClassVar

from kohakuaccel.machine import Target


@dataclass(frozen=True)
class TpuTarget(Target):
    """A KohakuTPU machine: engine counts and capacities.

    Describes what the elaborated mesh HAS, not what a program chooses to use; a
    schedule may use fewer clusters than `clusters`, never more.

    Capacities are in hardware units, not elements. `tiles` is output sub-tiles
    per accumulator, where a sub-tile is ``lanes x lanes``. `l1_a`/`l1_b` are L1
    entries, where an entry is ``lanes x kblock`` elements. `l1_a_banks` is how
    many chunks L1 A is divided into, so one chunk gets ``l1_a // l1_a_banks``.
    """

    #: `vec_reduce_writeback` costs no opcode -- it is VRED's reserved `vc` kind
    #: 5 -- so an old core given kind 5 runs a plain sum tree and is WRONG.
    FEATURES: ClassVar[tuple[str, ...]] = (
        "cu_merged",
        "emit_scale",
        "row_rescale",
        "vec_reduce_writeback",
        "vexpsum",
    )

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

    def block(self, gm: int, gn: int) -> tuple[int, int]:
        """Convert a sub-tile count to an output block in ELEMENTS.

        `gm`/`gn` count sub-tiles of `lanes` elements per edge, so at lanes=4
        ``gm=16, gn=32`` is a 64 x 128 element block. Returns ``(rows, cols)``.
        """
        return gm * self.lanes, gn * self.lanes


VU13P_8CU = TpuTarget(features=frozenset({"cu_merged"}))

# vector bring-up: four cores, four memory ports, no matmul at all
VU13P_VEC4 = TpuTarget(
    name="vu13p-vec4", clusters=0, vector_cores=4, features=frozenset({"cu_merged"})
)

# matmul only, for comparing against the legacy driver's numbers
VU13P_MM8 = TpuTarget(
    name="vu13p-mm8", vector_cores=0, features=frozenset({"cu_merged"})
)

# THE MACHINE ON SILICON: a two-cluster split-mgr/acu mesh with none of the
# optional features. Anything that fails to lower for this has broken something
# we can actually run.
VU13P_LEGACY = TpuTarget(
    name="vu13p-legacy",
    clusters=2,
    # FOUR, verified in the bitstream's own synthesis log. Harmless while only
    # matmul is dispatched; wrong the moment anything targets a vector core.
    vector_cores=4,
    # FROZEN AT SYNTHESIS: this bitstream carries TILES=256, GA=32, GB=32
    # against the RTL's current 512/128/256.
    tiles=256,
    l1_a=32,
    l1_b=32,
    features=frozenset(),
)
