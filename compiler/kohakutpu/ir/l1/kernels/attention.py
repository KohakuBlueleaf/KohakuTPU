"""Hand-written L1 flash attention for one query block, streamed over 64-key
blocks: ``softmax(q @ k.T) @ v`` with the online softmax.

`q` arrives PRE-SCALED by ``log2(e) / sqrt(64)``, so the scores are in the base
of `VEXP2`. Head dim 64; a query block is `4*gm` rows (gm <= 8: one register
chunk a band of four rows). Per key block `j`:

  cluster   S = q @ k_j.T          q resident in A bank 0, one sweep, DRAIN
  vector    m' = max(m, rowmax S), corr = 2^(m - m'), P = 2^(S - m'),
            l = l*corr + rowsum P, P written in the quantiser's entry order
  mover     P fp16 -> MXFP7
  cluster   PV = P @ v_j            P in A bank 1, v_j.T in B bank 1, DRAIN
  vector    O = O*corr + PV
and at the end O / l. Clusters take PRE-QUANTISED operands only and a vector
core writes fp16, so the mover's quantise is the one way from P to a GEMM.

THE VECTOR CORE WORKS IN SUB-TILE LAYOUT. A drained word is a 4x4 sub-tile
(lane 4i+k = row i, column k). The L1 walk puts band b (four rows) in chunk b
and one sub-tile column per register, so a row statistic is a register whose
lanes 4i..4i+3 all hold row i's value -- and it applies to every column
register as it stands. A row reduce folds the columns, then reduces each group
of four lanes (rotate 1 and 2: lane 4i is exact) and broadcasts lane 4i to its
group with three predicated rotates. P goes to the quantiser's order (four rows
of 32 keys, row by row) with the 4x4 granule transpose of
`kohakutpu.isa.relayout.Subtile`: predicated rotates, no merge.
"""

from functools import cache

import numpy as np
from kohakuaccel.package import mover as PM
from kohakutpu.hw import vector as V
from kohakutpu.ir.l1 import vsched
from kohakutpu.ir.l1.cluster import Drain, Fill, Gemm
from kohakutpu.ir.l1.kernels.matmul import ENTRY, SUBTILE
from kohakutpu.ir.l1.kernels.stream import walk_offsets
from kohakutpu.ir.l1.vector import (
    Alu,
    Bar,
    Desc,
    Dims,
    Halt,
    Image,
    Run,
    Seti,
    Setmode,
    Setvl,
    Vdrain,
    Vfill,
    Vld,
    Vshuf,
    Vst,
)

D = 64  # head dim, and the keys a block
COLS = D // 4  # sub-tile columns of S, PV and O
NK = D // 32  # K-blocks of both GEMMs
NEG = -60000.0

# S registers. S0 is VL; S1..S3 are VRED's.
S_VL, S_NEG = 0, 4
S_ROT = {1: 5, 2: 6, 13: 7, 14: 8, 15: 9, 4: 10, 8: 11, 12: 12}
S_K = {1: 13, 2: 14, 3: 15}  # the lane-group numbers predicates compare with
# Vector registers.
P_IN = (0, 1, 2, 3)
R_OUT = (4, 5, 6, 7)
ACC, TMP, MNEW, CORR, OLD, IDX, BC = 8, 9, 10, 11, 12, 13, 14
# Descriptors.
AD_COL, AD_ST, AD_IX, AD_IN, AD_PD, AD_OD, AD_IF = 0, 1, 2, 3, 4, 5, 6


def layout(gm: int) -> dict:
    """L1 word offsets: S/PV, P, O, stats (m, l, corr), the two index words."""
    span = gm * COLS
    st = 3 * span
    return {
        "in": 0,
        "p": span,
        "o": 2 * span,
        "m": st,
        "l": st + gm,
        "corr": st + 2 * gm,
        "ixg": st + 3 * gm,
        "ixm": st + 3 * gm + 1,
    }


def index_words() -> np.ndarray:
    """Two fp16 words: each lane's granule (lane // 4), then its place in it."""
    lanes = np.arange(16)
    return np.concatenate([lanes // 4, lanes % 4]).astype(np.float16)


def _op(name, vd, a, b=None, c=None, **kw) -> Alu:
    """Unused vector fields name `vd`: the issue hazard reads all four."""
    return Alu(
        name, vd=vd, va=a, vb=vd if b is None else b, vc=vd if c is None else c, **kw
    )


def _rotate(dst: int, src: int, lanes: int, pred: int):
    """``dst = src`` rotated by `lanes`, written where predicate `pred` holds
    (0: everywhere)."""
    kw = {"pr": pred, "pm": 1} if pred else {}
    if lanes:
        return Vshuf(dst, src, S_ROT[lanes], **kw)
    return _op("VMOV", dst, src, **kw)


def _predicates(ix: int) -> list:
    """P1..P3 = (index word `ix` == 1, 2, 3), the word in every chunk."""
    out = [Vld(IDX, AD_IX, ix)]
    out += [_op("VCMPEQ", IDX, IDX, b=S_K[k], sb=V.SRC_S, pr=k) for k in (1, 2, 3)]
    return out


def _row_reduce(op: str, reg: int, lay: dict) -> list:
    """`reg`'s lane groups of four reduced by `op`, the result in every lane of
    its group (predicates: lane // 4 ... built here as lane % 4)."""
    comb = (
        (lambda d, a, b: _op("VMAX", d, a, b=b))
        if op == "max"
        else (lambda d, a, b: _op("VADD", d, a, c=b))
    )
    out = _predicates(lay["ixm"])
    for r in (1, 2):
        out += [Vshuf(TMP, reg, S_ROT[r]), comb(reg, reg, TMP)]
    out.append(_op("VMOV", BC, reg))
    out += [Vshuf(BC, reg, S_ROT[16 - k], pr=k, pm=1) for k in (1, 2, 3)]
    return out


def softmax_image(gm: int) -> tuple:
    """The online-softmax RUN: S in at "in", P out at "p", stats updated."""
    lay = layout(gm)
    code = [Vfill(AD_IN, lay["in"]), Bar(), Vld(OLD, AD_ST, lay["m"])]
    # Row max over the 16 sub-tile columns.
    for c in range(COLS):
        x = P_IN[c % 4]
        code.append(Vld(x, AD_COL, lay["in"] + c))
        code.append(_op("VMOV", ACC, x) if c == 0 else _op("VMAX", ACC, ACC, b=x))
    code += _row_reduce("max", ACC, lay)
    code += [
        _op("VMAX", MNEW, OLD, b=BC),
        _op("VSUB", CORR, OLD, c=MNEW),
        _op("VEXP2", CORR, CORR),
        Vst(MNEW, AD_ST, lay["m"]),
        Vst(CORR, AD_ST, lay["corr"]),
    ]
    # P = 2^(S - m') a group of four columns at a time, summed, then transposed
    # into the quantiser's order: word ((h//2)*4 + i)*2 + h%2 of a band is row
    # i, keys 16h..16h+15.
    code += _predicates(lay["ixg"])
    for h in range(4):
        for k in range(4):
            x = P_IN[k]
            code += [
                Vld(x, AD_COL, lay["in"] + 4 * h + k),
                _op("VSUB", x, x, c=MNEW),
                _op("VEXP2", x, x),
            ]
            first = h == 0 and k == 0
            code.append(_op("VMOV", ACC, x) if first else _op("VADD", ACC, ACC, c=x))
        for i in range(4):
            # Output i takes granule i of input j into granule j: a rotation of
            # 4(i-j) lanes; input 0's write is whole, the rest predicated.
            out = R_OUT[i]
            code += [_rotate(out, P_IN[j], 4 * ((i - j) % 4), j) for j in range(4)]
            code.append(Vst(out, AD_COL, lay["p"] + ((h // 2) * 4 + i) * 2 + h % 2))
    code += _row_reduce("sum", ACC, lay)
    code += [
        Vld(OLD, AD_ST, lay["l"]),
        _op("VFMA", OLD, OLD, b=CORR, c=BC),
        Vst(OLD, AD_ST, lay["l"]),
        Vdrain(AD_PD, lay["p"]),
    ]
    return tuple(vsched.schedule(code, l1_map(gm)) + [Halt()])


def update_image(gm: int) -> tuple:
    """O = O*corr + PV, PV in at "in"."""
    lay = layout(gm)
    code = [Vfill(AD_IN, lay["in"]), Bar(), Vld(CORR, AD_ST, lay["corr"])]
    for c in range(COLS):
        o, pv = P_IN[c % 4], R_OUT[c % 4]
        code += [
            Vld(o, AD_COL, lay["o"] + c),
            Vld(pv, AD_COL, lay["in"] + c),
            _op("VFMA", o, o, b=CORR, c=pv),
            Vst(o, AD_COL, lay["o"] + c),
        ]
    return tuple(vsched.schedule(code, l1_map(gm)) + [Halt()])


def init_image(gm: int) -> tuple:
    """Scalars, the index words, m = NEG, l = 0, O = 0."""
    lay = layout(gm)
    code = [Seti(S_VL, 16 * gm), Seti(S_NEG, V.e8m15(NEG))]
    code += [Seti(s, r) for r, s in S_ROT.items()]
    code += [Seti(s, V.e8m15(float(k))) for k, s in S_K.items()]
    code += [Setvl(S_VL), Setmode(V.FLAT), Vfill(AD_IF, lay["ixg"]), Bar()]
    code += [
        _op("VMOV", OLD, S_NEG, sa=V.SRC_S),
        Vst(OLD, AD_ST, lay["m"]),
        _op("VMOV", TMP, 0, sa=V.SRC_K),  # K0 is zero from reset
        Vst(TMP, AD_ST, lay["l"]),
    ]
    code += [Vst(TMP, AD_COL, lay["o"] + c) for c in range(COLS)]
    return tuple(code + [Halt()])


def final_image(gm: int) -> tuple:
    """O / l, drained."""
    lay = layout(gm)
    code = [Vld(OLD, AD_ST, lay["l"]), _op("VINV", OLD, OLD)]
    for c in range(COLS):
        o = P_IN[c % 4]
        code += [
            Vld(o, AD_COL, lay["o"] + c),
            _op("VMUL", o, o, b=OLD),
            Vst(o, AD_COL, lay["o"] + c),
        ]
    code.append(Vdrain(AD_OD, lay["o"]))
    return tuple(vsched.schedule(code, l1_map(gm)) + [Halt()])


def l1_map(gm: int) -> vsched.L1Map:
    span = gm * COLS
    return vsched.L1Map(
        walks={
            AD_COL: (0, walk_offsets(((COLS, gm),))),
            AD_ST: (0, list(range(gm))),
            AD_IX: (0, [0] * gm),
        },
        fills={AD_IN: span, AD_PD: span, AD_OD: span, AD_IF: 2},
    )


def attention(
    prog, q_at, k_at, v_at, o_at, scratch, idx_at, gm: int, blocks: int
) -> None:
    """Queue one query block (`4*gm` rows) against `blocks` 64-key blocks.

    `q_at`: q MXFP7 A-packed (gm, nk 2), pre-scaled. `k_at`, `v_at`: per key
    block, k_j and v_j.T MXFP7 B-packed (gn 16, nk 2), contiguous by block.
    `o_at`: the result, `gm x 16` sub-tiles. `scratch`: S, PV, P fp16, P MXFP7.
    `idx_at`: `index_words()`.
    """
    if not 1 <= gm <= 8:
        raise ValueError(f"gm {gm}: a band of four rows a register chunk, eight chunks")
    mg, vc = prog.units("MG")[0], prog.units("VC")[0]
    span = gm * COLS
    s_at, pv_at = scratch, scratch + span * SUBTILE
    p16_at, p7_at = pv_at + span * SUBTILE, pv_at + 2 * span * SUBTILE
    kv = COLS * NK * ENTRY
    prog.send(vc, *setup_ops(gm, p16_at, o_at, idx_at), *run_ops(gm, "init"))
    prog.send(mg, Fill(q_at, gm * NK, sel=0, fbank=0))

    def scores(j):
        prog.send(
            mg,
            Fill(k_at + j * kv, COLS * NK, sel=1, fbank=0),
            Gemm(gm, COLS, NK, abank=0, bbank=0),
            Drain(s_at, span),
        )
        return prog.mark(mg)

    tok = scores(0)
    for j in range(blocks):
        prog.wait(mg, tok)
        prog.send(vc, *run_ops(gm, "softmax", s_at))
        prog.wait(vc)
        prog.move(PM.convert(p16_at, p7_at, gm * NK))
        prog.send(
            mg,
            Fill(p7_at, gm * NK, sel=0, fbank=1),
            Fill(v_at + j * kv, COLS * NK, sel=1, fbank=1),
            Gemm(gm, COLS, NK, abank=1, bbank=1),
            Drain(pv_at, span),
        )
        pv = prog.mark(mg)
        if j + 1 < blocks:
            tok = scores(j + 1)
        prog.wait(mg, pv)
        prog.send(vc, *run_ops(gm, "update", pv_at))
    prog.send(vc, *run_ops(gm, "final"))
    prog.barrier()


#: The four images, in instruction memory in this order.
RUNS = ("init", "softmax", "update", "final")


@cache
def images(gm: int) -> list:
    """``[(name, image, pc)]``: every attention image and where it sits."""
    made = [init_image(gm), softmax_image(gm), update_image(gm), final_image(gm)]
    out, at = [], 0
    for name, img in zip(RUNS, made, strict=True):
        out.append((name, img, at))
        at += sum(len(i.words()) for i in img)
    if at > 512:
        raise ValueError(f"the attention images take {at} of IMEM's 512 words")
    return out


def setup_ops(gm: int, p16_at: int, o_at: int, idx_at: int) -> list:
    """The images and every descriptor but the input's base: once a core."""
    span = gm * COLS
    walk = lambda n: ((V.WORD_BYTES, n),)
    ops = [Image(img, pc) for _, img, pc in images(gm)]
    return ops + [
        Desc(AD_COL, 0),
        Dims(AD_COL, ((COLS, gm),)),
        Desc(AD_ST, 0),
        Dims(AD_ST, ((1, gm),)),
        Desc(AD_IX, 0),
        Dims(AD_IX, ((0, gm),)),
        Dims(AD_IN, walk(span)),
        Dims(AD_PD, walk(span)),
        Dims(AD_OD, walk(span)),
        Desc(AD_PD, p16_at),
        Desc(AD_OD, o_at),
        Desc(AD_IF, idx_at),
        Dims(AD_IF, walk(2)),
    ]


def run_ops(gm: int, name: str, in_at: int | None = None) -> list:
    """One RUN of image `name`, its input (S or PV) at `in_at`."""
    pc = next(pc for n, _, pc in images(gm) if n == name)
    return ([Desc(AD_IN, in_at)] if in_at is not None else []) + [Run(pc)]
