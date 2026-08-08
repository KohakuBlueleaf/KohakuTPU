"""The level-3 timing model: cycles, and what they are spent on.

The model is analytic, so these check its STRUCTURE -- that it scales, that it
charges loop bodies per trip, that it bills the right engine -- plus the one
external anchor available without running xsim: the measured percentages of
peak in docs/perf.md must fall inside the bracket the model brackets them with.
"""

import pytest

import ktpu.dsl as D
from ktpu.codegen import codegen, cost_of
from ktpu.interp.timing import MACS_PER_CLUSTER, cost_of_inst, time_of
from ktpu.ir import FP16
from ktpu.ir.program import Opcode
from ktpu.passes import lower
from ktpu.target import VU13P_8CU, Target


def spec(*s):
    return D.TensorSpec(s, FP16)


def build(m, k, n, target=VU13P_8CU, epilogue=False):
    fn = (lambda a, b: D.gelu(a @ b)) if epilogue else (lambda a, b: a @ b)
    graph = D.trace(fn, spec(m, k), spec(k, n))
    sched = lower(graph, target)
    prog = codegen(sched, m, k, n, target)
    return prog, cost_of(prog, sched, target)


def peak_gflops(t):
    return MACS_PER_CLUSTER * 2 * t.clusters * t.mhz * 1e6 / 1e9


def test_gemm_costs_its_macs_at_the_cluster_rate():
    prog, _ = build(128, 128, 128)
    gemm = next(i for i in prog.insts if i.op is Opcode.GEMM)
    f = gemm.fields
    t = VU13P_8CU
    macs = (f["gm"] * t.lanes) * (f["nk"] * t.kblock) * (f["gn"] * t.lanes)
    assert cost_of_inst(gemm, t) == -(-macs // MACS_PER_CLUSTER)


def test_doubling_the_clusters_halves_the_cycles():
    """Every cluster holds its own stream and they run concurrently, so the
    critical path is the slowest node -- not a sum over dispatch rounds."""
    seen = {}
    for ncl in (1, 2, 4, 8):
        prog, cost = build(256, 1024, 256, Target(clusters=ncl))
        seen[ncl] = time_of(prog, Target(clusters=ncl), cost.flops).cycles
    for ncl in (2, 4, 8):
        assert seen[ncl] == pytest.approx(seen[1] / ncl, rel=0.02)


def test_a_loop_body_is_charged_once_per_trip():
    """VLOOP itself is free and its body runs `count` times. Charging the body
    once reads as a plausible number and undercounts by the trip count."""
    prog, cost = build(256, 256, 256, epilogue=True)
    loop = next((i for i in prog.insts if i.op is Opcode.VLOOP), None)
    assert loop is not None, "the epilogue should have produced a VLOOP"
    assert cost_of_inst(loop, VU13P_8CU) == 0

    timing = time_of(prog, VU13P_8CU, cost.flops)
    trips = loop.fields["count"]
    span = loop.fields["body"]
    body = prog.insts[prog.insts.index(loop) + 1 :][:span]
    per_trip = sum(cost_of_inst(x, VU13P_8CU) for x in body)
    assert timing.busy["vector"] >= per_trip * trips


def test_matmul_and_vector_are_billed_apart_though_they_share_nodes():
    """A cluster and a vector core have the same mesh coordinate, so keying the
    engine off the node silently loses one of them."""
    prog, cost = build(256, 1024, 256, epilogue=True)
    timing = time_of(prog, VU13P_8CU, cost.flops)
    assert timing.busy["matmul"] > 0
    assert timing.busy["vector"] > 0
    assert {e for e, _ in timing.per_node} == {"matmul", "vector"}


def test_a_kernel_with_no_epilogue_spends_nothing_on_the_vector_cores():
    prog, cost = build(256, 1024, 256)
    assert time_of(prog, VU13P_8CU, cost.flops).busy.get("vector", 0) == 0


@pytest.mark.parametrize(
    ("m", "k", "n", "clusters", "measured"),
    [
        # docs/perf.md: 81.2% at 4 CU (task #37), 72.7% at 8 CU on the new
        # geometry (s0.1). Both must fall inside the model's bracket.
        (256, 1024, 256, 4, 81.2),
        (512, 256, 1024, 8, 72.7),
    ],
)
def test_the_measured_peak_falls_inside_the_bracket(m, k, n, clusters, measured):
    """`cycles` charges every FILL; `overlapped` hides them all behind the GEMM
    before them. The real machine double-buffers A, so it sits between."""
    t = Target(clusters=clusters)
    prog, cost = build(m, k, n, t)
    timing = time_of(prog, t, cost.flops)
    hz = t.mhz * 1e6
    low = (cost.flops / (timing.cycles / hz) / 1e9) / peak_gflops(t) * 100
    high = (cost.flops / (timing.overlapped / hz) / 1e9) / peak_gflops(t) * 100
    assert low < measured < high, f"{low:.1f}% .. {high:.1f}% excludes {measured}%"


def test_seconds_and_gflops_follow_the_clock():
    prog, cost = build(256, 256, 256)
    timing = time_of(prog, VU13P_8CU, cost.flops)
    assert timing.seconds == pytest.approx(timing.cycles / (VU13P_8CU.mhz * 1e6))
    assert timing.gflops == pytest.approx(cost.flops / timing.seconds / 1e9)
