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
of four lanes cyclically (`group_reduce`: every lane exact, no broadcast). P
goes to the quantiser's order (four rows of 32 keys, row by row) with the 4x4
granule transpose of `kohakutpu.isa.relayout.Subtile` as a two-stage
butterfly (`transpose`). The four lane-group predicates are set once a RUN.
"""

from functools import cache

import numpy as np
from kohakutpu.hw import vector as V
from kohakutpu.ir.l1 import vsched
from kohakutpu.ir.l1.cluster import Drain, Fill, Gemm
from kohakutpu.ir.l1.kernels.matmul import ENTRY, SUBTILE
from kohakutpu.ir.l1.kernels.stream import walk_offsets
from kohakutpu.ir.l1.mover import Quantise
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
# Vector registers: two sets of four columns, so one set's loads and exps run
# beside the other's transpose.
SETS = ((0, 1, 2, 3), (4, 5, 6, 7))
ACC, TMP, MNEW, CORR, OLD, IDX, TMP2 = 8, 9, 10, 11, 12, 13, 14
# Descriptors.
AD_COL, AD_ST, AD_IX, AD_IN, AD_PD, AD_OD, AD_IF = 0, 1, 2, 3, 4, 5, 6
# Predicates: lane % 4 == 3, lane % 4 >= 2, granule >= 2, granule odd.
PM3, PM23, PG23, PGODD = 0, 1, 2, 3


def index_words() -> np.ndarray:
    """fp16 words: each lane's granule (lane // 4), its place in it (lane % 4),
    and the granule's parity."""
    lanes = np.arange(16)
    return np.concatenate([lanes // 4, lanes % 4, (lanes // 4) % 2]).astype(np.float16)


#: Words `index_words` takes.
IX_WORDS = index_words().size // 16


def layout(gm: int) -> dict:
    """L1 word offsets: S/PV, P, O, stats (m, l, corr), the index words."""
    span = gm * COLS
    st = 3 * span
    return {
        "in": 0,
        "p": span,
        "o": 2 * span,
        "m": st,
        "l": st + gm,
        "corr": st + 2 * gm,
        "ix": st + 3 * gm,
    }


def _op(name, vd, a, b=None, c=None, **kw) -> Alu:
    """Unused vector fields name `vd`: the issue hazard reads all four."""
    return Alu(
        name, vd=vd, va=a, vb=vd if b is None else b, vc=vd if c is None else c, **kw
    )


def predicates(load, cmp) -> list:
    """The four lane-group predicates (`PM3`, `PM23`, `PG23`, `PGODD`).
    `load(word)` loads index word `word` (0 granule, 1 lane % 4, 2 parity) and
    returns ``(code, reg)``; ``cmp(op, reg, k, pr)`` compares it with k."""
    out = []
    for word, tests in (
        (1, (("VCMPEQ", 3, PM3), ("VCMPGT", 1, PM23))),
        (0, (("VCMPGT", 1, PG23),)),
        (2, (("VCMPEQ", 1, PGODD),)),
    ):
        code, r = load(word)
        out += code + [cmp(op, r, k, pr) for op, k, pr in tests]
    return out


def group_reduce(comb, reg: int, tmp: int, rot) -> list:
    """Every lane of `reg` = `comb` over its group of four (lanes 4i..4i+3): a
    rotation by one within the group, then by two. `comb(d, a, b)` combines;
    `rot(n)` names the S register holding rotation n."""
    out = []
    for r, pr in ((1, PM3), (2, PM23)):
        out += [
            Vshuf(tmp, reg, rot(r)),
            Vshuf(tmp, reg, rot(12 + r), pr=pr, pm=1),
            comb(reg, reg, tmp),
        ]
    return out


def transpose(xs, t1: int, t2: int, rot, move) -> list:
    """The 4x4 granule transpose of four registers in place: ``xs[i]``'s
    granule j takes ``xs[j]``'s granule i. Two butterfly stages, on the
    granule's high bit (rotate 8) then its low bit (rotate 4), each over two
    register pairs (a, c): a's upper granules take c's lower, c's lower take
    a's upper. `move(d, s, pr, pm)` is a predicated copy."""
    x0, x1, x2, x3 = xs
    out = []
    for (a, b, c, d), lanes, pr in (
        ((x0, x1, x2, x3), 8, PG23),
        ((x0, x2, x1, x3), 4, PGODD),
    ):
        out += [
            Vshuf(t1, a, rot(lanes)),
            Vshuf(t2, b, rot(lanes)),
            Vshuf(a, c, rot(16 - lanes), pr=pr, pm=1),
            Vshuf(b, d, rot(16 - lanes), pr=pr, pm=1),
            move(c, t1, pr, 2),
            move(d, t2, pr, 2),
        ]
    return out


def _predicates(lay: dict) -> list:
    return predicates(
        lambda w: ([Vld(IDX, AD_IX, lay["ix"] + w)], IDX),
        lambda op, r, k, pr: _op(op, r, r, b=S_K[k], sb=V.SRC_S, pr=pr),
    )


def _comb(op: str):
    if op == "max":
        return lambda d, a, b: _op("VMAX", d, a, b=b)
    return lambda d, a, b: _op("VADD", d, a, c=b)


def _move(d: int, s: int, pr: int, pm: int) -> Alu:
    return _op("VMOV", d, s, pr=pr, pm=pm)


def _rot(n: int) -> int:
    return S_ROT[n]


def softmax_image(gm: int) -> tuple:
    """The online-softmax RUN: S in at "in", P out at "p", stats updated."""
    lay = layout(gm)
    code = [Vfill(AD_IN, lay["in"]), Bar(), Vld(OLD, AD_ST, lay["m"])]
    code += _predicates(lay)
    # Row max over the 16 sub-tile columns.
    for c in range(COLS):
        x = SETS[(c // 4) % 2][c % 4]
        code.append(Vld(x, AD_COL, lay["in"] + c))
        code.append(_op("VMOV", ACC, x) if c == 0 else _op("VMAX", ACC, ACC, b=x))
    code += group_reduce(_comb("max"), ACC, TMP, _rot)
    code += [
        _op("VMAX", MNEW, OLD, b=ACC),
        _op("VSUB", CORR, OLD, c=MNEW),
        _op("VEXP2", CORR, CORR),
        Vst(MNEW, AD_ST, lay["m"]),
        Vst(CORR, AD_ST, lay["corr"]),
    ]
    # P = 2^(S - m') a group of four columns at a time, summed, then transposed
    # into the quantiser's order: word ((h//2)*4 + i)*2 + h%2 of a band is row
    # i, keys 16h..16h+15.
    for h in range(4):
        xs = SETS[h % 2]
        for k, x in enumerate(xs):
            code += [
                Vld(x, AD_COL, lay["in"] + 4 * h + k),
                _op("VSUB", x, x, c=MNEW),
                _op("VEXP2", x, x),
            ]
            first = h == 0 and k == 0
            code.append(_op("VMOV", ACC, x) if first else _op("VADD", ACC, ACC, c=x))
        code += transpose(xs, TMP, TMP2, _rot, _move)
        code += [
            Vst(x, AD_COL, lay["p"] + ((h // 2) * 4 + i) * 2 + h % 2)
            for i, x in enumerate(xs)
        ]
    code += group_reduce(_comb("sum"), ACC, TMP, _rot)
    code += [
        Vld(OLD, AD_ST, lay["l"]),
        _op("VFMA", OLD, OLD, b=CORR, c=ACC),
        Vst(OLD, AD_ST, lay["l"]),
        Vdrain(AD_PD, lay["p"]),
    ]
    return tuple(vsched.schedule(code, l1_map(gm)) + [Halt()])


def update_image(gm: int) -> tuple:
    """O = O*corr + PV, PV in at "in"."""
    lay = layout(gm)
    code = [Vfill(AD_IN, lay["in"]), Bar(), Vld(CORR, AD_ST, lay["corr"])]
    for c in range(COLS):
        o, pv = SETS[0][c % 4], SETS[1][c % 4]
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
    code += [Setvl(S_VL), Setmode(V.FLAT), Vfill(AD_IF, lay["ix"]), Bar()]
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
        o = SETS[(c // 4) % 2][c % 4]
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
        fills={AD_IN: span, AD_PD: span, AD_OD: span, AD_IF: IX_WORDS},
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
        prog.move(Quantise(p16_at, p7_at, gm * NK))
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
        Dims(AD_IF, walk(IX_WORDS)),
    ]


def run_ops(gm: int, name: str, in_at: int | None = None) -> list:
    """One RUN of image `name`, its input (S or PV) at `in_at`."""
    pc = next(pc for n, _, pc in images(gm) if n == name)
    return ([Desc(AD_IN, in_at)] if in_at is not None else []) + [Run(pc)]
