"""Node-level L2 mover ops to L1 `move` statements (docs/spec/l2-program.md §5).

    copy DST <- SRC [perm=(1, 0)]        words of SRC to DST; perm reorders
                                         SRC's word axes (the last view axis
                                         counted in 32-byte words)
    copy DST <- BUF walk=((N, S), ..) [at=B]   BUF walked by signed byte
                                         strides from byte B (flip, permute,
                                         broadcast: a stride of 0 repeats);
                                         dwalk= / dat= the same for DST
    fill DST value=V [ew=8|16|32]        the immediate (walk= / at= as copy)
    gen DST seed=S                       address-keyed pseudo-random words
    gather DST <- SRC index=IDX [fp]     DST row r := SRC row IDX[r]
    scatter DST <- SRC index=IDX [fp]    DST row IDX[r] := SRC row r
    quant DST <- SRC                     row-major f16 to mx7a / mx7b packing
    gt4 DST <- SRC                       row-major f16 to tile= sub-tiles

A view is a walk of 32-byte words, outermost level first; the destination's
walk is the iteration order (a scatter's, its source's; a transform's steps
once per converted entry).
"""

from kohakuaccel.compiler.package.mover import MOVE_WORDS, NDIM
from kohakutpu.compiler.l2 import memory as M
from kohakutpu.compiler.l2 import sync
from kohakutpu.compiler.l2.body import const, value
from kohakutpu.language.text.syntax import Arrow, Int, Name, Tuple, View

WORD = 32
OPS = ("copy", "fill", "gen", "gather", "scatter", "quant", "gt4")
UNIT = "mover"


def words(body, st, t, env, fail) -> tuple:
    """``(buffer, byte offset, [(count, stride)])`` of a view as 32-byte
    words, outermost level first."""
    v = value(body, st, t if isinstance(t, View) else View(t.text, ()), env)
    buf = body.buffers.get(v.name)
    if buf is None:
        fail(st, f"no buffer {v.name}")
    try:
        off, dims, item = M.rowmajor_walk(buf, M.axes(v, buf.shape))
    except M.ViewError as e:
        fail(st, str(e))
    if not dims or dims[-1][1] != item:
        dims = [*dims, (1, item)]
    n, s = dims[-1]
    if s != item or (n * item) % WORD or off % WORD:
        fail(st, f"{v.name}: the innermost axis is whole 32-byte words")
    levels = [*dims[:-1], (n * item // WORD, WORD)]
    return v.name, off, levels


def _merge(levels) -> list:
    """Adjacent levels one run of the other: (a, s*b), (b, s) -> (a*b, s)."""
    out = []
    for n, s in reversed(levels):
        if out and out[-1][1] * out[-1][0] == s:
            m, t = out.pop()
            out.append((n * m, t))
        else:
            out.append((n, s))
    return list(reversed(out))


def _squeeze(levels) -> list:
    """Levels of one step dropped (a walk keeps at least one)."""
    return [lv for lv in levels if lv[0] != 1] or [levels[-1]]


def _walk(name, off, levels) -> str:
    lv = ", ".join(f"({n}, {s})" for n, s in _merge(levels))
    return f"w({name} + {off}, {lv})"


def _span(name, off, levels, step=WORD) -> tuple:
    """``(buffer, lo, hi)`` bytes a walk touches, its last element `step` long."""
    lo = hi = off
    for n, s in levels:
        if s >= 0:
            hi += (n - 1) * s
        else:
            lo += (n - 1) * s
    return name, lo, hi + step


def lower(body, op, env, fail) -> tuple:
    """``(L1 mover lines, accesses)`` of one node-level op."""
    st = op.st
    pos, kw = st.positional(), st.kwargs()
    if op.op in ("fill", "gen"):
        if len(pos) != 1:
            fail(st, f"{op.op} DST {'value=V' if op.op == 'fill' else 'seed=S'}")
        name, off, lv = words(body, st, pos[0], env, fail)
        if "walk" in kw:
            if not isinstance(pos[0], Name):
                fail(st, f"{op.op} BUFFER walk=((COUNT, STRIDE), ...) [at=BYTES]")
            lv = _squeeze(_levels(body, st, kw["walk"], env, fail))
            off = const(body, st, kw["at"], env) if "at" in kw else 0
            if off % WORD or any(s % WORD for _, s in lv):
                fail(st, "a walk steps whole 32-byte words")
        key = "value" if op.op == "fill" else "seed"
        if key not in kw:
            fail(st, f"{op.op} needs {key}=")
        extra = f" ew={kw['ew'].value}" if "ew" in kw else ""
        text = f"{op.op} {_walk(name, off, lv)} {key}={_int(st, kw[key], fail)}{extra}"
        return [text], [sync.mem(UNIT, True, *_span(name, off, lv))]
    fp = Name("fp") in pos
    pos = [p for p in pos if p != Name("fp")]
    if len(pos) != 3 or pos[1] != Arrow("<-"):
        fail(st, f"{op.op} DST <- SRC ...")
    dst, src = pos[0], pos[2]
    if op.op == "gt4" and "walk" in kw:
        return _gt4_walks(body, op, dst, src, env, fail)
    if op.op in ("quant", "gt4"):
        return _convert(body, op, dst, src, env, fail)
    if op.op == "copy":
        dn, doff, dl = words(body, st, dst, env, fail)
        sn, soff, sl = words(body, st, src, env, fail)
        if "walk" in kw:
            # An explicit signed walk of the source buffer (bytes, outermost
            # first), from `at` bytes in: a flip, a permute, a broadcast.
            if not isinstance(src, Name) or "perm" in kw:
                fail(st, "copy DST <- BUFFER walk=((COUNT, STRIDE), ...) [at=BYTES]")
            sl = _levels(body, st, kw["walk"], env, fail)
            soff = const(body, st, kw["at"], env) if "at" in kw else 0
            if soff % WORD or any(s % WORD for _, s in sl):
                fail(st, "a walk steps whole 32-byte words")
        if "dwalk" in kw:
            # The destination's own walk (a window of it), from `dat` bytes in.
            if not isinstance(dst, Name):
                fail(st, "copy BUFFER <- SRC dwalk=((COUNT, STRIDE), ...) [dat=BYTES]")
            dl = _levels(body, st, kw["dwalk"], env, fail)
            doff = const(body, st, kw["dat"], env) if "dat" in kw else 0
            if doff % WORD or any(s % WORD for _, s in dl):
                fail(st, "a walk steps whole 32-byte words")
        if "perm" in kw:
            p = kw["perm"]
            if not isinstance(p, Tuple) or sorted(i.value for i in p.items) != list(
                range(len(sl))
            ):
                fail(st, f"perm= orders all {len(sl)} word axes of the source")
            sl = [sl[i.value] for i in p.items]
        if _count(dl) != _count(sl):
            fail(st, f"copy of {_count(sl)} words into {_count(dl)}")
        for lv in (sl, dl):
            if len(_merge(_squeeze(lv))) > NDIM:
                fail(
                    st,
                    f"a walk of {len(_merge(_squeeze(lv)))} levels; a walker holds {NDIM}",
                )
        if "walk" in kw or "dwalk" in kw:
            sl, dl = _squeeze(sl), _squeeze(dl)
        text = f"copy {_walk(sn, soff, sl)} -> {_walk(dn, doff, dl)}"
        return [text], [
            sync.mem(UNIT, False, *_span(sn, soff, sl)),
            sync.mem(UNIT, True, *_span(dn, doff, dl)),
        ]
    if op.op in ("gather", "scatter"):
        if "index" not in kw:
            fail(st, f"{op.op} DST <- SRC index=IDX [fp]")
        idx = value(body, st, kw["index"], env)
        idx_name = idx.name if isinstance(idx, View) else idx.text
        ibuf = body.buffers.get(idx_name)
        if ibuf is None:
            fail(st, f"no index buffer {idx_name}")
        table, rows = (src, dst) if op.op == "gather" else (dst, src)
        tv = value(
            body, st, table if isinstance(table, View) else View(table.text, ()), env
        )
        tb = body.buffers[tv.name]
        pitch = tb.nbytes // tb.shape[0]
        rn, roff, rl = words(body, st, rows, env, fail)
        n = rl[0][0] if len(rl) > 1 else 1
        per = _count(rl) // n
        walk = _walk(rn, roff, rl)
        if op.op == "gather":
            text = f"gather {tv.name} -> {walk}"
        else:
            text = f"scatter {walk} -> {tv.name}"
        text += (
            f" index={idx_name} n={n} pitch={pitch} words={per}{' fp' if fp else ''}"
        )
        table_acc = sync.mem(UNIT, op.op == "scatter", tv.name, 0, tb.nbytes)
        rows_acc = sync.mem(UNIT, op.op == "gather", *_span(rn, roff, rl))
        return [text], [
            table_acc,
            rows_acc,
            sync.mem(UNIT, False, idx_name, 0, ibuf.nbytes),
        ]
    fail(st, f"no mover op {op.op!r}: {', '.join(OPS)}")


# ------------------------------------------------------------- conversions
def _gt4_walks(body, op, dst, src, env, fail) -> tuple:
    """`gt4 DST <- SRC walk=.. dwalk=..`: whole buffers, the source walk's
    innermost level the four words of a group, the destination one level
    fewer (a group's four words land consecutively)."""
    st = op.st
    kw = st.kwargs()
    if not isinstance(dst, Name) or not isinstance(src, Name) or "dwalk" not in kw:
        fail(
            st, "gt4 BUFFER <- BUFFER walk=((COUNT, STRIDE), ..) dwalk=(..) [at= dat=]"
        )
    sl = _levels(body, st, kw["walk"], env, fail)
    dl = _levels(body, st, kw["dwalk"], env, fail)
    soff = const(body, st, kw["at"], env) if "at" in kw else 0
    doff = const(body, st, kw["dat"], env) if "dat" in kw else 0
    if (
        sl[-1][0] != 4
        or len(dl) != len(sl) - 1
        or [n for n, _ in dl] != [n for n, _ in sl[:-1]]
    ):
        fail(
            st,
            "gt4 walks: the source's innermost level is the 4 words of a group, the rest match the destination's",
        )
    if soff % WORD or doff % WORD or any(s % WORD for _, s in sl + dl):
        fail(st, "a walk steps whole 32-byte words")
    lines = []
    for s0, d0, sl_, dl_ in _cut(sl, dl):
        sl_, dl_ = _merge(_squeeze(sl_)), _merge(_squeeze(dl_))
        if max(len(sl_), len(dl_)) > NDIM:
            fail(st, f"gt4: {max(len(sl_), len(dl_))} levels; a walker holds {NDIM}")
        lines.append(
            f"gt4 {_walk(src.text, soff + s0, sl_)} -> {_walk(dst.text, doff + d0, dl_)}"
        )
    return lines, [
        sync.mem(UNIT, False, *_span(src.text, soff, sl)),
        sync.mem(UNIT, True, *_span(dst.text, doff, dl, 4 * WORD)),
    ]


def _convert(body, op, dst, src, env, fail) -> tuple:
    """`quant` / `gt4`: the source walk visits the destination's entries in
    its own order -- each entry's source words innermost -- and the
    destination walk steps once per entry. Cut into moves of at most
    `MOVE_WORDS` source words along the outer levels."""
    st = op.st
    dv = value(body, st, dst if isinstance(dst, View) else View(dst.text, ()), env)
    sv = value(body, st, src if isinstance(src, View) else View(src.text, ()), env)
    dbuf, sbuf = body.buffers.get(dv.name), body.buffers.get(sv.name)
    if dbuf is None or sbuf is None:
        fail(st, f"no buffer {dv.name if dbuf is None else sv.name}")
    try:
        dax, sax = M.axes(dv, dbuf.shape), M.axes(sv, sbuf.shape)
        if M.shape_of(dax) != M.shape_of(sax) or len(M.shape_of(sax)) != 2:
            fail(st, f"{op.op}: the views are {M.shape_of(dax)} and {M.shape_of(sax)}")
        if sbuf.dtype != "f16":
            raise M.ViewError(f"{op.op} reads a row-major f16 buffer, not {sbuf.dtype}")
        soff = M.rowmajor_walk(sbuf, sax)[0]
        pitch = sbuf.shape[-1] * (sbuf.nbytes // _count([(n,) for n in sbuf.shape]))
        conv = _quant if op.op == "quant" else _gt4
        (dbase, slv, dlv), flag = conv(dbuf, dax, pitch)
    except M.ViewError as e:
        fail(st, str(e))
    lines = []
    for s0, d0, sl, dl in _cut(slv, dlv):
        sl, dl = _merge(_squeeze(sl)), _merge(_squeeze(dl))
        if len(sl) > NDIM:
            fail(st, f"{op.op}: {len(sl)} source levels; a walker holds {NDIM}")
        lines.append(
            f"{op.op} {_walk(sv.name, soff + s0, sl)} -> {_walk(dv.name, dbase + d0, dl)}"
            f"{flag}"
        )
    per = 4 * WORD if op.op == "gt4" else M.ENTRY
    return lines, [
        sync.mem(UNIT, False, *_span(sv.name, soff, slv)),
        sync.mem(UNIT, True, *_span(dv.name, dbase, dlv, per)),
    ]


def _blocks(lo: int, size: int, block: int, what: str) -> tuple:
    """``(first block, blocks, first unit in the block, units in each)``: a
    range inside one block, or whole blocks."""
    b0, r0 = divmod(lo, block)
    if r0 + size <= block:
        return b0, 1, r0, size
    if r0 or size % block:
        raise M.ViewError(
            f"{what}: [{lo}, {lo + size}) is inside one {block} or whole ones"
        )
    return b0, size // block, 0, block


def _quant(buf, ax, pitch) -> tuple:
    """``((dest byte offset, src levels, dest levels), flag)`` of a quantise
    into an mx7a / mx7b buffer packed tile-major (`tensor.pack`): entries
    (tile, chunk, group, block), each 4 rows x 32 values of the source (two
    words a row)."""
    if buf.dtype not in ("mx7a", "mx7b") or buf.layout.get("t"):
        raise M.ViewError(f"quant writes an mx7a / mx7b buffer, not {buf.dtype}")
    g = buf.layout["gm" if buf.dtype == "mx7a" else "gn"]
    nk = buf.layout["nk"]
    base, ((r0, rows), (k0, cols)) = M._slab(buf, ax)
    if r0 % 4 or rows % 4 or k0 % 32 or cols % 32:
        raise M.ViewError("quant converts whole groups of 4 rows by 32 values")
    t0, tn, g0, gs = _blocks(r0 // 4, rows // 4, g, "quant rows (groups)")
    c0, cn, b0, bs = _blocks(k0 // 32, cols // 32, nk, "quant K (blocks)")
    chunks = buf.shape[-1] // (32 * nk)
    first = ((t0 * chunks + c0) * g + g0) * nk + b0
    src = [
        (tn, 4 * g * pitch),
        (cn, nk * 64),
        (gs, 4 * pitch),
        (bs, 64),
        (4, pitch),
        (2, WORD),
    ]
    dst = [
        (tn, chunks * g * nk * M.ENTRY),
        (cn, g * nk * M.ENTRY),
        (gs, nk * M.ENTRY),
        (bs, M.ENTRY),
    ]
    flag = " bpack" if buf.dtype == "mx7b" else ""
    return (base + first * M.ENTRY, src, dst), flag


def _gt4(buf, ax, pitch) -> tuple:
    """The same for a GT4 transform into an f16 `tile=` buffer: each entry
    is 4 rows x 16 columns (four words, one a row) to four consecutive 4 x 4
    sub-tiles."""
    if buf.dtype != "f16" or "tile" not in buf.layout:
        raise M.ViewError("gt4 writes an f16 buffer with a tile= layout")
    gm, gn = buf.layout["tile"]
    if gn % 4:
        raise M.ViewError(f"gt4: a tile {gn} sub-tiles wide is not whole groups of 4")
    base, ((r0, rows), (c0, cols)) = M._slab(buf, ax)
    if r0 % 4 or rows % 4 or c0 % 16 or cols % 16:
        raise M.ViewError("gt4 converts whole groups of 4 rows by 16 columns")
    ti, tin, bi, bs = _blocks(r0 // 4, rows // 4, gm, "gt4 rows (bands)")
    tj, tjn, qi, qs = _blocks(c0 // 16, cols // 16, gn // 4, "gt4 columns (groups)")
    tn = buf.shape[-1] // (4 * gn)
    sub = M.SUBTILE
    first = M.subtile_address(buf, ti * gm + bi, tj * gn + 4 * qi)
    src = [
        (tin, 4 * gm * pitch),
        (bs, 4 * pitch),
        (tjn, 8 * gn),
        (qs, WORD),
        (4, pitch),
    ]
    dst = [
        (tin, tn * gm * gn * sub),
        (bs, gn * sub),
        (tjn, gm * gn * sub),
        (qs, 4 * sub),
    ]
    return (base + first, src, dst), ""


def _cut(slv, dlv) -> list:
    """``[(source offset, destination offset, source levels, destination
    levels)]``: the move as pieces of at most `MOVE_WORDS` source words, cut
    along the outer levels (the destination has one level fewer: the entry's
    own words)."""
    total = _count(slv)
    if total <= MOVE_WORDS or not dlv:
        return [(0, 0, slv, dlv)]
    (n, s), (_, ds) = slv[0], dlv[0]
    out = []
    for i in range(n):
        for so, do, sl, dl in _cut(slv[1:], dlv[1:]):
            out.append((i * s + so, i * ds + do, sl, dl))
    return out


def _count(levels) -> int:
    out = 1
    for n, *_ in levels:
        out *= n
    return out


def _levels(body, st, t, env, fail) -> list:
    """``[(count, stride)]`` of a ``((COUNT, STRIDE), ...)`` keyword."""
    if not isinstance(t, Tuple) or not t.items:
        fail(st, "walk=((COUNT, STRIDE), ...)")
    out = []
    for lv in t.items:
        if not isinstance(lv, Tuple) or len(lv.items) != 2:
            fail(st, f"a walk level is (COUNT, STRIDE), got {lv!r}")
        n, s = (const(body, st, x, env) for x in lv.items)
        if n < 1:
            fail(st, f"a walk level of {n} steps")
        out.append((n, s))
    return out


def _int(st, t, fail) -> int:
    if not isinstance(t, Int):
        fail(st, f"wanted a constant, got {t!r}")
    return t.value


__all__ = ["OPS", "UNIT", "lower", "words"]
