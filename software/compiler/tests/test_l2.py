"""L2 -> L1 and the L2 -> L2 passes (docs/spec/l2-program.md). A cluster
kernel's compiled package is witnessed against the hand-written L1's bytes;
the rest against the L1 text the lowering must write."""

import heapq
import random
import re
from dataclasses import replace

import pytest

from kohakuaccel.compiler.package.mover import MAX_INDEX, MOVE_WORDS, NDIM
from kohakutpu.compiler import build
from kohakutpu.compiler import target as T
from kohakutpu.compiler.l2 import body as B
from kohakutpu.compiler.l2 import lower, passes, pipeline
from kohakutpu.compiler.l2 import modulo as MO
from kohakutpu.compiler.l2 import mover as MV
from kohakutpu.compiler.l2 import timing as TI
from kohakutpu.compiler.l2 import vector as V
from kohakutpu.language import examples
from kohakutpu.language.text import module
from kohakutpu.language.text.reader import Reader
from kohakutpu.language.text.syntax import Int, TextError, emit


@pytest.fixture
def band(monkeypatch):
    """The band lowering only: these tests read its loop's shape."""
    monkeypatch.setattr(V, "STRATEGY", "band")


def _found(pattern: str, text: str) -> re.Match:
    m = re.search(pattern, text)
    assert m is not None, pattern
    return m


def replace_vregs(m, n: int):
    """Module `m` for vector cores of `n` registers."""
    return replace(m, vregs=n)


def packages(m, name, nodes=None) -> dict:
    mach = T.machine(nodes=nodes)
    params = m.body(name, "l1").params
    binds = {p: 0x100000 * (i + 1) for i, (p, _) in enumerate(params)}
    progs = build.programs(m, name, binds, mach)
    return {n: build.package(p)[0].to_bytes() for n, p in progs.items()}


def test_a_word_transpose_walks_the_source_column_first():
    l2 = examples.load(examples.example("transpose", "l2"))
    text = lower.lower(l2, "transpose")
    # y is one run of 128 words; x is read 16 word columns of 8 rows each.
    assert "copy w(x + 0, (16, 32), (8, 512)) -> w(y + 0, (128, 32))" in text


@pytest.mark.parametrize(
    "name", ["matmul", "longk", "gather", "scatter", "fill", "generate"]
)
def test_a_kernel_compiles_to_the_hand_package(name):
    l2 = examples.load(examples.example(name, "l2"))
    got = module.read(lower.lower(l2, name))
    hand = examples.load(examples.example(name))
    assert packages(got, name) == packages(hand, name)


def test_shared_fills_lower_to_the_node_rendezvous():
    l2 = examples.load(examples.example("share2", "l2"))
    text = lower.lower(l2, "share2", T.machine(nodes=2))
    node0 = text.split("    node 1")[0]
    # cross pairs: mg[0] leads mg[2], mg[1] leads mg[3] (lowest {y, x}).
    assert "peers=(mg[2])" in node0 and "peers=(mg[0])" in node0
    first = node0.index("t2 = mark mg[2]")
    own = node0.index("fill B0 <- w + 262144 n=128")  # mg[2]'s own B: tile 2, chunk 0
    shared = node0.index("fill A0 <- x + 0 n=128 peers=(mg[0])")
    wait = node0.index("wait mg[2] upto=t2")
    lead = node0.index("fill A0 <- x + 0 n=128 peers=(mg[2])")
    assert own < first < shared < wait < lead
    packages(module.read(text), "share2", nodes=2)


OPEN = """target ktpu.v9

fn mm.l2(x: mx7a[512, 512] @dram(gm=64, nk=2), w: mx7b[1024, 512] @dram(gn=64, nk=2), y: f16[512, 1024] @dram(tile=64x64))
    t = buffer : f16[512, 128] @mem
    par n in 0..2 on node[n]
        par u in 0..2 on mg[any]
            for i in 0..2
                a = fill x[i*256 : +256, 0 : +512]          : mx7a[256, 512] @A
                b = fill w[(2*n + u)*256 : +256, 0 : +512]  : mx7b[256, 512] @B
                acc = gemm a, b split                       : f32[256, 256] @acc
                drain acc -> y[i*256 : +256, (2*n + u)*256 : +256]
"""


def test_each_pass_writes_its_choice_in():
    m = module.read(OPEN)
    mach = T.machine(nodes=2)
    m = passes.run(m, "mm", ("place",), mach)
    assert "on mg[u]" in m.source and "any" not in m.source
    m = passes.run(m, "mm", ("space",), mach)
    assert "@stage" in m.source and "@mem" not in m.source
    m = passes.run(m, "mm", ("share",), mach)
    assert m.source.count("share=cross") == 1  # x's view does not name u
    m = passes.run(m, "mm", ("split",), mach)
    assert "for kk in 0..8" in m.source and "acc = zero" in m.source
    l1 = module.read(lower.lower(m, "mm", mach))
    got = packages(l1, "mm", nodes=2)
    assert set(got) == {0, 1}


def test_retile_takes_the_row_tile_the_issue_model_times_fastest(band):
    l2 = examples.load(examples.example("softmax", "l2"))
    eight = module.read(
        l2.source.replace("t*16 : +16", "t*8 : +8")
        .replace("[16, 128]", "[8, 128]")
        .replace("f32[16]", "f32[8]")
        .replace("0..32", "0..64")
    )
    got = passes.run(eight, "softmax", ("retile",), T.machine())
    # Twice the rows a trip, half the trips: 16 rows (32 do not fit L1).
    assert "for t in 0..32" in got.source and "t * 16 : +16" in got.source
    assert "f32[16, 128] @reg" in got.source
    # The written 16-row tile is already the fastest: unchanged.
    same = passes.run(l2, "softmax", ("retile",), T.machine())
    assert lower.lower(same, "softmax") == lower.lower(l2, "softmax")
    # Smaller silu and add tiles time the same in the model: a tie keeps the
    # written tile (on the card the smaller ones are 5-19% slower).
    for name in ("silu", "add"):
        k = examples.load(examples.example(name, "l2"))
        got = passes.run(k, name, ("retile",), T.machine())
        assert lower.lower(got, name) == lower.lower(k, name)


FAR = """\
target ktpu.v9

fn far.l2(x: f16[64, 70000] @dram, out: f16[64, 128] @dram)
    par c in 0..2 on vc[c]
        for t in 0..8
            xl = load x[c*32 + t*4 : +4, 0 : +128] : f16[4, 128] @l1(gt4)
            v = unpack xl : f32[4, 128] @reg(gt4)
            e = add v, 1.0 : f32[4, 128] @reg(gt4)
            p = pack e : f16[4, 128] @l1(gt4)
            store out[c*32 + t*4 : +4, 0 : +128] <- p
"""


def test_step_runs_trips_too_far_apart_for_ainc_as_a_node_loop():
    m = module.read(FAR)
    mach = T.machine(nodes=1)
    # Rows 140000 bytes apart: a trip of 4 steps 560000, past ainc's 18 bits.
    without = tuple(p for p in pipeline.PASSES if p != "step")
    with pytest.raises(TextError, match="ainc takes 18 signed bits"):
        pipeline.compile_l1(m, "far", mach, passes=without)
    got = passes.run(m, "far", ("step",), mach)
    assert "for t_trip in 0..8" in got.source
    assert "for t in t_trip..t_trip + 1" in got.source
    # The node loop holds the par, the par one trip.
    assert got.source.index("for t_trip") < got.source.index("par c")
    pipeline.compile_l1(m, "far", mach)
    # Rows close enough: unchanged.
    near = module.read(FAR.replace("70000", "128"))
    kept = passes.run(near, "far", ("step",), mach)
    assert "t_trip" not in kept.source
    assert lower.lower(kept, "far", mach) == lower.lower(near, "far", mach)


@pytest.mark.parametrize(
    "span, when, lo, hi",
    [
        ("0..8 - 3*c", "when t_trip < 8 - 3 * c", lambda c: 0, lambda c: 8 - 3 * c),
        ("2*c..8", "when t_trip >= 2 * c", lambda c: 2 * c, lambda c: 8),
    ],
)
def test_step_keeps_each_instance_to_its_own_trips(span, when, lo, hi):
    """Trips that depend on the instance: the node loop runs every
    instance's (0..8), a `when` keeps instance c to its own, an instance
    with none that trip runs nothing."""
    m = module.read(FAR.replace("for t in 0..8", f"for t in {span}"))
    mach = T.machine(nodes=1)
    got = passes.run(m, "far", ("step",), mach)
    assert "for t_trip in 0..8" in got.source and when in got.source
    l1 = pipeline.compile_l1(m, "far", mach).source
    runs = re.findall(r"send vc\[(\d)\]\n\s+run \w+\(x\+(\d+),", l1)
    # Instance c reads rows c*32 + 4t: 140000 bytes a row.
    want = [
        (c, (c * 32 + 4 * t) * 140000)
        for t in range(8)
        for c in (0, 1)
        if lo(c) <= t < hi(c)
    ]
    assert [(int(c), int(x)) for c, x in runs] == want
    assert len(re.findall(r"send vc\[1\]", l1)) == hi(1) - lo(1)


def test_step_finds_a_value_a_trip_carries_from_the_last():
    text = FAR.replace("e = add v, 1.0", "e = add v, 1.0\n            f = add e, e")
    par = next(s for s in passes._body(module.read(text), "far")[1] if s.op == "par")
    loop = par.body[0]
    assert not passes._carried(loop)
    # `f` read before its trip makes it: the last trip's value.
    b = loop.body
    assert passes._carried(replace(loop, body=[b[0], b[1], b[3], b[2], *b[4:]]))


WIDE = """\
target ktpu.v9

fn wide.l2(x: f16[64, 256] @dram, out: f16[64, 256] @dram)
    par c in 0..2 on vc[c]
        for t in 0..2
            xl = load x[c*32 + t*16 : +16, 0 : +256] : f16[16, 256] @l1(gt4)
            v = unpack xl : f32[16, 256] @reg(gt4)
            e = add v, 1.0 : f32[16, 256] @reg(gt4)
            p = pack e : f16[16, 256] @l1(gt4)
            store out[c*32 + t*16 : +16, 0 : +256] <- p
"""


def test_retile_fits_a_tile_the_core_does_not_hold():
    m = module.read(WIDE.replace("256", "512"))
    mach = T.machine(nodes=1)
    # 16 rows of 512: one buffer of the view is 16 KB, a core's whole L1.
    with pytest.raises(TextError, match="L1"):
        lower.lower(m, "wide", mach)
    got = passes.run(m, "wide", ("retile",), mach)
    rows = int(_found(r"t \* (\d+) : \+\1", got.source)[1])
    assert rows < 16 and 16 % rows == 0
    lower.lower(got, "wide", mach)
    # The trip count scales so the rows covered stay the same.
    assert f"for t in 0..{2 * 16 // rows}" in got.source


def test_retile_scales_a_trip_count_that_depends_on_the_instance():
    text = WIDE.replace("256", "512").replace("for t in 0..2", "for t in 0..2 - c")
    got = passes.run(module.read(text), "wide", ("retile",), T.machine(nodes=1))
    rows = int(_found(r"t \* (\d+) : \+\1", got.source)[1])
    hi = _found(r"for t in 0\.\.(.+)", got.source)[1]
    # Each instance covers the rows it did: (2 - c) trips of 16.
    for c in (0, 1):
        assert eval(hi, {}, {"c": c}) * rows == (2 - c) * 16


@pytest.mark.parametrize("span", ["1..2", "t_trip..t_trip + 1"])
@pytest.mark.parametrize("new", [4, 8, 32])
def test_retile_covers_the_rows_the_trips_did(span, new):
    """Trips LO..HI of 16 rows cover rows 16 LO .. 16 HI: at `new` rows a
    trip, LO and HI scale by 16 / new (a node loop's trip as LO included)."""
    text = WIDE.replace("for t in 0..2", f"for t in {span}")
    par = next(s for s in passes._body(module.read(text), "wide")[1] if s.op == "par")
    loop = par.body[0]
    dom = loop.positional()[0].rhs
    if not passes._scales(dom, 16, new):
        # 32-row trips start on rows 32 t: neither row 16 nor 16 t_trip.
        assert new == 32
        return
    got = emit([passes._scaled(loop, "t", 16, new, dom)], 0)
    lo, hi = _found(r"for t in (.+)\.\.(.+)", got).groups()
    for trip in (0, 3):
        a, b = (eval(x, {}, {"t_trip": trip}) for x in (lo, hi))
        want = (16 * eval(span.split("..")[0], {}, {"t_trip": trip}),) * 2
        assert (a * new, b * new) == (want[0], want[0] + 16)


def test_retile_scales_rows_not_a_column_run_of_the_same_size():
    text = WIDE.replace("t*16 : +16, 0 : +256", "t*32 : +32, 0 : +32").replace(
        "[16, 256]", "[32, 32]"
    )
    par = next(s for s in passes._body(module.read(text), "wide")[1] if s.op == "par")
    loop = par.body[0]
    dom = loop.positional()[0].rhs
    got = emit([passes._scaled(loop, "t", 32, 8, dom)], 0)
    assert "t * 8 : +8, 0 : +32" in got and "[8, 32]" in got


BAND = """\
target ktpu.v9

fn band.l2(x: f16[64, 128] @dram, g: f16[128] @dram, out: f16[64, 128] @dram)
    par c in 0..2 on vc[c]
        gl = load g[0 : +128] : f16[4, 128] @l1(gt4)
        gv = unpack gl : f32[4, 128] @reg(gt4)
        for t in 0..2
            xl = load x[c*32 + t*16 : +16, 0 : +128] : f16[16, 128] @l1(gt4)
            v = unpack xl : f32[16, 128] @reg(gt4)
            b = add gv, 2.0 : f32[4, 128] @reg(gt4)
            b2 = mul b, 0.5 : f32[4, 128] @reg(gt4)
            e = mul v, b2 : f32[16, 128] @reg(gt4)
            p = pack e : f16[16, 128] @l1(gt4)
            store out[c*32 + t*16 : +16, 0 : +128] <- p
"""


@pytest.mark.parametrize("rows, new", [(16, 4), (16, 8), (16, 32), (4, 8)])
def test_retile_keeps_a_band_made_before_the_loop(rows, new):
    """`b` reads only a band made before the loop: one value every trip, its
    4 rows not the stream's (even at a tile of 4); every other op's rows
    scale with the tile."""
    src = BAND.replace("t*16 : +16", f"t*{rows} : +{rows}").replace("[16,", f"[{rows},")
    m = module.read(src.replace("for t in 0..2", f"for t in 0..{32 // rows}"))
    loop = next(s for s in passes._body(m, "band")[1] if s.op == "par").body[2]
    assert passes._invariant(loop) == {"b", "b2"} and passes._stream_rows(loop) == rows
    got = emit([passes._scaled(loop, "t", rows, new, loop.positional()[0].rhs)], 0)
    assert re.search(r"b = add gv, 2\.0 +: f32\[4, 128\]", got)
    assert re.search(rf"v = unpack xl +: f32\[{new}, 128\]", got)
    out = passes.run(m, "band", ("retile",), T.machine(nodes=1))
    lower.lower(out, "band", T.machine(nodes=1))


SPLIT = """\
target ktpu.v9

fn split.l2(x: f16[8, 512] @dram, g: f16[512] @dram, h: f16[512] @dram, out: f16[8, 512] @dram)
    par c in 0..1 on vc[c]
        gl = load g[0 : +512] : f16[4, 512] @l1(gt4)
        gv = unpack gl : f32[4, 512] @reg(gt4)
        hl = load h[0 : +512] : f16[4, 512] @l1(gt4)
        hv = unpack hl : f32[4, 512] @reg(gt4)
        for t in 0..2
            xl = load x[t*4 : +4, 0 : +512] : f16[4, 512] @l1(gt4)
            v = unpack xl : f32[4, 512] @reg(gt4)
            a = mul v, gv : f32[4, 512] @reg(gt4)
            e = add a, hv : f32[4, 512] @reg(gt4)
            p = pack e : f16[4, 512] @l1(gt4)
            store out[t*4 : +4, 0 : +512] <- p
"""


WHOLE = (
    SPLIT.replace("g[0 : +512]", "g")
    .replace("x[t*4 : +4, 0 : +512]", "x[t*4 : +4]")
    .replace("out[t*4 : +4, 0 : +512]", "out[t*4 : +4]")
)


XLANE = SPLIT.replace(
    "a = mul v, gv", "u = xlane v, 1 : f32[4, 512] @reg(gt4)\n            a = mul u, gv"
)


@pytest.mark.parametrize(
    "src", [SPLIT, WHOLE, XLANE], ids=["runs", "whole_rows", "xlane"]
)
def test_retile_splits_columns_no_row_tile_holds(src):
    """Two 512-column bands held across the loop fill the registers at any
    row tile: the columns split into the fewest pieces that lower, a `par`
    a piece, which together cover every column of every view once (a view
    of whole rows given its run)."""
    m = module.read(src)
    mach = T.machine(nodes=1)
    with pytest.raises(TextError):
        lower.lower(m, "split", mach)
    got = passes.run(m, "split", ("retile",), mach)
    pars = [s for s in passes._body(got, "split")[1] if s.op == "par"]
    # Whole, it does not lower (above): halves are the fewest pieces.
    k = len(pars)
    assert k == 2
    w = 512 // k
    names: list = []
    runs: dict = {}
    for j, p in enumerate(pars):
        ops = passes._ops_of(p)
        names += [b.target for b in ops if b.target]
        for b in ops:
            if b.op in ("load", "store"):
                v = b.args[0]
                lo, hi = v.slices[-1].lo, v.slices[-1].hi
                runs.setdefault(v.name, []).append((lo.value, hi.x.value))
        assert all(
            b.annot.x.slices[-1] in (Int(w),)
            for b in ops
            if b.annot and b.op != "store"
        )
    assert len(names) == len(set(names))
    for buf in ("x", "g", "h", "out"):
        assert sorted(runs[buf]) == [(j * w, w) for j in range(k)]
    lower.lower(got, "split", mach)


def test_retile_refuses_when_no_tile_lowers_naming_why():
    # A max over 8192 columns of 32 rows (a 32-long statistic, like a band's
    # width): no tile holds it, no column split keeps it.
    text = (
        WIDE.replace("256", "8192")
        .replace("t*16 : +16", "t*32 : +32")
        .replace("[16, ", "[32, ")
        .replace("for t in 0..2", "for t in 0..1")
        .replace(
            "            e = add v, 1.0 : f32[32, 8192] @reg(gt4)\n"
            "            p = pack e : f16[32, 8192] @l1(gt4)\n"
            "            store out[c*32 + t*32 : +32, 0 : +8192] <- p\n",
            "            s = rmax v : f32[32] @reg\n"
            "            p = pack s : f16[32, 32] @l1(gt4)\n"
            "            store out[c*32 + t*32 : +32, 0 : +32] <- p\n",
        )
    )
    assert "rmax" in text
    with pytest.raises(TextError, match="no row tile of the stream lowers"):
        passes.run(module.read(text), "wide", ("retile",), T.machine(nodes=1))


def test_space_turns_an_any_tile_into_a_transfer():
    mach = T.machine(nodes=1)
    any_ = pipeline.compile_l1(
        examples.load(examples.example("anysilu", "l2")), "anysilu", mach
    )
    direct = pipeline.compile_l1(
        examples.load(examples.example("tilesilu", "l2")), "tilesilu", mach
    )
    # Written through memory with h @any, it compiles to the written transfer.
    assert any_.source.replace("anysilu", "tilesilu") == direct.source


def test_space_keeps_an_any_tile_too_big_to_land_in_memory():
    src = examples.example("mmsilu", "l2").read_text(encoding="utf-8")
    params, rest = src.split("    par u", 1)
    h = "h: f16[256, 256] @dram(tile=64x64), "
    text = (
        params.replace(h, "")
        + "    h = buffer : f16[256, 256] @any(tile=64x64)\n    par u"
        + rest
    )
    got = passes.run(module.read(text), "mmsilu", ("space",), T.machine())
    # A 256 x 256 tile is 4096 words; a core lands 256: through memory, the
    # staging store (128 KB fits it).
    assert ": f16[256, 256] @stage(tile=64x64)" in got.source
    assert "drain acc -> h" in got.source and "recv" not in got.source


def test_pipeline_lets_a_node_loop_of_two_unit_kinds_run_ahead():
    l2 = examples.load(examples.example("tilesilu", "l2"))
    bare = module.read(l2.source.replace(" pipe=2", ""))
    got = passes.run(bare, "tilesilu", ("pipeline",), T.machine())
    assert "for q in 0..16 pipe=2" in got.source
    # A loop of one unit kind gains nothing from it.
    mm = examples.load(examples.example("mmsilu", "l2"))
    assert "pipe" not in passes.run(mm, "mmsilu", ("pipeline",), T.machine()).source


def test_a_gemm_too_big_for_a_bank_is_refused_unless_split():
    text = OPEN.replace(" split", "").replace("mg[any]", "mg[u]")
    with pytest.raises(TextError, match="a bank holds 256"):
        lower.lower(module.read(text), "mm", T.machine(nodes=2))


@pytest.mark.parametrize(
    "line, said",
    [
        ("a = fill x[0 : +256, 32 : +64] : mx7a[256, 64] @A", "one packed chunk"),
        ("a = fill x[0 : +255, 0 : +64] : mx7a[255, 64] @A", "whole groups"),
        ("a = fill w[0 : +256, 0 : +64] : mx7b[256, 64] @A", "holds mx7a"),
    ],
)
def test_cluster_refusals(line, said):
    text = (
        "target ktpu.v9\n"
        "fn k.l2(x: mx7a[512, 512] @dram(gm=64, nk=2), w: mx7b[512, 512] @dram(gn=64, nk=2))\n"
        "    par u in 0..1 on mg[u]\n"
        f"        {line}\n"
    )
    with pytest.raises(TextError, match=said):
        lower.lower(module.read(text), "k")


@pytest.mark.parametrize("name", ["silu", "add", "softmax", "layernorm"])
def test_a_vector_kernel_lowers_to_a_loop_of_two_iterations(name, band):
    l2 = examples.load(examples.example(name, "l2"))
    text = lower.lower(l2, name)
    image = text.split("image ")[1]
    assert "loop s15" in image and image.count("vdrain") == 2
    assert image.count("vbar") >= 2
    packages(module.read(text), name)


def test_an_mxfp7_store_walks_the_a_packing(band):
    l2 = examples.load(examples.example("mlp", "l2"))
    text = lower.lower(l2, "mlp")
    # a2 is gm=64, nk=2: a band's 4 chunks of 2 entries (8 words), the chunks
    # 64 * 2 entries apart (16 KB), the next band 2 entries on (256 B).
    assert "desc a1 = p_a1 walk=((32, 8), (16384, 4))" in text
    assert "ainc a1 += 256" in text
    # Each descriptor's base is the run's argument: here a2 from its start.
    assert re.search(r"run mlp_n0_vc0_0\([^)]*\ba2\+0\b", text)
    # Row i, column half q (GT4 chunk 4q + i) to the quantiser's chunk 2i + q:
    # the op making the packed register writes one chunk at a time.
    moves = [
        ln.split()
        for ln in text.splitlines()
        if ln.strip().endswith("vl=16") and "[" in ln.split()[1]
    ]
    pairs = {
        (m[1].split("[")[1].rstrip(":],"), m[2].split("[")[1].rstrip(":],"))
        for m in moves[:8]
    }
    assert pairs == {
        (str(2 * i + q), str(4 * q + i)) for i in range(4) for q in range(2)
    }


def test_the_estimate_ranks_a_long_chain_band_wider():
    l2 = examples.load(examples.example("softmax", "l2"))
    body = B.read(l2, "softmax")
    par = body.stmts[0]
    reader = body.reader
    assert isinstance(reader, Reader)
    costs = {
        band: V._lower(body, par, {par.var: 0}, "k", reader.fail, band, False, True)[2]
        for band in (1, 2)
    }
    assert costs[2] < costs[1]


def test_the_quantiser_walks_the_destinations_entry_order():
    l2 = examples.load(examples.example("convert", "l2"))
    text = lower.lower(l2, "convert")
    # a: tiles of 8 groups (8192 B of x), chunks of 2 blocks (128 B), groups
    # (4 rows of 256 B), blocks (64 B), each entry 4 rows x 2 words.
    assert (
        "quant w(x + 0, (2, 8192), (2, 128), (8, 1024), (2, 64), (4, 256), (2, 32)) "
        "-> w(a + 0, (64, 128))" in text
    )
    assert (
        "quant w(x + 0, (16, 1024), (4, 64), (4, 256), (2, 32)) -> w(b + 0, (64, 128)) bpack"
        in text
    )
    # t: four rows of 16 columns to four consecutive sub-tiles of tile=8x8.
    assert (
        "gt4 w(x + 0, (16, 1024), (8, 32), (4, 256)) "
        "-> w(t + 0, (2, 8192), (8, 256), (4, 2048), (2, 128))" in text
    )


def test_a_large_conversion_is_cut_into_whole_moves():
    text = (
        "target ktpu.v9\n"
        "fn k.l2(x: f16[1024, 1024] @dram, a: mx7a[1024, 1024] @dram(gm=64, nk=2))\n"
        "    quant a <- x\n"
    )
    lines = [
        ln for ln in lower.lower(module.read(text), "k").splitlines() if "quant" in ln
    ]
    assert len(lines) == 4  # 65536 source words, a 64-group tile (16384) a move
    assert all("(16, 128)" in ln.split("->")[0] for ln in lines)


_WALK = re.compile(r"w\((\w+) \+ (-?\d+), ((?:\(\d+, -?\d+\)(?:, )?)+)\)")


def _addresses(walk: tuple) -> list:
    """Byte addresses a parsed walk visits, in its order."""
    name, off, levels = walk
    out = [off]
    for n, s in levels:
        out = [a + i * s for a in out for i in range(n)]
    return [(name, a) for a in out]


def _walks(line: str) -> list:
    return [
        (
            m[1],
            int(m[2]),
            [tuple(map(int, lv)) for lv in re.findall(r"\((\d+), (-?\d+)\)", m[3])],
        )
        for m in _WALK.finditer(line)
    ]


def _mover_lines(text: str, op: str) -> list:
    return [ln.strip() for ln in text.splitlines() if ln.strip().startswith(op + " w(")]


@pytest.mark.parametrize(
    "decl, stmt, want",
    [
        (
            "x: f16[2048, 1024] @dram, y: f16[2048, 1024] @dram",
            "copy y <- x",
            [("x", 0, [(131072, 32)]), ("y", 0, [(131072, 32)])],
        ),
        (
            "x: f16[1024, 512] @dram, y: f16[1024, 512] @dram",
            "copy y <- x",
            [("x", 0, [(32768, 32)]), ("y", 0, [(32768, 32)])],
        ),
        (
            "x: f16[1024, 2048] @dram, y: f16[1024, 1536] @dram",
            "copy y <- x[:, 0:1536]",
            [("x", 0, [(1024, 4096), (96, 32)]), ("y", 0, [(98304, 32)])],
        ),
        (
            "x: f16[1024, 1536] @dram, y: f16[1024, 2048] @dram",
            "copy y <- x walk=((2, 1572864), (49152, 32)) dwalk=((1024, 4096), (96, 32))",
            [("x", 0, [(98304, 32)]), ("y", 0, [(1024, 4096), (96, 32)])],
        ),
        ("y: f16[4096, 1024] @dram", "fill y value=0", [("y", 0, [(262144, 32)])]),
        (
            "y: f16[3, 70000, 16] @dram",
            "fill y value=7 walk=((3, 2240000), (70000, 32))",
            [("y", 0, [(3, 2240000), (70000, 32)])],
        ),
    ],
)
def test_a_large_move_is_cut_into_whole_moves_covering_the_walk(decl, stmt, want):
    text = lower.lower(
        module.read(f"target ktpu.v9\nfn k.l2({decl})\n    {stmt}\n"), "k"
    )
    lines = _mover_lines(text, stmt.split()[0])
    assert len(lines) > 1
    got = []
    for ln in lines:
        walks = _walks(ln)
        assert len(walks) == len(want)
        assert all(len(_addresses(w)) <= MOVE_WORDS for w in walks), ln
        assert all(c <= 0xFFFF for w in walks for c, _ in w[2]), ln
        got += list(zip(*(_addresses(w) for w in walks)))
    # Word for word, in each piece's own order: the uncut walk's pairs.
    assert sorted(got) == sorted(zip(*(_addresses(w) for w in want)))


def test_a_large_transform_is_cut_into_few_whole_moves():
    # A transpose's GT4 of 3,456 row groups (27,648 words): whole groups of
    # the outer level a move, not one move a group.
    text = (
        "target ktpu.v9\nfn k.l2(x: f16[13824, 32] @dram, t: f16[55296, 16] @dram)\n"
        "    gt4 t <- x walk=((3456, 256), (2, 32), (4, 64)) dwalk=((3456, 512), (2, 128))\n"
    )
    lines = _mover_lines(lower.lower(module.read(text), "k"), "gt4")
    assert len(lines) == 2
    got = []
    for ln in lines:
        s, d = _walks(ln)
        assert len(_addresses(s)) <= MOVE_WORDS
        src, dst = _addresses(s), _addresses(d)
        got += [(tuple(src[4 * k : 4 * k + 4]), dst[k]) for k in range(len(dst))]
    src = _addresses(("x", 0, [(3456, 256), (2, 32), (4, 64)]))
    dst = _addresses(("t", 0, [(3456, 512), (2, 128)]))
    # Entry for entry: four source words to their destination entry.
    assert got == [(tuple(src[4 * k : 4 * k + 4]), dst[k]) for k in range(len(dst))]


def _pieces_cover(stmt_decl: str, op: str, want: list) -> list:
    text = lower.lower(module.read(f"target ktpu.v9\nfn k.l2({stmt_decl}\n"), "k")
    lines = _mover_lines(text, op)
    got = []
    for ln in lines:
        walks = _walks(ln)
        assert all(len(_addresses(w)) <= MOVE_WORDS for w in walks), ln
        assert all(len(w[2]) <= NDIM for w in walks), ln
        got += list(zip(*(_addresses(w) for w in walks)))
    assert got == list(zip(*(_addresses(w) for w in want)))
    return lines


def test_a_large_move_whose_walks_do_not_nest_is_cut_step_for_step():
    walks = [("x", 0, [(3, 262144), (8192, 32)]), ("y", 0, [(2, 393216), (12288, 32)])]
    lines = _pieces_cover(
        "x: f16[24576, 16] @dram, y: f16[24576, 16] @dram)\n"
        "    copy y <- x walk=((3, 262144), (8192, 32)) "
        "dwalk=((2, 393216), (12288, 32))",
        "copy",
        walks,
    )
    assert len(lines) > 1


def test_a_copy_of_more_levels_than_a_walker_holds_is_cut():
    # A 7-axis permute: every level a different stride.
    src = [(2, 32), (3, 64), (2, 192), (3, 384), (2, 1152), (2, 2304), (2, 4608)]
    walks = [("x", 0, src), ("y", 0, [(288, 32)])]
    walk = ", ".join(f"({n}, {s})" for n, s in src)
    lines = _pieces_cover(
        "x: f16[288, 16] @dram, y: f16[288, 16] @dram)\n"
        f"    copy y <- x walk=({walk}) dwalk=((288, 32),)",
        "copy",
        walks,
    )
    assert len(lines) > 1


@pytest.mark.parametrize("rows, cols", [(5000, 64), (40, 1 << 18)])
def test_a_gather_past_one_moves_index_list_or_row_words_is_cut(rows, cols):
    text = lower.lower(
        module.read(
            "target ktpu.v9\n"
            f"fn k.l2(t: f16[64, {cols}] @dram, i: u32[{rows}] @dram, "
            f"y: f16[{rows}, {cols}] @dram)\n"
            "    gather y <- t index=i\n"
        ),
        "k",
    )
    lines = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("gather ")]
    row_words = cols // 16
    seen = []
    for ln in lines:
        m = re.search(
            r"gather t \+ (\d+) -> (w\(.*\)) index=i \+ (\d+) n=(\d+) pitch=(\d+) words=(\d+)",
            ln,
        )
        assert m, ln
        col, walk, idx, n, pitch, words = (
            int(m[1]),
            _walks(m[2])[0],
            int(m[3]),
            int(m[4]),
            int(m[5]),
            int(m[6]),
        )
        assert n <= MAX_INDEX and idx % 32 == 0 and words <= 0xFFFF
        assert pitch == cols * 2 and col % 32 == 0
        dst = _addresses(walk)
        for r in range(n):
            row = idx // 4 + r
            for c in range(words):
                seen.append((row, col // 32 + c, dst[r * words + c][1]))
    want = [
        (r, c, (r * row_words + c) * 32) for r in range(rows) for c in range(row_words)
    ]
    assert sorted(seen) == want


@pytest.mark.parametrize("seed", range(40))
def test_cut_pieces_take_every_step_in_order_within_the_limits(seed):
    rng = random.Random(seed)
    total = rng.choice(
        [2**a * 3**b * 5**c for a in range(9) for b in range(3) for c in range(3)]
    )

    def walk(total):
        counts = []
        while total > 1:
            f = rng.choice([d for d in range(2, total + 1) if total % d == 0][:6])
            counts.append(f)
            total //= f
        return [(n, 32 * rng.choice([1, 2, 3, -1, 7])) for n in counts] or [(1, 32)]

    walks = [walk(total), walk(total)]
    limit = rng.choice([1, 2, 7, 64, 1000, MOVE_WORDS])
    pieces = MV._cut(walks, limit)
    got = []
    for offs, lvs in pieces:
        assert MV._count(lvs[0]) <= max(limit, 1)
        assert all(len(MV._merge(MV._squeeze(lv))) <= NDIM for lv in lvs)
        got += list(zip(*(_addresses(("w", o, lv)) for o, lv in zip(offs, lvs))))
    assert got == list(zip(*(_addresses(("w", 0, lv)) for lv in walks)))


def test_a_window_beside_a_dense_walk_is_cut_at_its_levels():
    """im2col of a 3 x 3 window over [8, 34, 34] words into dense rows: the
    dense side splits at the window's levels, so each piece is one image
    (9,216 steps), not one tap's three words."""
    src = [(8, 36992), (32, 1088), (32, 32), (3, 1088), (3, 32)]
    dst = [(73728, 32)]
    pieces = MV._cut([src, dst])
    assert len(pieces) == 8
    got = []
    for offs, lvs in pieces:
        got += list(zip(*(_addresses(("w", o, lv)) for o, lv in zip(offs, lvs))))
    assert got == list(zip(*(_addresses(("w", 0, lv)) for lv in (src, dst))))


def test_three_walks_split_only_at_boundaries_that_nest():
    """Boundaries 2 and 3 inside one level of 6: one split (at 2), not both."""
    walks = [[(6, 32)], [(2, 96), (3, 32)], [(3, 64), (2, 32)]]
    got = []
    for offs, lvs in MV._cut(walks, 1):
        got += list(zip(*(_addresses(("w", o, lv)) for o, lv in zip(offs, lvs))))
    assert got == list(zip(*(_addresses(("w", 0, lv)) for lv in walks)))


def test_the_image_search_reads_each_candidate_text_once(monkeypatch):
    """Band shapes that make the same image are one parse and one timing."""
    searches: list = []
    active: list = []
    read, search = V.TM.read, V._search

    def counted(text, *a, **k):
        if active:
            active[-1].append(text)
        return read(text, *a, **k)

    def one(*a, **k):
        active.append([])
        try:
            return search(*a, **k)
        finally:
            searches.append(active.pop())

    monkeypatch.setattr(V.TM, "read", counted)
    monkeypatch.setattr(V, "_search", one)
    lower.lower(examples.load(examples.example("softmax", "l2")), "softmax")
    assert searches and all(len(t) == len(set(t)) for t in searches)
    assert any(len(t) > 1 for t in searches)


def test_a_small_move_stays_one_line():
    text = lower.lower(
        module.read(
            "target ktpu.v9\nfn k.l2(x: f16[64, 128] @dram, y: f16[64, 128] @dram)\n"
            "    copy y <- x\n"
        ),
        "k",
    )
    assert _mover_lines(text, "copy") == [
        "copy w(x + 0, (512, 32)) -> w(y + 0, (512, 32))"
    ]


def _stage_lines(text: str) -> list:
    return [ln.strip() for ln in text.split("fn ")[1].splitlines()[1:]]


def test_stages_wait_only_for_what_they_touch():
    text = (
        "target ktpu.v9\n"
        "fn k.l2(x: f16[64, 128] @dram, y: f16[64, 128] @dram, z: f16[64, 128] @dram)\n"
        "    copy y <- x\n"
        "    par c in 0..1 on vc[0]\n"
        "        for t in 0..16\n"
        "            xl = load x[t*4 : +4, :] : f16[4, 128] @l1\n"
        "            v = unpack xl : f32[4, 128] @reg\n"
        "            p = pack v : f16[4, 128] @l1\n"
        "            store z[t*4 : +4, :] <- p\n"
        "    copy x <- z\n"
    )
    lines = _stage_lines(lower.lower(module.read(text), "k"))
    run = next(i for i, ln in enumerate(lines) if ln.startswith("run "))
    # The core reads what the first copy only reads: no wait before it.
    assert not any(ln.startswith("wait") for ln in lines[:run])
    # The last copy overwrites what the core reads and reads what it wrote.
    last = next(i for i, ln in enumerate(lines) if ln == "move")
    assert "wait vc[0] upto=k1_vc0" in lines[run:last]
    assert "barrier" not in lines


def test_a_direct_transfer_is_counted_by_its_reader():
    # A drain retires once its flits left: the reader waits for them to land.
    l2 = examples.load(examples.example("mlpdirect", "l2"))
    text = lower.lower(l2, "mlpdirect")
    lines = _stage_lines(text)
    assert not any(ln.startswith("settle") for ln in lines)
    assert "drain n=256 to=vc[0] l1=256 count" in lines
    assert "vwait unpack rx n=256" in text
    sends = [ln for ln in text.splitlines() if "vdrain" in ln]
    assert all("to=mg[" in ln and "buf=0" in ln for ln in sends)
    assert "count=1" in text and any("recv n=" in ln for ln in lines)


def test_pipe_sends_the_next_tile_before_waiting():
    l2 = examples.load(examples.example("tilesilu", "l2"))
    lines = _stage_lines(lower.lower(l2, "tilesilu"))
    first_wait = next(i for i, ln in enumerate(lines) if ln.startswith("wait"))
    drains = [i for i, ln in enumerate(lines) if ln.startswith("drain n=128 to=vc[0]")]
    # Tiles 0 and 1 are both sent before the first wait, into the two slots.
    assert drains[0] < drains[1] < first_wait
    assert "l1=256" in lines[drains[0]] and "l1=384" in lines[drains[1]]


@pytest.mark.parametrize(
    "body, said",
    [
        (
            (
                "    par c in 0..1 on vc[0]\n"
                "        h = recv mg[0] : f16[4, 32] @l1(gt4)\n"
            ),
            "sent nothing",
        ),
        (
            (
                "    par u in 0..1 on mg[0]\n"
                "        a = fill x : mx7a[4, 64] @A\n"
                "        b = fill w : mx7b[4, 64] @B\n"
                "        c = gemm a, b : f32[4, 4] @acc\n"
                "        drain c -> vc[0]\n"
            ),
            "nothing receives",
        ),
    ],
)
def test_transfer_refusals(body, said):
    text = (
        "target ktpu.v9\n"
        "fn k.l2(x: mx7a[4, 64] @dram(gm=1, nk=2), w: mx7b[4, 64] @dram(gn=1, nk=2))\n"
        + body
    )
    with pytest.raises(TextError, match=said):
        lower.lower(module.read(text), "k")


def test_sticky_rewrites_its_values_in_place():
    l2 = examples.load(examples.example("lazysoftmax", "l2"))
    image = lower.lower(l2, "lazysoftmax").split("image ")[1]
    body = image.split("skipz s14\n")[1].split("\n")
    block = []
    for ln in body:
        if not ln.startswith(" " * 12):
            break
        block.append(ln.strip())
    first = [ln.split()[1].rstrip(",") for ln in image.splitlines() if "vexp2d" in ln][
        :8
    ]
    redo = [ln.split()[1].rstrip(",") for ln in block if ln.startswith("vexp2d")]
    # The skipped path keeps p: the block writes the same registers.
    assert redo == first
    assert "vsync fe on=math\n        getstk s14" in image


@pytest.mark.parametrize(
    "body, said",
    [
        (
            (
                "            if sticky\n"
                "                q = mul v, 2.0 : f32[8, 128] @reg\n"
                "            o = mul q, 1.0 : f32[8, 128] @reg\n"
            ),
            "only inside if sticky",
        ),
        (
            (
                "            if sticky\n"
                "                xl = load x[t*8 : +8, :] : f16[8, 128] @l1\n"
                "            o = mul v, 1.0 : f32[8, 128] @reg\n"
            ),
            "math and reductions",
        ),
    ],
)
def test_sticky_refusals(body, said):
    text = (
        "target ktpu.v9\n"
        "fn k.l2(x: f16[64, 128] @dram, y: f16[64, 128] @dram)\n"
        "    par c in 0..1 on vc[0]\n"
        "        for t in 0..8\n"
        "            xl = load x[t*8 : +8, :] : f16[8, 128] @l1\n"
        "            v = unpack xl : f32[8, 128] @reg\n"
        + body
        + "            ol = pack o : f16[8, 128] @l1\n"
        "            store y[t*8 : +8, :] <- ol\n"
    )
    with pytest.raises(TextError, match=said):
        lower.lower(module.read(text), "k")


def test_a_gt4_band_reduces_to_one_register_every_chunk_holds():
    l2 = examples.load(examples.example("mmsoftmax", "l2"))
    image = lower.lower(l2, "mmsoftmax").split("image ")[1]
    lines = [ln.strip() for ln in image.splitlines()]
    at = lines.index("vmax v4[0:], v4[0:], v4[4:] vl=64")
    # Registers folded, chunk 4 + i onto chunk i, the xor butterfly, then
    # chunks 0-3 copied to 4-7: chunk 4q + i holds row i's max.
    assert lines[at - 3 : at] == [
        "vmax v4, v0, v1",
        "vmax v5, v2, v3",
        "vmax v4, v4, v5",
    ]
    assert [ln.split("xor(v4, ")[1][0] for ln in lines[at + 1 : at + 5]] == list("8421")
    assert lines[at + 5] == "vmov v4[4:], v4[0:] vl=64"
    assert "vexp2d v0, v0, v4" in lines  # the band's statistic, elementwise


def test_a_compare_is_a_predicate_and_two_moves(band):
    l2 = examples.load(examples.example("leaky", "l2"))
    image = lower.lower(l2, "leaky").split("image ")[1]
    assert "vcmpgt p1, v0, k0" in image and "k1 if=p1" in image
    assert image.count("vsel ") == 16  # 8 rows a trip, two trips a VLOOP pass


@pytest.mark.parametrize(
    "cmp, a, arms, want",
    [
        ("cmpgt v, 0.0", "0.1", "m, v, s", "max v, s"),  # leaky relu
        ("cmpgt v, 0.0", "0.1", "m, s, v", "min v, s"),
        ("cmplt v, 0.0", "0.1", "m, s, v", "max v, s"),
        ("cmpgt 0.0, v", "0.1", "m, s, v", "max v, s"),
        ("cmpgt v, 0.0", "4.0", "m, v, s", "min v, s"),
        ("cmpgt v, 0.0", "-0.5", "m, v, s", None),  # a sign flip: no order
        ("cmpgt v, 1.0", "0.1", "m, v, s", None),  # not a sign test
    ],
)
def test_select_turns_a_sign_select_of_a_scale_into_max_or_min(cmp, a, arms, want):
    src = examples.example("leaky", "l2").read_text(encoding="utf-8")
    src = src.replace("cmpgt v, 0.0", cmp).replace("mul v, 0.1", f"mul v, {a}")
    src = src.replace("sel m, v, s", f"sel {arms}")
    got = passes.run(module.read(src), "leaky", ("select",)).source
    if want is None:
        assert f"sel {arms}" in got
    else:
        assert f"o = {want}" in got and "cmp" not in got and "sel" not in got


def test_modulo_pipelines_the_stream_loop_through_rings(monkeypatch):
    monkeypatch.setattr(V, "STRATEGY", "modulo")
    l2 = examples.load(examples.example("softmax", "l2"))
    mach = T.machine(vregs=128)
    text = lower.lower(replace_vregs(l2, 128), "softmax", mach)
    image = text.split("image ")[1].split("\n    send")[0]
    assert "loop s15" in image
    # every slot of a ring is reused behind a sync on the iteration K back
    assert "vsync fill on=unpack" in image and "vsync pack on=drain" in image
    assert "vsync unpack on=fill" in image and "vsync drain on=pack" in image
    packages(module.read(text), "softmax")


def _vector_stages_beside_clusters(text: str, name: str) -> set:
    body = B.read(module.read(text), name)
    return {
        lower._beside_clusters(body.stmts, i.par)
        for kind, _, x, _ in B.instances(body)
        if kind == "par"
        for i in x
        if i.kind == "vc"
    }


def test_a_vector_stage_in_a_loop_with_clusters_takes_the_shared_latency():
    src = examples.example("mmsilu", "l2").read_text(encoding="utf-8")
    head, _, rest = src.partition("    par u")
    looped = (
        head
        + "    for i in 0..2\n"
        + "".join("    " + ln + "\n" for ln in ("    par u" + rest).splitlines())
    )
    assert _vector_stages_beside_clusters(src, "mmsilu") == {False}
    assert _vector_stages_beside_clusters(looped, "mmsilu") == {True}
    assert T.mem(T.machine()) == T.MEM["alone"]
    assert T.mem(T.machine(), shared=True) == T.MEM["shared"]
    mach = T.machine(mem={"alone": (5, 1), "shared": (7, 3)})
    assert T.mem(mach, shared=True) == (7, 3)


def test_a_fill_lands_the_memory_latency_after_its_last_word():
    text = (
        "target ktpu.v9\n"
        "image k()\n"
        "    desc a0 = 0 walk=(32, 64)\n"
        "    vfill a0 -> l1[0]\n"
        "    vsync unpack on=fill\n"
        "    vunpk v0 <- l1[0] f16\n"
        "    halt\n"
    )
    near, far = (TI.cycles(text, "k", 32, 4, (lat, 1)) for lat in (16, 400))
    assert far - near == 384
    # each word of the 64 holds the memory port its cycles
    slow = TI.cycles(text, "k", 32, 4, (16, 5))
    assert slow - near == 4 * 64
    assert MO.lead(64, (330, 5)) == 330 + 5 * 64


def test_modulo_rotates_the_predicate_over_its_copies(monkeypatch):
    monkeypatch.setattr(V, "STRATEGY", "modulo")
    l2 = examples.load(examples.example("leaky", "l2"))
    image = lower.lower(replace_vregs(l2, 128), "leaky", T.machine(vregs=128))
    # K >= 2 copies of an iteration in flight: at least p1 and p2
    assert "vcmpgt p1," in image and "vcmpgt p2," in image
    assert "if=p1" in image and "if=p2" in image


def _place_by_scan(ops: list, ii: int) -> bool:
    """`MO._place` with its first fit found slot by slot: the reference."""
    n = len(ops)
    if any(o.occ > ii for o in ops):
        return False
    pred = [
        [(p, lat + (ops[p].occ if end else 0)) for p, lat, end in o.deps] for o in ops
    ]
    succ: list = [[] for _ in ops]
    for i in range(n):
        for p, d in pred[i]:
            succ[p].append((i, d))
    height = [0] * n
    for i in reversed(range(n)):
        height[i] = max((d + height[s] for s, d in succ[i]), default=ops[i].occ)
    owner: dict[str, list] = {e: [None] * ii for e in "MUP"}
    at: list = [None] * n
    last: list = [None] * n
    queue = [(-height[i], i) for i in range(n)]
    heapq.heapify(queue)
    budget = (MO.PLACE_BUDGET + 1) * n

    def evict(j):
        row = owner[ops[j].eng]
        for b in range(ops[j].occ):
            row[(at[j] + b) % ii] = None
        at[j] = None
        heapq.heappush(queue, (-height[j], j))

    while queue:
        _, i = heapq.heappop(queue)
        if at[i] is not None:
            continue
        budget -= 1
        if budget < 0:
            return False
        o, row = ops[i], owner[ops[i].eng]
        early = max((at[p] + d for p, d in pred[i] if at[p] is not None), default=0)
        lo = early if last[i] is None or last[i] < early else last[i] + 1
        for t in range(lo, lo + ii):
            if all(row[(t + b) % ii] is None for b in range(o.occ)):
                break
        else:
            t = lo
            for j in {row[(t + b) % ii] for b in range(o.occ)} - {None}:
                evict(j)
        for b in range(o.occ):
            row[(t + b) % ii] = i
        at[i] = last[i] = t
        for s, d in succ[i]:
            if at[s] is not None and at[s] < t + d:
                evict(s)
    for i, o in enumerate(ops):
        o.t, o.e = at[i], at[i] + o.occ
    return True


@pytest.mark.parametrize("seed", range(60))
def test_modulo_placement_takes_the_first_free_run(seed):
    """The bit-mask first fit places every op where a slot-by-slot scan
    does, over random dependence graphs, engines and IIs."""
    rng = random.Random(seed)
    n = rng.randint(1, 40)
    ops = []
    for i in range(n):
        eng = rng.choice("MMMUP")
        reads = tuple(f"%{rng.randrange(i)}" for _ in range(rng.randint(0, 2)) if i)
        ops.append(MO.MOp("", (), eng, reads, (f"%{i}",), rng.randint(1, 12)))
    lower = max(sum(o.occ for o in ops if o.eng == e) for e in "MUP")
    for ii in (lower, lower + rng.randint(0, 30), rng.randint(1, max(lower, 2))):
        a = [replace(o, deps=[]) for o in ops]
        b = [replace(o, deps=[]) for o in ops]
        MO._link(a)
        MO._link(b)
        assert MO._place(a, ii) == _place_by_scan(b, ii)
        assert [(o.t, o.e) for o in a] == [(o.t, o.e) for o in b]


def _colour_by_overlap(life: dict, ii: int, k: int, regs: int, first: int):
    """`MO._colour` checking each register's every arc: the reference."""
    period = ii * k
    arcs = []
    for x, (s, e) in life.items():
        if e - s > period:
            return None
        for j in range(k):
            a = (s + j * ii) % period
            arcs.append((a, a + e - s, x, j))
    arcs.sort()
    busy: dict = {}
    out = {}

    def overlap(a, b) -> bool:
        return any(a[0] < b[1] + sh and b[0] + sh < a[1] for sh in (-period, 0, period))

    for s, e, x, j in arcs:
        for r in range(first, regs):
            if all(not overlap((s, e), iv) for iv in busy.get(r, [])):
                busy.setdefault(r, []).append((s, e))
                out[(x, j)] = r
                break
        else:
            return None
    return out


@pytest.mark.parametrize("seed", range(80))
def test_modulo_colouring_matches_the_arc_by_arc_check(seed):
    rng = random.Random(seed)
    ii, k = rng.randint(1, 40), rng.randint(1, 6)
    life = {}
    for i in range(rng.randint(1, 30)):
        s = rng.randint(0, 3 * ii)
        life[f"%{i}"] = (s, s + rng.randint(0, ii * k + 2))
    regs, first = rng.randint(1, 24), rng.randint(0, 2)
    assert MO._colour(life, ii, k, regs, first) == _colour_by_overlap(
        life, ii, k, regs, first
    )


def test_modulo_stops_once_ii_spans_an_iteration(monkeypatch):
    """Forty values all live to one late op need forty registers at any II:
    the search ends at the II one iteration spans, not at `ii_max`."""
    ops = [MO.MOp("", (), "M", (), (f"%{i}",), 1) for i in range(40)]
    ops.append(MO.MOp("", (), "M", tuple(f"%{i}" for i in range(40)), ("%z",), 1))
    tried = []
    real = MO._place
    monkeypatch.setattr(MO, "_place", lambda o, ii: tried.append(ii) or real(o, ii))
    assert MO.plan(ops, 16, {}, 8, 100_000, 0) is None
    assert tried and max(tried) < 200


PLAIN = """\
target ktpu.v9

fn chain.l2(x: f16[64, 128] @dram, out: f16[64, 128] @dram)
    par c in 0..2 on vc[c]
        for t in 0..2
            xl = load x[c*32 + t*16 : +16, 0 : +128] : f16[16, 128] @l1
            v = unpack xl : f32[16, 128] @reg
{chain}            p = pack e{n} : f16[16, 128] @l1
            store out[c*32 + t*16 : +16, 0 : +128] <- p
"""


def _chain(n: int):
    chain = "            e0 = add v, 1.0 : f32[16, 128] @reg\n"
    chain += "".join(
        f"            e{i} = mul e{i - 1}, 1.5 : f32[16, 128] @reg\n"
        for i in range(1, n + 1)
    )
    return module.read(PLAIN.format(chain=chain, n=n))


@pytest.mark.parametrize("n, most", [(40, 8), (160, 2)])
def test_an_image_past_imem_is_refused_and_retiled_to_one_that_fits(n, most):
    m = _chain(n)
    mach = T.machine(nodes=1)
    # 16 plain rows of n + 1 ops: past VLOOP's body or IMEM.
    with pytest.raises(TextError, match="no image of the stream loop encodes"):
        lower.lower(m, "chain", mach)
    got = passes.run(m, "chain", ("retile",), mach)
    rows = int(_found(r"t \* (\d+) : \+\1", got.source)[1])
    # Plain rows are a register each: a tile of fewer than a GT4 band's 4.
    assert rows <= most
    lower.lower(got, "chain", mach)


CONSTS = """\
target ktpu.v9

fn chain.l2(x: f16[{rows}, 128] @dram, out: f16[{rows}, 128] @dram)
    par c in 0..1 on vc[c]
        for t in 0..{trips}
            xl = load x[t*8 : +8, 0 : +128] : f16[8, 128] @l1
            v = unpack xl : f32[8, 128] @reg
{chain}            p = pack e{n} : f16[8, 128] @l1
            store out[t*8 : +8, 0 : +128] <- p
"""


def _consts(n: int) -> tuple:
    """A chain reading n + 1 distinct numbers: its source and each row's value."""
    want: tuple = ("x",)
    chain = "            e0 = add v, 0.5 : f32[8, 128] @reg\n"
    want = ("vadd", want, 0.5)
    for i in range(1, n + 1):
        c = 1 + i / 64
        chain += f"            e{i} = mul e{i - 1}, {c} : f32[8, 128] @reg\n"
        want = ("vmul", want, c)
    return chain, want


def _packed(
    image: str, sticky: float = 0.0, drains: list | None = None, addrs: bool = False
) -> list:
    """Each `vpack`'s value, its rows' numbers read through the registers the
    image sets (scalars, `seti k3`, pinned `vmov`s): `loop sN` bodies run
    sN times, `skipz sN` blocks once unless sN is 0 (`getstk` reads `sticky`).
    With `drains`, each unpack is ("x", its count) and each 8-row drain is
    appended as (descriptor, the values its L1 words hold); with `addrs`,
    each unpack is ("x", the L1 word it reads)."""
    lines = [(len(s) - len(s.lstrip()), s.strip()) for s in image.splitlines()]
    regs: dict = {"k0": 0.0, "k1": 1.0, "k2": -1.0}
    out: list = []
    l1: dict = {}
    unpacks = [0]

    def val(a):
        return regs[a]

    def run(body):
        i = 0
        while i < len(body):
            ind, s = body[i]
            i += 1
            op, _, rest = s.partition(" ")
            args = [a.strip() for a in rest.split(",")]
            if op in ("loop", "skipz"):
                j = i
                while j < len(body) and body[j][0] > ind:
                    j += 1
                times = int(regs[args[0]]) if op == "loop" else int(regs[args[0]] != 0)
                for _ in range(times):
                    run(body[i:j])
                i = j
            elif op == "getstk":
                regs[args[0]] = sticky
            elif op == "vfma":
                regs[args[0]] = (op, *(val(a) for a in args[1:]))
            elif op == "seti":
                reg, v = (p.strip() for p in rest.split("="))
                regs[reg] = float(v)
            elif op == "vunpk":
                unpacks[0] += 1
                word = int(_found(r"l1\[(\d+)\]", s)[1])
                tag = word if addrs else unpacks[0] if drains is not None else None
                regs[args[0].split()[0]] = ("x",) if tag is None else ("x", tag)
            elif op == "vpack":
                out.append(val(args[0].split()[0]))
                l1[int(_found(r"l1\[(\d+)\]", s)[1])] = out[-1]
            elif op == "vdrain" and drains is not None:
                at = int(_found(r"l1\[(\d+)\]", s)[1])
                drains.append(
                    (args[0].split()[0], [l1.get(at + 8 * r) for r in range(8)])
                )
            elif op == "vmov":
                regs[args[0]] = val(args[1])
            elif op in ("vadd", "vmul"):
                regs[args[0]] = (op, val(args[1]), val(args[2]))

    run([x for x in lines if x[0] >= 4])
    return out


PIN = """\
target ktpu.v9

fn pin.l2(x: f16[32, 128] @dram, g: f16[1, 128] @dram, out: f16[32, 128] @dram)
    par c in 0..1 on vc[c]
        gl = load g[0 : +1, 0 : +128] : f16[1, 128] @l1
        gv = unpack gl : f32[1, 128] @reg
        for t in 0..4
            xl = load x[t*8 : +8, 0 : +128] : f16[8, 128] @l1
            v = unpack xl : f32[8, 128] @reg
            b = add gv, 2.0 : f32[1, 128] @reg
            e = mul v, b : f32[8, 128] @reg
            p = pack e : f16[8, 128] @l1
            store out[t*8 : +8, 0 : +128] <- p
"""


@pytest.mark.parametrize("pin", [True, False])
def test_a_row_unpacked_before_the_loop_is_pinned_or_unpacked_where_read(
    monkeypatch, pin
):
    """Pinned: unpacked once before the loop. Not pinned: unpacked in the
    loop from its L1 bytes, which the loop's buffers leave alone. Either
    way every packed row is x times (g + 2) of that one row of g."""
    real = V._lower
    monkeypatch.setattr(V, "_lower", lambda *a: real(*a[:11], pin=pin))
    monkeypatch.setattr(V, "STRATEGY", "band")
    text = lower.lower(module.read(PIN), "pin", T.machine(nodes=1))
    image = text[text.index("image ") :]
    g = int(_found(r"vfill a\d+ -> l1\[(\d+)\]\n", image)[1])
    head, loop = image.split("loop s15")
    reads = rf"vunpk \S+ <- l1\[{g}\]"
    assert len(re.findall(reads, head)) == pin and bool(re.findall(reads, loop)) != pin
    # The loop's fills (8 rows, 64 words) and packs (8 words a row) miss g's
    # 8 words when they are read in the loop.
    fills = [int(w) for w in re.findall(r"vfill a\d+ -> l1\[(\d+)\] rel", loop)]
    packs = [int(w) for w in re.findall(r"vpack \S+ -> l1\[(\d+)\]", loop)]
    spans = [(w, w + 64) for w in fills] + [(w, w + 8) for w in packs]
    assert pin or all(b <= g or g + 8 <= a for a, b in spans)
    got = _packed(image, addrs=True)
    assert len(got) == 32
    for p in got:
        assert p[0] == "vmul" and p[2] == ("vadd", ("x", g), 2.0)
        assert p[1][0] == "x" and p[1][1] != g


BLOCKS = """\
target ktpu.v9

fn blk.l2(x: f16[16, 512] @dram, out: f16[16, 512] @dram)
{pars}"""
BLOCK = """\
    par c{j} in 0..2 on vc[c{j}]
        for t{j} in 0..2
            xl{j} = load x[c{j}*8 + t{j}*4 : +4, {at} : +{w}] : f16[4, {w}] @l1(gt4)
            v{j} = unpack xl{j} : f32[4, {w}] @reg(gt4)
            e{j} = mul v{j}, {k} : f32[4, {w}] @reg(gt4)
            p{j} = pack e{j} : f16[4, {w}] @l1(gt4)
            store out[c{j}*8 + t{j}*4 : +4, {at} : +{w}] <- p{j}
"""


def test_a_shape_seen_before_is_not_searched_again(monkeypatch):
    """Column blocks of one shape at other offsets (and a block of another
    width, another constant): one search a shape, and the L1 every block
    lowers to is the L1 a fresh search gives it."""
    blocks = [(0, 128, 2.0), (128, 128, 2.0), (256, 128, 2.0), (384, 64, 2.0)]
    blocks.append((448, 64, 3.0))
    pars = "".join(
        BLOCK.format(j=j, at=at, w=w, k=k) for j, (at, w, k) in enumerate(blocks)
    )
    m = module.read(BLOCKS.format(pars=pars))
    mach = T.machine(nodes=1)
    searched = []
    real = V._search
    monkeypatch.setattr(V, "_search", lambda *a: searched.append(a[3]) or real(*a))
    V._chosen.clear()
    remembered = lower.lower(m, "blk", mach)
    # Three shapes: 128 wide by 2, 64 wide by 2, 64 wide by 3 (two cores each).
    assert len(searched) == 3
    # Again, every block remembered (no search), each image its own name.
    assert lower.lower(m, "blk", mach) == remembered and len(searched) == 3
    monkeypatch.setattr(V, "_best", V._search)
    assert lower.lower(m, "blk", mach) == remembered


PACKED = """\
target ktpu.v9

fn pk.l2(x: f16[32, 128] @dram, g: f16[8, 128] @dram, out: f16[32, 128] @dram, o2: f16[32, 128] @dram)
    par c in 0..1 on vc[c]
        gl = load g[0 : +8, 0 : +128] : f16[8, 128] @l1
        gv = unpack gl : f32[8, 128] @reg
        for t in 0..4
            xl = load x[t*8 : +8, 0 : +128] : f16[8, 128] @l1
            v = unpack xl : f32[8, 128] @reg
            e = add v, 1.0 : f32[8, 128] @reg
            p = pack e : f16[8, 128] @l1
            store out[t*8 : +8, 0 : +128] <- p
            q = pack gv : f16[8, 128] @l1
            store o2[t*8 : +8, 0 : +128] <- q
"""


@pytest.mark.parametrize("pin", [True, False])
def test_a_row_unpacked_before_the_loop_packs_in_it(monkeypatch, pin):
    """A row made before the loop and packed in it, no math between: row r
    of every trip's pack is g's row r, pinned or unpacked again."""
    real = V._lower
    monkeypatch.setattr(V, "_lower", lambda *a: real(*a[:11], pin=pin))
    monkeypatch.setattr(V, "STRATEGY", "band")
    text = lower.lower(module.read(PACKED), "pk", T.machine(nodes=1))
    image = text[text.index("image ") :]
    g = int(_found(r"vfill a\d+ -> l1\[(\d+)\]\n", image)[1])
    got = [p for p in _packed(image, addrs=True) if p[0] == "x"]
    assert got == [("x", g + 8 * r) for r in range(8)] * 4


FOLDED = """\
target ktpu.v9

fn fd.l2(x: f16[16, 128] @dram, g: f16[128] @dram, out: f16[16, 32] @dram)
    par c in 0..1 on vc[c]
        gl = load g[0 : +128] : f16[4, 128] @l1(gt4)
        gv = unpack gl : f32[4, 128] @reg(gt4)
        for t in 0..4
            xl = load x[t*4 : +4, 0 : +128] : f16[4, 128] @l1(gt4)
            v = unpack xl : f32[4, 128] @reg(gt4)
            e = mul v, gv : f32[4, 128] @reg(gt4)
            s = rsum e : f32[4] @reg
            p = pack s : f16[4, 32] @l1(gt4)
            store out[t*4 : +4, 0 : +32] <- p
"""


def test_a_row_not_pinned_reaches_a_reduction_through_its_product(monkeypatch):
    """`e = mul v, gv` read only by `rsum` is folded into it (never formed):
    gv, not pinned, is unpacked in the loop for the reduction itself."""
    real = V._lower
    monkeypatch.setattr(V, "_lower", lambda *a: real(*a[:11], pin=False))
    monkeypatch.setattr(V, "STRATEGY", "band")
    text = lower.lower(module.read(FOLDED), "fd", T.machine(nodes=1))
    image = text[text.index("image ") :]
    g = int(_found(r"vfill a\d+ -> l1\[(\d+)\]\n", image)[1])
    head, loop = image.split("loop s15")
    assert not re.search(rf"vunpk \S+ <- l1\[{g}", head)
    assert re.search(rf"vunpk \S+ <- l1\[{g}", loop)


KEEP = """\
target ktpu.v9

fn keep.l2(x: f16[64, 128] @dram, g: f16[8, 128] @dram, out: f16[64, 128] @dram)
    par c in 0..1 on vc[c]
        gl = load g[0 : +8, 0 : +128] : f16[8, 128] @l1
        gv = unpack gl : f32[8, 128] @reg
        for t in 0..4
            xl = load x[t*16 : +16, 0 : +128] : f16[16, 128] @l1
            v = unpack xl : f32[16, 128] @reg
            e = mul v, 2.0 : f32[16, 128] @reg
            p = pack e : f16[16, 128] @l1
            store out[t*16 : +16, 0 : +128] <- p
"""


@pytest.mark.parametrize("pin", [True, False])
def test_a_row_not_pinned_keeps_its_bytes_or_is_no_candidate(monkeypatch, pin):
    """16-row trips fill L1 (two loads, two stores of 128 words): pinned, g's
    64 words lie under them once unpacked; not pinned they would have to
    stay -- no image."""
    real = V._lower
    monkeypatch.setattr(V, "_lower", lambda *a: real(*a[:11], pin=pin))
    monkeypatch.setattr(V, "STRATEGY", "band")
    m = module.read(KEEP)
    if pin:
        lower.lower(m, "keep", T.machine(nodes=1))
        return
    with pytest.raises(TextError, match="vector registers"):
        lower.lower(m, "keep", T.machine(nodes=1))


TWIN = """\
target ktpu.v9

fn bc.l2(x: f16[{rows}, 128] @dram, out: f16[{rows2}, 128] @dram)
    par c in 0..1 on vc[c]
        for t in 0..{trips}
            xl = load x[t*8 : +8, 0 : +128] : f16[8, 128] @l1
            v = unpack xl : f32[8, 128] @reg
            e = mul v, 2.0 : f32[8, 128] @reg
            p = pack e : f16[8, 128] @l1
            store out[t*8 : +8, 0 : +128] <- p
            store out[{rows} + t*8 : +8, 0 : +128] <- p
"""


@pytest.mark.parametrize("trips", [2, 3, 4])
def test_a_value_stored_through_two_views_is_packed_once_for_both(trips):
    """Both views drain, every trip, the 8 rows that trip unpacked and packed
    (one pack into one pair of buffers)."""
    rows = 8 * trips
    src = TWIN.format(rows=rows, rows2=2 * rows, trips=trips)
    text = lower.lower(module.read(src), "bc", T.machine(nodes=1))
    image = text[text.index("image ") :]
    drains: list = []
    packs = _packed(image, drains=drains)
    assert len(packs) == rows
    assert [d for d, _ in drains] == ["a1", "a2"] * trips
    for k, (_, got) in enumerate(drains):
        trip = k // 2
        want = [("vmul", ("x", 8 * trip + r + 1), 2.0) for r in range(8)]
        assert got == want, (k, got)


LAYOUTS = """\
target ktpu.v9

fn gt.l2(x: f16[16, 32] @dram, o1: f16[16, 32] @dram, o2: f16[16, 32] @dram(tile=4x32))
    par c in 0..1 on vc[c]
        for t in 0..4
            xl = load x[t*4 : +4, 0 : +32] : f16[4, 32] @l1(gt4)
            v = unpack xl : f32[4, 32] @reg(gt4)
            e = mul v, 2.0 : f32[4, 32] @reg(gt4)
            p = pack e : f16[4, 32] @l1(gt4)
            store o1[t*4 : +4, 0 : +32] <- p
            store o2[t*4 : +4, 0 : +32] <- p
"""


def test_a_value_stored_in_two_l1_layouts_is_packed_into_each():
    """Row-major rows and a tile= buffer lay a band out apart: one pack into
    each view's buffers, each drained the band its trip made."""
    text = lower.lower(module.read(LAYOUTS), "gt", T.machine(nodes=1))
    image = text[text.index("image ") :]
    drains: list = []
    packs = _packed(image, drains=drains)
    assert len(packs) == 2 * 4
    assert sorted({d for d, _ in drains}) == ["a1", "a2"] and len(drains) == 8
    seen: dict = {}
    for d, got in drains:
        trip = seen[d] = seen.get(d, -1) + 1
        assert got[0] == ("vmul", ("x", trip + 1), 2.0), (d, trip, got[0])


STICKY = """\
target ktpu.v9

fn chain.l2(x: f16[16, 128] @dram, out: f16[16, 128] @dram)
    par c in 0..1 on vc[c]
        for t in 0..2
            xl = load x[t*8 : +8, 0 : +128] : f16[8, 128] @l1
            v = unpack xl : f32[8, 128] @reg
{chain}            h = fma g16, 3.25, 3.5 : f32[8, 128] @reg
            if sticky
                h = mul h, 3.75 : f32[8, 128] @reg
            k = mul h, 3.75 : f32[8, 128] @reg
            p = pack k : f16[8, 128] @l1
            store out[t*8 : +8, 0 : +128] <- p
"""


def test_a_staged_constant_is_set_again_past_a_skipped_block(monkeypatch):
    """An op reading two staged numbers takes both staging registers; a number
    staged in an `if sticky` block is set again after it (it may not run)."""
    chain, want = _consts(16)
    g = "".join(
        f"            g{i} = mul {'e16' if i == 1 else f'g{i - 1}'}, {1 + i / 64}"
        " : f32[8, 128] @reg\n"
        for i in range(1, 17)
    )
    for i in range(1, 17):
        want = ("vmul", want, 1 + i / 64)
    h = ("vfma", want, 3.25, 3.5)
    real = V._lower
    monkeypatch.setattr(V, "_lower", lambda *a: real(*a[:10], hold=False))
    text = lower.lower(
        module.read(STICKY.format(chain=chain + g)), "chain", T.machine(nodes=1)
    )
    image = text[text.index("image ") :]
    assert "skipz" in image and re.search(r"vfma \S+, \S+, (s13, k3|k3, s13)", image)
    for sticky, k in (
        (0.0, ("vmul", h, 3.75)),
        (1.0, ("vmul", ("vmul", h, 3.75), 3.75)),
    ):
        got = _packed(image, sticky)
        assert len(got) == 16 and all(x == k for x in got), sticky


@pytest.mark.parametrize("hold", [True, False])
@pytest.mark.parametrize("n, trips", [(18, 2), (18, 3), (40, 2)])
def test_constants_past_the_scalar_file_are_held_or_staged(monkeypatch, hold, n, trips):
    """Past the 14 scalar registers a number is held in a pinned register or
    set into a staging one before its reads: every packed row is the chain's
    value, every trip (an odd count runs a trip past the loop)."""
    chain, want = _consts(n)
    src = CONSTS.format(chain=chain, n=n, trips=trips, rows=8 * trips)
    real = V._lower
    monkeypatch.setattr(V, "_lower", lambda *a: real(*a[:10], hold=hold))
    mach = T.machine(nodes=1)
    text = lower.lower(module.read(src), "chain", mach)
    image = text[text.index("image ") :]
    got = _packed(image)
    assert len(got) == 8 * trips
    assert all(g == want for g in got)
    # Staged: set in the loop; held: set once, before it.
    looped = image[image.index("loop s15") :]
    assert bool(re.findall(r"seti (s13|k3) = ", looped)) == (not hold)
    assert ("vmov" in image) == hold


def test_a_view_off_a_word_is_refused():
    # 8 f16 columns in: the descriptor would start 16 bytes into a word.
    bad = FAR.replace("70000", "256").replace(
        "load x[c*32 + t*4 : +4, 0 : +128]", "load x[c*32 + t*4 : +4, 8 : +128]"
    )
    with pytest.raises(TextError, match="a descriptor starts on a 32-byte word"):
        lower.lower(module.read(bad), "far", T.machine(nodes=1))
    # A view stepping 8 columns a trip: 16 bytes, no whole word.
    steps = FAR.replace("70000", "256").replace(
        "load x[c*32 + t*4 : +4, 0 : +128]", "load x[c*32 : +4, t*8 : +128]"
    )
    with pytest.raises(TextError, match="a descriptor steps whole words"):
        lower.lower(module.read(steps), "far", T.machine(nodes=1))


def test_a_store_of_a_value_not_the_views_size_is_refused():
    bad = (
        FAR.replace(
            "store out[c*32 + t*4 : +4, 0 : +128] <- p",
            "store out[c*32 + t*4 : +4, 0 : +128] <- q",
        )
        .replace(
            "            p = pack e : f16[4, 128] @l1(gt4)\n",
            "            p = pack e : f16[4, 128] @l1(gt4)\n"
            "            q = load x[0 : +4, 0 : +32] : f16[4, 32] @l1(gt4)\n",
        )
        .replace("70000", "128")
    )
    with pytest.raises(TextError, match="the view holds 32 words, the value 8"):
        lower.lower(module.read(bad), "far", T.machine(nodes=1))


def test_modulo_parse_reads_an_ops_own_destination():
    o = MO.parse("vadd {0}, {0}, merge({0}[4 :], 4) vl=64", ("%1",), 4)
    assert o.reads == ("%1",) and o.writes == ("%1",) and o.occ == 4
    assert o.xreads == ("%1",)  # through the crossbar: LAT_XB later
    # a chunk-selected result writes its words in place: no read of the rest
    o = MO.parse("vadd {0}[4 :], {1}[0 :], {1}[4 :] vl=64", ("%1", "%2"), 4)
    assert o.reads == ("%2",) and o.writes == ("%1",) and o.xreads == ()


def test_modulo_placement_runs_each_op_back_to_back():
    ops = [
        MO.parse(t, s, 4)
        for t, s in [
            ("vunpk {0} <- l1[0] f16", ("%1",)),
            ("vmul {0}, {1}, s0", ("%2", "%1")),
            ("vmax {0}[0 :], {1}[0 :], {1}[4 :] vl=64", ("%3", "%2")),
            ("vadd {0}, {1}, merge({1}, 8)", ("%4", "%3")),
            ("vmul {0}, {1}, s0", ("%5", "%2")),
            ("vpack {0} -> l1[64] f16", ("%5",)),
        ]
    ]
    MO._link(ops)
    ii = 30  # the math ops' 28 beats and two free slots
    assert MO._place(ops, ii)
    for eng in "MUP":
        slots = [(o.t + b) % ii for o in ops if o.eng == eng for b in range(o.occ)]
        assert len(slots) == len(set(slots))  # no two ops share a slot
    for o in ops:
        assert o.e - o.t == o.occ
        for p, lat, end in o.deps:
            assert o.t >= (ops[p].e if end else ops[p].t) + lat


def _norm(w: int) -> str:
    """A GT4 layernorm over rows of `w` columns, 4-row trips, gamma and beta
    one row broadcast to the band (the DSL's form)."""
    return (
        "target ktpu.v9\n"
        f"fn ln.l2(x: f16[64, {w}] @dram, g: f16[{w}] @dram, b: f16[{w}] @dram, "
        f"y: f16[64, {w}] @dram)\n"
        "    par c in 0..2 on vc[c]\n"
        f"        gl = load g : f16[4, {w}] @l1(gt4)\n"
        f"        gv = unpack gl : f32[4, {w}] @reg(gt4)\n"
        f"        bl = load b : f16[4, {w}] @l1(gt4)\n"
        f"        bv = unpack bl : f32[4, {w}] @reg(gt4)\n"
        "        for t in 0..8\n"
        f"            xl = load x[c*32 + t*4 : +4] : f16[4, {w}] @l1(gt4)\n"
        f"            v = unpack xl : f32[4, {w}] @reg(gt4)\n"
        "            s1 = rsum v : f32[4] @reg\n"
        f"            mu = mul s1, {1 / w} : f32[4] @reg\n"
        f"            d = sub v, mu[:, *] : f32[4, {w}] @reg(gt4)\n"
        f"            q = mul d, d : f32[4, {w}] @reg(gt4)\n"
        "            s2 = rsum q : f32[4] @reg\n"
        f"            var = mul s2, {1 / w} : f32[4] @reg\n"
        "            var = add var, 1e-05 : f32[4] @reg\n"
        "            rs = rsqrt var : f32[4] @reg\n"
        f"            n = mul d, rs[:, *] : f32[4, {w}] @reg(gt4)\n"
        f"            n = mul n, gv : f32[4, {w}] @reg(gt4)\n"
        f"            o = add n, bv : f32[4, {w}] @reg(gt4)\n"
        f"            ol = pack o : f16[4, {w}] @l1(gt4)\n"
        "            store y[c*32 + t*4 : +4] <- ol\n"
    )


def test_a_gt4_sum_of_squares_folds_the_product(band):
    m = replace_vregs(module.read(_norm(256)), 128)
    image = lower.lower(m, "ln", T.machine(vregs=128)).split("image ")[1]
    # q = d * d is never formed: the first fold multiplies one register pair
    # and adds the other's product (4 multiplies + 4 FMAs over 8 registers).
    trip = image.split("loop s15")[1].split("vpack")[0]
    reg = r"v\d+(?:\[[^\]]*\])?"
    assert len(re.findall(rf"vfma ({reg}), ({reg}), \2, \1\n", trip + "\n")) >= 4


def test_a_store_without_l1_room_packs_over_its_trips_load(band):
    m = replace_vregs(module.read(_norm(1024)), 128)
    image = lower.lower(m, "ln", T.machine(vregs=128)).split("image ")[1]
    # Two 256-word load buffers fill the L1: the packs go over the trip's
    # load after its unpacks, the refill after the drain.
    assert "vsync pack on=unpack" in image and "vsync fill on=drain" in image
    loads = set(re.findall(r"vfill a\d+ -> l1\[(\d+)\] rel", image))
    drains = set(re.findall(r"vdrain a\d+ <- l1\[(\d+)\] rel", image))
    assert drains and drains <= loads


def test_a_wide_band_past_the_registers_is_refused():
    m = replace_vregs(module.read(_norm(1024)), 64)
    with pytest.raises(TextError, match="needs more than 64 vector registers"):
        lower.lower(m, "ln", T.machine(vregs=64))


def test_modulo_colouring_compares_arcs_on_the_circle():
    # II 300, K 3: a period of 900. The second value lives two periods after
    # the first; on the circle the two overlap.
    life = {"%a": (0, 100), "%b": (1800, 1900)}
    phys = MO._colour(life, 300, 3, 8, 0)
    assert phys is not None
    for j in range(3):
        assert phys[("%a", j)] != phys[("%b", j)]


def test_modulo_emission_reads_every_value_it_wrote(monkeypatch):
    monkeypatch.setattr(V, "STRATEGY", "modulo")
    m = replace_vregs(module.read(_norm(256)), 128)
    lower.lower(m, "ln", T.machine(vregs=128))  # the replay check passes

    # The check fails a colouring that gives every value one register.
    def one(life, ii, k, regs, first):
        return dict.fromkeys(((x, j) for x in life for j in range(k)), first)

    monkeypatch.setattr(MO, "_colour", one)
    V._chosen.clear()  # lowered again, not remembered
    with pytest.raises(RuntimeError, match="which holds"):
        lower.lower(m, "ln", T.machine(vregs=128))


def test_a_vector_body_past_its_registers_is_refused():
    rows = 40
    text = (
        "target ktpu.v9\n"
        f"fn k.l2(x: f16[{rows}, 128] @dram)\n"
        "    par c in 0..1 on vc[c]\n"
        f"        xl = load x : f16[{rows}, 128] @l1\n"
        f"        v = unpack xl : f32[{rows}, 128] @reg\n"
    )
    with pytest.raises(TextError):
        lower.lower(module.read(text), "k")
