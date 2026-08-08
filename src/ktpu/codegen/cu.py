"""Level 2 -> level 3: a Schedule becomes instructions and a memory map.

Assigns each band's grid instances to nodes of ITS OWN engine, lays out the
device image, and emits FILL/GEMM/DRAIN for the clusters and VLD/VALU/VST for
the vector cores.
"""

import numpy as np

from ktpu.codegen.operands import plan
from ktpu.ir.graph import REDUCTION
from ktpu.ir.program import Inst, MemoryMap, Opcode, Program
from ktpu.ir.sched import Band, Engine, Schedule, correspondence
from ktpu.target import Target

DRAIN_BURST = 8

#: `2 * SBIAS`, which cancels both stored block-scale biases in the accumulator.
#: A constant of the MXFP7 format, not a tunable -- docs/isa/cluster.md s4.3.
ANCHOR = 40


def _entries(rows: int, k: int, t: Target) -> int:
    """L1 entries for `rows` rows over `k` elements of K."""
    return (rows // t.lanes) * (k // t.kblock)


def cores_for(engine: Engine, t: Target) -> int:
    """How many nodes of `engine` the target has.

    A vector band dealt across `t.clusters` would name nodes that do not exist,
    which is why this is asked per band rather than once.
    """
    match engine:
        case Engine.MATMUL:
            return t.clusters
        case Engine.VECTOR:
            return t.vector_cores
        case _:
            return 1


def _fp16_words(elems: int) -> int:
    """32-bit words holding `elems` FP16 values, two per word."""
    return -(-elems // 2)


def _words24(elems: int) -> int:
    """32-bit words holding `elems` 24-bit values -- ACC24 or raw E8M15."""
    return -(-elems * 24 // 32)


def layout(
    m: int, k: int, n: int, t: Target, staged: bool = False, scratch: int = 0
) -> MemoryMap:
    """Build the device memory map for one `m x k x n` GEMM.

    A is `m x k`, B is `n x k` stored transposed, C is `m x n`. Each is ONE
    image addressed by tile rather than per-cluster bands, so a tile is
    assignable to any cluster.

    `staged` adds an `acc` region: a folded epilogue has the clusters drain
    ACC24 rather than converting to FP16 on the way out, so the intermediate
    needs its own span at 1.5x the width. That extra traffic is the price of the
    fold, and what it buys is that the value is rounded and clamped ONCE, on the
    vector core's final store, instead of twice.

    `scratch` adds a `tmp` region of that many elements, where a chain too deep
    for one VMODE pass parks its intermediate between micro-jobs. It is raw
    E8M15 rather than FP16 for the same reason `acc` is ACC24: a chain of 16 ops
    that rounded to FP16 three times on the way through would lose more than the
    ALU does.

    Every figure is in 32-BIT WORDS. Returns the map; `total` is the image size.
    """
    mm = MemoryMap()
    mm.add("a", _fp16_words(m * k), m * k, f"A operand, {m} x {k}, tile-major")
    mm.add("b", _fp16_words(n * k), n * k, f"B operand, {n} x {k}, stored transposed")
    c_burst = DRAIN_BURST * t.lanes * t.lanes
    c_elems = -(-(m * n) // c_burst) * c_burst
    if staged:
        mm.add(
            "acc",
            _words24(c_elems),
            m * n,
            f"matmul output, {m} x {n}, acc24, read by the folded epilogue",
        )
    mm.add(
        "c",
        _fp16_words(c_elems),
        m * n,
        f"C result, {m} x {n}, {t.lanes}x{t.lanes} sub-tiles",
    )
    if scratch:
        mm.add(
            "tmp",
            _words24(scratch),
            scratch,
            "vector scratch, e8m15, between micro-jobs of one chain",
        )
    return mm


def _bcast(elems: int, tiling: dict, kind: str, full: int) -> str:
    """Which axis an operand of `elems` elements spreads along.

    `col` is a bias -- one value per output column; `row` is a reduction fed
    back, one per output row, which is the shape flash attention's running max
    and denominator have. `elem` is a full operand and spreads along neither.

    `full` is the band's OWN span. An operand matching it is elementwise
    whatever its shape resembles -- a band working on row vectors reads another
    row vector one-for-one, and broadcasting it there would read one value per
    row of a tile the band is not walking.

    Past that, the element count alone CANNOT decide: at a square output
    `m == n` and both readings fit. `kind` breaks the tie by provenance, which
    is not a heuristic -- a bias is a graph input, a reduction result is
    produced by a band, and nothing else has this shape.
    """
    if elems == full:
        return "elem"
    m, n = tiling.get("m"), tiling.get("n")
    if elems == m and (kind in ("value", "band") or elems != n):
        return "row"
    if elems == n:
        return "col"
    if elems == m:
        return "row"
    return "elem"


def band_shape(b: Band, fallback: dict) -> dict:
    """`{"m", "n"}` for `b`'s own working shape, for `_bcast` to read.

    The program has ONE tiling but a graph has many shapes -- attention's two
    matmuls contract over the head dim and over the block. Falls back to the
    program tiling for a band lowered before `Band.shape` was carried.
    """
    if len(b.shape) < 2:
        return fallback
    return {"m": b.shape[-2], "n": b.shape[-1]}


def out_vid(b: Band) -> int:
    """The value id a band's LAST op produces. Not the only one that can
    outlive the band -- see `exports_of`."""
    return b.ops[-1].out


def exports_of(sched: Schedule) -> dict[str, frozenset[int]]:
    """Per band, the values it produces that must reach memory.

    A band's last op is not the only thing that outlives it: flash attention's
    softmax band yields both the probabilities a matmul consumes and the
    denominator a later band divides by. Anything a LATER band names, plus every
    result in `sched.outputs`, has to be stored.

    A graph can return more than one value -- causal attention carries softmax
    state per query block, so each block is a separate result -- and those are
    produced by bands that are not the last. Dropping them allocates no region
    and drains nothing.

    Returns band name -> value ids.
    """
    made: dict[int, str] = {}
    for b in sched.bands:
        for o in b.ops:
            made[o.out] = b.name
    wanted: dict[str, set[int]] = {b.name: set() for b in sched.bands}
    for b in sched.bands:
        for vid in b.ext:
            if vid in made and made[vid] != b.name:
                wanted[made[vid]].add(vid)
    for vid in sched.outputs or [out_vid(sched.bands[-1])]:
        if vid not in made:
            raise ValueError(
                f"schedule returns %{vid} but no band produces it; a result "
                "that is a graph INPUT or a pure view has no band to drain it"
            )
        wanted[made[vid]].add(vid)
    return {k: frozenset(v) for k, v in wanted.items()}


def allocate(sched: Schedule, t: Target, scratch: int = 0) -> tuple[MemoryMap, dict]:
    """Give every value that needs storage a region, and return `(map, where)`.

    `where` maps a level-1 value id to its region name. Regions are named after
    the VALUE they hold, because a graph with four matmuls has no single "the B
    operand" -- `a`, `b`, `c` and `acc` survive as aliases for the roles they
    play in a one-GEMM graph, which is what the packer and the tests use.

    Storage goes to every graph input a band reads and to every value in
    `exports_of`. A band whose output a folded epilogue consumes gets an ACC24
    region 1.5x the width; everything else is FP16.

    `scratch` adds `tmp` for micro-job spills. Returns `(map, where)`.
    """
    mm = MemoryMap()
    where: dict[int, str] = {}
    consumed = {b.consumes for b in sched.bands if b.consumes}
    exports = exports_of(sched)
    burst = DRAIN_BURST * t.lanes * t.lanes

    def region(vid: int, elems: int, wide: bool, note: str) -> None:
        if vid in where:
            return
        pad = -(-elems // burst) * burst if wide else elems
        where[vid] = f"v{vid}"
        mm.add(where[vid], (_words24 if wide else _fp16_words)(pad), elems, note)

    for b in sched.bands:
        for vid, (kind, w) in sorted(b.ext.items()):
            if kind == "input":
                region(vid, b.elems[vid], False, f"input {w}")

    for b in sched.bands:
        wide = b.name in consumed
        for vid in sorted(exports[b.name]):
            fmt = "acc24, read by a folded epilogue" if wide else "fp16"
            held = b.elems[vid]
            if b.engine is Engine.MATMUL:
                held = max(held, b.grid.m * b.tile.m * b.grid.n * b.tile.n)
            region(vid, held, wide, f"{b.name} produces %{vid}, {fmt}")

    ins = [where[v] for v, (k, _) in sorted(sched.bands[0].ext.items()) if k == "input"]
    for role, name in zip(("a", "b"), ins, strict=False):
        mm.alias(role, name)
    first_wide = next((b for b in sched.bands if b.name in consumed), None)
    if first_wide is not None:
        mm.alias("acc", where[out_vid(first_wide)])
    mm.alias("c", where[out_vid(sched.bands[-1])])

    if scratch:
        mm.add(
            "tmp",
            _words24(scratch),
            scratch,
            "vector scratch, e8m15, between micro-jobs of one chain",
        )
    return mm, where


def row_major_tiling(m: int, n: int) -> dict:
    """The layout of a graph with NO matmul in it.

    Sub-tile order exists because a cluster DRAINS in it. With no cluster
    involved nothing imposes an order, so the natural one is row-major -- and
    `result()` and every broadcast still need a map, which is why this is a real
    layout rather than an empty dict.
    """
    rows = np.repeat(np.arange(m), n)
    cols = np.tile(np.arange(n), m)
    inv = np.arange(m * n).reshape(m, n)
    return {
        "m": m,
        "n": n,
        "tm": m,
        "tn": n,
        "gn": 1,
        "tiles": 1,
        "rows": rows,
        "cols": cols,
        "inv": inv,
    }


def tiling_of(mm: Band, m: int, n: int, t: Target) -> dict:
    """How C is laid out for a matmul band: tile shape, tile count, and the
    logical row and column of every element of one tile's span.

    A drained tile is sub-tile order, row group major (isa/cluster.md s5), so
    element `e` of a tile's span is not element `e` of a row-major tile. Anything
    that has to map an address back to a position -- unpacking the result, or a
    broadcast operand working out which column it is on -- needs this.
    """
    return geometry(m, n, mm.tile.m, mm.tile.n, mm.grid.n, t, mm.grid.m * mm.grid.n)


def geometry(
    m: int, n: int, tm: int, tn: int, gn: int, t: Target, tiles: int = 0
) -> dict:
    """Tile geometry for an `m x n` value drained in `tm x tn` tiles.

    `tiles` defaults to what covers `m x n`. Returns the dict `result()` and
    every broadcast read use: logical size, tile size, grid width, and the row
    and column of each element of one tile's span.
    """
    lanes = t.lanes
    rows = np.arange(tm).repeat(tn).reshape(tm // lanes, lanes, tn // lanes, lanes)
    cols = np.tile(np.arange(tn), tm).reshape(tm // lanes, lanes, tn // lanes, lanes)
    order = (0, 2, 1, 3)
    r, c = rows.transpose(order).reshape(-1), cols.transpose(order).reshape(-1)
    inv = np.zeros((tm, tn), dtype=int)
    inv[r, c] = np.arange(tm * tn)
    return {
        "m": m,
        "n": n,
        "tm": tm,
        "tn": tn,
        "gn": gn,
        "tiles": tiles or (-(-m // tm) * -(-n // tn)),
        "rows": r,
        "cols": c,
        "inv": inv,
    }


MESH_COLS = 4


def node_of(index: int, cores: int) -> tuple[int, int]:
    """Mesh coordinate of core `index`, wrapping at `cores`.

    The coordinate is a function of the CORE, never of the instance index --
    deriving it from the instance is how a band of 64 instances over 8 cores
    ends up naming 64 distinct nodes.
    """
    i = index % max(1, cores)
    return 1 + (i % MESH_COLS), 1 + (i // MESH_COLS)


def assign(sched: Schedule, t: Target) -> dict[str, list[tuple[int, int]]]:
    """Deal every band's grid instances to nodes of that band's engine.

    Returns band name -> node per instance, in `Grid.instances()` order.
    Instances are dealt in CONTIGUOUS BLOCKS over `min(grid.size, cores)` nodes,
    so a core owns a run of neighbouring tiles rather than every n-th one: a run
    is one address descriptor and one hardware loop, and a stride of `cores`
    would be neither. A grid smaller than the mesh leaves the rest idle.

    Raises ValueError if a band's engine has no cores on `t`.
    """
    out: dict[str, list[tuple[int, int]]] = {}
    for b in sched.bands:
        cores = cores_for(b.engine, t)
        if cores < 1:
            raise ValueError(
                f"band {b.name} needs a {b.engine.value} core and {t.name} has none"
            )
        live = max(1, min(b.grid.size, cores))
        out[b.name] = [
            node_of(i * live // b.grid.size, live) for i in range(b.grid.size)
        ]
    return out


def _emit_matmul(
    prog: Program,
    b: Band,
    where: list[tuple[int, int]],
    t: Target,
    reg: dict[int, str],
    staged: bool = False,
):
    op = b.ops[0]
    a = prog.memory.get(reg[op.ins[0]])
    bb = prog.memory.get(reg[op.ins[1]])
    c = prog.memory.get(reg[op.out])
    a_off, b_off = (op.offs or (0, 0))[:2]
    mm = b.ops[0].attrs
    chunks = max(1, mm["k"] // b.tile.k)
    a_ent = _entries(b.tile.m, b.tile.k, t)
    b_ent = _entries(b.tile.n, b.tile.k, t)
    gm, gn = b.tile.m // t.lanes, b.tile.n // t.lanes
    nk = max(1, b.tile.k // t.kblock)
    subtiles = gm * gn
    words = _words24 if staged else _fp16_words
    tile_words = words(b.tile.m * b.tile.n)
    a_words = _fp16_words(b.tile.m * b.tile.k)
    b_words = _fp16_words(b.tile.n * b.tile.k)

    resident: dict[tuple[int, int], int] = {}
    for idx, (mo, no, _ko) in enumerate(b.grid.instances()):
        node = where[idx]
        keep = b.residency.get("b") and resident.get(node) == no
        resident[node] = no
        for ch in range(chunks):
            prog.emit(
                Inst(
                    Opcode.FILL,
                    Engine.MATMUL,
                    node,
                    {
                        "sel": "a",
                        "word": a.word
                        + _fp16_words(a_off)
                        + (mo * chunks + ch) * a_words,
                        "n": a_ent,
                        "eoff": (ch % 2) * a_ent,
                    },
                )
            )
            if not keep:
                prog.emit(
                    Inst(
                        Opcode.FILL,
                        Engine.MATMUL,
                        node,
                        {
                            "sel": "b",
                            "word": bb.word
                            + _fp16_words(b_off)
                            + (no * chunks + ch) * b_words,
                            "n": b_ent,
                            "eoff": ch * b_ent,
                        },
                    )
                )
            prog.emit(
                Inst(
                    Opcode.GEMM,
                    Engine.MATMUL,
                    node,
                    {
                        "gm": gm,
                        "gn": gn,
                        "nk": nk,
                        "anchor": ANCHOR,
                        "acc": int(ch > 0),
                        "aoff": (ch % 2) * a_ent,
                        "boff": ch * b_ent,
                        "chunk": ch,
                    },
                )
            )
        prog.emit(
            Inst(
                Opcode.DRAIN,
                Engine.MATMUL,
                node,
                {
                    "word": c.word + (mo * b.grid.n + no) * tile_words,
                    "n": subtiles,
                    "dtype": "acc24" if staged else "fp16",
                },
            )
        )


def vmode_for(band: Band) -> tuple[str, int]:
    """Pick the lane topology for a band, returning (mode, chain depth).

    FLAT runs 16 independent lanes and is memory-bound at one op per element;
    D2 already saturates the ALUs; D4 fuses four ops with no intermediate
    reaching the register file. A band containing a reduction needs TREE.
    See docs/isa/vector.md s4 and s6.
    """
    if any(o.kind in REDUCTION for o in band.ops):
        return "TREE", 1
    n = len(band.ops)
    if n >= 4:
        return "D4", 4
    if n >= 2:
        return "D2", 2
    return "FLAT", 1


def vector_spans(b: Band, producer: Band | None) -> list[tuple[int, int, int]]:
    """Where each instance of vector band `b` sits, as `(elem, tile, part)`.

    `elem` is the instance's element offset into C, `tile` the producer tile it
    covers and `part` which slice of that tile -- both `-1` when the band reads
    DRAM instead of a resident tile.

    A consuming band's offset is NOT its instance index times its tile: C is
    laid out by PRODUCER tile, and a grid that refines the producer's on the row
    axis walks those tiles in a different order than it walks its own instances.
    Returns one entry per instance, in `Grid.instances()` order.
    """
    elems = b.tile.m * b.tile.n
    if producer is None:
        return [(i * elems, -1, -1) for i in range(b.grid.size)]

    sm, sn = correspondence(producer, b)
    out = []
    for mo, no, _ko in b.grid.instances():
        pm, part_m = divmod(mo, sm)
        pn, part_n = divmod(no, sn)
        tile = pm * producer.grid.n + pn
        part = part_m * sn + part_n
        out.append(((tile * sm * sn + part) * elems, tile, part))
    return out


def _runs(offsets: list[int], step: int) -> list[tuple[int, int]]:
    """Collapse sorted `offsets` into `(start, count)` runs spaced by `step`."""
    out: list[tuple[int, int]] = []
    for off in offsets:
        if out and out[-1][0] + out[-1][1] * step == off:
            out[-1] = (out[-1][0], out[-1][1] + 1)
        else:
            out.append((off, 1))
    return out


def scratch_for(b: Band, exports: frozenset[int] = frozenset()) -> int:
    """Elements of `tmp` a vector band needs, one span per spilled value.

    A band whose ops all fit the VMODE depth spills nothing and needs none.
    """
    _, depth = vmode_for(b)
    return plan(b, depth, exports).nspill * b.grid.size * b.tile.m * b.tile.n


def _emit_vector(
    prog: Program,
    b: Band,
    where: list[tuple[int, int]],
    t: Target,
    reg: dict[int, str],
    producer: Band | None = None,
    exports: frozenset[int] = frozenset(),
    feeds_matmul: dict[int, tuple[int, int]] | None = None,
    made_tile: dict[int, tuple[int, int]] | None = None,
):
    """Emit ONE program per vector core, looping over the data that core owns.

    Two things keep the stream short. Every instance of a band runs the same
    sequence, so the code is issued once per CORE and VLOOP covers all the
    chunks that core was dealt -- issuing per instance instead multiplies the
    whole body by the grid size for no benefit.

    A chain deeper than the mode splits into MICRO-JOBS of `depth` ops, each
    with its own VLOOP over the same data. A 16-op chain in D4 becomes four
    passes of four fused ops rather than one 16-deep pass the hardware cannot
    wire, and each pass still amortises its setup over every chunk.

    A value produced in one micro-job and read in a later one round-trips
    through `tmp` in raw E8M15 -- `operands.plan` finds those and nothing else,
    since the register file does not survive a VLOOP that walks every chunk.

    With `producer`, the band is a folded epilogue: it loads ACC24 straight from
    the span that band drained, so the intermediate is never converted to FP16
    and the only rounding and the only clamp are on this band's own store.
    """
    feeds_matmul = feeds_matmul or {}
    made_tile = made_tile or {}
    strided: dict[int, dict] = {}
    for op in b.ops:
        for v, s, o in zip(op.ins, op.strides or (), op.offs or (), strict=False):
            if s == 1:
                continue
            bm, bn = made_tile.get(v, (0, 0))
            want = {"stride": s, "base": o, "bm": bm, "bn": bn}
            if strided.setdefault(v, want) != want:
                raise ValueError(
                    f"band {b.name} reads %{v} at {strided[v]} and at {want}; "
                    "one load cannot serve two windows of the same value, so "
                    "the uses must be split across bands"
                )
    mode, depth = vmode_for(b)
    p = plan(b, depth, exports)
    src_tile = made_tile.get(p.source)
    feed = prog.memory.get(reg[p.source]) if p.source in reg else None
    acc = feed if producer is not None else None
    elems = b.tile.m * b.tile.n
    reduces = any(o.kind in REDUCTION for o in b.ops)
    vl = min(t.vlmax, b.tile.n if reduces else elems)
    per_instance = max(1, -(-elems // vl))
    e = Engine.VECTOR
    spans = vector_spans(b, producer)

    owned: dict[tuple[int, int], list[int]] = {}
    for idx in range(b.grid.size):
        owned.setdefault(where[idx], []).append(idx)

    for node, idxs in owned.items():
        prog.emit(Inst(Opcode.VSETVL, e, node, {"vl": vl}))
        prog.emit(Inst(Opcode.VSETMD, e, node, {"mode": mode, "depth": depth}))
        for vid, val in sorted(p.consts.items()):
            prog.emit(
                Inst(
                    Opcode.VSETI,
                    e,
                    node,
                    {"sd": sorted(p.consts).index(vid), "imm": val, "vid": vid},
                )
            )
        for start, count in _runs(sorted(spans[i][0] for i in idxs), elems):
            word = (feed.word if feed else 0) + _fp16_words(start)
            chunks = per_instance * count
            for j, job in enumerate(p.jobs):
                body = len(job.loads) + len(job.ops) + len(job.exports)
                prog.emit(
                    Inst(
                        Opcode.VLOOP, e, node, {"count": chunks, "body": body, "job": j}
                    )
                )
                for vid, src, vd in job.loads:
                    match src.kind:
                        case "tensor":
                            r = prog.memory.get(reg[vid])
                            ld = {
                                "src": src.name,
                                "dtype": "fp16",
                                "word": r.word,
                                **strided.get(vid, {}),
                                **(
                                    {"stm": src_tile[0], "stn": src_tile[1]}
                                    if src_tile
                                    else {}
                                ),
                                "bcast": (
                                    "row"
                                    if vid in strided
                                    else _bcast(
                                        b.elems[vid],
                                        band_shape(b, prog.tiling),
                                        b.ext[vid][0],
                                        elems * b.grid.size,
                                    )
                                ),
                                "coff": start,
                            }
                        case _ if acc is not None:
                            ld = {
                                "word": acc.word + _words24(start),
                                "dtype": "acc24",
                                "src": producer.name,
                                "tile": spans[min(idxs)][1],
                                "part": spans[min(idxs)][2],
                                "ntile": count,
                            }
                        case _:
                            ld = {"word": word, "dtype": "fp16"}
                    if reduces:
                        flat = ld.get("bcast", "elem") == "elem"
                        ld |= {
                            "word": prog.memory.get(reg[vid]).word,
                            "gather": "row" if flat else "rowbcast",
                            "coff": start,
                            "tn": b.tile.n,
                        }
                        if flat and vid in feeds_matmul:
                            rows, kk = feeds_matmul[vid]
                            ld |= {"layout": "entry", "erows": rows, "ek": kk}
                        elif flat and src_tile:
                            ld |= {"stm": src_tile[0], "stn": src_tile[1]}
                    prog.emit(Inst(Opcode.VLD, e, node, {**ld, "vd": vd, "vid": vid}))
                for op, srcs, dst in job.binds:
                    kind = Opcode.VRED if op.kind in REDUCTION else Opcode.VALU
                    prog.emit(
                        Inst(
                            kind,
                            e,
                            node,
                            {
                                "op": op.kind.value,
                                "vd": -1 if dst.kind == "chain" else dst.idx,
                                "dst": dst.kind,
                                "srcs": [str(s) for s in srcs],
                                "vid": op.out,
                            },
                        )
                    )
                dsts = {o.out: d for o, _, d in job.binds}
                for vid, vs in job.exports:
                    r = prog.memory.get(reg[vid])
                    shrink = max(1, elems * b.grid.size // max(1, b.elems[vid]))
                    scalar = dsts[vid].kind == "sreg"
                    st = {
                        "word": r.word + _fp16_words(start // shrink),
                        "dtype": "fp16",
                        "vs": vs,
                        "vid": vid,
                        "src": "sreg" if scalar else "vreg",
                        "n": 1 if scalar else vl,
                    }
                    if reduces and not scalar:
                        st |= {
                            "word": r.word,
                            "gather": "row",
                            "coff": start,
                            "tn": b.tile.n,
                            **(
                                {"stm": src_tile[0], "stn": src_tile[1]}
                                if src_tile
                                else {}
                            ),
                        }
                    if vid in feeds_matmul and not scalar:
                        rows, kk = feeds_matmul[vid]
                        st |= {
                            "word": r.word,
                            "layout": "entry",
                            "erows": rows,
                            "ek": kk,
                            "coff": start,
                        }
                    prog.emit(Inst(Opcode.VST, e, node, st))


def _emit_host(prog: Program, b: Band, where: list[tuple[int, int]]):
    for idx, _ in enumerate(b.grid.instances()):
        prog.emit(
            Inst(
                Opcode.HOST,
                Engine.HOST,
                where[idx],
                {"ops": [o.kind.value for o in b.ops]},
            )
        )


def codegen(sched: Schedule, m: int, k: int, n: int, t: Target) -> Program:
    """Lower `sched` to a Program for an `m x k x n` problem on `t`.

    Builds the memory map, assigns instances per engine, emits instructions for
    every band, and cuts them into rounds bounded by `t.stage_flits`. Raises
    ValueError via `assign` if a band needs an engine the target lacks.
    """
    prog = Program(name=sched.name)
    consumed = {b.consumes for b in sched.bands if b.consumes}
    exports = exports_of(sched)
    scratch = max(
        (
            scratch_for(b, exports[b.name])
            for b in sched.bands
            if b.engine is Engine.VECTOR
        ),
        default=0,
    )
    prog.memory, reg = allocate(sched, t, scratch)
    where = assign(sched, t)
    by_name = {b.name: b for b in sched.bands}

    last = sched.bands[-1]
    feeds_matmul = {
        b.ops[0].ins[0]: (b.tile.m, b.tile.k)
        for b in sched.bands
        if b.engine is Engine.MATMUL
    }
    made_tile = {
        b.ops[0].out: (b.tile.m, b.tile.n)
        for b in sched.bands
        if b.engine is Engine.MATMUL
    }
    made_gn = {b.ops[0].out: b.grid.n for b in sched.bands if b.engine is Engine.MATMUL}
    made_tiles = {
        b.ops[0].out: b.grid.m * b.grid.n
        for b in sched.bands
        if b.engine is Engine.MATMUL
    }
    made_shape = {
        b.ops[0].out: b.shape for b in sched.bands if b.engine is Engine.MATMUL
    }
    for b in sched.bands:
        if b.engine is not Engine.VECTOR:
            continue
        src = plan(b, vmode_for(b)[1], exports[b.name]).source
        shape = made_tile.get(src)
        if shape:
            for o in b.ops:
                made_tile.setdefault(o.out, shape)
                made_gn.setdefault(o.out, made_gn.get(src, 1))
                made_shape.setdefault(o.out, b.shape)
                made_tiles.setdefault(o.out, made_tiles.get(src, 0))
    mmb = by_name.get(last.consumes or "") or next(
        (b for b in reversed(sched.bands) if b.engine is Engine.MATMUL), None
    )
    if mmb is not None:
        prog.tiling = tiling_of(mmb, m, n, t)
    else:
        prog.tiling = row_major_tiling(m, n)

    # Per region, so a value drained by one matmul is not unpacked with
    # another's tile. Only values whose shape fills the tile order are entered.
    held = {vid: n for b in sched.bands for vid, n in b.elems.items()}
    for vid, (tm, tn) in made_tile.items():
        shape = made_shape.get(vid, ())
        if vid not in reg or len(shape) < 2:
            continue
        rm, rn = shape[-2], shape[-1]
        # A reduction's output inherits its band's WORKING shape, which is the
        # wider input. Its own count is the check that it really fills the tile.
        if rm % tm or rn % tn or held.get(vid) != rm * rn:
            continue
        prog.tilings[reg[vid]] = geometry(
            rm, rn, tm, tn, made_gn.get(vid, 1), t, made_tiles.get(vid, 0)
        )
    for b in sched.bands:
        for vid, (kind, w) in b.ext.items():
            if kind == "input":
                prog.inputs[str(w)] = reg[vid]
    for b in sched.bands:
        if b.engine is not Engine.MATMUL:
            continue
        op = b.ops[0]
        for role, vid in (("a", op.ins[0]), ("b", op.ins[1])):
            spec = {
                "role": role,
                "rows": b.tile.m if role == "a" else b.tile.n,
                "k": b.tile.k,
                "flip": bool(op.attrs.get("bflip")) if role == "b" else False,
            }
            have = prog.packing.get(reg[vid])
            if have is not None and have != spec:
                raise ValueError(
                    f"%{vid} is packed as {have} for one matmul and {spec} for "
                    f"{b.name}; one region cannot hold two layouts, so the "
                    "operand needs a copy or the tiles must agree"
                )
            prog.packing[reg[vid]] = spec

    for b in sched.bands:
        if b.engine is not Engine.VECTOR:
            continue
        # Any operand from a DRAIN, not `plan().source`: a band reading a
        # constant reports the CONSTANT as its source.
        src = next(
            (v for o in b.ops for v in o.ins if v in made_tile),
            None,
        )
        shape = made_tile.get(src) if src is not None else None
        if not shape:
            continue
        full = b.tile.m * b.tile.n * b.grid.size
        for vid, (kind, _) in b.ext.items():
            if kind != "input":
                continue
            if _bcast(b.elems[vid], band_shape(b, prog.tiling), kind, full) != "elem":
                continue
            spec = {
                "role": "tile",
                "tm": shape[0],
                "tn": shape[1],
                "gn": made_gn.get(src, 1),
            }
            have = prog.packing.get(reg[vid])
            if have is not None and have != spec:
                raise ValueError(
                    f"%{vid} is packed as {have} but {b.name} reads it element "
                    f"for element against a drained tile, which needs {spec}; "
                    "one region cannot hold two layouts, so it needs a copy"
                )
            prog.packing[reg[vid]] = spec

    for b in sched.bands:
        match b.engine:
            case Engine.MATMUL:
                _emit_matmul(prog, b, where[b.name], t, reg, b.name in consumed)
            case Engine.VECTOR:
                _emit_vector(
                    prog,
                    b,
                    where[b.name],
                    t,
                    reg,
                    by_name.get(b.consumes or ""),
                    exports[b.name],
                    feeds_matmul,
                    made_tile,
                )
            case Engine.HOST:
                _emit_host(prog, b, where[b.name])

    cut, cur = [], []
    for i in range(len(prog.insts)):
        if len(cur) >= t.stage_flits:
            cut.append(cur)
            cur = []
        cur.append(i)
    if cur:
        cut.append(cur)
    prog.rounds = cut
    return prog
