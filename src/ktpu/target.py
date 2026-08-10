"""Machine descriptions. A level-1 graph is valid against a family of machines;
one of these picks a member."""

from dataclasses import dataclass

# Optional hardware features. A bitstream either has one or it does not, and a
# program built for a machine without it must still lower and still run -- so
# every entry here is something codegen may only EMIT when it is present, never
# something it assumes.
#
#   cu_merged     a cluster is one NoC node (CU_X/CU_Y) rather than a manager
#                 and an accumulator at separate coordinates.
#   emit_scale    the accumulator applies a per-tile constant on the way out,
#                 so `s * scale * log2 e` costs no vector pass.
#   row_rescale   the accumulator applies a per-row 2^k bias in place, so flash
#                 attention's `acc * corr` costs no vector pass. Requires the
#                 driver to round the running max up in log2 space.
#   vec_reduce_writeback
#                 a reduction keeps its elementwise result in the same pass.
#                 Costs no opcode -- it is VRED's reserved `vc` kind 5 -- so an
#                 old core given kind 5 runs a plain sum tree and is WRONG.
#                 That is what this gate exists to prevent.
#   vexpsum       fused exp2(a - bias) feeding the reduction tree.
FEATURES = (
    "cu_merged",
    "emit_scale",
    "row_rescale",
    "vec_reduce_writeback",
    "vexpsum",
)


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

    #: Optional hardware features this machine HAS -- see FEATURES. Empty means
    #: the conservative machine, which every program must still be able to
    #: target: there is a bitstream in the field with none of them.
    features: frozenset = frozenset()

    def has(self, feature: str) -> bool:
        """True if this machine implements `feature`. Raises on a typo.

        A misspelled feature must not read as absent -- that silently produces
        the slow path forever and looks like the optimisation never worked.
        """
        if feature not in FEATURES:
            raise ValueError(f"unknown feature {feature!r}; known are {list(FEATURES)}")
        return feature in self.features

    def block(self, gm: int, gn: int) -> tuple[int, int]:
        """Convert a sub-tile count to an output block in ELEMENTS.

        `gm`/`gn` count sub-tiles of `lanes` elements per edge, so at lanes=4
        `gm=16, gn=32` is a 64 x 128 element block. Returns `(rows, cols)`.
        """
        return gm * self.lanes, gn * self.lanes


VU13P_8CU = Target(features=frozenset({"cu_merged"}))

# vector bring-up: four cores, four MAG ports, no matmul at all
VU13P_VEC4 = Target(
    name="vu13p-vec4", clusters=0, vector_cores=4, features=frozenset({"cu_merged"})
)

# matmul only, for comparing against the legacy driver's numbers
VU13P_MM8 = Target(name="vu13p-mm8", vector_cores=0, features=frozenset({"cu_merged"}))

# THE MACHINE ON SILICON. A two-cluster split-mgr/acu mesh with none of the
# optional features -- the bitstream synthesised before 2026-08-10 and the one
# hardware experiments run against. It is the compatibility floor: anything
# that fails to lower for this target has broken something we can actually run.
VU13P_LEGACY = Target(
    name="vu13p-legacy",
    clusters=2,
    # FOUR, verified in the bitstream's own synthesis log -- vec_cu plus three
    # parameterized variants at (1,0), (2,0), (1,3), (2,3). Harmless while only
    # matmul is dispatched; wrong the moment anything targets a vector core.
    vector_cores=4,
    # Capacities are FROZEN AT SYNTHESIS and the RTL's have since moved: this
    # bitstream carries TILES=256, GA=32, GB=32 against the current 512/128/256.
    # Planning for the RTL put 15,440 of 16,384 elements past 10% error on real
    # silicon. `ktpu.hw.board.Board` holds the authoritative per-bitstream copy.
    tiles=256,
    l1_a=32,
    l1_b=32,
    features=frozenset(),
)
