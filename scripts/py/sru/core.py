"""The SRU core, as specified: one job over sub-rows of sub-tile words.

Composes the unit references -- e8.fma and fn.lane (the pool's two op kinds),
red (R1, R2), the MXFP7 spec -- in the order sru.md gives, so a whole-job bench
checks the core's sequencing, schedule and plumbing against units that are
each proven alone. A pass's program is evaluated op by op on each element; the
pool's schedule is not modelled (it must not change a result).

The finalise ops: MOV, ADD, SUB, MUL, FMA, MAX, MIN, and RCP / RSQRT (the
mantissa ROM's 1/x and 1/sqrt(x), fn.lane MANT / HALF with NEG).

FP16 is IEEE (ieee=True) or the clusters' accumulator form: exponent 0 a
signed zero and 1..31 ordinary binades in; out rounded to nearest even at the
value's binade, a zero below 2^-14, +-65504 at 2^16 or above (inf and NaN
included) -- mx_fpacc_to_fp16's emission.
"""

from fractions import Fraction

import e8
import fn
import numpy as np
import red

from kohakutpu.language.numerics import mxfp7

# the finalise's opcodes
MOV, ADD, SUB, MUL, FMA, MAX, MIN, RCP, RSQRT = 0, 3, 4, 5, 6, 8, 9, 10, 11
MROM = fn.mrom_table()


def f16_to_e8_acc(h):
    s, e, m = h >> 15, (h >> 10) & 0x1F, h & 0x3FF
    if e == 0:
        return s << 23
    v = Fraction(0x400 + m, 1 << 10) * Fraction(2) ** (e - 15)
    return e8.encode(-v if s else v, s)


def e8_to_f16_acc(w):
    s, e, _ = e8.fields(w)
    if e == 0:
        return s << 15
    if e == 255:
        return (s << 15) | 0x7BFF
    a = abs(e8.value(w))
    big = a.numerator.bit_length() - a.denominator.bit_length()
    if Fraction(2) ** big > a:
        big -= 1
    q = a / Fraction(2) ** (big - 10)
    n, r = divmod(q.numerator, q.denominator)
    half = 2 * r - q.denominator
    if half > 0 or (half == 0 and n & 1):
        n += 1
    if n == 2048:
        n, big = 1024, big + 1
    if big < -14:
        return s << 15
    if big > 15:
        return (s << 15) | 0x7BFF
    return (s << 15) | ((big + 15) << 10) | (n - 1024)


def f2e(ieee):
    return f16_to_e8 if ieee else f16_to_e8_acc


def e2f(ieee):
    return e8_to_f16 if ieee else e8_to_f16_acc


def f16_to_e8(h):
    s, e, m = h >> 15, (h >> 10) & 0x1F, h & 0x3FF
    if e == 31:
        return (s << 23) | (0xFF << 15) | (m << 5)
    v = (
        Fraction(m, 1 << 24)
        if e == 0
        else Fraction(0x400 + m, 1 << 10) * Fraction(2) ** (e - 15)
    )
    return e8.encode(-v if s else v, s)


def e8_to_f16(w):
    """v2_e8_to_f16: RNE to FP16 with subnormals; past the top saturates to 65504."""
    s, e, m = e8.fields(w)
    if e == 0:
        return s << 15
    if e == 255:
        return (s << 15) | (0x1F << 10) | ((0x200 | (m >> 5)) if m else 0)
    v = e8.value(w)
    a = -v if s else v
    # FP16 grid: 2^-24 steps below 2^-14, 11-bit significands above
    big = a.numerator.bit_length() - a.denominator.bit_length()
    if Fraction(2) ** big > a:
        big -= 1
    step = Fraction(2) ** (max(big, -14) - 10)
    q = a / step
    n, r = divmod(q.numerator, q.denominator)
    half = 2 * r - q.denominator
    if half > 0 or (half == 0 and n & 1):
        n += 1
    val = n * step
    if val >= Fraction(65520):  # rounds past the largest finite: saturate
        return (s << 15) | 0x7BFF
    if val == 0:
        return s << 15
    big = val.numerator.bit_length() - val.denominator.bit_length()
    if Fraction(2) ** big > val:
        big -= 1
    if big < -14:
        return (s << 15) | int(val / Fraction(2) ** -24)
    mant = int(val / Fraction(2) ** (big - 10)) - 0x400
    return (s << 15) | ((big + 15) << 10) | mant


def alu(op, a, b, c):
    if op == MOV:
        return a if not e8.is_zero(a) else 0  # v2_alu moves -0 to +0
    if op == ADD:
        return e8.fma(a, 0x3F8000, c)
    if op == SUB:
        return e8.fma(a, 0x3F8000, c ^ 0x800000)
    if op == MUL:
        return e8.fma(a, b, 0, add=False)
    if op == FMA:
        return e8.fma(a, b, c)
    if op in (MAX, MIN):
        ka, kb = red.key(a), red.key(b)
        w = a if ((ka >= kb) if op == MAX else (ka <= kb)) else b
        return w if not e8.is_zero(w) else 0
    if op in (RCP, RSQRT):
        mode = fn.MANT if op == RCP else fn.HALF
        return fn.lane(a, 0, mode, 0, None, neg=1, mrom=MROM)
    raise ValueError(op)


def program(ops, x, b, rows, cols, table, rom):
    """A pass program on one word: ops as cfg.O fields with sources by name
    (A: X B O ("H", k); B, C: K R C B X ("H", k)), H k the result of op k."""
    vals = []

    def src(name, j, rsel, csel, kv):
        if isinstance(name, tuple):
            return vals[name[1]][j]
        if name == "X":
            return x[j]
        if name == "B":
            return b[j]
        if name == "O":
            return x[j ^ 1]
        if name == "K":
            return kv
        if name == "R":
            return rows[j // 4][rsel]
        return cols[j % 4][csel]

    for op in ops:
        res = []
        for j in range(16):
            av = src(op.get("A", "X"), j, 0, 0, 0) ^ (op.get("NA", 0) << 23)
            bv = src(
                op.get("B", "K"), j, op.get("RB", 0), op.get("CB", 0), op.get("KB", 0)
            )
            if op.get("KIND", 0):
                tab = (table[0], op.get("CS", 0)) if table else None
                pre = _signed6(op.get("PRE", 0))
                mode = 2 * op.get("MANT", 0) + op.get("FIX", 0)
                res.append(fn.lane(av, bv, mode, pre, tab, rom, neg=op.get("NEG", 0)))
            else:
                cv = src(
                    op.get("C", "K"),
                    j,
                    op.get("RC", 0),
                    op.get("CC", 0),
                    op.get("KC", 0),
                )
                cv ^= op.get("NC", 0) << 23
                res.append(e8.fma(av, bv, cv))
        vals.append(res)
    return vals[-1] if vals else list(x)


class SubRow:
    """One sub-row of a job: its words, its 4 rows' working set and T."""

    def __init__(self, job, g, a_words, b_words, rows_mem, ieee=True):
        self.G, self.P, self.FIN, self.OPS = job["G"], job["P"], job["FIN"], job["OPS"]
        self.f2e, self.e2f = f2e(ieee), e2f(ieee)
        nw = self.G["NW"]
        self.g = g
        self.words = a_words[g * nw : (g + 1) * nw]
        if self.G["USE_B"]:
            self.bws = b_words[g * nw : (g + 1) * nw]
        else:
            self.bws = [[0] * 16] * nw
        self.row0 = self.G["ROW0"] + 4 * g
        self.W = [list(rows_mem[self.row0 + r]) for r in range(4)]
        self.T = [0, 0, 0, 0]
        self.st1 = None
        if self.G["R1"]:
            self.st1 = []
            for r in range(4):
                best = max(
                    (wd[4 * r + cc] for wd in self.words for cc in range(4)),
                    key=lambda h: (
                        ((~h) & 0x7FFF) if h >> 15 else (0x8000 | (h & 0x7FFF))
                    ),
                )
                self.st1.append(self.f2e(best))

    def finalise(self, pnum, st2):
        G, ps = self.G, self.P[pnum]
        for k in range(ps["FINLEN"]):
            ins = self.FIN[ps["FINBASE"] + k]
            res = []
            for r in range(4):
                srcs = [
                    self.W[r][0],
                    self.W[r][1],
                    self.W[r][2],
                    self.T[r],
                    self.st1[r] if self.st1 else 0,
                    st2[r] if st2 else 0,
                    G["K0"],
                    G["K1"],
                    G["K2"],
                    G["K3"],
                ]
                res.append(
                    alu(ins["OP"], srcs[ins["A"]], srcs[ins["B"]], srcs[ins["C"]])
                )
            for r in range(4):
                if ins["DST"] == 3:
                    self.T[r] = res[r]
                else:
                    self.W[r][ins["DST"]] = res[r]

    def chain(self, pnum, table, cols_mem, rom=False):
        G, ps, W = self.G, self.P[pnum], self.W
        ops = self.OPS[ps["OPBASE"] : ps["OPBASE"] + ps["NOPS"]]
        sums = None
        fp = []
        for h, (wd, bw) in enumerate(zip(self.words, self.bws)):
            x = [self.f2e(v) for v in wd]
            if G["MASK"]:
                for j in range(16):
                    col = G["COLOFF"] + 4 * h + j % 4
                    row = G["ROWOFF"] + 4 * self.g + j // 4
                    if (col - row) > G["DHI"] or col >= G["CLIM"]:
                        x[j] = G["MVAL"]
            bv = [self.f2e(v) for v in bw]
            cw = cols_mem[G["COL0"] + h]
            cols = [[self.f2e(cw[c][0]), self.f2e(cw[c][1])] for c in range(4)]
            x = program(ops, x, bv, W, cols, table, rom)
            if ps["R2EN"]:
                sums = sums or [[] for _ in range(4)]
                for r in range(4):
                    sums[r].append(x)
            if ps["EMIT"] and G["OUT"]:
                fp.append([self.e2f(v) for v in x])
        st2 = None
        if ps["R2EN"]:
            st2 = []
            for r in range(4):
                sp, em, aa = red.row_sum(sums[r], r)
                st2.append(sum_to_e8(sp, em, aa))
        return fp, st2


def run(job, a_words, b_words, table, cols_mem, rows_mem, rom=False, ieee=True):
    """One job. a_words/b_words: lists of 16-FP16 lists in arrival order.
    table: (entries, cs) of the function table or None (rom: the 128-segment
    exp2 ROM). ieee: the FP16 form (module docstring).
    cols_mem: 512 x 4 x (C0, C1) FP16.
    rows_mem: 512 x [R0, R1, R2] E8M15, updated in place. Returns out words
    (int, 256-bit)."""
    G, P = job["G"], job["P"]
    nsr = G["NSR"]
    buf = G["BUF"]
    out_mode = G["OUT"]
    outs = []
    mx_rows = []  # per sub-row FP16 words awaiting MXFP7

    for g in range(nsr):
        sr = SubRow(job, g, a_words, b_words, rows_mem, ieee)
        if buf:
            sr.finalise(0, None)
            passes = range(1, G["NPASS"] + 1)
        else:
            passes = [0]
        for pnum in passes:
            fp, st2 = sr.chain(pnum, table, cols_mem, rom)
            if fp:
                if out_mode == 1:
                    outs += [pack16(w) for w in fp]
                else:
                    mx_rows.append(fp)
            if buf or P[pnum]["FINLEN"]:
                sr.finalise(pnum, st2)
        for r in range(4):
            rows_mem[sr.row0 + r] = sr.W[r]

    if out_mode in (2, 3):
        for fp in mx_rows:
            outs += mx7_words(fp, out_mode == 3)
    return outs


def _signed6(v):
    return v - 64 if v & 0x20 else v


def sum_to_e8(sp, em, a):
    if sp == 1:
        return e8.NAN
    if sp == 2:
        return 0xFF << 15
    if sp == 3:
        return (1 << 23) | (0xFF << 15)
    return e8.encode(Fraction(a) * Fraction(2) ** (em - 150), 0)


def pack16(vals):
    w = 0
    for j, v in enumerate(vals):
        w |= v << (16 * j)
    return w


def mx7_words(fp, b_layout):
    """FP16 sub-tile words of one sub-row -> MXFP7 words, 8 words an entry."""
    words = []
    for e in range(len(fp) // 8):
        blk = np.zeros((4, 32), dtype=np.uint16)
        for w in range(8):
            for r in range(4):
                for c in range(4):
                    blk[r, 4 * w + c] = fp[8 * e + w][4 * r + c]
        q, es, m8 = mxfp7.quantise_fp16(blk.view(np.float16).astype(np.float32))
        fields = (((es + mxfp7.SBIAS) << 3) | (m8 - 8)).reshape(4)
        for w in range(4):
            word = 0
            for slot in range(32):
                lane, kk = (slot % 4, slot // 4) if b_layout else (slot // 8, slot % 8)
                word |= (int(q[lane, w * 8 + kk]) & 0x7F) << (255 - slot * 7 - 6)
            for r in range(4):
                word |= int(fields[r]) << (31 - r * 8 - 7)
            words.append(word)
    return words
