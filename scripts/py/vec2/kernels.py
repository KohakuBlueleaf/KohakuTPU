"""V2 vector kernels for `bench.py`: each returns (build(addr) -> Kernel2, arrays,
reference). They are measurements of the core, written by hand."""

import numpy as np
from kohakutpu.hw import tensor as TT
from kohakutpu.hw import vector2 as A
from kohakutpu.hw.vector2 import Kernel2, dim, e8m15

LOG2E = 1.4426950408889634
#: Elements of one fp16 stream a RUN covers: 128 KB, its rel offset's reach.
RUN_ELEMS = 65536


def _f16(x):
    return np.asarray(x, np.float16)


DEF_CHK = ((0, 1), (0, 1), (0, 1), (0, 1))
DEF_XB = (A.XM_NONE, 0, 0, False, 0, 0)


class Em:
    """Emits math with the VL / VCFG it needs, skipping repeats. VL and the
    chunk addressing go out together in one CHKVL."""

    def __init__(self, k):
        self.k = k
        self.forget()

    def forget(self):
        self.cvl, self.cchk, self.cxb = None, None, None
        self.cstr = {"u": None, "p": None}

    def walk(self, kind, word, stride=None):
        """A VUNPK / VPACK word, after the USTR / PSTR its walk needs."""
        st = stride or (1, 4)
        if st != self.cstr[kind]:
            self.k.emit(A.cfg_stride(A.CFG_USTR if kind == "u" else A.CFG_PSTR, *st))
            self.cstr[kind] = st
        self.k.emit(word)

    def cost(self, vl=128, chk=None, xb=None):
        """VCFG words an op with this VL / chk / xb would emit now."""
        om = chk is not None or xb is not None
        c = (chk or DEF_CHK) if om else (self.cchk or DEF_CHK)
        n = int(vl != self.cvl or c != self.cchk)
        return n + int(om and (xb or DEF_XB) != self.cxb)

    def op(self, mn, vd, va=0, vb=0, vc=0, sa=0, sb=0, sc=0, vl=128, chk=None, xb=None):
        om = chk is not None or xb is not None
        c = (chk or DEF_CHK) if om else (self.cchk or DEF_CHK)
        if vl != self.cvl or c != self.cchk:
            self.k.emit(A.chkvl(vl, *c))
            self.cvl, self.cchk = vl, c
        if om:
            x = xb or DEF_XB
            if x != self.cxb:
                self.k.emit(A.xb(*x))
                self.cxb = x
        self.k.emit(A.math(mn, vd, va, vb, vc, sa, sb, sc, om=om))

    def comb(self, mn, vd, ra, ba, rb, bb, db, vl):
        """vd[db..] = ra[ba..] (op) rb[bb..]: MAX/MIN read b, ADD reads c."""
        if mn == "VADD":
            self.op(mn, vd, ra, 0, rb, vl=vl, chk=((ba, 1), (0, 1), (bb, 1), (db, 1)))
        else:
            self.op(mn, vd, ra, rb, 0, vl=vl, chk=((ba, 1), (bb, 1), (0, 1), (db, 1)))

    def merge(self, mn, g0, g1, gm, dst, dchunk):
        """Rows 0..7 in g0's chunks, 8..15 in g1's (16 partials each) ->
        dst chunk dchunk, lane r = row r's reduction. 15 beats."""
        xc = mn == "VADD"
        self.op(
            mn, gm, g0, g1, g1, vl=128, chk=DEF_CHK, xb=(A.XM_MERGE, 8, 0, xc, 0, 0)
        )
        self.op(
            mn,
            gm,
            gm,
            gm,
            gm,
            vl=64,
            chk=((0, 1), (4, 1), (4, 1), (0, 1)),
            xb=(A.XM_MERGE, 4, 0, xc, 0, 0),
        )
        self.op(
            mn,
            gm,
            gm,
            gm,
            gm,
            vl=32,
            chk=((0, 1), (2, 1), (2, 1), (0, 1)),
            xb=(A.XM_MERGE, 2, 0, xc, 0, 0),
        )
        self.op(
            mn,
            dst,
            gm,
            gm,
            gm,
            vl=16,
            chk=((0, 1), (1, 1), (1, 1), (dchunk, 1)),
            xb=(A.XM_MERGE, 1, 0, xc, 0, 0),
        )

    def rowreduce(self, mn, rows, tregs, g, gm, dst, dchunk):
        """Up to 16 rows of 8 chunks (one register each) -> dst chunk, lane r =
        row r. With 8 rows or fewer, g may name one register twice."""
        rr = range(len(rows))
        for r in sorted(rr, key=lambda r: r % 2):
            self.comb(mn, tregs[r // 2], rows[r], 0, rows[r], 4, 4 * (r % 2), 64)
        for r in sorted(rr, key=lambda r: r % 2):
            tb = 4 * (r % 2)
            self.comb(
                mn, tregs[r // 2], tregs[r // 2], tb, tregs[r // 2], tb + 2, tb, 32
            )
        for r in sorted(rr, key=lambda r: r % 8):
            tb = 4 * (r % 2)
            self.comb(
                mn, g[r // 8], tregs[r // 2], tb, tregs[r // 2], tb + 1, r % 8, 16
            )
        self.merge(mn, g[0], g[1], gm, dst, dchunk)

    def bcast(self, mn, vd, va, vrow, chunk, lane, vl=128, vc=0, sc=0):
        """vd = va (op) row-scalar: b = vrow[chunk] lane `lane` on every beat."""
        self.op(
            mn,
            vd,
            va,
            vrow,
            vc,
            sc=sc,
            vl=vl,
            chk=((0, 1), (chunk, 0), (0, 1), (0, 1)),
            xb=(A.XM_BCAST, lane, 3, False, 0, 0),
        )


LAT = 21  # math issue -> result readable, cycles
ULAT = 10  # unpack issue -> register written


class Rec:
    """Records one stream of ops (math through Em's signature, the rest raw)
    with the registers each reads and writes, for `schedule`."""

    def __init__(self):
        self.ops = []

    def op(self, mn, vd, va=0, vb=0, vc=0, sa=0, sb=0, sc=0, vl=128, chk=None, xb=None):
        code = A.OPS[mn]
        reads = set()
        unary = code in (0, 1, 2, 0x0E, 0x0F, 0x10, 0x11)
        usesb = code in (5, 6, 7, 8, 9, 10, 11, 12, 13, 0x12)
        usesc = code in (3, 4, 6, 7, 10)
        xc = xb is not None and xb[3]
        if sa == 0:
            reads.add(va)
        if sb == 0 and (usesb or (xc and usesc)) and not unary:
            reads.add(vb)
        if sc == 0 and usesc:
            reads.add(vc)
        cmp = code in (11, 12, 13)
        self.ops.append(
            (
                "m",
                (mn, vd, va, vb, vc, sa, sb, sc, vl, chk, xb),
                reads,
                set() if cmp else {vd},
                -(-vl // 16),
                None,
                None,
            )
        )

    def raw(self, word, reads=(), writes=(), kind="x", n=0, mark=None, need=None):
        """`mark` names this op; an op with `need` waits until the op that
        marked that name (in any stream) has been emitted."""
        self.ops.append((kind, word, set(reads), set(writes), n, mark, need))

    def unpk(self, vd, *a, mark=None, need=None, stride=None, **kw):
        n = kw.get("n", 8)
        self.raw(
            (A.vunpk(vd, *a, **kw), stride),
            writes=[vd],
            kind="u",
            n=n,
            mark=mark,
            need=need,
        )

    def pack(self, vs, *a, mark=None, need=None, stride=None, **kw):
        n = kw.get("n", 8)
        self.raw(
            (A.vpack(vs, *a, **kw), stride),
            reads=[vs],
            kind="p",
            n=n,
            mark=mark,
            need=need,
        )

    # Em's helpers, recorded instead of emitted.
    comb = Em.comb
    merge = Em.merge
    rowreduce = Em.rowreduce
    bcast = Em.bcast


def schedule(streams, em, offsets=None):
    """Interleave streams (each kept in order) by a time model of the core:
    emit next whichever head can start earliest. Math: one beat a cycle,
    results LAT later; unpack: a word a cycle, ULAT latency; a write waits for
    every earlier read of its register (the dispatch WAR stall). `offsets`
    delays a stream's first op, so equal streams run out of phase."""
    heads = [0] * len(streams)
    offs = offsets or [0] * len(streams)
    wready, lastread = {}, {}
    tm = tu = tp = 0
    marked = set()
    names = {o[5] for s in streams for o in s if o[5]}

    def ready(o):
        kind, _, rd, wr, _n, _, need = o
        if need and need in names and need not in marked:
            return float("inf")
        t = max([wready.get(r, 0) for r in rd] + [0])
        t = max(
            [t]
            + [lastread.get(r, 0) for r in wr]
            + [wready.get(r, 0) - LAT for r in wr]
        )
        return max(t, {"m": tm, "u": tu, "p": tp}.get(kind, 0))

    last = 0
    while any(h < len(s) for h, s in zip(heads, streams)):
        best = None
        for i, s in enumerate(streams):
            if heads[i] < len(s):
                # Ties go to the op needing the fewest VCFG words, then
                # stay on the last stream.
                o = s[heads[i]]
                cw = em.cost(o[1][8], o[1][9], o[1][10]) if o[0] == "m" else 0
                key = (max(ready(o), offs[i]), cw, i != last)
                if best is None or key < best[0]:
                    best = (key, i)
        (t, _, _), i = best
        assert t != float("inf"), "every stream waits on a mark not yet emitted"
        last = i
        kind, payload, rd, wr, n, mark, _ = streams[i][heads[i]]
        heads[i] += 1
        if mark:
            marked.add(mark)
        if kind == "m":
            em.op(*payload[:8], vl=payload[8], chk=payload[9], xb=payload[10])
            tm = t + n
            for r in rd:
                lastread[r] = max(lastread.get(r, 0), t + n)
            for r in wr:
                wready[r] = t + n + LAT
        else:
            if kind in ("u", "p"):
                em.walk(kind, *payload)
            else:
                em.k.emit(payload)
            if kind == "u":
                tu = t + n
                for r in wr:
                    wready[r] = t + n + ULAT
            elif kind == "p":
                tp = t + n
                for r in rd:
                    lastread[r] = max(lastread.get(r, 0), t + n)
    return tm


def body_words(k):
    return [k.words[i] for i in range(len(k.words))]


def softmax_mod(rows=128, cols=128):
    """Software-pipelined softmax, 8-row tiles. Half 1 of tile k (unpack,
    scale, row max, EXP2D, pack e) runs beside half 2 of tile k-1 (unpack e,
    row sum, INV, scale, pack out); e crosses through L1, so the halves own
    disjoint registers and every loop iteration is the same code."""
    assert cols == 128 and rows % 16 == 0
    rng = np.random.default_rng(3)
    x = _f16(rng.standard_normal((rows, cols)) * 2)
    nt = rows // 8
    XA, TA, GA, MA, VA = list(range(8)), [16, 17, 18, 19], 20, 21, 22
    EB, TB, GB, MB, VB = list(range(8, 16)), [23, 24, 25, 26], 27, 28, 29
    XIN, EBUF, OUT = [0, 64], [256, 320], [384, 448]
    # Marks, by buffer parity j: x unpacked, e packed, e unpacked, out packed.
    MX, MEP, MEU, MOP = 0, 2, 4, 6

    def ref():
        v = x.astype(np.float64)
        e = np.exp(v - v.max(1, keepdims=True))
        return e / e.sum(1, keepdims=True)

    def half1(j, fill):
        r = Rec()
        r.raw(A.vbar())
        for i, y in enumerate(XA):
            r.unpk(y, XIN[j] + 8 * i)
        r.raw(A.vmark(MX + j, A.K_UNPACK))
        if fill:  # the next tile, into the buffer x(k-1) left
            r.raw(A.vsync(A.W_A, mark=MX + 1 - j))
            r.raw(A.vfill(0, XIN[1 - j], rel=True))
        for y in XA:
            r.op("VMUL", y, y, 0, sb=A.SRC_S)
        r.rowreduce("VMAX", XA, TA, [GA, GA], MA, VA, 0)
        for i, y in enumerate(XA):
            r.bcast("VEXP2D", y, y, VA, 0, i)
        r.raw(A.vsync(A.W_P, mark=MEU + j))  # e(k-2) read out of EBUF[j]
        for i, y in enumerate(XA):
            r.pack(y, EBUF[j] + 8 * i)
        r.raw(A.vmark(MEP + j, A.K_PACK))
        return r.ops

    def half2(j):
        r = Rec()
        r.raw(A.vsync(A.W_U, mark=MEP + j))
        for i, e in enumerate(EB):
            r.unpk(e, EBUF[j] + 8 * i)
        r.raw(A.vmark(MEU + j, A.K_UNPACK))
        r.rowreduce("VADD", EB, TB, [GB, GB], MB, VB, 1)
        r.op("VINV", VB, VB, vl=16, chk=((1, 1), (0, 1), (0, 1), (2, 1)))
        for i, e in enumerate(EB):
            r.bcast("VMUL", e, e, VB, 2, i)
        r.raw(A.vsync(A.W_P, A.K_DRAIN, slack=1))  # OUT[j] drained 2 tiles ago
        for i, e in enumerate(EB):
            r.pack(e, OUT[j] + 8 * i)
        r.raw(A.vmark(MOP + j, A.K_PACK))
        r.raw(A.vsync(A.W_D, mark=MOP + j))
        r.raw(A.vdrain(1, OUT[j], rel=True))
        return r.ops

    def build(addr):
        k = Kernel2()
        em = Em(k)
        tb = 8 * 8 * 32
        k.desc(0, addr["x"], dim(32, 64))
        k.desc(1, addr["out"], dim(32, 64))
        k.seti(0, e8m15(LOG2E))
        k.emit(A.ainc(0, tb), A.ainc(1, tb), A.vfill(0, XIN[0], rel=True))
        schedule([half1(0, True)], em)
        # Steps k = 1..nt in pairs, so buffer parity is fixed per position.
        # Step nt's half 1 runs on the fill past the input and is discarded.
        em.forget()
        body = Kernel2()
        eb = Em(body)
        schedule([half1(1, True), half2(0)], eb)
        schedule([half1(0, True), half2(1)], eb)
        k.seti(5, nt // 2)
        k.emit(A.vloop(5, len(body.words)), *body_words(body), A.vhalt())
        return k

    return build, {"x": x}, ref


def softmax(rows=128, cols=128, sched=1, off=0):
    """Row softmax, tiles of 16 rows (one register a row): scale, row max,
    EXP2D, row sum, INV, scale -- 5 lane-ops an element."""
    assert cols == 128 and rows % 16 == 0
    rng = np.random.default_rng(3)
    x = _f16(rng.standard_normal((rows, cols)) * 2)
    nt = rows // 16
    Y = list(range(16))
    T = list(range(16, 24))
    G = [24, 25]
    GM, VR = 26, 27

    def ref():
        v = x.astype(np.float64)
        e = np.exp(v - v.max(1, keepdims=True))
        return e / e.sum(1, keepdims=True)

    def tile(rec, y, t, g, gm, vr):
        for r in y:
            rec.op("VMUL", r, r, 0, sb=A.SRC_S)
        rec.rowreduce("VMAX", y, t, [g, g] if len(y) <= 8 else g, gm, vr, 0)
        for i, r in enumerate(y):
            rec.bcast("VEXP2D", r, r, vr, 0, i)
        rec.rowreduce("VADD", y, t, [g, g] if len(y) <= 8 else g, gm, vr, 1)
        rec.op("VINV", vr, vr, vl=16, chk=((1, 1), (0, 1), (0, 1), (2, 1)))
        for i, r in enumerate(y):
            rec.bcast("VMUL", r, r, vr, 2, i)

    def build(addr):
        k = Kernel2()
        Em(k)
        tb = 16 * 8 * 32
        k.desc(0, addr["x"], dim(32, 128))
        k.desc(1, addr["out"], dim(32, 128))
        k.seti(0, e8m15(LOG2E))
        k.seti(5, nt)
        k.emit(A.ainc(0, tb), A.ainc(1, tb), A.vfill(0, 0, rel=True))
        body = Kernel2()
        eb = Em(body)
        body.emit(
            A.vbar(),
            *[A.vunpk(r, r * 8) for r in Y],
            A.vsync(A.W_A, A.K_UNPACK),
            A.vfill(0, 0, rel=True),
            A.vsync(A.W_P, A.K_DRAIN),
        )
        if sched:
            # Two 8-row tiles with disjoint registers, interleaved.
            sa, sb = Rec(), Rec()
            tile(sa, Y[:8], [16, 17, 18, 19], 20, 21, 22)
            tile(sb, Y[8:], [23, 24, 25, 26], 27, 28, 29)
            for i, r in enumerate(Y[:8]):
                sa.pack(r, 256 + r * 8)
            for i, r in enumerate(Y[8:]):
                sb.pack(r, 256 + r * 8)
            schedule([sa.ops, sb.ops], eb, [0, off])
        else:
            tile(eb, Y, T, G, GM, VR)
            body.emit(*[A.vpack(r, 256 + r * 8) for r in Y])
        body.emit(A.vsync(A.W_D, A.K_PACK), A.vdrain(1, 256, rel=True))
        bw = [body.words[i] for i in range(len(body.words))]
        k.emit(A.vloop(5, len(bw)), *bw, A.vhalt())
        return k

    return build, {"x": x}, ref


def layernorm(rows=128, cols=128, off=0, dbuf=1):
    """Row layernorm with gamma/beta, tiles of 16 rows as two interleaved
    8-row streams. Sum and sum of squares reduce together (x reduces in
    place and is unpacked again); y = ((x - mean) * rstd) * g + b."""
    assert cols == 128 and rows % 16 == 0
    rng = np.random.default_rng(4)
    x = _f16(rng.standard_normal((rows, cols)) * 2 + 0.5)
    g = _f16(rng.standard_normal(cols) * 0.5 + 1)
    b = _f16(rng.standard_normal(cols) * 0.5)
    nt = rows // 16
    GR, BR = 30, 31
    S_INV, S_EPS = 1, 2

    def ref():
        v = x.astype(np.float64)
        y = (v - v.mean(1, keepdims=True)) / np.sqrt(v.var(1, keepdims=True) + 1e-5)
        return y * g.astype(np.float64) + b.astype(np.float64)

    def half(r0, X, Q, gs, gq, vr, xin=0, oat=256, pre=(), post=()):
        rec = Rec()
        rec.ops += list(pre)
        for r in range(8):
            rec.unpk(X[r], xin + (r0 + r) * 8)
        for r in range(8):  # Q[r//2] chunks tb.. = x[0:4]^2 + x[4:8]^2
            tb = 4 * (r % 2)
            rec.op(
                "VMUL",
                Q[r // 2],
                X[r],
                X[r],
                vl=64,
                chk=((4, 1), (4, 1), (0, 1), (tb, 1)),
            )
        for r in range(8):
            tb = 4 * (r % 2)
            rec.op(
                "VFMA",
                Q[r // 2],
                X[r],
                X[r],
                Q[r // 2],
                vl=64,
                chk=((0, 1), (0, 1), (tb, 1), (tb, 1)),
            )
        for r in range(8):
            rec.comb("VADD", X[r], X[r], 0, X[r], 4, 0, 64)
        for r in range(8):
            tb = 4 * (r % 2)
            rec.comb("VADD", X[r], X[r], 0, X[r], 2, 0, 32)
            rec.comb("VADD", Q[r // 2], Q[r // 2], tb, Q[r // 2], tb + 2, tb, 32)
        for r in range(8):
            tb = 4 * (r % 2)
            rec.comb("VADD", gs, X[r], 0, X[r], 1, r, 16)
            rec.comb("VADD", gq, Q[r // 2], tb, Q[r // 2], tb + 1, r, 16)
        rec.merge("VADD", gs, gs, gs, vr, 0)
        rec.merge("VADD", gq, gq, gq, vr, 1)

        def sc(mn, d, a, bb=None, c=None, sb=0, scc=0, vb=0, vc=0):
            rec.op(
                mn,
                vr,
                vr,
                vr if bb is not None else vb,
                vr if c is not None else vc,
                sb=sb,
                sc=scc,
                vl=16,
                chk=((a, 1), (bb or 0, 1), (c or 0, 1), (d, 1)),
            )

        sc("VMUL", 2, 0, sb=A.SRC_S, vb=S_INV)  # mean
        sc("VMUL", 3, 1, sb=A.SRC_S, vb=S_INV)  # E[x^2]
        sc("VFNMA", 3, 2, bb=2, c=3)  # var = E[x^2] - mean^2
        sc("VADD", 3, 3, scc=A.SRC_S, vc=S_EPS)
        rec.op("VRSQRT", vr, vr, vl=16, chk=((3, 1), (0, 1), (0, 1), (4, 1)))
        for r in range(8):
            rec.unpk(X[r], xin + (r0 + r) * 8)
        for r in range(8):  # x - mean: c is b's crossbar output
            rec.op(
                "VSUB",
                X[r],
                X[r],
                vr,
                vr,
                vl=128,
                chk=((0, 1), (2, 0), (0, 1), (0, 1)),
                xb=(A.XM_BCAST, r, 3, True, 0, 0),
            )
        for r in range(8):
            rec.bcast("VMUL", X[r], X[r], vr, 4, r)
        for r in range(8):
            rec.op("VFMA", X[r], X[r], GR, BR)
        for r in range(8):
            rec.pack(X[r], oat + (r0 + r) * 8)
        rec.ops += list(post)
        return rec.ops

    def build2(addr):
        """Input and output double-buffered by tile parity j through marks;
        two tiles per loop body, scheduled as four streams."""
        k = Kernel2()
        tb = 16 * 8 * 32
        XIN, OUT = [0, 128], [256, 384]
        MX, MF, MOP = 0, 2, 4  # reloads done, fill dispatched, out packed

        def raw(word, mark=None, need=None):
            return ("x", word, set(), set(), 0, mark, need)

        def setmark(ops, i, name):
            o = ops[i]
            ops[i] = (o[0], o[1], o[2], o[3], o[4], name, o[6])

        def step(t, j):
            pre_a = [
                raw(A.vsync(A.W_A, mark=MX + 1 - j)),
                raw(A.vfill(0, XIN[1 - j], rel=True)),
                raw(A.vmark(MF + 1 - j, A.K_FILL)),
                raw(A.vsync(A.W_U, mark=MF + j)),
            ]
            pre_b = [raw(A.vsync(A.W_U, mark=MF + j))]
            sa = half(
                0,
                list(range(8)),
                [16, 17, 18, 19],
                20,
                21,
                22,
                XIN[j],
                OUT[j],
                pre_a,
            )
            sb = half(
                8,
                list(range(8, 16)),
                [23, 24, 25, 26],
                27,
                28,
                29,
                XIN[j],
                OUT[j],
                pre_b,
            )
            for s, nm in ((sa, "a"), (sb, "b")):
                us = [i for i, o in enumerate(s) if o[0] == "u"]
                setmark(s, us[-1], f"rl{nm}{t}")
                ps = [i for i, o in enumerate(s) if o[0] == "p"]
                s.insert(ps[0], raw(A.vsync(A.W_P, A.K_DRAIN, slack=1)))
                setmark(s, len(s) - 1, f"pk{nm}{t}")
            sb.append(raw(A.vmark(MX + j, A.K_UNPACK), need=f"rla{t}"))
            sb.append(raw(A.vmark(MOP + j, A.K_PACK), need=f"pka{t}"))
            sb.append(raw(A.vsync(A.W_D, mark=MOP + j)))
            sb.append(raw(A.vdrain(1, OUT[j], rel=True)))
            return sa, sb

        k.desc(0, addr["x"], dim(32, 128))
        k.desc(1, addr["out"], dim(32, 128))
        k.desc(2, addr["gb"], dim(32, 16))
        k.seti(S_INV, e8m15(1 / cols))
        k.seti(S_EPS, e8m15(1e-5))
        k.seti(5, nt // 2)
        k.emit(
            A.vfill(2, 384),
            A.vbar(),
            A.vunpk(GR, 384),
            A.vunpk(BR, 392),
            A.vsync(A.W_FE, A.K_UNPACK),
            A.ainc(0, tb),
            A.ainc(1, tb),
            A.vfill(0, XIN[0], rel=True),
            A.vmark(MF, A.K_FILL),
        )
        body = Kernel2()
        a0, b0 = step(0, 0)
        a1, b1 = step(1, 1)
        # Tile 1's streams start once tile 0's same-register stream is emitted.
        a1[0] = a1[0][:6] + ("pka0",)
        b1[0] = b1[0][:6] + ("pkb0",)
        schedule([a0, b0, a1, b1], Em(body), [0, off, 0, off])
        k.emit(A.vloop(5, len(body.words)), *body_words(body), A.vhalt())
        return k

    def build(addr):
        k = Kernel2()
        tb = 16 * 8 * 32
        k.desc(0, addr["x"], dim(32, 128))
        k.desc(1, addr["out"], dim(32, 128))
        k.desc(2, addr["gb"], dim(32, 16))
        k.seti(S_INV, e8m15(1 / cols))
        k.seti(S_EPS, e8m15(1e-5))
        k.seti(5, nt)
        k.emit(
            A.vfill(2, 128),
            A.vbar(),
            A.vunpk(GR, 128),
            A.vunpk(BR, 136),
            A.ainc(0, tb),
            A.ainc(1, tb),
            A.vfill(0, 0, rel=True),
        )
        body = Kernel2()
        body.emit(A.vbar(), A.vsync(A.W_P, A.K_DRAIN))
        sa = half(0, list(range(8)), [16, 17, 18, 19], 20, 21, 22)
        sb = half(8, list(range(8, 16)), [23, 24, 25, 26], 27, 28, 29)
        # The next tile's fill waits for this tile's second unpack.
        sa.append(("x", A.vsync(A.W_A, A.K_UNPACK), set(), set(), 0, "fillok", "unpk2"))
        sa.append(("x", A.vfill(0, 0, rel=True), set(), set(), 0, None, None))
        sb_last_unpk = max(i for i, o in enumerate(sb) if o[0] == "u")
        o = sb[sb_last_unpk]
        sb[sb_last_unpk] = (o[0], o[1], o[2], o[3], o[4], "unpk2", o[6])
        schedule([sa, sb], Em(body), [0, off])
        body.emit(A.vsync(A.W_D, A.K_PACK), A.vdrain(1, 256, rel=True))
        k.emit(A.vloop(5, len(body.words)), *body_words(body), A.vhalt())
        return k

    return (build2 if dbuf else build), {"x": x, "gb": np.concatenate([g, b])}, ref


def _subtile(m):
    """(4*gm, 64) -> fp16 words in sub-tile layout: word b*16 + c holds rows
    4b..4b+3, columns 4c..4c+3, lane 4i + k."""
    gm = m.shape[0] // 4
    return m.reshape(gm, 4, 16, 4).transpose(0, 2, 1, 3).reshape(-1)


def _quant_order(p):
    """(32, 64) P -> the quantiser's words: word ((h//2)*4 + i)*2 + h%2 of
    band b is row 4b+i, keys 16h..16h+15."""
    q = np.zeros((8, 16, 16))
    for b in range(8):
        for h in range(4):
            for i in range(4):
                q[b, ((h // 2) * 4 + i) * 2 + h % 2] = p[
                    4 * b + i, 16 * h : 16 * h + 16
                ]
    return q.reshape(-1)


ATT_NEG = -60000.0


def attn(gm=8, blocks=4, check="o", off=0, pipe=1, unroll=2, qb=1):
    """Flash-attention vector work for one 32-row query block (the v1
    algorithm), with S_j and PV_j given in sub-tile layout, as vl1's `attn`:
    per block m' = max(m, rowmax S), P = 2^(S - floor m'), l = l*corr +
    rowsum P, O = O*corr + PV with corr = 2^(floor m - floor m'); then O / l.

    Registers: the two 16-row halves run as two streams, each with eight
    plane registers (S, then P, then PV), four reduce temporaries and two
    stats registers of its own; O lives in L1 and streams through the
    temporaries and two more registers. check=p drains every block's P
    (quantiser order) to `out` instead of O."""
    assert gm == 8
    rng = np.random.default_rng(5)
    s = [_f16(rng.standard_normal((32, 64)) * 3) for _ in range(blocks)]
    pv = [_f16(rng.standard_normal((32, 64))) for _ in range(blocks)]
    S_BUF, O_BUF, PV_BUF, P_BUF = 0, 128, 256, 384
    # Per half: ST0 chunks m, l, ie, corr at q, 2+q, 4+q, 6+q; ST1 the new
    # row max, row sum and e at the same chunks.
    ST0S, ST1S, OSS = (24, 26), (25, 27), ((28, 29), (30, 31))
    M_SF, M_SU, M_PP, M_PVU, M_PVF, M_OP = 0, 1, 2, 3, 4, 5
    S_NEG = 1
    GT = (1, 16)  # plane walk: 4 sub-tile columns, 2 bands

    def ref():
        # Offsets are floor(m): PV_j stands for P_j @ V_j with that P.
        m = np.full((32, 1), ATT_NEG)
        lsum = np.zeros((32, 1))
        o = np.zeros((32, 64))
        ps = []
        for j in range(blocks):
            sj = s[j].astype(np.float64)
            mn = np.maximum(m, sj.max(1, keepdims=True))
            p = np.exp2(sj - np.floor(mn))
            ps.append(_quant_order(p))
            corr = np.exp2(np.floor(m) - np.floor(mn))
            lsum = lsum * corr + p.sum(1, keepdims=True)
            o = o * corr + pv[j].astype(np.float64)
            m = mn
        if check == "p":
            return np.concatenate(ps)
        return _subtile(o / lsum).astype(np.float64)

    def raw(word, mark=None, need=None):
        return ("x", word, set(), set(), 0, mark, need)

    def plane(q, gg, h):
        """L1 offset of plane h of row group 2q+gg in sub-tile layout."""
        return (4 * q + 2 * gg) * 16 + 4 * h

    def half(j, q):
        r = Rec()
        X = {(h, gg): 8 * q + 2 * h + gg for h in range(4) for gg in range(2)}
        T = [16 + 4 * q + i for i in range(4)]
        ST0, ST1 = ST0S[q], ST1S[q]
        os_ = T + list(OSS[q])
        r.raw(A.vsync(A.W_U, mark=M_SF))
        for gg in range(2):
            for h in range(4):
                r.unpk(X[h, gg], S_BUF + plane(q, gg, h), 8, A.GT4, stride=GT)
        r.ops[-1] = r.ops[-1][:5] + (f"su{q}", None)

        def tree(mn):
            for gg in range(2):
                for d, a, b in (
                    (T[2 * gg], X[0, gg], X[1, gg]),
                    (T[2 * gg + 1], X[2, gg], X[3, gg]),
                    (T[2 * gg], T[2 * gg], T[2 * gg + 1]),
                ):
                    if mn == "VADD":
                        r.op(mn, d, a, 0, b)
                    else:
                        r.op(mn, d, a, b)

        tree("VMAX")
        r.merge("VMAX", T[0], T[2], T[0], ST1, q)
        r.op("VMAX", ST0, ST0, ST1, vl=16, chk=((q, 1), (q, 1), (0, 1), (q, 1)))
        r.op(
            "VEXP2D",
            ST1,
            0,
            ST0,
            sa=A.SRC_K,
            vl=16,
            chk=((0, 1), (q, 1), (0, 1), (4 + q, 1)),
        )
        r.op(
            "VMUL",
            ST0,
            ST1,
            ST0,
            vl=16,
            chk=((4 + q, 1), (4 + q, 1), (0, 1), (6 + q, 1)),
        )
        r.op("VINV", ST0, ST1, vl=16, chk=((4 + q, 1), (0, 1), (0, 1), (4 + q, 1)))
        for gg in range(2):
            for h in range(4):
                r.op(
                    "VEXP2D",
                    X[h, gg],
                    X[h, gg],
                    ST0,
                    chk=((0, 1), (q, 0), (0, 1), (0, 1)),
                    xb=(A.XM_BCAST, 8 * gg, 0, False, 0, 0),
                )
        r.raw(A.vsync(A.W_P, A.K_DRAIN))
        for gg in range(2):
            for h in range(4):
                base = (4 * q + 2 * gg) * 16 + (h // 2) * 8 + h % 2
                r.pack(X[h, gg], P_BUF + base, 8, A.F16, stride=(2, 16))
        r.ops[-1] = r.ops[-1][:5] + (f"pp{q}", None)
        tree("VADD")
        r.merge("VADD", T[0], T[2], T[0], ST1, 2 + q)
        r.op(
            "VFMA",
            ST0,
            ST0,
            ST0,
            ST1,
            vl=16,
            chk=((2 + q, 1), (6 + q, 1), (2 + q, 1), (2 + q, 1)),
        )
        r.raw(A.vsync(A.W_U, mark=M_PVF))
        for gg in range(2):
            for h in range(4):
                r.unpk(X[h, gg], PV_BUF + plane(q, gg, h), 8, A.GT4, stride=GT)
        r.ops[-1] = r.ops[-1][:5] + (f"pvu{q}", None)
        r.raw(A.vsync(A.W_U, mark=M_OP + q))
        for i, (gg, h) in enumerate((gg, h) for gg in range(2) for h in range(4)):
            o = os_[i % len(os_)]
            r.unpk(o, O_BUF + plane(q, gg, h), 8, A.GT4, stride=GT)
            r.op(
                "VFMA",
                o,
                o,
                ST0,
                X[h, gg],
                chk=((0, 1), (6 + q, 0), (0, 1), (0, 1)),
                xb=(A.XM_BCAST, 8 * gg, 0, False, 0, 0),
            )
            r.pack(o, O_BUF + plane(q, gg, h), 8, A.GT4, stride=GT)
        r.raw(A.vmark(M_OP + q, A.K_PACK))
        r.ops[-1] = r.ops[-1][:5] + (f"op{q}", None)
        if q == 1:
            # The L1 buffers' next users, once both halves are through.
            k = r.ops
            ins = next(i for i, o in enumerate(k) if o[5] == "su1") + 1
            k[ins:ins] = [
                raw(A.vmark(M_SU, A.K_UNPACK), need="su0"),
                raw(A.vsync(A.W_A, mark=M_SU)),
                raw(A.vfill(0, S_BUF, rel=True)),
                raw(A.vmark(M_SF, A.K_FILL)),
            ]
            ins = next(i for i, o in enumerate(k) if o[5] == "pp1") + 1
            k[ins:ins] = [
                raw(A.vmark(M_PP, A.K_PACK), need="pp0"),
                raw(A.vsync(A.W_D, mark=M_PP)),
                raw(A.vdrain(2, P_BUF, rel=True)),
            ]
            ins = next(i for i, o in enumerate(k) if o[5] == "pvu1") + 1
            k[ins:ins] = [
                raw(A.vmark(M_PVU, A.K_UNPACK), need="pvu0"),
                raw(A.vsync(A.W_A, mark=M_PVU)),
                raw(A.vfill(1, PV_BUF, rel=True)),
                raw(A.vmark(M_PVF, A.K_FILL)),
            ]
        return r.ops

    def final(q):
        r = Rec()
        ST0, ST1 = ST0S[q], ST1S[q]
        os_ = [16 + 4 * q + i for i in range(4)] + list(OSS[q])
        r.op("VINV", ST1, ST0, vl=16, chk=((2 + q, 1), (0, 1), (0, 1), (q, 1)))
        r.raw(A.vsync(A.W_U, mark=mop + q))
        for i, (gg, h) in enumerate((gg, h) for gg in range(2) for h in range(4)):
            o = os_[i % len(os_)]
            r.unpk(o, O_BUF + plane(q, gg, h), 8, A.GT4, stride=GT)
            r.op(
                "VMUL",
                o,
                o,
                ST1,
                chk=((0, 1), (q, 0), (0, 1), (0, 1)),
                xb=(A.XM_BCAST, 8 * gg, 0, False, 0, 0),
            )
            r.pack(o, O_BUF + plane(q, gg, h), 8, A.GT4, stride=GT)
        return r.ops

    # Pipelined layout: S_j and then P_j share SP[j % 2] (S is all in
    # registers before P is packed over it), so S is double-buffered and the
    # fill of S_{j+2} follows the drain of P_j in the DMA queue.
    SP = (0, 256)
    PV_B = 384
    QB = qb
    MP_SF, MP_SU, MP_PP, MP_PVU, MP_PVF, MP_OP = 0, 2, 3, 4, 5, 6
    mop = MP_OP if pipe else M_OP

    def streams(u, last_of=None):
        """Block u of a body as four streams: per half q, A (S, max, P, sum)
        and B (O += corr * PV through staging pairs). `last_of` names the
        previous block's marks a stream must follow, register by register."""
        out = []
        par = u % 2
        for q in range(2):
            X = {(h, gg): 8 * q + 2 * h + gg for h in range(4) for gg in range(2)}
            T = [16 + 4 * q + i for i in range(4)]
            ST0, ST1 = ST0S[q], ST1S[q]
            prev = last_of is not None
            a = Rec()
            a.raw(A.vsync(A.W_U, mark=MP_SF + par))
            for gg in range(2):
                for h in range(4):
                    a.unpk(X[h, gg], SP[par] + plane(q, gg, h), 8, A.GT4, stride=GT)
            if prev:  # X after its last reader: the l update ends A
                a.ops[1] = a.ops[1][:6] + (f"sum{q}{last_of}",)
            a.ops[-1] = a.ops[-1][:5] + (f"su{q}{u}", None)
            first = True
            for gg in range(2):
                for d, x0, x1 in (
                    (T[2 * gg], X[0, gg], X[1, gg]),
                    (T[2 * gg + 1], X[2, gg], X[3, gg]),
                    (T[2 * gg], T[2 * gg], T[2 * gg + 1]),
                ):
                    a.op("VMAX", d, x0, x1)
                    if first and last_of is not None:
                        a.ops[-1] = a.ops[-1][:6] + (f"ob{q}{last_of}",)
                    first = False
            a.merge("VMAX", T[0], T[2], T[0], ST1, q)
            a.op("VMAX", ST0, ST0, ST1, vl=16, chk=((q, 1), (q, 1), (0, 1), (q, 1)))
            a.op(
                "VEXP2D",
                ST1,
                0,
                ST0,
                sa=A.SRC_K,
                vl=16,
                chk=((0, 1), (q, 1), (0, 1), (4 + q, 1)),
            )
            a.op(
                "VMUL",
                ST0,
                ST1,
                ST0,
                vl=16,
                chk=((4 + q, 1), (4 + q, 1), (0, 1), (6 + q, 1)),
            )
            a.ops[-1] = a.ops[-1][:5] + (f"corr{q}{u}", None)
            a.op("VINV", ST0, ST1, vl=16, chk=((4 + q, 1), (0, 1), (0, 1), (4 + q, 1)))
            for gg in range(2):
                for h in range(4):
                    a.op(
                        "VEXP2D",
                        X[h, gg],
                        X[h, gg],
                        ST0,
                        chk=((0, 1), (q, 0), (0, 1), (0, 1)),
                        xb=(A.XM_BCAST, 8 * gg, 0, False, 0, 0),
                    )
            # P over S_u: both halves' S unpacks first.
            a.raw(A.vsync(A.W_P, mark=MP_SU), need=f"msu{u}")
            for gg in range(2):
                for h in range(4):
                    base = (4 * q + 2 * gg) * 16 + (h // 2) * 8 + h % 2
                    a.pack(X[h, gg], SP[par] + base, 8, A.F16, stride=(2, 16))
            a.ops[-1] = a.ops[-1][:5] + (f"pp{q}{u}", None)
            for gg in range(2):  # row sum in place: P is packed
                a.op("VADD", X[0, gg], X[0, gg], 0, X[1, gg])
                a.op("VADD", X[2, gg], X[2, gg], 0, X[3, gg])
                a.op("VADD", X[0, gg], X[0, gg], 0, X[2, gg])
            a.merge("VADD", X[0, 0], X[0, 1], X[0, 0], ST1, 2 + q)
            a.op(
                "VFMA",
                ST0,
                ST0,
                ST0,
                ST1,
                vl=16,
                chk=((2 + q, 1), (6 + q, 1), (2 + q, 1), (2 + q, 1)),
            )
            a.ops[-1] = a.ops[-1][:5] + (f"sum{q}{u}", None)

            b = Rec()
            st = T + list(OSS[q])
            # The O update runs on unpack / pack queue 1 (QB): waiting for PV
            # it does not hold the S and P traffic on queue 0.
            b.raw(
                A.vsync(A.W_U, mark=MP_PVF, q=QB),
                need=f"pvf{last_of}" if prev else None,
            )
            b.raw(A.vsync(A.W_U, mark=MP_OP + q, q=QB), need=f"corr{q}{u}")
            for i, (gg, h) in enumerate((gg, h) for gg in range(2) for h in range(4)):
                o, p = st[2 * (i % 3)], st[2 * (i % 3) + 1]
                b.unpk(o, O_BUF + plane(q, gg, h), 8, A.GT4, stride=GT, q=QB)
                b.unpk(p, PV_B + plane(q, gg, h), 8, A.GT4, stride=GT, q=QB)
                b.op(
                    "VFMA",
                    o,
                    o,
                    ST0,
                    p,
                    chk=((0, 1), (6 + q, 0), (0, 1), (0, 1)),
                    xb=(A.XM_BCAST, 8 * gg, 0, False, 0, 0),
                )
                b.pack(o, O_BUF + plane(q, gg, h), 8, A.GT4, stride=GT, q=QB)
            pvu = max(i for i, o in enumerate(b.ops) if o[0] == "u")
            b.ops[pvu] = b.ops[pvu][:5] + (f"pvu{q}{u}", None)
            b.raw(A.vmark(MP_OP + q, A.K_PACK1 if QB else A.K_PACK))
            b.ops[-1] = b.ops[-1][:5] + (f"ob{q}{u}", None)
            out += [a.ops, b.ops]
        a1, b1 = out[2], out[3]

        def after(ops, name, items):
            i = next(n for n, o in enumerate(ops) if o[5] == name) + 1
            ops[i:i] = items

        after(
            a1,
            f"su1{u}",
            [raw(A.vmark(MP_SU, A.K_UNPACK), mark=f"msu{u}", need=f"su0{u}")],
        )
        # P_u out, then S_{u+2} into the same buffer: the DMA queue orders them.
        after(
            a1,
            f"pp1{u}",
            [
                raw(A.vmark(MP_PP, A.K_PACK), need=f"pp0{u}"),
                raw(A.vsync(A.W_D, mark=MP_PP)),
                raw(A.vdrain(2, SP[par], rel=True)),
                raw(A.vsync(A.W_A, A.K_DRAIN)),
                raw(A.vfill(0, SP[par], rel=True)),
                raw(A.vmark(MP_SF + par, A.K_FILL)),
            ],
        )
        after(
            b1,
            f"pvu1{u}",
            [
                raw(
                    A.vmark(MP_PVU, A.K_UNPACK1 if QB else A.K_UNPACK), need=f"pvu0{u}"
                ),
                raw(A.vsync(A.W_A, mark=MP_PVU)),
                raw(A.vfill(1, PV_B, rel=True)),
                raw(A.vmark(MP_PVF, A.K_FILL), mark=f"pvf{u}"),
            ],
        )
        return out

    def build(addr):
        k = Kernel2()
        em = Em(k)
        blk = 128 * 32
        k.desc(0, addr["s"], dim(32, 128))
        k.desc(1, addr["pv"], dim(32, 128))
        k.desc(2, addr["out" if check == "p" else "psink"], dim(32, 128))
        k.desc(3, addr["osink" if check == "p" else "out"], dim(32, 128))
        k.seti(S_NEG, e8m15(ATT_NEG))
        k.emit(A.ainc(0, blk), A.ainc(1, blk), A.ainc(2, blk))
        if pipe:
            k.emit(
                A.vfill(0, SP[0], rel=True),
                A.vmark(MP_SF, A.K_FILL),
                A.vfill(0, SP[1], rel=True),
                A.vmark(MP_SF + 1, A.K_FILL),
                A.vfill(1, PV_B, rel=True),
                A.vmark(MP_PVF, A.K_FILL),
            )
        else:
            k.emit(
                A.vfill(0, S_BUF, rel=True),
                A.vmark(M_SF, A.K_FILL),
                A.vfill(1, PV_BUF, rel=True),
                A.vmark(M_PVF, A.K_FILL),
            )
        for st in ST0S:
            em.op("VMOV", st, 0, sa=A.SRC_K)
            em.op("VMOV", st, S_NEG, sa=A.SRC_S, vl=32, chk=DEF_CHK)
        em.op("VMOV", OSS[0][0], 0, sa=A.SRC_K)
        for i in range(16):
            em.walk("p", A.vpack(OSS[0][0], O_BUF + 8 * i))
        k.emit(A.vmark(mop, A.K_PACK), A.vmark(mop + 1, A.K_PACK))
        body = Kernel2()
        if pipe:
            assert blocks % unroll == 0
            ss = []
            for u in range(unroll):
                ss += streams(u, u - 1 if u else None)
            schedule(ss, Em(body), [0, 0, off, off] * unroll)
            k.seti(5, blocks // unroll)
        else:
            schedule([half(0, 0), half(0, 1)], Em(body), [0, off])
            k.seti(5, blocks)
        k.emit(A.vloop(5, len(body.words)), *body_words(body))
        em.forget()
        schedule([final(0), final(1)], em)
        k.emit(A.vsync(A.W_D, A.K_PACK), A.vdrain(3, O_BUF), A.vhalt())
        return k

    arrays = {
        "s": np.concatenate([_subtile(x) for x in s]),
        "pv": np.concatenate([_subtile(x) for x in pv]),
    }
    sink = np.zeros(blocks * 128 * 16 if check == "o" else 128 * 16, np.float16)
    arrays["psink" if check == "o" else "osink"] = sink
    return build, arrays, ref


def attn_om(blocks=6, rs=1, drift=0.0, reuse=0, seed=7):
    """Flash-attention vector work with O accumulated on the mat: one 32-row
    query tile, 64-key blocks, S_j given in sub-tile layout.

    Per block P = 2^(S - I), I an integer per row, I = floor(row max of S_0).
    EXP2D's sticky bit (a P reached 2^8) closes the segment at block j: the
    rows whose max P reached 2^8 move I by floor(log2 max P), P scales by
    corr = 2^(I_old - I_new), and the mat's segment O_s (slot s here) folds
    into acc = (acc + O_s) * corr, as does l. Row sums on the vector (rs=1)
    or from the mat's ones column (rs=0: l_s beside O_s). The end folds the
    open segment and writes O = acc / l.

    `out` = every block's P (quantiser order; with `reuse`, the one block
    repeated) then O (sub-tile layout). `reuse` reads one S block every time,
    for cycle counts over many blocks."""
    assert blocks % 2 == 0
    rng = np.random.default_rng(seed)
    nb = blocks
    ns = 1 if reuse else nb
    rise = drift * rng.uniform(0.0, 1.0, (32, 1))
    s = [_f16(rng.standard_normal((32, 64)) * 2 + j * rise) for j in range(ns)]
    v = [rng.standard_normal((64, 64)) / 8 for _ in range(ns)]

    # The reference runs the kernel's schedule: segments, slots, P and O.
    sf = [x.astype(np.float64) for x in s]
    I = np.floor(sf[0].max(1, keepdims=True))
    segs = [[]]
    ps = []
    lvec = np.zeros((32, 1))
    lacc = np.zeros((32, 1))
    corrs = []
    for j in range(nb):
        x = sf[j % ns]
        if (x - I >= 8).any():
            top = np.floor(x.max(1, keepdims=True))
            inew = np.where(x.max(1, keepdims=True) - I >= 8, top, I)
            corrs.append((np.exp2(I - inew), lvec))
            segs.append([])
            lvec = np.zeros((32, 1))
            I = inew
        p = np.exp2(x - I)
        p16 = _f16(p).astype(np.float64)
        ps.append(p16)
        segs[-1].append((j, p16))
        lvec = lvec + p.sum(1, keepdims=True)
    oslot = [_f16(sum(p @ v[j % ns] for j, p in sg)) for sg in segs]
    lslot = [_f16(sum(p.sum(1) for _, p in sg)) for sg in segs]
    acc = np.zeros((32, 64))
    for k, (corr, lv) in enumerate(corrs):
        lk = lv if rs else lslot[k].astype(np.float64)[:, None]
        acc = (acc + oslot[k].astype(np.float64)) * corr
        lacc = (lacc + lk) * corr
    llast = lvec if rs else lslot[-1].astype(np.float64)[:, None]
    o_ref = (acc + oslot[-1].astype(np.float64)) / (lacc + llast)
    pq = [_quant_order(p) for p in (ps[-1:] if reuse else ps)]
    want = np.concatenate(pq + [_subtile(o_ref)])

    def ref():
        return want

    ref.parts = [("P", sum(x.size for x in pq)), ("O", 2048)]
    ref.info = {"segments": len(segs), "trips": len(corrs)}

    SB, PB = (0, 128), (256, 384)
    ME, MS, MU, MP, MF = 0, (1, 2), (3, 4), 5, (6, 7)
    ST, RSUM, R26, T, Z = 24, 25, 26, [27, 28, 29, 30], 31
    F = list(range(16, 24))
    RT = T + F[:4]  # row-sum temporaries, two per (half, row group)
    S_MAGIC, S_THR, S_STK, S_LOOP, S_TRIP, S_ZERO = 1, 2, 3, 5, 6, 7
    GT = (1, 16)
    QB = 64 * 32  # one acc quarter, bytes

    def X(q, h, gg):
        return 8 * q + 2 * h + gg

    def plane(q, gg, h):
        return (4 * q + 2 * gg) * 16 + 4 * h

    def tree2(em, mn, dst):
        """dst chunk q lane r = the reduction of row r of half q, both halves
        in step."""

        def op(d, a, b):
            if mn == "VADD":
                em.op(mn, d, a, 0, b)
            else:
                em.op(mn, d, a, b)

        for q in range(2):
            for gg in range(2):
                i = 4 * q + 2 * gg
                op(RT[i], X(q, 0, gg), X(q, 1, gg))
                op(RT[i + 1], X(q, 2, gg), X(q, 3, gg))
        for i in (0, 2, 4, 6):
            op(RT[i], RT[i], RT[i + 1])
        merge2(
            em, mn, [(RT[4 * q], RT[4 * q + 2], RT[4 * q], dst, q) for q in range(2)]
        )

    def merge2(em, mn, groups, steps=(0, 1, 2, 3)):
        """Em.merge over several (g0, g1, gm, dst, dchunk) groups, step by step,
        so each step's latency hides behind the other groups."""
        xc = mn == "VADD"
        for st in steps:
            for g0, g1, gm, dst, dch in groups:
                if st == 0:
                    em.op(
                        mn,
                        gm,
                        g0,
                        g1,
                        g1,
                        vl=128,
                        chk=DEF_CHK,
                        xb=(A.XM_MERGE, 8, 0, xc, 0, 0),
                    )
                elif st < 3:
                    kk, vl = ((4, 64), (2, 32))[st - 1]
                    em.op(
                        mn,
                        gm,
                        gm,
                        gm,
                        gm,
                        vl=vl,
                        chk=((0, 1), (kk, 1), (kk, 1), (0, 1)),
                        xb=(A.XM_MERGE, kk, 0, xc, 0, 0),
                    )
                else:
                    em.op(
                        mn,
                        dst,
                        gm,
                        gm,
                        gm,
                        vl=16,
                        chk=((0, 1), (1, 1), (1, 1), (dch, 1)),
                        xb=(A.XM_MERGE, 1, 0, xc, 0, 0),
                    )

    def lane_op(em, mn, vd, va, vb=0, vc=0, ch=(0, 0, 0, 0), sa=0, sb=0, sc=0, pm=0):
        """A two-beat op over chunks 0..1 (one per half) of each operand."""
        em.op(
            mn,
            vd,
            va,
            vb,
            vc,
            sa=sa,
            sb=sb,
            sc=sc,
            vl=32,
            chk=tuple((c, 1) for c in ch),
            xb=(A.XM_NONE, 0, 0, False, pm, 0),
        )

    def bcast_rows(em, mn, vd, va, chunk, gg, vc=0):
        em.op(
            mn,
            vd,
            va,
            ST,
            vc,
            chk=((0, 1), (chunk, 0), (0, 1), (0, 1)),
            xb=(A.XM_BCAST, 8 * gg, 0, False, 0, 0),
        )

    def exp2d(em):
        for q in range(2):
            for gg in range(2):
                for h in range(4):
                    bcast_rows(em, "VEXP2D", X(q, h, gg), X(q, h, gg), q, gg)

    def floor_to_int(em, src, dst):
        """R26 chunks dst.. = floor(R26 chunks src..), exactly: x - log2(2^frac),
        rounded to an integer by +/- 1.5 * 2^15."""
        lane_op(em, "VEXP2D", R26, R26, R26, ch=(src, src, 0, 6))
        lane_op(em, "VLOG2", R26, R26, ch=(6, 0, 0, 6))
        lane_op(em, "VSUB", R26, R26, 0, R26, ch=(src, 0, 6, dst))
        lane_op(em, "VADD", R26, R26, 0, S_MAGIC, sc=A.SRC_S, ch=(dst, 0, 0, dst))
        lane_op(em, "VSUB", R26, R26, 0, S_MAGIC, sc=A.SRC_S, ch=(dst, 0, 0, dst))

    def quarter_walk(k, word, ad, qi):
        """A rel fill/drain over the four acc quarters; the fourth steps the
        descriptor's offset back to quarter 0."""
        if qi == 3:
            k.emit(A.ainc(ad, -3 * QB))
        k.emit(word)
        if qi == 3:
            k.emit(A.ainc(ad, QB))

    def unpack_s(em, par):
        k = em.k
        k.emit(A.vsync(A.W_U, mark=MS[par]))
        for q in range(2):
            for gg in range(2):
                for h in range(4):
                    em.walk(
                        "u",
                        A.vunpk(X(q, h, gg), SB[par] + plane(q, gg, h), 8, A.GT4),
                        GT,
                    )
        # S_{j+2} into this buffer once these unpacks are through.
        k.emit(
            A.vmark(MU[par], A.K_UNPACK),
            A.vsync(A.W_A, mark=MU[par]),
            A.vfill(0, SB[par], rel=True),
            A.vmark(MS[par], A.K_FILL),
        )

    def fold(em, regions, last, acc=True):
        """acc quarters += O slot quarters; then * corr (a close) or / l and
        packed to `out` (the end; without `acc`, O alone). Quarters
        alternate between two L1 regions, so the next quarter's fills run
        beside this one's work."""
        k = em.k
        quarters = [(q, gg) for q in range(2) for gg in range(2)]

        def fetch(qi):
            reg = regions[qi % 2]
            if acc:
                quarter_walk(k, A.vfill(6, reg, rel=True), 6, qi)
            k.emit(A.vfill(5, reg + 64, rel=True), A.vmark(MF[qi % 2], A.K_FILL))

        fetch(0)
        fetch(1)
        for qi, (q, gg) in enumerate(quarters):
            reg = regions[qi % 2]
            k.emit(A.vsync(A.W_U, mark=MF[qi % 2]))
            if acc:
                for h in range(4):
                    em.walk("u", A.vunpk(F[h], reg + 16 * h, 8, A.F32), (1, 4))
            for h in range(4):
                em.walk("u", A.vunpk(F[4 + h], reg + 64 + 4 * h, 8, A.GT4), GT)
            for h in range(4):
                if acc:
                    em.op("VADD", F[h], F[h], 0, F[4 + h])
                else:
                    em.op("VMOV", F[h], F[4 + h])
            for h in range(4):
                bcast_rows(em, "VMUL", F[h], F[h], 6 + q, gg)
            if last:
                for h in range(4):
                    em.walk("p", A.vpack(F[h], PB[1] + plane(q, gg, h), 8, A.GT4), GT)
            else:
                for h in range(4):
                    em.walk("p", A.vpack(F[h], reg + 16 * h, 8, A.F32), (1, 4))
                k.emit(A.vsync(A.W_D, A.K_PACK))
                quarter_walk(k, A.vdrain(3, reg, rel=True), 3, qi)
            if qi + 2 < 4:
                k.emit(A.vsync(A.W_A, A.K_UNPACK if last else A.K_DRAIN))
                fetch(qi + 2)

    def l_slot(em, at):
        """R26 chunks 2..3 = the mat's row sums for this segment (rs=0)."""
        em.k.emit(A.vfill(7, at, rel=True), A.vbar())
        em.walk("u", A.vunpk(R26, at, 2, A.F16, cb=2), (1, 4))

    def handler(par):
        """The close at block j: sticky set by P_j's EXP2D."""
        hk = Kernel2()
        hk.seti(S_TRIP, e8m15(1.0))
        em = Em(hk)
        tree2(em, "VMAX", R26)  # max P per row
        lane_op(em, "VCMPGT", R26, R26, S_THR, sb=A.SRC_S, ch=(0, 0, 0, 0))
        lane_op(em, "VLOG2", R26, R26, ch=(0, 0, 0, 2))  # log2 max P
        floor_to_int(em, 2, 2)  # d = floor(log2 max P)
        lane_op(em, "VMOV", R26, ST, ch=(0, 0, 0, 4))  # I_old
        lane_op(em, "VADD", ST, ST, 0, R26, ch=(0, 0, 2, 0), pm=1)  # I += d where P0
        lane_op(em, "VEXP2D", ST, R26, ST, ch=(4, 0, 0, 6))  # corr = 2^(I_old - I_new)
        if rs:
            lane_op(em, "VADD", ST, ST, 0, ST, ch=(4, 0, 2, 4))
            lane_op(em, "VMOV", ST, 0, sa=A.SRC_K, ch=(0, 0, 0, 2))
        else:
            hk.emit(A.vsync(A.W_A, A.K_DRAIN, slack=1))
            l_slot(em, PB[par] + 96)
            lane_op(em, "VADD", ST, ST, 0, R26, ch=(4, 0, 2, 4))
        lane_op(em, "VMUL", ST, ST, ST, ch=(4, 6, 0, 4))  # l_acc *= corr
        hk.emit(A.vsync(A.W_A, A.K_DRAIN))  # P_{j-2}, P_{j-1} are out
        fold(em, (PB[par], PB[1 - par]), False)
        hk.emit(A.vsync(A.W_P, A.K_DRAIN))
        for q in range(2):  # P_j *= corr
            for gg in range(2):
                for h in range(4):
                    bcast_rows(em, "VMUL", X(q, h, gg), X(q, h, gg), 6 + q, gg)
        return [hk.words[i] for i in range(len(hk.words))]

    def pq_at(q, gg, h):
        """P's word for register (q, h, gg) chunk 0 in quantiser order; the
        walk is (2, 16)."""
        return (4 * q + 2 * gg) * 16 + (h // 2) * 8 + h % 2

    def step(em, j):
        """check(j) and the close; then per register: P_j out, S_{j+1} in,
        P_{j+1}; then the row sum of P_j, read back from L1."""
        k = em.k
        par, nxt = j % 2, 1 - j % 2
        hw = handler(par)
        k.emit(A.vsync(A.W_FE, mark=ME), A.getstk(S_STK), A.vskipz(S_STK, len(hw)), *hw)
        em.forget()
        k.emit(A.vsync(A.W_P, A.K_DRAIN, slack=1))
        if rs:
            # Row sum, first level, while P_j is still in its registers.
            for q in range(2):
                for gg in range(2):
                    t0, t1 = RT[4 * q + 2 * gg], RT[4 * q + 2 * gg + 1]
                    em.op("VADD", t0, X(q, 0, gg), 0, X(q, 1, gg))
                    em.op("VADD", t1, X(q, 2, gg), 0, X(q, 3, gg))
        # The rest of the row sum rides between P_{j+1}'s EXP2Ds, where math
        # would wait for S_{j+1}; its last step follows them.
        groups = [(RT[4 * q], RT[4 * q + 2], RT[4 * q], RSUM, q) for q in range(2)]
        rest = {}
        if rs:
            rest[3] = lambda: [
                em.op("VADD", RT[i], RT[i], 0, RT[i + 1]) for i in (0, 2, 4, 6)
            ]
            rest[7] = lambda: merge2(em, "VADD", groups, steps=(0,))
            rest[9] = lambda: merge2(em, "VADD", groups, steps=(1,))
            rest[11] = lambda: merge2(em, "VADD", groups, steps=(2,))
        k.emit(A.vsync(A.W_U, mark=MS[nxt]))
        r = 0
        for q in range(2):
            for gg in range(2):
                for h in range(4):
                    x = X(q, h, gg)
                    em.walk(
                        "p", A.vpack(x, PB[par] + pq_at(q, gg, h), 8, A.F16), (2, 16)
                    )
                    em.walk("u", A.vunpk(x, SB[nxt] + plane(q, gg, h), 8, A.GT4), GT)
                    bcast_rows(em, "VEXP2D", x, x, q, gg)
                    if r in rest:
                        rest[r]()
                    r += 1
        k.emit(
            A.vmark(MP, A.K_PACK),
            A.vsync(A.W_D, mark=MP),
            A.vdrain(2, PB[par], rel=True),
            A.vmark(MU[nxt], A.K_UNPACK),
            A.vsync(A.W_A, mark=MU[nxt]),
            A.vfill(0, SB[nxt], rel=True),
            A.vmark(MS[nxt], A.K_FILL),
            A.vmark(ME, A.K_MATH),
        )
        if rs:
            merge2(em, "VADD", groups, steps=(3,))
            lane_op(em, "VADD", ST, ST, 0, RSUM, ch=(2, 0, 0, 2))

    def build(addr):
        k = Kernel2()
        em = Em(k)
        k.desc(0, addr["s"], dim(32, 128))
        k.desc(2, addr["out"], dim(32, 128))
        k.desc(3, addr["acc"], dim(32, 64))
        k.desc(4, addr["out"] + len(pq) * 128 * 32, dim(32, 128))
        k.desc(5, addr["oslot"], dim(32, 32))
        k.desc(6, addr["acc"], dim(32, 64))
        k.desc(7, addr["lslot"], dim(32, 2))
        k.seti(S_MAGIC, e8m15(49152.0))
        k.seti(S_THR, e8m15(255.5))
        k.seti(S_TRIP, 0)
        k.seti(S_ZERO, 0)
        k.emit(
            A.ainc(0, 0 if reuse else 128 * 32),
            A.ainc(2, 0 if reuse else 128 * 32),
            A.ainc(3, QB),
            A.ainc(5, 32 * 32),
            A.ainc(6, QB),
            A.ainc(7, 8 * 32),
        )
        k.emit(
            A.vfill(0, SB[0], rel=True),
            A.vmark(MS[0], A.K_FILL),
            A.vfill(0, SB[1], rel=True),
            A.vmark(MS[1], A.K_FILL),
        )
        # acc = 0 in memory; l, l_acc, corr = 0.
        em.op("VMOV", Z, 0, sa=A.SRC_K)
        em.op("VMOV", ST, 0, sa=A.SRC_K)
        for i in range(4):
            em.walk("p", A.vpack(Z, PB[1] + 16 * i, 8, A.F32), (1, 4))
        k.emit(A.vsync(A.W_D, A.K_PACK))
        for qi in range(4):
            quarter_walk(k, A.vdrain(3, PB[1], rel=True), 3, qi)
        # Block 0: I = floor(row max S_0), then P_0.
        unpack_s(em, 0)
        tree2(em, "VMAX", R26)
        floor_to_int(em, 0, 2)
        lane_op(em, "VMOV", ST, R26, ch=(2, 0, 0, 0))
        exp2d(em)
        k.emit(A.vmark(ME, A.K_MATH))
        body = Kernel2()
        eb = Em(body)
        step(eb, 0)
        step(eb, 1)
        k.seti(S_LOOP, nb // 2)
        k.emit(A.vloop(S_LOOP, len(body.words)), *body_words(body))
        em.forget()
        # The end: the open segment folds in, O = acc / l.
        k.emit(
            A.vsync(A.W_FE, A.K_FILL),
            A.vsync(A.W_FE, A.K_UNPACK),
            A.vsync(A.W_FE, A.K_DRAIN),
        )
        if rs:
            lane_op(em, "VADD", ST, ST, 0, ST, ch=(4, 0, 2, 4))
        else:
            l_slot(em, 96)
            lane_op(em, "VADD", ST, ST, 0, R26, ch=(4, 0, 2, 4))
        lane_op(em, "VINV", ST, ST, ch=(4, 0, 0, 6))
        # A run that closed no segment has acc = 0: O alone, acc not read.
        with_acc, alone = Kernel2(), Kernel2()
        fold(Em(with_acc), SB, True)
        fold(Em(alone), SB, True, acc=False)
        wa, wo = body_words(with_acc), body_words(alone)
        k.emit(A.vskipz(S_TRIP, len(wa) + 1), *wa, A.vskipz(S_ZERO, len(wo)), *wo)
        k.emit(A.vsync(A.W_D, A.K_PACK), A.vdrain(4, PB[1]), A.vhalt())
        return k

    pad = [np.full((32, 64), -60000.0, np.float16)] * (0 if reuse else 3)
    arrays = {
        "s": np.concatenate([_subtile(x) for x in s + pad]).astype(np.float16),
        "oslot": np.concatenate([_subtile(o) for o in oslot]).astype(np.float16),
        "lslot": np.concatenate(
            [np.concatenate([x, np.zeros(96, np.float16)]) for x in lslot]
        ).astype(np.float16),
        "acc": np.zeros(256 * 16, np.float16),
    }
    return build, arrays, ref


def copy(nw=8, mode=0, pmode=None):
    """L1 -> unpack(mode) -> pack(pmode) -> memory; GT4 both ways is identity."""
    pmode = mode if pmode is None else pmode
    rng = np.random.default_rng(1)
    x = _f16(rng.standard_normal(nw * 16))

    def ref():
        v = x.astype(np.float64).reshape(nw // 4 if (mode or pmode) else nw, -1)
        if mode != pmode:  # one GT4: the granule transpose of each 4-word group
            g = x.reshape(nw // 4, 4, 4, 4)  # group, word, granule, elem
            return g.transpose(0, 2, 1, 3).reshape(-1).astype(np.float64)
        return v.reshape(-1)

    def build(addr):
        k = Kernel2()
        k.desc(0, addr["x"], dim(32, nw))
        k.desc(1, addr["out"], dim(32, nw))
        k.emit(A.vfill(0, 0), A.vbar())
        for r in range(nw // 8):
            k.emit(A.vunpk(r, r * 8, 8, mode))
        for r in range(nw // 8):
            k.emit(A.vpack(r, 256 + r * 8, 8, pmode))
        k.emit(A.vsync(A.W_D, A.K_PACK), A.vdrain(1, 256), A.vhalt())
        return k

    return build, {"x": x}, ref


def stream(fn="silu", n=32768, tile=0, grp=4):
    """Elementwise over n fp16 of any length (a whole number of tile pairs):
    one VLOOP over two `tile`-word tiles, each a rel VFILL into one of two
    input buffers and a rel VDRAIN out of one of two output buffers. `fn`
    silu (5 lane-ops an element) or add (x + y, a second input stream).
    `build_at(x, y, out)` builds it for addresses alone; out may be x (in
    place: a tile drains after its own fill and before nothing reads it)."""
    two = fn == "add"
    tile = tile or (64 if two else 128)
    rng = np.random.default_rng(2)
    x = _f16(rng.standard_normal(n) * 3)
    y = _f16(rng.standard_normal(n) * 3)
    # A RUN's rel DMA offset is signed 18 bits (v2_agu.v): it covers 128 KB of
    # a stream, so a longer stream is several RUNs of one image.
    piece = min(n, RUN_ELEMS)
    if n % piece:
        raise ValueError(f"n {n}: whole RUNs of {piece} elements")
    nw = piece // 16
    nt = nw // tile
    if nw % (2 * tile) or tile % (8 * grp):
        raise ValueError(f"n {n}: tiles of {tile} words in pairs, groups of {grp}")
    gpt = tile // (8 * grp)
    if two:
        ina, inb, outb = [0, tile], [2 * tile, 3 * tile], [4 * tile, 5 * tile]
    else:
        ina, inb, outb = [0, tile], None, [256, 256 + tile]

    def ref():
        xf = x.astype(np.float64)
        return xf + y.astype(np.float64) if two else xf / (1 + np.exp(-xf))

    def ops(s, j):
        """Group slot s (register set), lane j: result in register 16 + ..."""
        xr, yr, t = s * grp + j, 8 + s * grp + j, 16 + s * grp + j
        if two:
            return [A.math("VADD", t, xr, 0, yr)]  # VADD reads a and c
        return [
            A.math("VMUL", t, xr, 0, sb=A.SRC_S),  # t = x * -log2e
            A.math("VEXP2", t, t),
            A.math("VADD", t, t, 0, 1, sc=A.SRC_K),  # t = t + 1
            A.math("VINV", t, t),
            A.math("VMUL", t, xr, t),
        ]

    def build_at(xa, ya, oa):
        k = Kernel2()
        k.desc(0, xa, dim(32, tile))
        k.desc(1, oa, dim(32, tile))
        k.emit(A.ainc(0, tile * 32), A.ainc(1, tile * 32))
        if two:
            k.desc(2, ya, dim(32, tile))
            k.emit(A.ainc(2, tile * 32))
        else:
            k.seti(0, e8m15(-LOG2E))
        k.emit(A.vfill(0, ina[0], rel=True))
        if two:
            k.emit(A.vfill(2, inb[0], rel=True))
        groups = [(p, g) for p in (0, 1) for g in range(gpt)]

        def unpk(i):
            p, g = groups[i]
            out = []
            if g == 0:
                out += [
                    A.vbar(),  # this tile's fills landed
                    A.vsync(A.W_A, A.K_UNPACK),  # the other buffer is read out
                    A.vfill(0, ina[1 - p], rel=True),  # the next tile (one past
                ]  # the end on the last: read, never used)
                if two:
                    out.append(A.vfill(2, inb[1 - p], rel=True))
            s = i % 2
            for j in range(grp):
                w = (g * grp + j) * 8
                out.append(A.vunpk(s * grp + j, ina[p] + w))
                if two:
                    out.append(A.vunpk(8 + s * grp + j, inb[p] + w))
            return out

        body = Kernel2()
        body.emit(*unpk(0))
        for i, (p, g) in enumerate(groups):
            if i + 1 < len(groups):
                body.emit(*unpk(i + 1))
            s = i % 2
            chains = [ops(s, j) for j in range(grp)]
            for st in range(len(chains[0])):
                for j in range(grp):
                    body.emit(chains[j][st])
            if g == 0:
                # this buffer's drain, two tiles back, finished
                body.emit(A.vsync(A.W_P, A.K_DRAIN, slack=1))
            for j in range(grp):
                body.emit(A.vpack(16 + s * grp + j, outb[p] + (g * grp + j) * 8))
            if g == gpt - 1:
                body.emit(A.vsync(A.W_D, A.K_PACK), A.vdrain(1, outb[p], rel=True))
        k.seti(5, nt // 2)
        k.emit(A.vloop(5, len(body.words)), *body_words(body), A.vhalt())
        return k

    def build(addr):
        """One kernel a RUN, in order."""
        step = piece * 2
        x0, y0, o0 = addr["x"], addr.get("y", 0), addr["out"]
        return [
            build_at(x0 + r * step, y0 + r * step if two else 0, o0 + r * step)
            for r in range(n // piece)
        ]

    build.at = build_at
    arrays = {"x": x, "y": y} if two else {"x": x}
    return build, arrays, ref


def mxpack(regs=16, bl=0, seed=5, rounds=1):
    """VPACK MX7 against the Python quantiser: `regs` registers of FP16 in,
    one MXFP7 entry each out (`rounds` passes over them, the last kept), bit
    exact. Blocks span 2^-24..2^15 in magnitude, with zero blocks, subnormal
    blocks and lone peaks, in both packings."""
    rng = np.random.default_rng(seed)
    blocks = []
    for i in range(regs * 4):
        kind = i % 6
        if kind == 0:
            b = np.zeros(32)
        elif kind == 1:
            b = rng.standard_normal(32) * 2.0**-18  # subnormal-heavy
        elif kind == 2:
            b = rng.standard_normal(32) * 0.01
            b[rng.integers(32)] = 3000.0 * rng.choice([-1, 1])  # one peak
        else:
            b = rng.standard_normal(32) * 2.0 ** int(rng.integers(-14, 12))
        blocks.append(b)
    x = _f16(np.clip(np.stack(blocks).reshape(-1), -60000, 60000))
    want = []
    for r in range(regs):
        words = TT.to_mxfp7_words_tiled(
            x[r * 128 : (r + 1) * 128].reshape(4, 32), 1, 1, bl
        )
        want += [w.to_bytes(32, "little") for w in words]
    want = np.frombuffer(b"".join(want), np.float16).copy()

    def ref():
        return want

    ref.exact = True

    def build(addr):
        k = Kernel2()
        k.desc(0, addr["x"], dim(32, regs * 8))
        k.desc(1, addr["out"], dim(32, regs * 4))
        k.emit(A.vfill(0, 0), A.vbar())
        for _ in range(rounds):
            for r in range(regs):
                k.emit(A.vunpk(r, 8 * r))
            for r in range(regs):
                k.emit(A.vpack_mx7(r, 256 + 4 * r, b_layout=bool(bl)))
        k.emit(A.vsync(A.W_D, A.K_PACK), A.vdrain(1, 256), A.vhalt())
        return k

    return build, {"x": x}, ref


def ew(fn="silu", n=8192, tile=128, grp=4):
    """Elementwise over n fp16: tiles of `tile` words double-buffered in L1,
    `grp` registers in flight so dependent ops hide the lane latency."""
    rng = np.random.default_rng(2)
    x = _f16(rng.standard_normal(n) * 3)
    nw = n // 16
    nt = nw // tile
    assert nt <= 4 and tile % (8 * grp) == 0

    def ref():
        xf = x.astype(np.float64)
        if fn == "silu":
            return xf / (1 + np.exp(-xf))
        if fn == "add1":
            return xf + 1
        raise ValueError(fn)

    def ops(xr, t):
        """The op chain for x in register xr, temp t; result in t."""
        if fn == "silu":
            return [
                A.math("VMUL", t, xr, 0, sb=A.SRC_S),  # t = x * -log2e
                A.math("VEXP2", t, t),
                A.math("VADD", t, t, 0, 1, sc=A.SRC_K),  # t = t + 1
                A.math("VINV", t, t),
                A.math("VMUL", t, xr, t),
            ]
        return [A.math("VADD", t, xr, 0, 1, sc=A.SRC_K)]

    def build(addr):
        k = Kernel2()
        for t in range(nt):
            k.desc(t, addr["x"] + t * tile * 32, dim(32, tile))
            k.desc(4 + t, addr["out"] + t * tile * 32, dim(32, tile))
        k.seti(0, e8m15(-LOG2E))
        inb = [0, tile]  # bank 0
        outb = [256, 256 + tile]  # bank 1
        k.emit(A.vfill(0, inb[0]))
        groups = [(t, g) for t in range(nt) for g in range(tile // (8 * grp))]
        nchain = len(ops(0, 16))

        def unpk(i):
            t, g = groups[i]
            out = []
            if g == 0:
                out.append(A.vbar())  # fills of tile t landed
                if t + 1 < nt:
                    out += [
                        A.vsync(A.W_A, A.K_UNPACK),  # buffer free
                        A.vfill(t + 1, inb[(t + 1) % 2]),
                    ]
            for j in range(grp):
                out.append(A.vunpk((i % 2) * grp + j, inb[t % 2] + (g * grp + j) * 8))
            return out

        k.emit(*unpk(0))
        for i, (t, g) in enumerate(groups):
            if i + 1 < len(groups):
                k.emit(*unpk(i + 1))
            xs = [(i % 2) * grp + j for j in range(grp)]
            ts = [16 + (i % 2) * grp + j for j in range(grp)]
            chains = [ops(xs[j], ts[j]) for j in range(grp)]
            for s in range(nchain):
                for j in range(grp):
                    k.emit(chains[j][s])
            if g == 0 and t >= 2:
                k.emit(A.vsync(A.W_P, A.K_DRAIN))  # out buffer drained
            for j in range(grp):
                k.emit(A.vpack(ts[j], outb[t % 2] + (g * grp + j) * 8))
            if g == tile // (8 * grp) - 1:
                k.emit(A.vsync(A.W_D, A.K_PACK), A.vdrain(4 + t, outb[t % 2]))
        k.emit(A.vhalt())
        return k

    return build, {"x": x}, ref
