"""L2 -> L1 and the L2 -> L2 passes (docs/spec/l2-program.md). A cluster
kernel's compiled package is witnessed against the hand-written L1's bytes;
the rest against the L1 text the lowering must write."""

import re
from dataclasses import replace

import pytest

from kohakutpu.compiler import build
from kohakutpu.compiler import target as T
from kohakutpu.compiler.l2 import lower, passes, pipeline
from kohakutpu.compiler.l2 import modulo as MO
from kohakutpu.compiler.l2 import vector as V
from kohakutpu.language import examples
from kohakutpu.language.text import module
from kohakutpu.language.text.syntax import TextError


@pytest.fixture
def band(monkeypatch):
    """The band lowering only: these tests read its loop's shape."""
    monkeypatch.setattr(V, "STRATEGY", "band")


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
    from kohakutpu.compiler.l2 import body as B

    body = B.read(l2, "softmax")
    par = body.stmts[0]
    costs = {
        band: V._lower(
            body, par, {par.var: 0}, "k", body.reader.fail, band, False, True
        )[2]
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


def test_modulo_rotates_the_predicate_over_its_copies(monkeypatch):
    monkeypatch.setattr(V, "STRATEGY", "modulo")
    l2 = examples.load(examples.example("leaky", "l2"))
    image = lower.lower(replace_vregs(l2, 128), "leaky", T.machine(vregs=128))
    # K >= 2 copies of an iteration in flight: at least p1 and p2
    assert "vcmpgt p1," in image and "vcmpgt p2," in image
    assert "if=p1" in image and "if=p2" in image


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
