"""The KohakuSIMT instruction set: ONE field table, four consumers.

The four are the Python assembler (`rv_simt_asm.py`), the golden model
(`rv_simt_model.py`), the RTL decode (`kht_isa.vh`, generated from here) and the
disassembler that a bench prints on a mismatch (also in `rv_simt_asm.py`).
`rv_simt_isa_test.py` proves they agree bit for bit, which is what makes "one
source of truth" true rather than intended.

There is deliberately NO C intrinsic header, unlike the SIMD tier's fourth
consumer. The reason is the source language: these programs are shaders through
a frontend, not C through GCC, and a `.insn` macro whose semantics are "for each
active lane" has no meaning in a C expression.

Built on the framework's own `kohakuaccel.backend.isa` at a 32-bit container,
exactly as the SIMD tier's table is, so the two PEs share the encoding machinery
and never two copies of it.

## The register-class rule, which is the whole design

    RV32I opcode space addresses the PER-THREAD (vector) file.
    The scalar file and all control flow live in the custom space.

So `add x5, x3, x4` is, for every active lane, `x5[lane] = x3[lane] + x4[lane]`.
That keeps shader code in ordinary encodings and preserves the pure-SIMT
property: `x1`-`x31` ARE the per-lane registers, so no new register class exists
in the base ISA and no compiler fork is needed to allocate one.

This is AMD GCN's SALU/VALU split with the polarity inverted. On GCN the vector
side is the addition; here the base ISA slot is spent on the per-thread file, so
the SCALAR side is what the custom space adds.

## Two opcode majors, and why both

    custom-2  0x5B   R-type groups: funct3 names the group, funct7 the operation
    custom-3  0x7B   I-type groups: funct3 names ONE instruction, imm is 12 bits

An I-type layout has no funct7, so an I-type group holds exactly one
instruction -- the SIMD tier hit the same wall and spent a whole funct3 on `vld`
and another on `vst`. Splitting R from I across two majors buys eight of each
instead of eight in total.

custom-0 and custom-1 are left alone. A GPU build does not carry the SIMD tiers,
so they would be free -- but leaving them untouched means a hypothetical PE
carrying both never has to renumber either set.

## What has no encoding, deliberately

**No gather or scatter opcode.** An ordinary RV32I `lw` whose base register
differs per lane IS a gather; the coalescer sits under the ordinary load path.
The `vmem` group here is not a gather -- it is the *uniform-base* case, handed
to the coalescer instead of rediscovered by it (see below).

**RV32I conditional branches are reserved and ILLEGAL in shader code.** A branch
reading a masked per-thread condition into a single PC is undefined. Uniform
control uses `sbeqz`/`sbnez` on the scalar file; divergent control uses
`split`/`join`. "Legal only when the compiler proved it uniform" is an
unfalsifiable contract across four consumers, so the encoding refuses instead.
`jal` and `jalr` STAY as ordinary RV32I and are wave-wide: a call is uniform by
construction.

**No `tex`.** Address math is integer, the fetch is an ordinary load, and
filtering is FMAs. There is no sampler to invoke, and reserving an encoding for
a block that may never exist is four files carrying a promise.

## Getting a value from the per-thread side to the scalar side

Three instructions, and they do different things rather than overlapping:

    vreadfirst sd, vs1     the LOWEST ACTIVE lane's value. One lane, selected.
    ballot     sd, vs1     one bit per lane. A predicate, across lanes.
    redux*     sd, vs1     add/max/min/and/or. A reduction, across lanes.

`vreadfirst` is what makes a memory-resident uniform reachable: `rdctl` cannot
read memory and the scalar ALU only computes from scalars, so without it the
only path is `redux.or` over a vector known to be uniform -- three butterfly
passes to move one word, and an idiom rather than a design. It is fully defined
under a mask because it names the lowest ACTIVE lane, never lane 0, which may be
masked off. **An all-zero active mask is not a defined case**: the scheduler
must never issue a wave whose mask is zero, and the bench asserts that rather
than reasoning that it cannot happen.

## The three addressing tiers, and what the hardware actually knows

    form                          hardware knows            requests
    lane-linear  (vlin/vsin)      everything, at decode      ALWAYS 1
    uniform base (vl/vs)          the high bits are equal    1..LANES
    RV32I lw/sw, per-lane base    nothing                    1..LANES

**A uniform base does NOT imply contiguous offsets.** `s[ss1] + (v[vs2] <<
scale)` with arbitrary per-lane offsets is still a scatter and still needs the
full leader/follower pass. What the uniform-base form buys is narrower and real:
the coalescer compares OFFSET FIELDS rather than full computed addresses, and it
knows the high bits cannot differ. The compare is narrower, **never skipped** --
including when `ss1` is `s0`, which is legal and degenerates to a pure vector
address.

The lane-linear form is the one that is genuinely free: no vector operand at
all, addresses known at decode, one request by construction. It is also the
commonest access a shader makes -- lane *i* reads `A + 4i`, eight lanes, one
32-byte entry, one request.

Two semantics pinned so the RTL and the model cannot drift:

* **Offsets are SIGNED.** Negative strides are real.
* **`s + (v << scale)` wraps at 32 bits**, defined, rather than being undefined.

## What the 32 control slots are for

`rdctl` reads LAUNCH AND DISPATCH STATE: workgroup id, grid dimensions,
workgroup base and count, the shader and descriptor base pointers, the deadline.
That is about fifteen of the thirty-two.

**Bulk constants do not go here.** A uniform buffer arrives the way a real GPU
does it -- a base pointer in a slot, and the data read from memory with
`vreadfirst` to bring one word to the scalar side.
"""

import pathlib
import sys
from dataclasses import dataclass

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "compiler"))

from kohakuaccel.backend.isa import Field, InstFormat, InstSet, ISAError

OPC_KHG = 0x5B  # custom-2: the R-type groups
OPC_KHGI = 0x7B  # custom-3: the I-type groups

# custom-2 funct3: the group
F3_SALU, F3_SMOV, F3_DIV, F3_SUB, F3_VMEM, F3_FLT = range(6)

# custom-3 funct3: one instruction each
F3I_ADDI, F3I_ANDI, F3I_ORI, F3I_SLLI, F3I_SRLI, F3I_SRAI, F3I_BEQZ, F3I_BNEZ = range(8)

#: Access width for the vmem group, in funct7[1:0].
MW_B, MW_H, MW_W = 0, 1, 2
MW_NAME = {MW_B: "b", MW_H: "h", MW_W: "w"}
MW_BYTES = {MW_B: 1, MW_H: 2, MW_W: 4}

#: Defaults the assembler range-checks against. The ENCODING always allows 32 of
#: each; these bound what a given build actually has, so widening a file is a
#: parameter change and never an encoding change.
SREGS = 32
WAVES = 16
LANES = 8
IPDOM_DEPTH = 8


@dataclass(frozen=True)
class Operand:
    """One assembly operand: which instruction field it lands in, and what it names.

    `kind` is what the C generator needs and the field table cannot say. `sreg`
    and `vreg` are register indices; `imm` is a constant baked into the word.
    There is no `xreg` kind here: the SIMT PE's scalar operands are never GCC's
    to allocate, because its programs come from a shader frontend rather than
    from C.
    """

    name: str
    field: str
    kind: str  # sreg | vreg | imm
    doc: str = ""


SD = Operand("sd", "rd", "sreg", "destination scalar register")
SS1 = Operand("ss1", "rs1", "sreg", "first source scalar register")
SS2 = Operand("ss2", "rs2", "sreg", "second source scalar register")
VD = Operand("vd", "rd", "vreg", "destination per-thread register")
VS1 = Operand("vs1", "rs1", "vreg", "first source per-thread register")
VS2 = Operand("vs2", "rs2", "vreg", "second source per-thread register")
VS = Operand("vs", "rd", "vreg", "source per-thread register (store data)")
IMM = Operand("imm", "imm", "imm", "signed 12-bit immediate")
SH = Operand("sh", "rs2", "imm", "shift amount, 0..31")
CIDX = Operand("cidx", "rs2", "imm", "control/launch slot index, 0..31")
LANE = Operand("lane", "rs2", "imm", "lane index, 0..LANES-1")


@dataclass(frozen=True)
class Op:
    name: str
    group: int
    funct7: int | None  # None for the I-type groups
    operands: tuple
    fmt: InstFormat
    doc: str
    opcode: int = OPC_KHG


def _r_format(name, funct3, funct7, opcode=OPC_KHG):
    return InstFormat(
        name,
        (
            Field("funct7", 7, const=funct7),
            Field("rs2", 5),
            Field("rs1", 5),
            Field("funct3", 3, const=funct3),
            Field("rd", 5),
            Field("opcode", 7, const=opcode),
        ),
        width=32,
    )


def _i_format(name, funct3, opcode=OPC_KHGI):
    return InstFormat(
        name,
        (
            Field("imm", 12, signed=True),
            Field("rs1", 5),
            Field("funct3", 3, const=funct3),
            Field("rd", 5),
            Field("opcode", 7, const=opcode),
        ),
        width=32,
    )


ISA: dict[str, Op] = {}
SET = InstSet("khg")


def _add(name, group, funct7, operands, doc, opcode=OPC_KHG):
    if name in ISA:
        raise ISAError("khg: %r is already defined" % name)
    fmt = (
        _i_format(name, group, opcode)
        if funct7 is None
        else _r_format(name, group, funct7, opcode)
    )
    SET.add(fmt)
    ISA[name] = Op(name, group, funct7, tuple(operands), fmt, doc, opcode)


# ------------------------------------------------------- scalar ALU, register
# Uniform arithmetic: loop counters, base pointers, workgroup indices. One PC's
# worth of work per wave, not one per lane -- which is the whole point of having
# a scalar file at all.
_SALU = (
    ("sadd", 0, "sd <- ss1 + ss2"),
    ("ssub", 1, "sd <- ss1 - ss2"),
    ("ssll", 2, "sd <- ss1 << ss2[4:0]"),
    ("sslt", 3, "sd <- (ss1 < ss2) signed, as 0 or 1"),
    ("ssltu", 4, "sd <- (ss1 < ss2) unsigned, as 0 or 1"),
    ("sxor", 5, "sd <- ss1 ^ ss2"),
    ("ssrl", 6, "sd <- ss1 >> ss2[4:0] logical"),
    ("ssra", 7, "sd <- ss1 >> ss2[4:0] arithmetic"),
    ("sor", 8, "sd <- ss1 | ss2"),
    ("sand", 9, "sd <- ss1 & ss2"),
)
for _n, _f7, _d in _SALU:
    _add(_n, F3_SALU, _f7, (SD, SS1, SS2), _d)

# ------------------------------------------------- scalar <-> vector, control
_add(
    "s2v",
    F3_SMOV,
    0,
    (VD, SS1),
    "every ACTIVE lane of vd <- ss1. The scalar-to-vector broadcast; masked-off "
    "lanes keep their previous value, like every other vector write.",
)
_add(
    "rdctl",
    F3_SMOV,
    1,
    (SD, CIDX),
    "sd <- control/launch slot `cidx`. How a wave learns its own identity and "
    "its launch constants -- workgroup id, grid dimensions, base pointers, the "
    "kick argument. The only path by which a scalar register acquires a value "
    "that is not an immediate or a reduction.",
)

# ------------------------------------------------------------------ divergence
# The IPDOM mechanism. `split` pushes two entries -- the fall-through mask and
# the false-predicate lanes with their PC -- and continues with the true lanes;
# `join` pops. Overflow at IPDOM_DEPTH raises a fault: not a wrap, not a mask
# merge, not a truncation. A masked-off lane that silently reactivates is a
# wrong answer with no witness.
_add(
    "split",
    F3_DIV,
    0,
    (VS1,),
    "diverge on the per-lane predicate in vs1 (non-zero = true). Pushes the "
    "current mask as the fall-through and the false lanes with the next PC, "
    "then continues with the true lanes. Faults on stack overflow.",
)
_add(
    "join",
    F3_DIV,
    1,
    (),
    "pop the IPDOM stack: restore the saved mask and jump to its saved PC. "
    "Reconvergence is a pop, not an analysis.",
)
_add(
    "tmc",
    F3_DIV,
    2,
    (SS1,),
    "thread-mask control: the active mask <- ss1[LANES-1:0]. Sets the initial "
    "mask when a workgroup is smaller than the wave, and performs early exit. "
    "A mask of zero retires the wave.",
)
_add(
    "bar",
    F3_DIV,
    3,
    (SS1, SS2),
    "workgroup barrier `ss1` across `ss2` waves. Workgroup scope only: one "
    "workgroup is one PE, so only local barriers exist and the scope bit that "
    "would select a global barrier is reserved and unimplemented.",
)

# ------------------------------------------------------------------- subgroup
# The primitives a shader frontend lowers subgroup intrinsics TO. A butterfly
# network covers shuffle-xor, broadcast and every reduction in log2(LANES)
# passes, which is why the full crossbar the SIMD tier carries is replaced here
# rather than inherited.
_add(
    "shflxor",
    F3_SUB,
    0,
    (VD, VS1, SS2),
    "vd[lane] <- vs1[lane ^ ss2]. The butterfly shuffle; a lane whose partner "
    "is inactive reads its own value.",
)
_add(
    "bcast",
    F3_SUB,
    1,
    (VD, VS1, LANE),
    "vd[every active lane] <- vs1[lane]. Subgroup broadcast -- one LANE's value "
    "to all of them, which is not the same operation as s2v.",
)
_add(
    "ballot",
    F3_SUB,
    2,
    (SD, VS1),
    "sd <- one bit per lane, set where vs1 is non-zero AND the lane is active. "
    "One of the two defined vector-to-scalar paths.",
)
_REDUX = (
    ("reduxadd", 3, "sum"),
    ("reduxmax", 4, "signed maximum"),
    ("reduxmin", 5, "signed minimum"),
    ("reduxand", 6, "bitwise and"),
    ("reduxor", 7, "bitwise or"),
)
for _n, _f7, _what in _REDUX:
    _add(
        _n,
        F3_SUB,
        _f7,
        (SD, VS1),
        "sd <- the %s of vs1 over the ACTIVE lanes; inactive lanes contribute "
        "the identity." % _what,
    )
_add(
    "vlaneid",
    F3_SUB,
    9,
    (VD,),
    "vd[lane] <- lane, in every active lane. A lane has no other way to learn "
    "which lane it is, and every per-thread address ultimately derives from "
    "it. Costs no read port and no storage: the value is a constant per lane.",
)
_add(
    "vreadfirst",
    F3_SUB,
    8,
    (SD, VS1),
    "sd <- vs1 from the LOWEST ACTIVE lane -- not lane 0, which may be masked "
    "off. The only path from a memory-resident uniform to the scalar file, and "
    "fully defined under a mask. An all-zero mask is not a defined case: the "
    "scheduler must never issue such a wave.",
)

# ------------------------------- scalar base + vector offset, loads and stores
# funct7 = op<<4 | scale<<2 | width, so all three ride one field and the RTL
# reads them straight off the instruction rather than out of a decode case.
#: (mnemonic stem, funct7 op field, doc, lane-linear?)
_VMEM_OPS = (
    ("vl", 0, "load, sign-extended", False),
    ("vlu", 1, "load, zero-extended", False),
    ("vs", 2, "store", False),
    ("vlin", 3, "lane-linear load, sign-extended", True),
    ("vlinu", 4, "lane-linear load, zero-extended", True),
    ("vsin", 5, "lane-linear store", True),
)

for _stem, _opv, _what, _lin in _VMEM_OPS:
    for _w in (MW_B, MW_H, MW_W):
        if _opv in (1, 4) and _w == MW_W:
            continue  # a full word has nothing to extend
        for _s in range(4):
            _f7 = (_opv << 4) | (_s << 2) | _w
            _nm = "%s%s%d" % (_stem, MW_NAME[_w], _s)
            _store = _stem in ("vs", "vsin")
            if _lin:
                # No vector operand at all: the address is s[ss1] + (lane<<s),
                # so every address is known at decode and the request count is
                # ONE by construction rather than by comparison.
                _ops = (VS, SS1) if _store else (VD, SS1)
                _doc = (
                    "%s of %d byte(s) at s[ss1] + (lane << %d). The whole "
                    "wave is one line when the span fits one: no vector "
                    "operand, no compare, one request." % (_what, MW_BYTES[_w], _s)
                )
            else:
                _ops = (VS, SS1, VS2) if _store else (VD, SS1, VS2)
                _doc = (
                    "%s of %d byte(s) at s[ss1] + (v[vs2] << %d), per lane. "
                    "The base is uniform, so the coalescer compares OFFSETS "
                    "rather than whole addresses -- narrower, never skipped: "
                    "arbitrary offsets are still a scatter." % (_what, MW_BYTES[_w], _s)
                )
            _add(_nm, F3_VMEM, _f7, _ops, _doc)

# ---------------------------------------------------------------- float (G9)
# THE ARITHMETIC IS THE DSP TIER'S, NOT A SECOND ONE. Every operation here is
# `khs_float_lane` -- FP16 in, E8M15 through vec_alu's FMA, FP16 out -- so the
# golden model is the SIMD tier's model and its vectors, already verified.
# FP16 -> E8M15 is EXACT, which is the whole reason this format was chosen over
# storing FP32 and rounding it on every operation.
#
#     vreg[31:16]  element 1  RESERVED, must be written zero, reads undefined
#     vreg[15:0]   element 0  FP16
#
# Reserved, NOT unused: undefined bits become somebody's undefined behaviour,
# and packed 2xFP16 later turns element 1 live without changing the layout.
#
# vfma's destination is also its addend, because an R-type has two sources and
# an FMA needs three. That is also exactly the lane's own shape: FMA(a, b, c).
# funct7 = {half, op[2:0]}. THE FORMAT BIT IS THE TOP ONE so that adding an
# operation never disturbs it: the seeds below took funct7[2], which the format
# used to occupy.
FLT_FMA, FLT_MUL, FLT_ADD, FLT_SUB = 0, 1, 2, 3
FLT_EXP2, FLT_LOG2, FLT_RCP, FLT_RSQRT = 4, 5, 6, 7
FLT_HALF = 8

_add(
    "vfma",
    F3_FLT,
    FLT_FMA,
    (VD, VS1, VS2),
    "vd <- vs1 * vs2 + vd, per lane, FP32. The destination is the addend "
    "because an R-type has two source fields and an FMA needs three sources; "
    "the lane's own interface is FMA(a, b, c) for the same reason.",
)
_add(
    "vfmul",
    F3_FLT,
    FLT_MUL,
    (VD, VS1, VS2),
    "vd <- vs1 * vs2, per lane, FP32. The same lane with the addend forced to "
    "zero -- one datapath, not a second multiplier.",
)
_add(
    "vfadd",
    F3_FLT,
    FLT_ADD,
    (VD, VS1, VS2),
    "vd <- vs1 + vs2, per lane, FP32. The same lane with the multiplier forced "
    "to one.",
)
_add(
    "vfsub",
    F3_FLT,
    FLT_SUB,
    (VD, VS1, VS2),
    "vd <- vs1 - vs2, per lane, FP32. vs2's sign bit is inverted on the way "
    "in; subtraction is not a separate operation in the lane.",
)

# THE FOUR SEEDS, and they are the SIMD PE's FSFU group on the same lane.
# `vec_alu` computes all four at FULL RATE, II=1, sharing the FMA's normaliser
# and rounder -- a real GPU's special-function unit runs them at a quarter rate.
# They are a parameter (HAS_FSFU) rather than a fixture: exposing them stops the
# tool constant-folding the lane's operation select, and this PE has twice the
# SIMD PE's float lanes to pay that on.
#
# THEY BELONG HERE AND NOT ONLY ON THE SIMD PE. rsqrt is normalisation and
# lighting, exp2/log2 are tone-mapping and fog -- fragment-shader work, which is
# exactly what the classifier routes here for being divergent. Without them a
# transcendental-heavy divergent shader has to pick between the wrong execution
# model and a software polynomial.
for _n, _op, _d in (
    ("vfexp2", FLT_EXP2, "2 raised to vs1"),
    ("vflog2", FLT_LOG2, "the base-2 logarithm of vs1"),
    ("vfrcp", FLT_RCP, "1 / vs1"),
    ("vfrsqrt", FLT_RSQRT, "1 / sqrt(vs1)"),
):
    _add(
        _n,
        F3_FLT,
        _op,
        (VD, VS1),
        "vd <- %s, per lane, FP32. Full rate, II=1, through the same "
        "normaliser and rounder the FMA uses." % _d,
    )

# funct7[3] is the format bit; nothing else moves. FP32 -> E8M15 copies the
# exponent verbatim and only truncates mantissa, so the wide format is the one
# that cannot surprise a shader with an overflow -- hence the default.
for _n, _op, _d in (
    ("vfma_h", FLT_FMA, "vd <- vs1 * vs2 + vd, per lane, FP16 in vreg[15:0]"),
    ("vfmul_h", FLT_MUL, "vd <- vs1 * vs2, per lane, FP16 in vreg[15:0]"),
    ("vfadd_h", FLT_ADD, "vd <- vs1 + vs2, per lane, FP16 in vreg[15:0]"),
    ("vfsub_h", FLT_SUB, "vd <- vs1 - vs2, per lane, FP16 in vreg[15:0]"),
):
    _add(
        _n,
        F3_FLT,
        FLT_HALF | _op,
        (VD, VS1, VS2),
        _d + ". FP16 -> E8M15 is EXACT, so this is the cheaper-to-store format "
        "and never the less accurate one going in; only the result narrows, "
        "and that direction saturates rather than wrapping.",
    )
for _n, _op, _d in (
    ("vfexp2_h", FLT_EXP2, "2 raised to vs1"),
    ("vflog2_h", FLT_LOG2, "the base-2 logarithm of vs1"),
    ("vfrcp_h", FLT_RCP, "1 / vs1"),
    ("vfrsqrt_h", FLT_RSQRT, "1 / sqrt(vs1)"),
):
    _add(
        _n,
        F3_FLT,
        FLT_HALF | _op,
        (VD, VS1),
        "vd <- %s, per lane, FP16 in vreg[15:0]." % _d,
    )
# NO int <-> float CONVERSION, and that is deliberate. `vec_cvt` carries
# FP16/FP32 <-> E8M15 and nothing integer, so an int->float instruction would
# mean inventing normalise-and-round arithmetic HERE -- the fork the tier ruling
# refuses. It is also not needed to have a working float tier: an FP16 bit
# pattern is a 16-bit integer, so a shader builds constants with `saddi`+`s2v`
# and reads real data straight out of memory, which is where a shader's floats
# come from anyway. funct7 4 and 5 are left UNENCODED rather than encoded and
# faulting, so the gap is visible in the table instead of at run time.

# ------------------------------------------------------- scalar ALU, immediate
# custom-3. One instruction per funct3 because an I-type has no funct7.
_add("saddi", F3I_ADDI, None, (SD, SS1, IMM), "sd <- ss1 + imm", OPC_KHGI)
_add("sandi", F3I_ANDI, None, (SD, SS1, IMM), "sd <- ss1 & imm", OPC_KHGI)
_add("sori", F3I_ORI, None, (SD, SS1, IMM), "sd <- ss1 | imm", OPC_KHGI)
_add(
    "sslli",
    F3I_SLLI,
    None,
    (SD, SS1, IMM),
    "sd <- ss1 << imm[4:0]. With saddi this builds a 32-bit constant in three "
    "instructions, which is what a scalar path with no wide immediate costs.",
    OPC_KHGI,
)
_add("ssrli", F3I_SRLI, None, (SD, SS1, IMM), "sd <- ss1 >> imm[4:0] logical", OPC_KHGI)
_add(
    "ssrai",
    F3I_SRAI,
    None,
    (SD, SS1, IMM),
    "sd <- ss1 >> imm[4:0] arithmetic",
    OPC_KHGI,
)
_add(
    "sbeqz",
    F3I_BEQZ,
    None,
    (SS1, IMM),
    "if ss1 == 0, PC <- PC + imm. UNIFORM control: every lane of the wave "
    "takes it or none does, because the condition is a scalar.",
    OPC_KHGI,
)
_add(
    "sbnez",
    F3I_BNEZ,
    None,
    (SS1, IMM),
    "if ss1 != 0, PC <- PC + imm. Uniform control, as sbeqz.",
    OPC_KHGI,
)


# ------------------------------------------------------------------- encoding


def encode(name, **operands) -> int:
    """One instruction word. Unnamed fields are zero; every value is range-checked."""
    op = ISA[name]
    want = {o.name for o in op.operands}
    got = set(operands)
    if got != want:
        raise ISAError("%s takes %s, got %s" % (name, sorted(want), sorted(got)))
    fields = {}
    for o in op.operands:
        v = operands[o.name]
        if o.kind == "sreg" and not 0 <= v < SREGS:
            raise ISAError(
                "%s: %s is s%d, but this build has %d scalar "
                "registers" % (name, o.name, v, SREGS)
            )
        if o.kind == "vreg" and not 0 <= v < 32:
            raise ISAError(
                "%s: %s is x%d, outside the 32 per-thread registers" % (name, o.name, v)
            )
        if o is LANE and not 0 <= v < LANES:
            raise ISAError(
                "%s: lane %d, but this build has %d lanes" % (name, v, LANES)
            )
        if o is CIDX and not 0 <= v < 32:
            raise ISAError("%s: control slot %d, but there are 32" % (name, v))
        fields[o.field] = v
    for f in op.fmt.settable():
        fields.setdefault(f, 0)
    return op.fmt.encode(**fields)


def decode(word: int):
    """``(name, {operand: value})``, or None if no KohakuSIMT format claims it."""
    opc = word & 0x7F
    if opc not in (OPC_KHG, OPC_KHGI):
        return None
    f3 = (word >> 12) & 7
    f7 = (word >> 25) & 0x7F
    for name, op in ISA.items():
        # The opcode major is part of the match, not an assumption: custom-2 and
        # custom-3 both number their groups from zero.
        if op.opcode != opc or op.group != f3:
            continue
        if op.funct7 is not None and op.funct7 != f7:
            continue
        raw = op.fmt.decode(word)
        return name, {o.name: raw[o.field] for o in op.operands}
    return None


def is_khg(word: int) -> bool:
    """Any KohakuSIMT instruction, either major."""
    return (word & 0x7F) in (OPC_KHG, OPC_KHGI)


def groups():
    """Group name per funct3 on custom-2, for the generated headers."""
    return ["SALU", "SMOV", "DIV", "SUB", "VMEM", "FLT", "RSVD6", "RSVD7"]


def igroups():
    """Instruction name per funct3 on custom-3."""
    return ["ADDI", "ANDI", "ORI", "SLLI", "SRLI", "SRAI", "BEQZ", "BNEZ"]
