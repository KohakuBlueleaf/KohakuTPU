"""The L3 -> L2 compiler: every reference program compiled and run on the unit
models against the L3 interpreter; the compiled schedules against the
hand-written ones (bit for bit where they do the same arithmetic, in modelled
cycles); the layouts the host packs against the clusters' drain order; and
each refusal at the form it refuses."""

import itertools

import numpy as np
import pytest
from kohakuaccel.ir.l3.instance import Stmt, instantiate
from kohakutpu.ir import l2, l3
from kohakutpu.ir.l1 import vsched
from kohakutpu.ir.l1.kernels import attention as AT
from kohakutpu.ir.l1.kernels import matmul as MM
from kohakutpu.ir.l1.kernels import vgen as VG
from kohakutpu.ir.l1.kernels import vrun as VR
from kohakutpu.ir.l2 import text as l2_text
from kohakutpu.ir.l2.layouts import Flat, MxA, MxB, TileCols, Tiles
from kohakutpu.ir.l2.lowerers import (
    DISPATCH,
    _run_cycles,
    _stream_programs,
    stream_cost,
)
from kohakutpu.ir.l3 import lower
from kohakutpu.ir.numerics import (
    LOG2E,
    UnitModel,
    _model_attention,
    _model_matmul,
    _Rig,
)


def h(rng, *shape, scale=1.0, shift=0.0):
    return (rng.standard_normal(shape) * scale + shift).astype(np.float16)


def rel_l2(got, want) -> float:
    return float(np.linalg.norm(got - want) / np.linalg.norm(want))


def run(module, program, arrays, **knobs):
    t = UnitModel()
    shapes = {k: np.shape(v) for k, v in arrays.items()}
    comp = lower.compile(module, program, shapes, t, **knobs)
    return comp, comp.run(t, arrays)


def cases():
    rng = np.random.default_rng(7)
    q = h(rng, 2, 64, 64, scale=LOG2E / 8)
    return [
        ("linear", "matmul", {"x": h(rng, 64, 128), "w": h(rng, 96, 128)}),
        (
            "linear",
            "linear",
            {"x": h(rng, 64, 128), "w": h(rng, 96, 128), "bias": h(rng, 96)},
        ),
        ("linear", "linear_silu", {"x": h(rng, 128, 64), "w": h(rng, 128, 64)}),
        ("linear", "silu_call", {"x": h(rng, 32, 512, scale=2)}),
        ("rows", "softmax", {"x": h(rng, 32, 320, scale=2)}),
        (
            "rows",
            "layernorm",
            {
                "x": h(rng, 48, 128, scale=2, shift=0.5),
                "g": h(rng, 128, scale=0.5, shift=1),
                "b": h(rng, 128, scale=0.5),
            },
        ),
        ("conv", "conv_only", {"x": h(rng, 8, 12, 64), "w": h(rng, 32, 3, 3, 64)}),
        (
            "conv",
            "conv",
            {
                "x": h(rng, 8, 12, 64),
                "w": h(rng, 32, 3, 3, 64),
                "x2": h(rng, 8, 12, 32),
            },
        ),
        (
            "attention",
            "attention",
            {"q": q, "k": h(rng, 2, 192, 64), "vt": h(rng, 2, 64, 192)},
        ),
    ]


#: rel_l2 against the interpreter: an fp16 store; attention stores S and P in
#: fp16 where the interpreter keeps them f32.
BOUND = {"attention": 3e-3}


@pytest.mark.parametrize("module,program,arrays", cases(), ids=[c[1] for c in cases()])
def test_a_reference_program_compiled_is_the_interpreter(module, program, arrays):
    m = l3.kernel(module)
    _, got = run(m, program, arrays)
    want = l3.run(m, program, arrays)
    for name in want:
        assert got[name].shape == want[name].shape
        assert rel_l2(got[name], want[name]) < BOUND.get(program, 4e-4), name


def test_the_cluster_bias_path_is_the_hand_schedule_bit_for_bit():
    """Same items, same packing: the compiled linear (bias on the clusters, the
    default) and `l2.ops.matmul` with its ones x bias block give the same bytes."""
    rng = np.random.default_rng(1)
    x, w, b = h(rng, 128, 128, scale=0.5), h(rng, 128, 128, scale=0.5), h(rng, 128)
    comp, got = run(l3.kernel("linear"), "linear", {"x": x, "w": w, "bias": b})
    assert not any(i.kind == "vec_stream" for i in comp.schedule.items)
    assert np.array_equal(got["y"], _model_matmul(UnitModel(), x, w, bias=b))


def test_the_core_bias_path_adds_the_bias_exactly():
    """``bias="core"``: the product's fp16 drain plus the fp16 bias, rounded
    once -- what the interpreter computes, element for element."""
    rng = np.random.default_rng(2)
    x, w, b = h(rng, 64, 128, scale=0.5), h(rng, 64, 128, scale=0.5), h(rng, 64)
    m = l3.kernel("linear")
    _, got = run(m, "linear", {"x": x, "w": w, "bias": b}, bias="core")
    _, plain = run(m, "matmul", {"x": x, "w": w})
    want = (plain["y"] + b.astype(np.float64)).astype(np.float16).astype(np.float64)
    assert np.array_equal(got["y"], want)


def test_tile_epilogues_alternate_between_the_cores():
    """The node waits for the core that can start soonest: with equal tiles on
    every cluster and their drains held late, the epilogue sends alternate
    between the two cores rather than queueing one core's waits ahead of the
    other's ready work (the soonest producer alone queued four in a row)."""
    m, k, n, g = 1024, 512, 1024, 64
    r = _Rig(UnitModel())
    a = r.buf("a", MxA(m, k, g, 2))
    b = r.buf("b", MxB(n, k, g, 2))
    c = r.buf("c", Tiles(m, n, g, g))
    sinks = [r.buf(f"sink{i}", Flat(256 * 16)) for i in range(len(r.vcs))]
    l2.ops.matmul_silu(r.s, r.mgs, r.vcs, a, b, c, sinks, late=True)
    (prog,) = l2.compile(r.s)
    order = [
        s[1] for s in prog.steps if s[0] == "send" and s[1] in set(map(tuple, r.vcs))
    ]
    assert len(order) == 16
    assert all(x != y for x, y in itertools.pairwise(order))


BIAS_SILU = """level l3
program p(x: mx7[M, K], w: mx7[N, K], bias: f16[N])
    tile b = 32
    y = output : f16[M, N]
    map i in tiles(M, b), j in tiles(N, b)
        c = mmt x[i, :], w[j, :] : f16[b, b]
        r = add c, bias[j] : f16[b, b]
        t = mul r, -1.4426950408889634 : f32[b, b]
        e = exp2 t : f32[b, b]
        d = add e, 1.0 : f32[b, b]
        q = inv d : f32[b, b]
        z = mul r, q : f16[b, b]
        store y[i, j] = z
"""


def test_a_bias_under_more_epilogue_rides_the_clusters():
    """``add c, bias[j]`` read once goes to the bias K-block; the silu after it
    stays on the cores, reading the biased tile."""
    rng = np.random.default_rng(6)
    m = l3.read(BIAS_SILU)
    arrays = {"x": h(rng, 64, 64), "w": h(rng, 64, 64), "bias": h(rng, 64)}
    comp, got = run(m, "p", arrays)
    tiles = [i for i in comp.schedule.items if i.kind == "gemm_tile"]
    assert all(i.params.get("bias_at") is not None for i in tiles)
    (body,) = {i.params["body"] for i in comp.schedule.items if i.kind == "vec_stream"}
    assert not any(n[1] == "add" and ("col", 0) in n[2] for n in body[5])
    assert rel_l2(got["y"], l3.run(m, "p", arrays)["y"]) < 4e-4


def test_compiled_attention_is_the_hand_kernel_to_its_row_sums():
    """The same phases and roundings but one: the generated row sum folds the
    columns into two partials where the hand kernel runs one chain. The sums
    differ in their last E8M15 bits (~1e-4 after 64 adds), which flips some
    outputs' fp16 rounding: under half an fp16 ulp in rel_l2."""
    rng = np.random.default_rng(3)
    q = h(rng, 32, 64, scale=LOG2E / 8)
    k, v = h(rng, 128, 64), h(rng, 128, 64)
    arrays = {"q": q[None], "k": k[None], "vt": v.T[None]}
    _, got = run(l3.kernel("attention"), "attention", arrays)
    hand = _model_attention(UnitModel(), q, k, v)
    assert rel_l2(got["o"][0], hand) < 2.0**-11


# ------------------------------------------------------------ schedules
def compiled(module, program, shapes, **knobs):
    t = UnitModel()
    return lower.compile(l3.kernel(module), program, shapes, t, **knobs)


def test_a_compiled_schedule_reads_back_from_its_l2_text():
    comp = compiled(
        "attention",
        "attention",
        {"q": (1, 32, 64), "k": (1, 128, 64), "vt": (1, 64, 128)},
    )
    text = l2_text.write(comp.schedule)
    back = l2_text.read(text)
    assert l2_text.write(back) == text
    built = [p.build(None, {}).build().to_bytes() for p in l2.compile(comp.schedule)]
    again = [p.build(None, {}).build().to_bytes() for p in l2.compile(back)]
    assert built == again


def vp_of(module, program, shapes, **knobs):
    comp = compiled(module, program, shapes, **knobs)
    it, *_ = [i for i in comp.schedule.items if i.kind == "vec_stream"]
    return it.params["body"], it.params["words"], it.params["runs"]


#: Hand kernels and the vector programs generated for the same L3 op, in
#: modelled cycles over the compiled RUNs, install included.
@pytest.mark.parametrize(
    "module,program,shapes,hand",
    [
        ("rows", "softmax", {"x": (64, 128)}, ("softmax", 128, 16, 8)),
        (
            "rows",
            "layernorm",
            {"x": (64, 128), "g": (128,), "b": (128,)},
            ("layernorm", 128, 16, 1e-5),
        ),
        ("linear", "silu_call", {"x": (64, 1024)}, ("silu", 3, 2)),
    ],
)
def test_a_generated_vector_program_is_within_3_percent_of_the_hand_kernel(
    module, program, shapes, hand
):
    spec, words, runs = vp_of(module, program, shapes)
    assert stream_cost(spec, words, runs) <= 1.03 * stream_cost(hand, words, runs)


def test_a_run_size_is_chosen_with_its_images_install():
    """softmax 32x128 on two cores: one 128-word RUN models 375 cycles under
    two 64-word RUNs, but its images are 200 words larger, about 10k cycles of
    install (card_v9_1n not resident: 50.4k against 30.0k). A core that holds
    the images (`installs=0`) takes the one RUN."""
    spec, words, runs = vp_of("rows", "softmax", {"x": (32, 128)})
    assert (words, runs) == (64, 2)
    assert _run_cycles(spec, 128, 1) + DISPATCH < 2 * (
        _run_cycles(spec, 64, 2) + DISPATCH
    )
    assert stream_cost(spec, 64, 2) < stream_cost(spec, 128, 1)
    assert vp_of("rows", "softmax", {"x": (32, 128)}, installs=0)[1:] == (128, 1)


def test_a_generated_add_is_the_hand_binary_kernels_order():
    """Flat slices four at a time, loads first, one input after another: the
    generated add's images are the hand kernel's, instruction for instruction
    but for register names (card: 93.9k each, against 102.6k one slice at a
    time)."""

    def shape(ins):
        return (type(ins).__name__, getattr(ins, "op", None)) + tuple(
            getattr(ins, f, None) for f in ("ad", "off", "l1", "imm", "mode")
        )

    text = """level l3
program add(a: f16[N], b: f16[N])
    y = output : f16[N]
    s = add a, b : f16[N]
    store y = s
"""
    comp = lower.compile(
        l3.read(text), "add", {"a": (65536,), "b": (65536,)}, UnitModel(), installs=0
    )
    p = comp.schedule.items[0].params
    gen = _stream_programs(tuple(p["body"]), p["words"], p["runs"], p["install"])
    hand = _stream_programs(("binary", "add", 4, 2), p["words"], p["runs"], 1.0)
    assert [[shape(x) for x in i] for i in gen[0]] == [
        [shape(x) for x in i] for i in hand[0]
    ]


def test_epilogue_streams_on_a_core_share_one_install():
    """16 tiles on two cores: each core installs its images once for its 8."""
    text = """level l3
program linear(x: mx7[M, K], w: mx7[N, K], bias: f16[N])
    tile bm = 256
    tile bn = 256
    y = output : f16[M, N]
    map i in tiles(M, bm), j in tiles(N, bn)
        c = mmt x[i, :], w[j, :] : f16[bm, bn]
        r = add c, bias[j] : f16[bm, bn]
        store y[i, j] = r
"""
    shapes = {"x": (1024, 512), "w": (1024, 512), "bias": (1024,)}
    comp = lower.compile(l3.read(text), "linear", shapes, UnitModel(), bias="core")
    vec = [i for i in comp.schedule.items if i.kind == "vec_stream"]
    assert len(vec) == 16
    assert {i.params["install"] for i in vec} == {1 / 8}


def test_generated_attention_runs_are_within_3_percent_of_the_hand_images():
    comp = compiled(
        "attention",
        "attention",
        {"q": (1, 32, 64), "k": (1, 128, 64), "vt": (1, 64, 128)},
    )
    spec = next(i.params["prog"] for i in comp.schedule.items if i.kind == "vec_prog")
    gm = 8
    hand = {
        "p1": AT.softmax_image(gm),
        "p4": AT.update_image(gm),
        "final": AT.final_image(gm),
    }
    for name, img in hand.items():
        assert VR.cycles(spec, name) <= 1.03 * vsched.cycles(img, AT.l1_map(gm)), name


# -------------------------------------------------------------- layouts
def test_tiles_pack_is_the_drain_order():
    """Word `t` of tile (i, j) is sub-tile (t // gn, t % gn), its 16 lanes the
    4x4 block row-major -- spelled out here, not decoded back."""
    rows, cols, gm, gn = 16, 32, 2, 4
    x = np.arange(rows * cols).reshape(rows, cols).astype(np.float16)
    raw = np.frombuffer(Tiles(rows, cols, gm, gn).pack(x), np.float16)
    words = raw.reshape(-1, 16)
    tn = cols // (4 * gn)
    for ti in range(rows // (4 * gm)):
        for tj in range(tn):
            for t in range(gm * gn):
                sr, sc = divmod(t, gn)
                r0, c0 = (ti * gm + sr) * 4, (tj * gn + sc) * 4
                want = x[r0 : r0 + 4, c0 : c0 + 4].ravel()
                assert np.array_equal(words[(ti * tn + tj) * gm * gn + t], want)
    got = MM.unpack(
        lambda at, n: bytes(raw.tobytes()[at : at + n]),
        [(0, gm, 0, 0)],
        4 * gm,
        4 * gn,
        gm,
        gn,
    )
    assert np.array_equal(got, x[: 4 * gm, : 4 * gn])


def test_tilecols_puts_each_columns_value_in_its_four_rows():
    cols, gn = 32, 2
    v = np.arange(cols, dtype=np.float16) + 1
    raw = np.frombuffer(TileCols(cols, gn, 1).pack([v]), np.float16).reshape(-1, 16)
    for j in range(cols // (4 * gn)):
        for sc in range(gn):
            word = raw[j * gn + sc].reshape(4, 4)
            for i in range(4):
                assert np.array_equal(
                    word[i], v[(j * gn + sc) * 4 : (j * gn + sc) * 4 + 4]
                )


# ------------------------------------------------------------ the passes
def test_contract_fuses_a_mul_read_once_by_an_add():
    nodes = (
        ("t", "mul", (("in", 0), ("s", 2.0))),
        ("y", "add", (("v", "t"), ("in", 0))),
    )
    assert VG.contract(nodes) == (("y", "fma", (("in", 0), ("s", 2.0), ("in", 0))),)
    assert VG.contract(nodes, keep={"t"}) == nodes
    twice = nodes + (("z", "add", (("v", "t"), ("v", "y"))),)
    assert VG.contract(twice) == twice


def test_rows_too_wide_for_two_images_share_one_compute_image():
    """softmax over 512 columns: two parity images pass instruction memory,
    so one compute image walks the region `AD_L1`'s base names."""
    narrow, wide = (vp_of("rows", "softmax", {"x": (32, c)}) for c in (128, 512))
    assert len(VG.programs(*narrow)[0]) == 5
    progs = VG.programs(*wide)[0]
    assert len(progs) == 6 and stream_words(progs) <= VG.IMEM


def stream_words(progs) -> int:
    return sum(len(i.words()) for p in progs for i in p)


def test_a_wide_value_read_a_pass_later_goes_through_l1():
    """softmax's ``e``: made before the sum, read after it -- stored over the
    input's slot, whose last read is the same pass (no scratch)."""
    spec, *_ = vp_of("rows", "softmax", {"x": (16, 128)})
    p = VG.plan(spec)
    assert p.passes == 3 and p.scratch == 0
    (e,) = [n for n in p.slot if n != p.out]
    assert p.slot[e] == 0


SELECT = """level l3
program relu_add(a: f16[N], b: f16[N])
    y = output : f16[N]
    s = add a, b : f32[N]
    neg = cmp.lt s, 0.0 : bool[N]
    z = mul s, 0.0 : f32[N]
    r = select neg, z, s : f16[N]
    store y = r
"""

TWO_PASS = """level l3
program centre_exp(x: f16[R, C])
    tile br = 8
    y = output : f16[R, C]
    map i in tiles(R, br)
        e = exp2 x[i, :] : f32[br, C]
        m = reduce.sum x[i, :] axis=1 : f32[br]
        n = reduce.max e axis=1 : f32[br]
        d = sub e, m[:, *] : f32[br, C]
        w = mul d, n[:, *] : f16[br, C]
        store y[i, :] = w
"""


def test_compare_and_select_lower_to_a_predicated_move():
    rng = np.random.default_rng(4)
    m = l3.read(SELECT)
    arrays = {"a": h(rng, 4096), "b": h(rng, 4096)}
    _, got = run(m, "relu_add", arrays)
    assert rel_l2(got["y"], l3.run(m, "relu_add", arrays)["y"]) < 4e-4


def test_a_program_of_two_reductions_on_different_values():
    rng = np.random.default_rng(5)
    m = l3.read(TWO_PASS)
    arrays = {"x": h(rng, 32, 128, scale=0.5)}
    _, got = run(m, "centre_exp", arrays)
    assert rel_l2(got["y"], l3.run(m, "centre_exp", arrays)["y"]) < 4e-4


def test_instantiate_inlines_calls_and_drops_dead_values():
    m = l3.kernel("linear")
    inst = instantiate(m, "silu_call", {"x": (32, 64)})
    (loop,) = inst.body
    names = [s.name for s in loop.body if isinstance(s, Stmt)]
    assert names == ["a.t", "a.e", "a.d", "a.r", "a.y"]
    assert inst.values["a.t"] == ("f32", (16, 64))


# ------------------------------------------------------------- refusals
REFUSED = [
    (
        """level l3
program p(x: mx7[M, K], w: mx7[N, K])
    tile b = 32
    y = output : f16[M, N]
    map i in tiles(M, b), j in tiles(N, b)
        c = mmt x[i, :], w[j, :] : f16[b, b]
        s = reduce.max c axis=1 : f32[b]
        d = sub c, s[:, *] : f16[b, b]
        store y[i, j] = d
""",
        {"x": (64, 64), "w": (64, 64)},
        "reduction",
    ),
    (
        """level l3
program p(a: f16[N], b: f16[N], c: f16[N])
    y = output : f16[N]
    s = add a, b : f32[N]
    t = add s, c : f16[N]
    store y = t
""",
        {"a": (4096,), "b": (4096,), "c": (4096,)},
        "3 streamed inputs",
    ),
    (
        """level l3
program p(x: f16[N])
    y = output : f16[N]
    z = output : f16[N]
    a = exp2 x : f16[N]
    store y = a
    store z = a
""",
        {"x": (4096,)},
        "stores 2 values",
    ),
    (
        """level l3
program p(x: f16[N])
    y = output : f32[N]
    a = exp2 x : f32[N]
    store y = a
""",
        {"x": (4096,)},
        "fp16",
    ),
]


@pytest.mark.parametrize(
    "text,shapes,what",
    REFUSED,
    ids=["reduce-tile", "three-inputs", "two-stores", "f32-out"],
)
def test_a_form_the_compiler_does_not_lower_is_refused_by_name(text, shapes, what):
    m = l3.read(text)
    with pytest.raises(lower.LowerError) as e:
        compiled_text(m, shapes)
    assert what in str(e.value)


def compiled_text(m, shapes):
    (name,) = m.programs
    return lower.compile(m, name, shapes, UnitModel())
