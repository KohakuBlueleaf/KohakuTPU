"""The Streaming Row Unit's instructions and configuration map.

The authority is ``src/kohakutpu/sru/sru_cu.v`` (the five instructions) and
this module's map, from which ``scripts/py/sru/cfg.py`` writes the RTL's
``sru_cfg.vh``. A configuration is 32-bit words at 8-bit addresses, written by
CFG; every field is (word, low bit, width). docs/projects/kohakutpu/sru.md
is the design.

A pass runs a PROGRAM of NOPS pool ops (OPBASE.. in the op store) on every
word; an op is an FMA or a table function on every element. `schedule` turns
a program into the op-store fields for a pool of F_LANES lanes: issue
offsets on the modulo schedule, word-slot depths and result-tap depths.
"""

import math
from fractions import Fraction

from kohakuaccel.compiler.isa import Field, InstFormat, InstSet

# ---------------------------------------------------------------- the map
#: Global words.
G = {
    "NW": (0x00, 0, 12),  # words per sub-row
    "NPASS": (0x00, 12, 2),  # passes - 1
    "BUF": (0x00, 14, 1),  # pass 0 fills the sub-row buffer; passes 1.. replay it
    "USE_B": (0x00, 15, 1),  # stream B pairs with A (a single-pass program)
    "OUT": (0x00, 16, 2),  # 0 none, 1 f16, 2 MXFP7 A, 3 MXFP7 B
    "MASK": (0x00, 18, 1),  # the index mask on the word as it enters
    "R1": (0x00, 19, 1),  # R1 (row max) on the input
    "NSR": (0x01, 0, 16),  # sub-rows in the job
    "ROW0": (0x01, 16, 9),  # row-state row of the job's first row
    "COL0": (0x02, 0, 9),  # column-constant word of the job's first word
    "DHI": (0x02, 16, 16),  # mask: col - row > DHI (signed)
    "CLIM": (0x03, 0, 16),  # mask: col >= CLIM
    "MVAL": (0x04, 0, 24),  # the masked value (E8M15)
    "COLOFF": (0x05, 0, 16),  # absolute column of the job's column 0 (signed)
    "ROWOFF": (0x05, 16, 16),  # absolute row of the job's row 0 (signed)
    "K0": (0x08, 0, 24),  # finalise constants (E8M15)
    "K1": (0x09, 0, 24),
    "K2": (0x0A, 0, 24),
    "K3": (0x0B, 0, 24),
}

#: Per pass p, at PASS_BASE + PASS_STRIDE * p.
PASS_BASE, PASS_STRIDE, NPASS = 0x10, 0x02, 4
P = {
    "NOPS": (0, 0, 4),  # pool ops of the pass's program (0: the word as it enters)
    "OPBASE": (0, 4, 4),  # its first op in the op store
    "R2EN": (0, 8, 1),  # R2 sums the program's result
    "EMIT": (0, 9, 1),  # the result leaves (f16 / MXFP7)
    "FINLEN": (0, 10, 4),  # finalise steps after this pass's statistics
    "FINBASE": (0, 14, 4),
}

#: Op store: op i at OP_BASE + OP_STRIDE * i, read by the pool one op a cycle.
OP_BASE, OP_STRIDE, OP_DEPTH = 0x40, 0x04, 16
O = {
    "KIND": (0, 0, 1),  # 0 FMA (+-a) * b + (+-c); 1 function f(a), k = b
    "A": (0, 1, 2),  # ASRC
    "B": (0, 3, 3),  # BSRC
    "C": (0, 6, 3),  # BSRC
    "NA": (0, 9, 1),
    "NC": (0, 10, 1),
    "RB": (0, 11, 2),  # R0 / R1 / R2 for b
    "RC": (0, 13, 2),
    "CB": (0, 15, 1),  # C0 / C1 for b
    "CC": (0, 16, 1),
    "FIX": (0, 17, 1),  # function: FIX mode
    "CS": (0, 18, 2),  # function: table slope scale
    "PRE": (0, 20, 6),  # function: x * 2^PRE (signed)
    "HA": (0, 26, 1),  # result tap (H0 / H1) of a, b, c when they read H
    "HB": (0, 27, 1),
    "HC": (0, 28, 1),
    "MANT": (0, 29, 1),  # function: MANT (FIX 0) / HALF (FIX 1) on the table
    "NEG": (0, 30, 1),  # function, MANT / HALF: n = -e / -q
    "KB": (1, 0, 24),
    "KW": (1, 24, 5),  # schedule: word slots between the word's entry and this op
    "KC": (2, 0, 24),
    "D0": (3, 0, 5),  # schedule: H0 / H1 tap depths
    "D1": (3, 5, 5),
}
#: Operand sources; O is the pair partner's X (element j ^ 1).
ASRC = {"X": 0, "B": 1, "O": 2, "H": 3}
BSRC = {"K": 0, "R": 1, "C": 2, "B": 3, "X": 4, "H": 5}

#: Finalise program, word FIN_BASE + i: one op on every row of a sub-row.
FIN_BASE, FIN_DEPTH = 0x80, 16
F = {
    "OP": (0, 5),
    "DST": (5, 2),  # 0 R0, 1 R1, 2 R2, 3 T
    "A": (7, 4),  # source codes below
    "B": (11, 4),
    "C": (15, 4),
}
#: Finalise opcodes: ADD/SUB/MUL/FMA and RCP/RSQRT (lane 0's mantissa ROM,
#: FIN_RCP) on pool lane 0, MOV/MAX/MIN on the compare-select (sru_core.v).
FOP = {
    "mov": 0,
    "add": 3,
    "sub": 4,
    "mul": 5,
    "fma": 6,
    "max": 8,
    "min": 9,
    "rcp": 10,
    "rsqrt": 11,
}
SRC = {
    "R0": 0,
    "R1": 1,
    "R2": 2,
    "T": 3,
    "ST1": 4,
    "ST2": 5,
    "K0": 6,
    "K1": 7,
    "K2": 8,
    "K3": 9,
}

ADDR_BITS = 8

#: Pool timing the schedule is built on (sru_pool.v's header): a result is
#: readable LDEP slot cycles after its op's slot; a result tap reads depth
#: sigma_i - sigma_k - LDEP; a word's first op slot is at least WLEAD cycles
#: after the word is asked for; taps and word slots hold 32.
LDEP, WLEAD, TAPS = 18, 2, 32


def e8(x: float) -> int:
    """`x` as an E8M15 word, rounded to nearest even: 24 bits {sign, 8-bit
    exponent, 15-bit mantissa}, no subnormals (a magnitude below 2^-126
    flushes to a signed zero), 2^128 and up infinite."""
    if math.isnan(x):
        return 0x7FC000
    s = int(math.copysign(1.0, x) < 0)
    if math.isinf(x):
        return s << 23 | 0xFF << 15
    a = Fraction(abs(x))
    if a == 0:
        return s << 23
    big = a.numerator.bit_length() - a.denominator.bit_length()
    if Fraction(2) ** big > a:
        big -= 1
    q, r = divmod(a / Fraction(2) ** (big - 15), 1)
    q = int(q)
    if r > Fraction(1, 2) or (r == Fraction(1, 2) and q & 1):
        q += 1
    if q == 1 << 16:
        q, big = q >> 1, big + 1
    e = big + 127
    if e >= 255:
        return s << 23 | 0xFF << 15
    if e <= 0:
        return s << 23
    return s << 23 | e << 15 | (q & 0x7FFF)


def pack(fields: dict, values: dict) -> dict:
    """``{word address: value}`` for `values` (field -> value) over `fields`."""
    words: dict = {}
    for name, v in values.items():
        a, lo, w = fields[name]
        if not 0 <= v < (1 << w):
            raise ValueError(f"{name}={v} does not fit {w} bits")
        words[a] = words.get(a, 0) | (v << lo)
    return words


def schedule(prog: list, f_lanes: int, h_taps: int = 2) -> tuple:
    """Op-store field dicts for a program on F_LANES lanes with H_TAPS result
    taps a lane.

    prog: ops, each a dict of O fields except the schedule ones, the H sources
    named by op index: ``"A": ("H", k)`` etc. Returns ``(fields, II, sigma)``.
    Raises :class:`ValueError` when the program does not fit the taps."""
    m = 16 // f_lanes
    n = len(prog)
    ii = n * m
    sig: list = []
    for i, op in enumerate(prog):
        srcs = [op[s][1] for s in ("A", "B", "C") if isinstance(op.get(s), tuple)]
        if any(k >= i for k in srcs):
            raise ValueError(f"op {i}: reads the result of a later op")
        lo = max([sig[k] + LDEP for k in srcs] + [0])
        k = 0
        while i * m + k * ii < lo:
            k += 1
        sig.append(i * m + k * ii)
    lead = -(-WLEAD // ii)
    out = []
    for i, op in enumerate(prog):
        f = {k: v for k, v in op.items() if k not in ("A", "B", "C")}
        taps: list = []
        for s in ("A", "B", "C"):
            v = op.get(s, "X" if s == "A" else "K")
            if isinstance(v, tuple):
                if v[1] not in taps:
                    taps.append(v[1])
                if len(taps) > h_taps:
                    raise ValueError(f"op {i}: more than {h_taps} earlier results")
                f[s] = ASRC["H"] if s == "A" else BSRC["H"]
                f["H" + s] = taps.index(v[1])
            else:
                f[s] = ASRC[v] if s == "A" else BSRC[v]
        for t, kk in enumerate(taps):
            d = sig[i] - sig[kk] - LDEP
            if not 0 <= d < TAPS:
                raise ValueError(f"op {i}: result of op {kk} is {d} deep")
            f[f"D{t}"] = d
        kw = sig[i] // ii + lead
        if kw >= TAPS:
            raise ValueError(f"op {i}: {kw} word slots deep")
        f["KW"] = kw
        out.append(f)
    return out, ii, sig


def words(g: dict, passes: list, ops: list, fin: list, f_lanes: int, h_taps: int):
    """Every configuration word of a job: ``{address: value}``.

    g: G fields; passes: up to NPASS dicts of P fields; ops: the op store, O
    fields with sources by name (`schedule`'s form); fin: F field dicts. Each
    pass's program (its NOPS ops from OPBASE) is scheduled for the pool."""
    out = pack(G, {k: v & ((1 << G[k][2]) - 1) for k, v in g.items()})
    for p, d in enumerate(passes):
        base = PASS_BASE + PASS_STRIDE * p
        for a, v in pack(P, d).items():
            out[base + a] = out.get(base + a, 0) | v
        if d.get("NOPS", 0):
            prog = ops[d["OPBASE"] : d["OPBASE"] + d["NOPS"]]
            fields, _, _ = schedule(prog, f_lanes, h_taps)
            for i, f in enumerate(fields):
                f = {k: v & ((1 << O[k][2]) - 1) for k, v in f.items()}
                for a, v in pack(O, f).items():
                    out[OP_BASE + OP_STRIDE * (d["OPBASE"] + i) + a] = v
    for i, f in enumerate(fin):
        w = 0
        for k, (lo, wd) in F.items():
            w |= (f.get(k, 0) & ((1 << wd) - 1)) << lo
        out[FIN_BASE + i] = w
    return out


# ---------------------------------------------------------- instructions
OP_CFG, OP_LOAD, OP_RUN, OP_STORE, OP_POST = 1, 2, 3, 4, 5
#: LOAD targets: the function table (4 entries a word), column constants (2
#: entries a word), row state (a row a word, f32 R0 R1 R2).
LOAD_TABLE, LOAD_COLS, LOAD_ROWS = 0, 1, 2
#: A sub-row of NW words is read and written as runs of 32-byte words; the
#: strides count words.
WORD_BYTES = 32


def _pad(name: str, width: int) -> Field:
    return Field(name, width, default=0)


def _op(code: int) -> Field:
    return Field("op", 4, const=code)


CFG = InstFormat(
    "CFG",
    [
        _op(OP_CFG),
        Field("word", ADDR_BITS, doc="configuration word address"),
        _pad("_pad", 212),
        Field("value", 32),
    ],
    doc="write one configuration word",
)
LOAD = InstFormat(
    "LOAD",
    [
        _op(OP_LOAD),
        Field("what", 2, doc="0 table, 1 column constants, 2 row state"),
        Field("first", 10, default=0, doc="first entry"),
        Field("addr", 40),
        Field("words", 12, doc="32-byte words to read"),
    ],
    doc="table / column constants / row state from memory",
)
RUN = InstFormat(
    "RUN",
    [
        _op(OP_RUN),
        Field("amem", 1, default=1, doc="A from memory (1) or a COUNT stream (0)"),
        Field("peer", 1, default=0, doc="out to a unit (1) or memory (0)"),
        Field("aaddr", 40, default=0),
        Field("astride", 16, default=0, doc="A sub-row stride, words"),
        Field("oaddr", 40, default=0),
        Field("ostride", 16, default=0, doc="out sub-row stride, words"),
        Field("baddr", 40, default=0),
        Field("bstride", 16, default=0),
        Field("px", 4, default=0),
        Field("py", 4, default=0),
        Field("pbuf", 4, default=0),
        Field("poff", 16, default=0, doc="peer offset, granules"),
        Field("count", 1, default=0, doc="peer bursts count toward its fills"),
        Field("ax", 4, default=0, doc="A's producer, for its credits"),
        Field("ay", 4, default=0),
        Field("words", 24, default=0, doc="a stream's words (NW x NSR): its credits"),
    ],
    doc="one job over the configuration",
)
STORE = InstFormat(
    "STORE",
    [
        _op(OP_STORE),
        _pad("_pad", 3),
        Field("first", 9, default=0, doc="first row"),
        Field("addr", 40),
        Field("rows", 12),
    ],
    doc="row state to memory, a row a word",
)
POST = InstFormat(
    "POST",
    [
        _op(OP_POST),
        Field("x", 4),
        Field("y", 4),
        _pad("_pad", 228),
        Field("n", 16),
    ],
    doc="a CREDIT of n to (x, y)",
)
ISA = InstSet("kohakutpu-sru", [CFG, LOAD, RUN, STORE, POST])

__all__ = [
    "ADDR_BITS",
    "ASRC",
    "BSRC",
    "CFG",
    "FIN_BASE",
    "FIN_DEPTH",
    "FOP",
    "ISA",
    "LDEP",
    "LOAD",
    "LOAD_COLS",
    "LOAD_ROWS",
    "LOAD_TABLE",
    "NPASS",
    "OP_BASE",
    "OP_DEPTH",
    "OP_STRIDE",
    "PASS_BASE",
    "PASS_STRIDE",
    "POST",
    "RUN",
    "SRC",
    "STORE",
    "TAPS",
    "WLEAD",
    "WORD_BYTES",
    "F",
    "G",
    "O",
    "P",
    "e8",
    "pack",
    "schedule",
    "words",
]
