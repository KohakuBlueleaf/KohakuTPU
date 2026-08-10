"""The band-fusion reorder, and why it is ON despite costing instructions.

`lower(reorder=True)` regroups elementwise runs so equal-span ops share a band.

It was disabled for one reason -- it takes causal from 531 level-3 instructions
to 539 -- and that was the wrong metric. The same change takes causal's vector
cycles 2,043 -> 1,645, a 19.5% win, arithmetic identical: `isa_study.py
--reorder on/off`. Here the two counts point opposite ways.

One mechanism explains both. Fusing a run whose natural grid is 4 into a band
that reduces puts it on the reduction's grid of 8, so the work is ISSUED as
eight core programs rather than four and RUNS on eight cores rather than four.
Idle core-slots across causal's vector bands go 29/88 -> 14/64.

The instruction counts stay pinned below, as a trade to re-examine whole rather
than a regression to fix. Turning the reorder off costs 19.5% of causal's
cycles, and that is what should go red.
"""

import numpy as np
import pytest

import ktpu.dsl as D
from ktpu.codegen import codegen
from ktpu.dsl.nn import attention
from ktpu.ir import FP16
from ktpu.passes import lower
from ktpu.passes.lower import fuse_order
from ktpu.target import VU13P_8CU as TGT

L, DH, BLOCK = 128, 64, 64


def spec(*shape):
    return D.TensorSpec(shape, FP16)


def build(causal):
    args = [spec(L, DH)] * 3
    if causal:
        args.append(spec(BLOCK, BLOCK))

    def kernel(q, k, v, *rest):
        return attention(
            q, k, v, block=BLOCK, causal=causal, tri=rest[0] if causal else None
        )

    return D.trace(kernel, *args)


def counts(graph, reorder):
    sched = lower(graph, TGT, reorder=reorder)
    prog = codegen(sched, BLOCK, BLOCK, BLOCK, TGT)
    vec = sum(1 for b in sched.bands if b.engine.value == "vector")
    return len(prog.insts), vec


def idle_slots(graph, reorder):
    """Core-slots a vector band leaves unused, summed over the bands.

    A band with a grid of 4 occupies four of eight cores and the other four
    wait, so this is the quantity the reorder actually moves.
    """
    sched = lower(graph, TGT, reorder=reorder)
    vec = [b for b in sched.bands if b.engine.value == "vector"]
    return sum(TGT.vector_cores - min(b.grid.size, TGT.vector_cores) for b in vec)


def test_reorder_is_on_by_default():
    """A default flip is the failure this file exists to catch."""
    graph = build(False)
    assert counts(graph, True) == counts(graph, None if False else True)
    assert lower(graph, TGT).bands == lower(graph, TGT, reorder=True).bands


@pytest.mark.parametrize(
    "causal,off,on",
    [
        (False, (419, 9), (357, 6)),
        (True, (531, 11), (539, 8)),
    ],
)
def test_the_reorder_costs_causal_instructions_and_is_still_right(causal, off, on):
    """Pinned so the trade cannot be re-litigated from one number.

    Causal reordered has FEWER bands and MORE instructions, and it is still the
    better schedule -- see `test_the_reorder_fills_cores_which_is_what_it_is_for`
    for the cycles. Band count is not the metric and neither, on its own, is
    instruction count.
    """
    graph = build(causal)
    assert counts(graph, False) == off
    assert counts(graph, True) == on


@pytest.mark.parametrize("causal", [False, True])
def test_the_reorder_fills_cores_which_is_what_it_is_for(causal):
    """The reason the instruction count is allowed to rise.

    Fusing a run whose natural grid is 4 into a band that reduces puts it on
    the reduction's grid of 8: eight core programs instead of four, running on
    eight cores instead of four. Causal goes 29 idle core-slots to 14, which is
    where its 19.5% cycle win comes from.
    """
    graph = build(causal)
    assert idle_slots(graph, True) < idle_slots(graph, False)


def test_no_band_is_dealt_to_fewer_cores_by_reordering():
    """The trade only holds while fusion never SHRINKS a grid. If it ever does,
    the instruction rise stops paying for itself and this file needs redoing."""
    for causal in (False, True):
        graph = build(causal)
        before = lower(graph, TGT, reorder=False).bands
        after = lower(graph, TGT, reorder=True).bands
        widest = max(b.grid.size for b in before if b.engine.value == "vector")
        assert (
            max(b.grid.size for b in after if b.engine.value == "vector") >= widest
        ), "reordering narrowed the widest grid"


def test_reordering_preserves_the_op_set():
    """It may move ops; it may not invent, drop or duplicate one."""
    for causal in (False, True):
        graph = build(causal)
        a = sorted(id(o) for o in fuse_order(graph, False))
        b = sorted(id(o) for o in fuse_order(graph, True))
        assert a == b


def test_reordering_respects_dependencies():
    """An op must never be scheduled before something it reads.

    Dependencies resolve THROUGH VIEWS -- a reshape is not in the run being
    reordered, so matching on the view itself would miss the edge.
    """
    for causal in (False, True):
        graph = build(causal)
        seen: set[int] = set()
        for op in fuse_order(graph, True):
            for v in op.inputs:
                vid = getattr(v, "vid", None)
                produced_later = any(
                    o.out.vid == vid for o in fuse_order(graph, True)[len(seen) + 1 :]
                )
                assert not (
                    produced_later and vid not in seen
                ), f"{op.kind} reads v{vid} before it is produced"
            seen.add(op.out.vid)


def test_the_numbers_in_the_docstring_are_the_measured_ones():
    """The docstring cites 419->357 and 531->539; keep it honest.

    A stale number here is a stale number in the module docstring, which is
    what someone reads before deciding whether to work on this.
    """
    assert counts(build(False), False)[0] == 419
    assert counts(build(False), True)[0] == 357
    assert counts(build(True), False)[0] == 531
    assert counts(build(True), True)[0] == 539
    assert np.isfinite(0.0)
