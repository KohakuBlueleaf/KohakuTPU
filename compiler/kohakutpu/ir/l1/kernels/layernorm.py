"""Hand-written L1 layer norm over rows, ``(x - mean) * rstd * gamma + beta``,
on every vector core.

Rows across chunks, as `softmax`: a step is 8 rows (VL 128, one row per chunk),
register `j` holds word-column `j` of them (the L1 walk steps `w` words).
  stats: s1 = sum x and s2 = sum x^2 by tree folds, then the VSHUF butterfly;
         mean = s1/n, rstd = 1/sqrt(s2/n - mean^2 + eps), c = -mean*rstd
  apply: y_j = (x_j*rstd + c) * gamma_j + beta_j, two VFMAs
gamma and beta are one resident block a core, read through a STRIDE-0 walk
(`AD_B`): one VLD puts word `j` in all 8 chunks, so neither is replicated in
memory or in L1. Each core runs the streaming pipeline.
"""

from kohakutpu.hw import vector as V
from kohakutpu.ir.l1.kernels import stream
from kohakutpu.ir.l1.kernels.softmax import ROWS, S_ROT
from kohakutpu.ir.l1.vector import (
    Alu,
    Desc,
    Dims,
    Seti,
    Setmode,
    Setvl,
    Vld,
    Vshuf,
    Vst,
)

S_VL, S_INV, S_EPS = 0, 5, 6
#: The broadcast walk: the same L1 word in each of the 8 chunks.
AD_B = 5
BROADCAST = ((0, ROWS),)
BLOCK = 4
X, Q = (0, 1, 2, 3), (4, 5, 6, 7)
A1, A2, T1, T2, C1, C2 = 8, 9, 10, 11, 12, 13


def head(cols: int, eps: float) -> list:
    code = [Seti(S_VL, V.VLMAX)]
    code += [Seti(s, k) for s, k in zip(S_ROT, (8, 4, 2, 1), strict=True)]
    code += [Seti(S_INV, V.e8m15(1.0 / cols)), Seti(S_EPS, V.e8m15(eps))]
    return code + [Setvl(S_VL), Setmode(V.FLAT)]


def _op(name: str, vd: int, a: int, b: int | None = None, c: int | None = None, **kw):
    """An ALU op whose unused vector fields name `vd` (the issue hazard reads all)."""
    return Alu(
        name, vd=vd, va=a, vb=vd if b is None else b, vc=vd if c is None else c, **kw
    )


def _fold(regs: list) -> list:
    """Sum `regs` into regs[0], pairwise."""
    out, live = [], list(regs)
    while len(live) > 1:
        nxt = []
        for i in range(0, len(live) - 1, 2):
            out.append(_op("VADD", live[i], live[i], c=live[i + 1]))
            nxt.append(live[i])
        if len(live) % 2:
            nxt.append(live[-1])
        live = nxt
    return out


def step(base: int, w: int, res: int) -> list:
    """One 8-row step: rows `w` words apart from L1 word `base`; gamma at `res`,
    beta at ``res + w``."""
    out = []
    blocks = [list(range(b0, min(b0 + BLOCK, w))) for b0 in range(0, w, BLOCK)]
    for b, cols in enumerate(blocks):
        xs, qs = X[: len(cols)], Q[: len(cols)]
        out += [Vld(x, stream.AD_L1, base + j) for x, j in zip(xs, cols, strict=True)]
        out += [_op("VMUL", q, x, b=x) for q, x in zip(qs, xs, strict=True)]
        out += _fold(list(xs)) + _fold(list(qs))
        if b == 0:
            out += [_op("VMOV", A1, xs[0]), _op("VMOV", A2, qs[0])]
        else:
            out += [_op("VADD", A1, A1, c=xs[0]), _op("VADD", A2, A2, c=qs[0])]
    for s in S_ROT:
        out += [Vshuf(T1, A1, s), Vshuf(T2, A2, s)]
        out += [_op("VADD", A1, A1, c=T1), _op("VADD", A2, A2, c=T2)]
    out += [
        _op("VMUL", A1, A1, b=S_INV, sb=V.SRC_S),  # mean
        _op("VMUL", A2, A2, b=S_INV, sb=V.SRC_S),  # E[x^2]
        _op("VMUL", T1, A1, b=A1),
        _op("VSUB", A2, A2, c=T1),  # var
        _op("VADD", A2, A2, c=S_EPS, sc=V.SRC_S),
        _op("VRSQRT", C1, A2),  # rstd
        _op("VMUL", C2, A1, b=C1),
        _op("VNEG", C2, C2),  # -mean * rstd
    ]
    gammas, betas = Q, (A1, A2, T1, T2)
    for cols in blocks:
        xs = X[: len(cols)]
        out += [Vld(x, stream.AD_L1, base + j) for x, j in zip(xs, cols, strict=True)]
        out += [Vld(g, AD_B, res + j) for g, j in zip(gammas, cols, strict=False)]
        out += [Vld(t, AD_B, res + w + j) for t, j in zip(betas, cols, strict=False)]
        out += [_op("VFMA", x, x, b=C1, c=C2) for x in xs]
        out += [
            _op("VFMA", x, x, b=g, c=t)
            for x, g, t in zip(xs, gammas, betas, strict=False)
        ]
        out += [Vst(x, stream.AD_L1, base + j) for x, j in zip(xs, cols, strict=True)]
    return out


def layernorm(
    prog, src: int, dst: int, gb: int, n_rows: int, cols: int, rows=8, eps=1e-5
) -> int:
    """Queue layer norm over `(n_rows, cols)` fp16, `rows` rows a RUN; `gb` holds
    gamma then beta (fp16, `cols` each). Returns the image words."""
    if cols % 16:
        raise ValueError(f"a {cols}-wide row is not whole 16-element words")
    w = cols // 16
    units = prog.units("VC")
    per = n_rows // len(units)
    if n_rows % len(units) or per % rows or rows % ROWS:
        raise ValueError(f"{per} rows a core is not whole RUNs of {rows}")
    words = rows * w
    res = stream.resident_at(words, 1)

    def body(slots):
        return [
            x
            for s in range(rows // ROWS)
            for x in step(slots[0] + s * ROWS * w, w, res)
        ]

    dims = ((w, ROWS),)
    progs = stream.programs(
        head(cols, eps), body, words, dims, resident=2 * w, walks={AD_B: BROADCAST}
    )
    setup = [Desc(AD_B, 0), Dims(AD_B, BROADCAST)]
    size = 0
    for c, core in enumerate(units):
        off = c * per * cols * 2
        size = stream.send(
            prog,
            core,
            progs,
            dims,
            words,
            src + off,
            dst + off,
            per // rows,
            rows * cols * 2,
            resident_src=gb,
            resident=2 * w,
            setup=setup,
        )
    prog.barrier()
    return size
