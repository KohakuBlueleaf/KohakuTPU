"""Hand-written L1 row softmax on every vector core.

Rows across chunks: a step is 8 rows (VL 128, one row per chunk); register `j`
holds word-column `j` of those rows (the L1 window walks 8 words `w` apart).
  max:   m = max_j x_j by tree folds, then 4 VSHUF rotate+max -> every lane of a
         chunk holds its row's max
  exp:   p_j = 2^(x_j*log2e - m*log2e) as ONE VFMA + VEXP2, stored in place;
         s = sum_j p_j by tree folds, then the butterfly
  scale: y_j = p_j * (1/s), the reciprocal once a step
Two register sets alternate by block. Each core runs the streaming pipeline.
"""

from kohakutpu.hw import vector as V
from kohakutpu.ir.l1.kernels import stream
from kohakutpu.ir.l1.vector import Alu, Seti, Setmode, Setvl, Vld, Vshuf, Vst

LOG2E = 1.4426950408889634
S_VL, S_ROT, S_LOG2E = 0, (1, 2, 3, 4), 5
ROWS = 8  # rows a step: one per chunk at VL 128
STEPS = 2  # steps interleaved
DATA = list(range(8))
R_M, R_S, R_C, R_T = (8, 9), (10, 11), (12, 13), (14, 15)


def head() -> list:
    code = [Seti(S_VL, V.VLMAX)]
    code += [Seti(s, k) for s, k in zip(S_ROT, (8, 4, 2, 1), strict=True)]
    return code + [Seti(S_LOG2E, V.e8m15(LOG2E)), Setvl(S_VL), Setmode(V.FLAT)]


def combine(op: str, vd: int, a: int, b: int) -> Alu:
    return Alu(op, vd=vd, va=a, vb=b) if op == "VMAX" else Alu(op, vd=vd, va=a, vc=b)


def tree(op: str, regs: list) -> tuple[list, int]:
    """Fold `regs` pairwise into regs[0]: depth log2, every level independent."""
    out, live = [], list(regs)
    while len(live) > 1:
        nxt = []
        for i in range(0, len(live) - 1, 2):
            out.append(combine(op, live[i], live[i], live[i + 1]))
            nxt.append(live[i])
        if len(live) % 2:
            nxt.append(live[-1])
        live = nxt
    return out, live[0]


def butterflies(op: str, accs: tuple) -> list:
    out = []
    for s in S_ROT:
        out += [Vshuf(R_T[k], a, s) for k, a in enumerate(accs)]
        out += [combine(op, a, a, R_T[k]) for k, a in enumerate(accs)]
    return out


def steps(bases: list, w: int, block: int) -> list:
    """`len(bases)` 8-row steps at once; rows `w` words apart in L1."""
    n = len(bases)
    per = min(block, len(DATA) // (2 * n))
    out = []
    which = [0]

    def blocks():
        for b0 in range(0, w, per):
            yield list(range(b0, min(b0 + per, w)))

    def regs_of(blk):
        base = which[0] * n * per
        which[0] ^= 1
        return [[DATA[base + k * per + i] for i in range(len(blk))] for k in range(n)]

    def load(blk, regs):
        return [
            Vld(r, stream.AD_L1, bases[k] + j)
            for k in range(n)
            for r, j in zip(regs[k], blk, strict=True)
        ]

    def store(blk, regs):
        return [
            Vst(r, stream.AD_L1, bases[k] + j)
            for k in range(n)
            for r, j in zip(regs[k], blk, strict=True)
        ]

    def fold_into(op, acc, regs, first):
        trees = [tree(op, regs[k]) for k in range(n)]
        depth = max(len(t[0]) for t in trees)
        for d in range(depth):
            out.extend(t[0][d] for t in trees if d < len(t[0]))
        for k in range(n):
            top = trees[k][1]
            out.append(
                Alu("VMOV", vd=acc[k], va=top)
                if first
                else combine(op, acc[k], acc[k], top)
            )

    for b, blk in enumerate(blocks()):
        regs = regs_of(blk)
        out += load(blk, regs)
        fold_into("VMAX", R_M, regs, b == 0)
    out += butterflies("VMAX", R_M[:n])
    out += [Alu("VMUL", vd=R_C[k], va=R_M[k], vb=S_LOG2E, sb=V.SRC_S) for k in range(n)]
    out += [Alu("VNEG", vd=R_C[k], va=R_C[k]) for k in range(n)]
    for b, blk in enumerate(blocks()):
        regs = regs_of(blk)
        out += load(blk, regs)
        out += [
            Alu("VFMA", vd=r, va=r, vb=S_LOG2E, sb=V.SRC_S, vc=R_C[k])
            for k in range(n)
            for r in regs[k]
        ]
        out += [Alu("VEXP2", vd=r, va=r) for k in range(n) for r in regs[k]]
        out += store(blk, regs)
        fold_into("VADD", R_S, regs, b == 0)
    out += butterflies("VADD", R_S[:n])
    out += [Alu("VINV", vd=R_C[k], va=R_S[k]) for k in range(n)]
    for blk in blocks():
        regs = regs_of(blk)
        out += load(blk, regs)
        out += [Alu("VMUL", vd=r, va=r, vb=R_C[k]) for k in range(n) for r in regs[k]]
        out += store(blk, regs)
    return out


def softmax(prog, src: int, dst: int, n_rows: int, cols: int, rows=8, block=8) -> int:
    """Queue softmax over `(n_rows, cols)` fp16, `rows` rows a RUN; returns image words."""
    if cols % 16:
        raise ValueError(f"a {cols}-wide row is not whole 16-element words")
    w = cols // 16
    units = prog.units("VC")
    per = n_rows // len(units)
    if n_rows % len(units) or per % rows or rows % ROWS:
        raise ValueError(f"{per} rows a core is not whole RUNs of {rows}")

    def body(region):
        bases = [region + s * ROWS * w for s in range(rows // ROWS)]
        return [
            x
            for g in range(0, len(bases), STEPS)
            for x in steps(bases[g : g + STEPS], w, block)
        ]

    progs = stream.programs(head(), body, rows * w, ((w, ROWS),))
    size = 0
    for c, core in enumerate(units):
        size = stream.send(
            prog,
            core,
            progs,
            ((w, ROWS),),
            rows * w,
            src + c * per * cols * 2,
            dst + c * per * cols * 2,
            per // rows,
            rows * cols * 2,
        )
    prog.barrier()
    return size
