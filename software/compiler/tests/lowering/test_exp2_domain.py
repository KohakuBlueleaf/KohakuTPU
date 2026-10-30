"""The EXP2 / EXP2D domain guard, run on the emitted L1 of every user.

The frozen ALU decodes specials only at |a| >= 256 and wraps [128, 256) and
(-256, -128) (v2_alu.v:374,420-471); as designed, |a| >= 128 is the special.
Each kernel compiles as lowered and under `l2.vector/exp2-wrapped-domain` (no
guard). `Core` runs a program's vector images in program order. The guarded
program on the frozen ALU must leave DRAM word for word as the unguarded one on
the ALU as designed, over inputs covering every argument class; the unguarded
program on the frozen ALU must not, nor the guarded one with its predicates
inverted or its compares' operands swapped. Mover copies are not modelled: a
buffer word no image wrote holds the input set's word.

Range analysis (`exp2_ranges`) drops the guard checks a proof covers: each
setting must leave DRAM as designed on every input set (a NaN at every lane
of a row included), drop guard ops exactly where it proves an argument, and
each of its mutants must break a user.
"""

import json
import math
import re

import numpy as np
import pytest

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.kernels.classes.policy import ShapeClass
from kohakutpu.application.runtime.offline import OfflineCard
from kohakutpu.language.l1 import ops
from kohakutpu.language.numerics import e8m15, fp16
from kohakutpu.language.text import module
from kohakutpu.language.text.syntax import (
    AddAssign,
    Assign,
    Call,
    Float,
    Int,
    Kw,
    Name,
    Neg,
    Offset,
    Slice,
    Tuple,
    View,
    fmt,
)
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs

MACHINE = "multimesh_v9t6"
MUTANT = "l2.vector/exp2-wrapped-domain"
VC = json.loads(rs.machine_json(MACHINE))["machine"]["vc"]
LANES, CHUNKS, VLMAX = VC["lanes"], VC["chunks"], VC["vlmax"]
L1_WORDS, TAU = VC["l1_words"], VC["tau"]
WORD = 2 * LANES
#: (s0, s1) of a walk the text leaves out (ISA.VC.027).
FLAT = (1, 4)
#: A vector register operand, whole or a view.
VREG = r"v\d+(?:\[[^\]]*\])?"

#: One argument of each class, fp16-exact.
REPS = [
    math.nan,
    -math.inf,
    -65504.0,
    -300.0,
    -256.0,
    -255.875,
    -200.0,
    -128.5,
    -128.0,
    -127.5,
    -127.0,
    -126.5,
    -126.0,
    -3.25,
    -0.0,
    0.0,
    0.75,
    64.5,
    127.875,
    128.0,
    150.5,
    255.875,
    256.0,
    300.0,
    65504.0,
    math.inf,
]
CLASSES = (
    "nan",
    "-inf",
    "<=-256",
    "(-256,-128)",
    "[-128,-127)",
    "[-127,-126)",
    "[-126,128)",
    "[128,256)",
    ">=256",
    "+inf",
)


def arg_class(a):
    if math.isnan(a):
        return "nan"
    if math.isinf(a):
        return "+inf" if a > 0 else "-inf"
    if a <= -256:
        return "<=-256"
    edges = ((-128, "(-256,-128)"), (-127, "[-128,-127)"), (-126, "[-127,-126)"))
    edges += ((128, "[-126,128)"), (256, "[128,256)"))
    return next((name for edge, name in edges if a < edge), ">=256")


# ------------------------------------------------------------------ the ALU
def raw_value(raw):
    """24 bits of an S register read as E8M15."""
    sign = -1.0 if raw >> 23 & 1 else 1.0
    e, m = raw >> 15 & 0xFF, raw & 0x7FFF
    if e == 0:
        return sign * 0.0
    if e == 0xFF:
        return sign * math.inf if m == 0 else math.nan
    return sign * (1 + m / 32768) * 2.0 ** (e - 127)


def biased(x):
    """E8M15's biased exponent of a finite nonzero value."""
    return math.frexp(abs(x))[1] + 126


def floor_b(b):
    """EXP2D's floor of b, clamped to [-256, 255] (v2_alu.v:438-448)."""
    if math.isnan(b) or math.isinf(b) or (b != 0 and biased(b) > 134):
        return -256 if math.copysign(1.0, b) < 0 else 255
    if b == 0 or biased(b) < 127:
        return -1 if b < 0 else 0
    return math.floor(b)


def exp2_lane(a, fb, wrap):
    """`(2^(a - fb), overflow)` of one lane. `wrap`: the frozen ALU, whose
    s8.17 reducer wraps below the |a| >= 256 specials; else the specials at
    |a| >= 128, as designed."""
    if math.isnan(a):
        return math.nan, True
    if math.isinf(a) or (a != 0 and biased(a) > (134 if wrap else 133)):
        return (math.inf, True) if a > 0 else (0.0, False)
    out = math.floor(abs(a) * 2.0**18)
    r = (out >> 1) + (out & 1)
    r = -r if a < 0 else r
    if wrap:
        r = (r + (1 << 24)) % (1 << 25) - (1 << 24)
    k, f = r >> 17, r & 0x1FFFF
    mant = float(e8m15.value(2.0 ** (f / 2.0**17)))
    if mant == 2.0:
        k, mant = k + 1, 1.0
    e = k + 127 - fb
    if e >= 255:
        return math.inf, True
    if e <= 0:
        return 0.0, False
    return math.ldexp(mant, e - 127), e >= 127 + TAU


def number(t):
    match t:
        case Int(value, _):
            return value
        case Float(value):
            return value
        case Neg(x):
            return -number(x)
    raise TypeError(f"no number {fmt(t)}")


def nib(t):
    """`(register, (base, stride))` of a destination or a walk's register."""
    if isinstance(t, View):
        (s,) = t.slices
        return t.name, (number(s.lo), 1) if isinstance(s, Slice) else (number(s), 0)
    return t.text, (0, 1)


# ----------------------------------------------------------------- memory
class Memory:
    """DRAM as 32-byte words keyed (buffer, byte offset); a word nothing wrote
    reads as `init(buffer, offset)`."""

    def __init__(self, init):
        self.init = init
        self.words = {}
        self.written = {}

    def read(self, at):
        if at not in self.words:
            self.words[at] = self.init(*at)
        return self.words[at]

    def write(self, at, word):
        self.words[at] = self.written[at] = word.copy()


class Core:
    """One vector core: its registers, predicates, scalars, L1 and sticky bit
    persist across its RUNs; descriptors and their running offsets restart."""

    def __init__(self, memory, wrap, seen):
        self.memory, self.wrap, self.seen = memory, wrap, seen
        self.v = np.zeros((VC["regs"], CHUNKS, LANES))
        self.p = np.zeros((VC["preds"], CHUNKS, LANES), bool)
        self.s = [0] * VC["sregs"]
        self.k = [0.0, 1.0, -1.0, 0.0]
        self.l1 = np.zeros((L1_WORDS, WORD), np.uint8)
        self.sticky = False
        self.ptr = {"uptr": 0, "uinc": 0, "pptr": 0, "pinc": 0}

    def run(self, image, args):
        self.bound = dict(zip(image.params, args, strict=True))
        self.desc, self.offset, self.ainc = {}, {}, {}
        self.block(image.stmts)

    def block(self, stmts):
        for st in stmts:
            if st.op == "halt":
                return True
            if st.op == "loop":
                times = max(self.s[int(st.positional()[0].text[1:])], 1)
                for _ in range(times):
                    if self.block(st.body):
                        return True
            elif st.op == "skipz":
                if self.s[int(st.positional()[0].text[1:])] and self.block(st.body):
                    return True
            else:
                self.step(st)
        return False

    def step(self, st):
        pos, kw = st.positional(), st.kwargs()
        match st.op:
            case "desc":
                self.descriptor(st)
            case "ainc":
                (a,) = st.args
                assert isinstance(a, AddAssign)
                self.ainc[a.lhs.text] = number(a.rhs)
            case "vfill" | "vdrain":
                assert "to" not in kw, "a peer drain"
                self.dma(st.op == "vfill", pos)
            case "vunpk" | "vpack":
                self.walk(st.op == "vunpk", pos, kw)
            case "seti":
                a = st.args[0]
                assert isinstance(a, Assign)
                value = a.rhs
                raw = (
                    e8m15.encode(number(value))
                    if isinstance(value, Float)
                    or isinstance(value, Neg)
                    and isinstance(value.x, Float)
                    else number(value) & 0xFFFFFF
                )
                if a.lhs.text == "k3":
                    self.k[3] = raw_value(raw)
                else:
                    self.s[int(a.lhs.text[1:])] = raw
            case "getstk":
                self.s[int(pos[0].text[1:])] = 0x3F8000 if self.sticky else 0
                self.sticky = False
            case "uptr" | "uinc" | "pptr" | "pinc":
                self.ptr[st.op] = number(pos[0])
            case "vsync" | "vbar" | "vmark" | "vwait" | "vpost":
                pass
            case op if op in ops.MATH:
                self.math(st, pos, kw)
            case op:
                raise NotImplementedError(f"no model of {op}")

    # -------------------------------------------------------------- DMA
    def address(self, t):
        """`(buffer, byte offset)` of an image parameter, maybe offset."""
        if isinstance(t, Offset):
            buf, at = self.address(t.base)
            return buf, at + number(t.off)
        bound = self.bound[t.text]
        if isinstance(bound, Offset):
            return bound.base.text, number(bound.off)
        return bound.text, 0

    def descriptor(self, st):
        kw = st.kwargs()
        walk = kw.pop("walk")
        ((ad, base),) = kw.items()
        items = walk.items
        dims = [items] if not isinstance(items[0], Tuple) else [d.items for d in items]
        self.desc[ad] = (
            self.address(base),
            [(number(s), max(number(b), 1)) for s, b in dims],
        )
        self.offset[ad] = 0

    def dma(self, fill, pos):
        ad, off = pos[0].text, number(pos[2].slices[0])
        rel = Name("rel") in pos[3:]
        (buf, base), dims = self.desc[ad]
        if rel:
            base += self.offset[ad]
            self.offset[ad] += self.ainc.get(ad, 0)
        at = [0]
        for stride, bound in dims:
            at = [x + i * stride for i in range(bound) for x in at]
        for i, a in enumerate(at):
            word = (off + i) % L1_WORDS
            if fill:
                self.l1[word] = self.memory.read((buf, base + a))
            else:
                self.memory.write((buf, base + a), self.l1[word])

    # ---------------------------------------------------------- unpack/pack
    def walk(self, unpack, pos, kw):
        reg, (cb, _) = nib(pos[0])
        reg = int(reg[1:])
        mode = pos[3].text
        n = number(kw["n"]) if "n" in kw else CHUNKS - cb
        s0, s1 = (number(x) for x in kw["walk"].items) if "walk" in kw else FLAT
        base = number(pos[2].slices[0])
        if Name("rel") in pos[4:]:
            ptr, inc = ("uptr", "uinc") if unpack else ("pptr", "pinc")
            base += self.ptr[ptr]
            self.ptr[ptr] += self.ptr[inc]
        count = 2 * n if mode == "f32" else n
        words = [(base + (w % 4) * s0 + (w // 4) * s1) % L1_WORDS for w in range(count)]
        chunks = [(cb + i) % CHUNKS for i in range(n)]
        if unpack:
            got = self.l1[words]
            if mode == "gt4":
                got = gt4(got)
            if mode == "f32":
                lanes = e8m15.value(got.reshape(n, LANES * 4).view("<f4"))
            else:
                lanes = got.view("<f2").astype(np.float64)
            self.v[reg, chunks] = lanes
            return
        lanes = self.v[reg, chunks]
        if mode == "f32":
            out = lanes.astype("<f4").view(np.uint8).reshape(count, WORD)
        else:
            out = fp16.write(lanes).astype("<f2").view(np.uint8).reshape(n, WORD)
        self.l1[words] = gt4(out) if mode == "gt4" else out

    # ------------------------------------------------------------------ math
    def source(self, t):
        """An operand: ("v", register, base, stride, crossbar) or ("c", value,
        nonzero): a scalar's nonzero is its raw magnitude bits (v2_alu.v:219),
        not its value (raw 1 reads as 0.0)."""
        if isinstance(t, Call):
            inner = self.source(t.args[0])
            kw = {a.name: number(a.value) for a in t.args[1:] if isinstance(a, Kw)}
            rest = [number(a) for a in t.args[1:] if not isinstance(a, Kw)]
            if t.name == "bcast":
                xbar = ("bcast", kw.get("lane", 0), kw.get("sh", 0))
            else:
                xbar = (t.name, rest[0], 0)
            return inner[:4] + (xbar,)
        name, (base, stride) = nib(t)
        kind, n = name[0], int(name[1:])
        if kind == "v":
            return ("v", n, base, stride, None)
        if kind == "s":
            return ("c", raw_value(self.s[n]), self.s[n] & 0x7FFFFF != 0)
        return ("c", self.k[n], self.k[n] != 0)

    def lanes(self, src, i):
        if src[0] == "c":
            return np.full(LANES, src[1])
        _, reg, base, stride, _ = src
        return self.v[reg, (base + stride * i) % CHUNKS].copy()

    def math(self, st, pos, kw):
        op = st.op
        slots = ["a"] + ["b"] * (op in ops.READS_B) + ["c"] * (op in ops.READS_C)
        assert len(pos) == 1 + len(slots), fmt(st)
        src = {s: self.source(t) for s, t in zip(slots, pos[1:], strict=True)}
        dest, (dbase, dstride) = nib(pos[0])
        vl = number(kw["vl"]) if "vl" in kw else VLMAX
        lane = np.arange(LANES)
        writes = []
        for i in range(-(-vl // LANES)):
            vals = {s: self.lanes(src[s], i) for s in slots}
            crossed = [s for s in slots if src[s][0] == "v" and src[s][4] is not None]
            if crossed:
                mode, k, sh = src[crossed[0]][4]
                x = vals[crossed[0]]
                if mode == "merge":
                    a = vals["a"]
                    vals["a"] = np.where(lane & k == 0, a, x)
                    x = np.where(lane & k == 0, a[lane ^ k], x[lane ^ k])
                elif mode == "xor":
                    x = x[lane ^ k]
                elif mode == "rot":
                    x = x[(lane + k) % LANES]
                else:
                    x = np.full(LANES, x[(k + (i >> sh)) % LANES])
                for s in crossed:
                    vals[s] = x
            if "c" in vals:
                c = src["c"]
                vals["cnz"] = np.full(LANES, c[2]) if c[0] == "c" else vals["c"] != 0
            mask = lane < vl - i * LANES
            chunk = (dbase + dstride * i) % CHUNKS
            writes.append((chunk, mask, self.alu(op, vals, mask)))
        if op in ops.COMPARE:
            pr = int(dest[1:])
            for chunk, mask, (r, _) in writes:
                self.p[pr, chunk, mask] = r[mask]
            return
        reg = int(dest[1:])
        for chunk, mask, (r, ovf) in writes:
            if "if" in kw:
                mask = mask & self.p[int(kw["if"].text[1:]), chunk]
            if "unless" in kw:
                mask = mask & ~self.p[int(kw["unless"].text[1:]), chunk]
            self.sticky |= bool(ovf.any())
            self.v[reg, chunk, mask] = r[mask]

    def alu(self, op, x, mask):
        """`(result, sticky lanes)` of one beat."""
        a, b, c = x.get("a"), x.get("b"), x.get("c")
        none = np.zeros(LANES, bool)
        with np.errstate(all="ignore"):
            match op:
                case "vmov":
                    return e8m15.moved(a), none
                case "vneg":
                    return e8m15.moved(-a), none
                case "vabs":
                    return e8m15.moved(np.abs(a)), none
                case "vadd":
                    return e8m15.fma(a, 1.0, c), none
                case "vsub":
                    return e8m15.fma(a, 1.0, -c), none
                case "vmul":
                    return e8m15.fma(a, b, 0.0), none
                case "vfma":
                    return e8m15.fma(a, b, c), none
                case "vfnma":
                    return e8m15.fma(-a, b, c), none
                case "vmax":
                    return e8m15.moved(np.where(a < b, b, a)), none
                case "vmin":
                    return e8m15.moved(np.where(a > b, b, a)), none
                case "vsel":
                    return e8m15.moved(np.where(x["cnz"], a, b)), none
                case "vinv":
                    return e8m15.value(1.0 / a), none
                case "vrsqrt":
                    return e8m15.value(1.0 / np.sqrt(a)), none
                case "vlog2":
                    return e8m15.value(np.log2(a)), none
                case "vcmplt":
                    return a < b, none
                case "vcmpgt":
                    return a > b, none
                case "vcmpeq":
                    return a == b, none
                case "vexp2" | "vexp2d":
                    fbs = [floor_b(v) for v in b] if op == "vexp2d" else [0] * LANES
                    got = [exp2_lane(v, fb, self.wrap) for v, fb in zip(a, fbs)]
                    for v, fb, m in zip(a, fbs, mask):
                        if m:
                            self.seen.append((op, float(v), fb))
                    ovf = np.array([o for _, o in got]) & mask
                    return np.array([r for r, _ in got]), (
                        ovf if op == "vexp2d" else none
                    )
        raise NotImplementedError(f"no ALU op {op}")


def gt4(words):
    """The GT4 transposition of each group of four words' 8-byte granules
    (ISA.VC.108); it is its own inverse (ISA.VC.109)."""
    g = words.reshape(-1, 4, 4, 8)
    return g.transpose(0, 2, 1, 3).reshape(-1, WORD)


def execute(l1, init, wrap):
    """`(DRAM words the program wrote, EXP2 arguments)` of an L1 text."""
    m = module.read(l1)
    (body,) = [b for (_, level), b in m.fns.items() if level == "l1"]
    memory, seen, cores = Memory(init), [], {}

    def walk(stmts, unit):
        for st in stmts:
            if st.op == "send":
                walk(st.body, fmt(st.positional()[0]))
            elif st.op == "run":
                (call,) = st.positional()
                core = cores.setdefault(unit, Core(memory, wrap, seen))
                core.run(m.images[call.name], call.args)

    walk(body.stmts, None)
    return memory.written, seen


# ------------------------------------------------------------- input sets
def word_of(values):
    return fp16.write(np.asarray(values, np.float64)).astype("<f2").view(np.uint8)


def uniform(value):
    word = word_of([value] * LANES)
    return lambda buf, at: word


def pair(a, b, first):
    """Buffer `first` holds `a` everywhere, every other buffer `b`."""
    wa, wb = word_of([a] * LANES), word_of([b] * LANES)
    return lambda buf, at: wa if buf == first else wb


def mixed(buf, at):
    start = (at // WORD) * LANES + sum(map(ord, buf))
    return word_of([REPS[(start + i) % len(REPS)] for i in range(LANES)])


def input_sets(two):
    """Every class in every lane alone, one mixed set, and with two inputs
    every pair of classes."""
    if two is None:
        return [uniform(v) for v in REPS] + [mixed]
    sub = [math.nan, -math.inf, -300.0, -200.0, -127.5, -126.5, -3.25, 0.0, 0.75]
    sub += [127.875, 150.5, 300.0, math.inf]
    return [pair(a, b, two) for a in REPS for b in sub] + [mixed]


# ------------------------------------------------------------------ users
HEAD = "target ktpu.v9\n\n"


def rows(shape, body, gt4_=False):
    r, c = shape
    tile = "(tile=1x8)" if gt4_ else ""
    l1 = "@l1(gt4)" if gt4_ else "@l1"
    reg = "@reg(gt4)" if gt4_ else "@reg"
    load = "x" if gt4_ else f"x[t*{r} : +{r}, :]"
    store = "y" if gt4_ else f"y[t*{r} : +{r}, :]"
    lines = "\n".join(
        "            " + ln.format(reg=reg, r=r, c=c)
        for ln in body.strip().splitlines()
    )
    return (
        HEAD
        + f"fn k.l2(x: f16[{r}, {c}] @dram{tile}, y: f16[{r}, {c}] @dram{tile})\n"
        + "    par c in 0..1 on vc[c]\n"
        + "        for t in 0..1\n"
        + f"            xl = load {load} : f16[{r}, {c}] {l1}\n"
        + f"            v = unpack xl : f32[{r}, {c}] {reg}\n"
        + lines
        + f"\n            ol = pack o : f16[{r}, {c}] {l1}\n"
        + f"            store {store} <- ol\n"
    )


MODULO = (
    HEAD
    + "fn k.l2(x: f16[128, 128] @dram, y: f16[128, 128] @dram)\n"
    + "    par c in 0..1 on vc[c]\n"
    + "        for t in 0..16\n"
    + "            xl = load x[t*8 : +8, :] : f16[8, 128] @l1\n"
    + "            v = unpack xl : f32[8, 128] @reg\n"
    + "            o = {op} : f32[8, 128] @reg\n"
    + "            ol = pack o : f16[8, 128] @l1\n"
    + "            store y[t*8 : +8, :] <- ol\n"
)

STICKY = rows(
    (8, 128),
    """
p = exp2d v, 0.0 : f32[{r}, {c}] {reg}
if sticky
    m = rmax v : f32[{r}] @reg
    p = exp2d v, m[:, *] : f32[{r}, {c}] {reg}
l = rsum p : f32[{r}] @reg
r = inv l : f32[{r}] @reg
o = mul p, r[:, *] : f32[{r}, {c}] {reg}
""",
)


def classed(e, out):
    machine = OfflineCard(MACHINE).machine
    kernel, given, _ = V.rows_kernel(
        e,
        (V.Operand("full", pitch=128),),
        32,
        128,
        out,
        machine.count("VC"),
        shape_class=ShapeClass(32, 256),
    )
    return json.dumps(kernel.trace(given, machine=K.dsl_machine(machine)).graph)


def two_inputs(shape, body="o = exp2d v, u : f32[{r}, {c}] @reg"):
    """`body` over `v` (from `x`) and `u` (from `w`), into `o`."""
    r, c = shape
    lines = "\n".join(
        "            " + ln.format(r=r, c=c) for ln in body.strip().splitlines()
    )
    return (
        HEAD
        + f"fn k.l2(x: f16[{r}, {c}] @dram, w: f16[{r}, {c}] @dram, "
        + f"y: f16[{r}, {c}] @dram)\n"
        + "    par c in 0..1 on vc[c]\n"
        + "        for t in 0..1\n"
        + f"            xl = load x[t*{r} : +{r}, :] : f16[{r}, {c}] @l1\n"
        + f"            wl = load w[t*{r} : +{r}, :] : f16[{r}, {c}] @l1\n"
        + f"            v = unpack xl : f32[{r}, {c}] @reg\n"
        + f"            u = unpack wl : f32[{r}, {c}] @reg\n"
        + lines
        + f"\n            ol = pack o : f16[{r}, {c}] @l1\n"
        + f"            store y[t*{r} : +{r}, :] <- ol\n"
    )


#: (user, source, two-input buffer, op, what its guarded L1 shows).
USERS = [
    (
        "band rows",
        rows((8, 128), "o = exp2 v : f32[{r}, {c}] {reg}"),
        None,
        "vexp2",
        r"vmax v\d+, v\d+, s\d+$",
    ),
    (
        "gt4 rows",
        rows((16, 32), "o = exp2 v : f32[{r}, {c}] {reg}", gt4_=True),
        None,
        "vexp2",
        r"vunpk .* gt4",
    ),
    (
        "row statistics",
        rows(
            (8, 128),
            "m = rmax v : f32[{r}] @reg\ne = exp2 m : f32[{r}] @reg\no = mul v, e[:, *] : f32[{r}, {c}] {reg}",
        ),
        None,
        "vexp2",
        r"vcmplt p\d, v\d+\[0 :\], s\d+ vl=16",
    ),
    (
        "gt4 statistics",
        rows(
            (16, 32),
            "m = rmax v : f32[{r}] @reg\ne = exp2 m : f32[{r}] @reg\no = mul v, e[:, *] : f32[{r}, {c}] {reg}",
            gt4_=True,
        ),
        None,
        "vexp2",
        r"vcmplt p\d, v\d+, s\d+$",
    ),
    ("exp2d rows", two_inputs((8, 128)), "w", "vexp2d", rf"vexp2d v\d+, v\d+, {VREG}$"),
    (
        "exp2d row broadcast",
        rows(
            (8, 128),
            "m = rmax v : f32[{r}] @reg\no = exp2d v, m[:, *] : f32[{r}, {c}] {reg}",
        ),
        None,
        "vexp2d",
        r"vexp2d v\d+, v\d+, bcast\(",
    ),
    (
        "exp2d gt4 broadcast",
        rows(
            (16, 32),
            "m = rmax v : f32[{r}] @reg\no = exp2d v, m[:, *] : f32[{r}, {c}] {reg}",
            gt4_=True,
        ),
        None,
        "vexp2d",
        r"vunpk .* gt4",
    ),
    (
        "exp2d constant",
        rows((8, 128), "o = exp2d v, 0.0 : f32[{r}, {c}] {reg}"),
        None,
        "vexp2d",
        r"vexp2d v\d+, v\d+, (s|k)\d+$",
    ),
    (
        "exp2d statistics",
        rows(
            (8, 128),
            "m = rmax v : f32[{r}] @reg\nq = exp2d m, 0.0 : f32[{r}] @reg\no = mul v, q[:, *] : f32[{r}, {c}] {reg}",
        ),
        None,
        "vexp2d",
        r"vexp2d v\d+\[0 :\], .* vl=16",
    ),
    ("sticky", STICKY, None, "vexp2d", r"getstk s\d+"),
    (
        "compare mask",
        rows(
            (4, 128),
            "e = exp2 v : f32[{r}, {c}] {reg}\nm = cmpgt v, e : f32[{r}, {c}] {reg}\no = mul v, m : f32[{r}, {c}] {reg}",
        ),
        None,
        "vexp2",
        r"vmov v\d+, k1 if=p\d",
    ),
    (
        "select",
        rows(
            (4, 128),
            "e = exp2 v : f32[{r}, {c}] {reg}\nm = cmplt v, e : f32[{r}, {c}] {reg}\no = sel m, v, e : f32[{r}, {c}] {reg}",
        ),
        None,
        "vexp2",
        rf"vmov v\d+, {VREG} if=p\d",
    ),
    ("modulo", MODULO.format(op="exp2 v"), None, "vexp2", r"unless=p[2-4]"),
    ("modulo exp2d", MODULO.format(op="exp2d v, 0.0"), None, "vexp2d", r"if=p[2-4]"),
    ("classed", "classed rows", None, "vexp2", r"skipz s\d+"),
    ("classed statistics", "classed stats", None, "vexp2", r"skipz s\d+"),
]


def source_of(src):
    if src == "classed rows":
        return classed(X.op("exp2", X.Load(0)), V.Operand("full", pitch=128))
    if src == "classed stats":
        return classed(X.op("exp2", X.Reduce("max", X.Load(0))), V.Operand("row"))
    return src


def compile_l1(src, mutant, ranges=None):
    """L1 of `src` under `mutant`, the range analysis at `ranges` (None: the
    compiler's default)."""
    machine = OfflineCard(MACHINE).machine
    got = rs._rs.compile(
        source_of(src),
        rs.machine_json(machine),
        cache_dir="",
        fetch=True,
        mutant=mutant,
        exp2_ranges=ranges,
    )
    assert not got["verdict"]["refused"], got["verdict"]
    return got["l1"]


def count(pattern, text):
    return len(re.findall(rf"^\s*{pattern}", text, re.MULTILINE))


def guarded_ops(l1, op):
    """Whether every `op` of `l1` is completed by one guard op, and it has some."""
    return count(op + " ", l1) == count(GUARD[op], l1) > 0


def inverted(l1):
    """Every predicated write's sense inverted."""
    swap = {"if": "unless", "unless": "if"}
    return re.sub(r"\b(if|unless)=", lambda m: swap[m[1]] + "=", l1)


OPERAND = r"[vsk]\d+(?:\[[^\]]*\])?"
#: The op that completes each guard: EXP2's doubling of its own temporary,
#: EXP2D's move of the in-range argument.
GUARD = {
    "vexp2": rf"vadd ({VREG}), \1, \1 unless=p\d",
    "vexp2d": rf"vmov {VREG}, {VREG} if=p\d",
}


def swapped(l1):
    """Every compare's operands swapped."""
    return re.sub(
        rf"(vcmplt p\d+(?:\[[^\]]*\])?), ({OPERAND}), ({OPERAND})",
        r"\1, \3, \2",
        l1,
    )


def differing(program, wants, sets):
    """Input sets on which `program` on the frozen ALU leaves DRAM other
    than `wants` says."""
    bad = []
    for n, (want, init) in enumerate(zip(wants, sets, strict=True)):
        got, _ = execute(program, init, wrap=True)
        if want.keys() != got.keys() or any(
            not np.array_equal(want[k], got[k]) for k in want
        ):
            bad.append(n)
    return bad


@pytest.fixture(scope="module", autouse=True)
def carried():
    if MUTANT not in rs.mutants():
        pytest.skip("_rs carries no mutants (built without the `mutants` feature)")


@pytest.mark.parametrize("user", USERS, ids=[u[0] for u in USERS])
def test_guarded_exp2_is_the_designed_alu_on_every_class(user):
    name, src, two, op, shows = user
    guarded, plain = compile_l1(src, None), compile_l1(src, MUTANT)

    # The guard and its mutant may lower to different variants (another
    # modulo unroll): each program is checked against its own EXP2 ops.
    assert guarded_ops(guarded, op)
    assert count(op + " ", plain) > 0 == count(GUARD[op], plain)
    one_dropped = re.sub(
        rf"^\s*{GUARD[op]}.*\n", "", guarded, count=1, flags=re.MULTILINE
    )
    assert not guarded_ops(one_dropped, op)
    assert re.search(shows, guarded, re.MULTILINE), f"{name}: no `{shows}`"
    sets = input_sets(two)

    wants, seen = [], set()
    for init in sets:
        want, args = execute(plain, init, wrap=False)
        wants.append(want)
        seen |= {(o, arg_class(a), fb < 0) for o, a, fb in args}
    assert {c for o, c, _ in seen if o == op} == set(CLASSES)
    if two is not None:
        assert (op, "[-128,-127)", True) in seen

    assert differing(guarded, wants, sets) == []
    assert differing(plain, wants, sets) != []
    assert differing(inverted(guarded), wants, sets) != []
    assert differing(swapped(guarded), wants, sets) != []


RANGES = ("off", "intervals", "rowmax")
ROW = ": f32[{r}, {c}] @reg"
STAT = ": f32[{r}] @reg"


def body(*ops):
    """Row ops, each typed as a row (a statistic for `rmax` / `rsum`)."""
    return "\n".join(
        f"{op} {STAT if ' rmax ' in op or ' rsum ' in op else ROW}" for op in ops
    )


#: `e - rmax e` into EXP2: normalised by the row sum, and stored as it is.
SOFTMAX_HEAD = (
    "e = mul v, 0.7213475204444817",
    "s = rmax e",
    "d = sub e, s[:, *]",
    "p = exp2 d",
)
SOFTMAX = rows(
    (8, 128),
    "\n".join(
        (
            body(*SOFTMAX_HEAD, "l = rsum p"),
            f"r = inv l {STAT}",
            "o = mul p, r[:, *] " + ROW,
        )
    ),
)
UNNORMALISED = rows((8, 128), body(*SOFTMAX_HEAD, "o = mov p"))
#: `c = max a, y`: a NaN `y` gives `a`, which reaches -200.
NAN_SECOND = two_inputs(
    (8, 128),
    body(
        "a = min v, -150.0",
        "a = max a, -200.0",
        "y = max u, 50.0",
        "c = max a, y",
        "d = min c, 100.0",
        "o = exp2 d",
    ),
)
#: `a` clamped only inside `if sticky`, which `u`'s EXP2D overflow sets.
STICKY_REBOUND = two_inputs(
    (8, 128),
    f"""
p = exp2d u, 0.0 {ROW}
a = mul v, 2.0 {ROW}
if sticky
    b = min v, 10.0 {ROW}
    a = max b, -10.0 {ROW}
q = exp2 a {ROW}
o = mul q, p {ROW}
""",
)

#: (user, source, two-input buffer, the settings proving an argument).
RANGED = [
    ("unbounded", rows((8, 128), body("o = exp2 v")), None, ()),
    (
        "clamped",
        rows((8, 128), body("b = min v, 100.0", "a = max b, -100.0", "o = exp2 a")),
        None,
        ("intervals", "rowmax"),
    ),
    (
        "clamped above",
        rows((8, 128), body("b = min v, 100.0", "o = exp2 b")),
        None,
        ("intervals", "rowmax"),
    ),
    (
        "tanh bounded",
        rows(
            (8, 128),
            body(
                "a = mul v, -2.8853900817779268",
                "b = exp2 a",
                "c = add b, 1.0",
                "d = inv c",
                "f = fma d, 2.0, -1.0",
                "g = mul f, 8.0",
                "o = exp2 g",
            ),
        ),
        None,
        ("intervals", "rowmax"),
    ),
    (
        "exp2d clamped above",
        rows((8, 128), body("b = min v, 100.0", "o = exp2d b, 0.0")),
        None,
        ("intervals", "rowmax"),
    ),
    (
        "exp2d clamped below",
        rows((8, 128), body("b = max v, -100.0", "o = exp2d b, 0.0")),
        None,
        ("intervals", "rowmax"),
    ),
    ("nan second", NAN_SECOND, "w", ("intervals", "rowmax")),
    ("sticky rebound", STICKY_REBOUND, "w", ()),
    ("softmax", SOFTMAX, None, ("rowmax",)),
    ("unnormalised softmax", UNNORMALISED, None, ()),
]


def guard_share(l1):
    """Guard-like math lines over EXP2 / EXP2D lines: both scale with the
    variant's row instances, the guard ops alone with the proofs."""
    math_ = count(r"(vmax|vmin|vabs|vcmplt|vcmpgt|vadd|vmov)\b", l1)
    return math_ / count(r"vexp2d? ", l1)


def nan_rows():
    """Row words with a NaN in lane `p` (each slot of every tree level) and
    250 in lane `q`, else 0: input sets, every buffer word one of them."""
    for p in range(LANES):
        for q in (0, 5, 10, 15):
            if p != q:
                values = [0.0] * LANES
                values[p], values[q] = math.nan, 250.0
                word = word_of(values)
                yield lambda buf, at, word=word: word


@pytest.mark.parametrize("user", RANGED, ids=[u[0] for u in RANGED])
def test_range_analysis_drops_only_proven_guards(user):
    _, src, two, proven = user
    plain = compile_l1(src, MUTANT)
    sets = input_sets(two) + (list(nan_rows()) if two is None else [])
    wants = [execute(plain, init, wrap=False)[0] for init in sets]
    l1 = {r: compile_l1(src, None, r) for r in RANGES}
    assert compile_l1(src, None) == l1["rowmax"], "the default is rowmax"
    for r in RANGES:
        if r in proven:
            assert guard_share(l1[r]) < guard_share(l1["off"]), r
        else:
            assert l1[r] == l1["off"], r
        assert differing(l1[r], wants, sets) == [], r


def test_a_nan_row_breaks_the_row_maximum_bound_itself():
    """`vmax a, NaN` keeps `a`: the lane tree drops a subtree and its maximum,
    so `e - rmax e` reaches EXP2 at 128 and above on a row holding a NaN.
    RowMax refuses the bound where `p` is stored as it is; taken anyway
    (`l2.vector/exp2-rowmax-unpoisoned`), DRAM differs on such rows."""
    plain = compile_l1(UNNORMALISED, MUTANT)
    sets = list(nan_rows())
    wants = [execute(plain, init, wrap=False)[0] for init in sets]
    assert differing(compile_l1(UNNORMALISED, None), wants, sets) == []
    taken = compile_l1(UNNORMALISED, "l2.vector/exp2-rowmax-unpoisoned")
    assert differing(taken, wants, sets) != []


#: Each range-analysis mutant and a user it must break.
RANGE_MUTANTS = [
    ("l2.vector/exp2-range-assumed", "clamped above"),
    ("l2.vector/exp2-range-assumed", "exp2d clamped above"),
    ("l2.vector/exp2-range-assumed", "exp2d clamped below"),
    ("l2.vector/exp2-floor-everywhere", "unbounded"),
    ("l2.vector/exp2-range-nan-blind", "nan second"),
    ("l2.vector/exp2-range-stale", "sticky rebound"),
]


@pytest.mark.parametrize("mutant, user", RANGE_MUTANTS)
def test_each_range_mutant_breaks_a_user(mutant, user):
    _, src, two, _ = next(u for u in RANGED if u[0] == user)
    plain = compile_l1(src, MUTANT)
    sets = input_sets(two)
    wants = [execute(plain, init, wrap=False)[0] for init in sets]
    assert differing(compile_l1(src, None), wants, sets) == []
    bad = differing(compile_l1(src, mutant), wants, sets)
    assert bad != []
    if mutant == "l2.vector/exp2-floor-everywhere":
        # Every [128, 256) argument wraps past the floor alone.
        wrapped = [n for n, v in enumerate(REPS) if arg_class(v) == "[128,256)"]
        assert set(wrapped) <= set(bad)


SELECT = HEAD + "image w()\n    seti s1 = {raw}\n    vsel v2, v0, v1, s1\n    halt\n"


@pytest.mark.parametrize(
    "raw, picks_a",
    [
        (0x000001, True),  # E8M15 exponent 0: reads as 0.0, its bits are not
        (0x800000, False),  # -0: sign bit only
        (0x3F8000, True),  # 1.0: exponent bits only
        (0x000000, False),
    ],
)
def test_vsel_tests_a_scalar_condition_by_its_magnitude_bits(raw, picks_a):
    """VSEL picks `a` where the condition's bits 22:0 are not all zero
    (v2_alu.v:219, `sel_nz = |s1_c[22:0]`), not where its value is nonzero:
    the classed kernels' `seti s13 = 1` keeps a row."""
    image = module.read(SELECT.format(raw=hex(raw))).images["w"]
    core = Core(Memory(None), wrap=True, seen=[])
    core.v[0], core.v[1] = 1.0, 2.0
    core.run(image, [])
    assert (core.v[2] == (1.0 if picks_a else 2.0)).all()
