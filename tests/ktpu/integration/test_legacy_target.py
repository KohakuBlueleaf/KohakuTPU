"""The compatibility floor: everything must still lower for the machine on silicon.

There is a bitstream in the field -- two clusters, manager and accumulator at
separate NoC coordinates, none of the optional accumulator or vector features.
Hardware experiments run against it, so a change that stops a kernel lowering
for `VU13P_LEGACY` has broken something we can actually execute, not a
hypothetical.

These tests are cheap on purpose. They are a tripwire, not a substitute for
`test_attention.py`, which checks the numbers.
"""

import numpy as np
import pytest

import ktpu.dsl as D
from ktpu.codegen import codegen
from ktpu.dsl.nn import attention
from ktpu.hw import bench
from ktpu.interp.mesh import Mesh
from ktpu.ir import FP16
from ktpu.passes import lower
from ktpu.target import FEATURES, VU13P_8CU, VU13P_LEGACY

BLOCK, DH = 64, 64


def spec(*shape):
    return D.TensorSpec(shape, FP16)


def test_the_legacy_target_has_no_optional_features():
    """If this fails someone made a feature mandatory by making it the default."""
    assert VU13P_LEGACY.features == frozenset()
    for f in FEATURES:
        assert not VU13P_LEGACY.has(f), f"{f} must be optional"


def test_a_typo_in_a_feature_name_raises_rather_than_reading_as_absent():
    """`has` returning False for a misspelling would silently keep the slow path."""
    with pytest.raises(ValueError, match="unknown feature"):
        VU13P_8CU.has("cu_merge")  # the real one is cu_merged


def test_the_current_target_declares_the_merged_cluster():
    assert VU13P_8CU.has("cu_merged")
    assert not VU13P_LEGACY.has("cu_merged")


# The SHIPPED bitstream's L1 is 32 entries, an eighth of the current RTL's, so
# the block that fits the machine in the field is smaller than the one the
# simulator runs. See VU13P_LEGACY's capacity comment.
LEGACY_BLOCK = 16


def _attention_graph(causal, blk=64, dh=64):
    rng = np.random.default_rng(3)
    L = 2 * blk
    arrays = [rng.standard_normal((L, dh)) * 0.3 for _ in range(3)]
    args = [spec(L, dh)] * 3
    if causal:
        args.append(spec(blk, blk))
        arrays.append(np.tril(np.ones((blk, blk))))

    def kernel(qq, kk, vv, *rest):
        return attention(
            qq, kk, vv, block=blk, causal=causal, tri=rest[0] if causal else None
        )

    return D.trace(kernel, *args), arrays


@pytest.mark.parametrize("causal", [False, True])
def test_attention_still_lowers_for_the_legacy_machine(causal):
    """The compatibility floor: it must still COMPILE for the card in the field.

    Execution is a separate question -- see the xfail below. Lowering is what
    this file exists to protect, because a change that stops the shipped machine
    compiling is a change nobody can run on hardware at all.
    """
    graph, _ = _attention_graph(causal)
    sched = lower(graph, VU13P_LEGACY)
    prog = codegen(sched, 64, 64, 64, VU13P_LEGACY)
    assert prog.insts, "legacy target produced an empty program"
    assert any(b.engine.value == "matmul" for b in sched.bands)


@pytest.mark.xfail(
    reason="the planner does not validate against target capacities: it emits a "
    "program that overruns TILES=256/GA=32/GB=32 and the interpreter indexes "
    "past the tile memory. ON SILICON the same fault is SILENT -- 15,440 of "
    "16,384 elements past 10% error with every gate passing. See "
    "ktpu.hw.board.Board, which holds the per-bitstream capacities the driver "
    "plans under as a workaround.",
    raises=(IndexError, ValueError),
    strict=True,
)
def test_attention_executes_on_the_legacy_capacities():
    """Should pass once the planner respects `Target.tiles`/`l1_a`/`l1_b`."""
    graph, arrays = _attention_graph(False)
    sched = lower(graph, VU13P_LEGACY)
    prog = codegen(sched, 64, 64, 64, VU13P_LEGACY)
    mesh = Mesh(prog, VU13P_LEGACY)
    mesh.upload(sched.bands[0], arrays[0], arrays[1])
    names = [graph.producer(v).attrs["name"] for v in graph.inputs]
    for name, arr in list(zip(names, arrays, strict=True))[2:]:
        mesh.bind(name, arr)
    mesh.run()


def test_the_legacy_capacities_are_the_bitstreams_and_not_the_rtls():
    """Frozen at synthesis, and the RTL has moved on since.

    Planning for the RTL's 512/128/256 against a machine built with 256/32/32
    put 15,440 of 16,384 elements past 10% error ON REAL SILICON -- the CU wraps
    and computes on operands that have overwritten each other. Pinned because
    the failure is a plausible wrong answer, not a crash.
    """
    assert (VU13P_LEGACY.tiles, VU13P_LEGACY.l1_a, VU13P_LEGACY.l1_b) == (256, 32, 32)
    assert (VU13P_8CU.tiles, VU13P_8CU.l1_a, VU13P_8CU.l1_b) == (512, 128, 256)
    # Four vector cores on the card: vec_cu plus three parameterized variants.
    assert VU13P_LEGACY.vector_cores == 4


def test_the_split_layout_reproduces_the_coordinates_on_silicon():
    """Manager and accumulator in ADJACENT COLUMNS of one row.

    Transcribed from the shipped bitstream's own OOC synthesis log, which
    elaborated MGR=(1,1) ACU=(2,1) and MGR=(1,2) ACU=(2,2) -- the accumulator
    is beside its manager, not below it. A flit sent to the wrong node is a
    hang, so these are pinned rather than derived here, and
    `boards/singlemesh_2x2.json` carries the same numbers for the driver.
    """
    m = bench.mesh(2, "split")
    assert (m["ncol"], m["nrow"]) == (2, 2)
    assert [tuple(c["cu"]) for c in m["clusters"]] == [(1, 1), (1, 2)]
    assert [tuple(c["acu"]) for c in m["clusters"]] == [(2, 1), (2, 2)]

    # A row is a MAG port and holds a whole cluster, so at eight the pair count
    # doubles per row rather than the band count doubling down the grid.
    m8 = bench.mesh(8, "split")
    assert (m8["ncol"], m8["nrow"]) == (4, 4)
    assert [c["cu"][1] for c in m8["clusters"]] == [1, 1, 2, 2, 3, 3, 4, 4]
    assert [c["acu"][1] for c in m8["clusters"]] == [1, 1, 2, 2, 3, 3, 4, 4]
    assert [c["cu"][0] for c in m8["clusters"]] == [1, 3] * 4
    assert [c["acu"][0] for c in m8["clusters"]] == [2, 4] * 4


def test_split_tops_out_at_eight_and_merged_does_not():
    """A split cluster eats two columns, and a four-column grid holds two rows of them."""
    assert bench.cluster_counts("split") == tuple(range(1, 9))
    assert bench.cluster_counts("merged") == tuple(range(1, 17))


def test_the_merged_layout_is_the_default():
    assert bench.LAYOUT == "merged"
    assert bench.cluster_list(2) == [(1, 1), (1, 2)]
    assert bench.cluster_list(2, "split") == [(1, 1), (1, 2)]
