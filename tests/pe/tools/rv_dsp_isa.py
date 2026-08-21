"""The KohakuDSP instruction set: ONE field table, four consumers.

The four are the Python assembler (`rv_asm.py`), the golden model
(`rv_dsp_model.py`), the RTL decode (`khd_isa.vh`, generated from here) and the
C intrinsic header (`khd_intrin.h`, likewise). `rv_dsp_isa_test.py` proves they
agree bit for bit, which is what makes "one source of truth" true rather than
intended.

It is built on the framework's own `kohakuaccel.backend.isa` -- the same `Field`
/ `InstFormat` / `InstSet` that encode compute-unit instructions, at a 32-bit
container instead of 256 -- because 09B S2.2 asks for exactly that and because
`Field.fit` already raises with the legal range, which is the check that catches
a wrapped field at generation time rather than on silicon.

## The encoding

RISC-V custom-0 (`0x0B`) carries tier 1: packed integer, memory, moves.
**Custom-1 (`0x2B`) is reserved to the float tiers**, so a variant built without
E8M15 or FP32 lacks those encodings at the opcode major and a program using them
faults with an illegal instruction rather than computing something plausible.

```
    31      25 24   20 19   15 14  12 11    7 6      0
   |  funct7  |  rs2  |  rs1  | fn3 |  rd   | 0001011 |     R-type
   |     imm[11:0]    |  rs1  | fn3 |  rd   | 0001011 |     I-type
```

| `funct3` | group | `funct7` |
|---|---|---|
| 0 | `VLD` | I-type: vector load |
| 1 | `VST` | I-type: vector store |
| 2 | `VINT` | `op<<2 \\| et` -- packed integer arithmetic |
| 3 | `VBIT` | `op` -- bitwise, untyped |
| 4 | `VSHI` | `op<<2 \\| et`, `rs2` = shift amount |
| 5 | `VMAC` | `op<<2 \\| et` -- dot product and the accumulators |
| 6 | `VMOV` | `op` -- scalar/vector moves and reductions |
| 7 | `VPRM` | `op<<3 \\| idx` -- permute: slide, pack, unpack |

The element type lives in `funct7[1:0]` for every typed group, so the datapath
reads it straight off the instruction word rather than out of a decode case.

## The store is I-type, deliberately

RV32's S-format splits its immediate across two fields so that `rs1` and `rs2`
stay put for the register file read. A vector store's data comes from the
*vector* file, addressed by an immediate field rather than by an allocated
register, so that constraint does not apply -- and a split immediate cannot be
expressed as one `InstFormat` field. `vst` therefore uses the I-type layout with
the vector source in the `rd` position.

## Why every intrinsic is `volatile`, which is a cost worth naming

Vector register numbers are **immediates**, not operands GCC allocates -- that
is what buys "no compiler fork" (09B S2.2). The consequence is that GCC cannot
see the vector state at all: two identical `vdot` calls are not the same value,
they accumulate. So every vector intrinsic must be `volatile`, and the compiler
therefore **cannot schedule or software-pipeline the vector datapath**.

On an in-order single-issue core whose multi-cycle ops stall in the existing
hazard unit that costs much less than it would elsewhere, and it is the honest
price of the no-fork path. It is also the concrete argument for a real backend
if scheduling ever turns out to matter.
"""

import pathlib
import sys
from dataclasses import dataclass

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "compiler"))

from kohakuaccel.backend.isa import Field, InstFormat, InstSet, ISAError  # noqa: E402

OPC_KHD = 0x0B          # custom-0: tier 1
OPC_KHF = 0x2B          # custom-1: reserved to the float tiers

F3_VLD, F3_VST, F3_VINT, F3_VBIT, F3_VSHI, F3_VMAC, F3_VMOV, F3_VPRM = range(8)

ET_S8, ET_S16, ET_S32 = 0, 1, 2
ET_NAME = {ET_S8: "s8", ET_S16: "s16", ET_S32: "s32"}
ET_BITS = {ET_S8: 8, ET_S16: 16, ET_S32: 32}

#: Defaults the assembler range-checks against. The ENCODING always allows 32 of
#: each -- these bound what a given build actually has, so widening the register
#: file is a parameter change and never an encoding change.
VREGS = 8
NACC = 2


@dataclass(frozen=True)
class Operand:
    """One assembly operand: which instruction field it lands in, and what it names.

    `kind` is what the C generator needs and the field table cannot say: `xreg`
    is a scalar register GCC allocates and reaches through an `"r"` constraint;
    `vreg` and `areg` are indices baked into the `.insn` string as literal
    register names; `imm` is a constant.
    """

    name: str
    field: str
    kind: str           # xreg | vreg | areg | imm
    doc: str = ""


# The operand vocabulary. A field appears at most once per instruction, so the
# same instruction field can carry different kinds in different instructions.
VD = Operand("vd", "rd", "vreg", "destination vector register")
VS1 = Operand("vs1", "rs1", "vreg", "first source vector register")
VS2 = Operand("vs2", "rs2", "vreg", "second source vector register")
VS = Operand("vs", "rd", "vreg", "source vector register (store data)")
AD = Operand("ad", "rd", "areg", "destination accumulator")
AS1 = Operand("as1", "rs1", "areg", "source accumulator")
XD = Operand("xd", "rd", "xreg", "destination scalar register")
XS1 = Operand("xs1", "rs1", "xreg", "source scalar register")
IMM = Operand("imm", "imm", "imm", "byte offset, signed 12 bits")
SH = Operand("sh", "rs2", "imm", "shift amount")


@dataclass(frozen=True)
class Op:
    name: str
    group: int
    funct7: int | None      # None for the I-type groups
    operands: tuple
    fmt: InstFormat
    et: int | None
    doc: str
    opcode: int = OPC_KHD


def _r_format(name, funct3, funct7, opcode=OPC_KHD):
    return InstFormat(name, (
        Field("funct7", 7, const=funct7),
        Field("rs2", 5),
        Field("rs1", 5),
        Field("funct3", 3, const=funct3),
        Field("rd", 5),
        Field("opcode", 7, const=opcode),
    ), width=32)


def _i_format(name, funct3, opcode=OPC_KHD):
    return InstFormat(name, (
        Field("imm", 12, signed=True),
        Field("rs1", 5),
        Field("funct3", 3, const=funct3),
        Field("rd", 5),
        Field("opcode", 7, const=opcode),
    ), width=32)


ISA: dict[str, Op] = {}
SET = InstSet("khd")


def _add(name, group, funct7, operands, doc, et=None, opcode=OPC_KHD):
    if name in ISA:
        raise ISAError("khd: %r is already defined" % name)
    fmt = (_i_format(name, group, opcode) if funct7 is None
           else _r_format(name, group, funct7, opcode))
    SET.add(fmt)
    ISA[name] = Op(name, group, funct7, tuple(operands), fmt, et, doc, opcode)


def _typed(base, group, op, ets, operands, doc, shift=2):
    """Register one instruction per element type, `.s8` / `.s16` / `.s32`."""
    for et in ets:
        _add("%s.%s" % (base, ET_NAME[et]), group, (op << shift) | et,
             operands, doc + " (%s elements)" % ET_NAME[et], et=et)


# ---------------------------------------------------------------- memory
_add("vld", F3_VLD, None, (VD, IMM, XS1),
     "vd <- the vector at rs1+imm. Line-aligned by contract: an address that is "
     "not a multiple of the vector width faults.")
_add("vst", F3_VST, None, (VS, IMM, XS1),
     "the vector at rs1+imm <- vs. Line-aligned; a misaligned address faults.")

# ------------------------------------------------------- packed integer ALU
_ALL = (ET_S8, ET_S16, ET_S32)
_typed("vadd", F3_VINT, 0, _ALL, (VD, VS1, VS2), "element-wise wrapping add")
_typed("vsub", F3_VINT, 1, _ALL, (VD, VS1, VS2), "element-wise wrapping subtract")
_typed("vsadd", F3_VINT, 2, _ALL, (VD, VS1, VS2), "signed saturating add")
_typed("vssub", F3_VINT, 3, _ALL, (VD, VS1, VS2), "signed saturating subtract")
_typed("vmin", F3_VINT, 4, _ALL, (VD, VS1, VS2), "signed minimum")
_typed("vmax", F3_VINT, 5, _ALL, (VD, VS1, VS2), "signed maximum")
_typed("vmul", F3_VINT, 6, (ET_S8, ET_S16), (VD, VS1, VS2),
       "element-wise product, low half kept")

# ------------------------------------------------------------------ bitwise
_add("vand", F3_VBIT, 0, (VD, VS1, VS2), "bitwise and")
_add("vor", F3_VBIT, 1, (VD, VS1, VS2), "bitwise or")
_add("vxor", F3_VBIT, 2, (VD, VS1, VS2), "bitwise exclusive or")
_add("vandn", F3_VBIT, 3, (VD, VS1, VS2), "vs1 & ~vs2")

# ---------------------------------------------------------- immediate shifts
_typed("vslli", F3_VSHI, 0, _ALL, (VD, VS1, SH), "shift left logical")
_typed("vsrli", F3_VSHI, 1, _ALL, (VD, VS1, SH), "shift right logical")
_typed("vsrai", F3_VSHI, 2, _ALL, (VD, VS1, SH), "shift right arithmetic")
_typed("vsrari", F3_VSHI, 3, _ALL, (VD, VS1, SH),
       "shift right arithmetic, ROUNDING (add half an ulp first) -- the "
       "requantise primitive, and the one a plain vsrai gets subtly wrong")

# ------------------------------------------------------- dot and accumulators
_typed("vdot", F3_VMAC, 0, (ET_S8, ET_S16), (AD, VS1, VS2),
       "acc[ad] += the dot product of the elements within each 32-bit lane")
_typed("vdotn", F3_VMAC, 1, (ET_S8, ET_S16), (AD, VS1, VS2),
       "acc[ad] -= the dot product of the elements within each 32-bit lane")
_add("vaccz", F3_VMAC, (2 << 2) | 0, (AD,), "acc[ad] <- 0")
_add("vaccrd", F3_VMAC, (3 << 2) | 0, (VD, AS1), "vd <- acc[as1], as int32 lanes")
_add("vaccwr", F3_VMAC, (4 << 2) | 0, (AD, VS1),
     "acc[ad] <- vs1, as int32 lanes -- how a bias vector seeds an accumulation")

# ------------------------------------------------ scalar moves and reductions
_add("vsplat", F3_VMOV, 0, (VD, XS1), "every 32-bit lane of vd <- xs1")
_add("vextr", F3_VMOV, 1, (XD, VS1, SH), "xd <- 32-bit lane `sh` of vs1")
_add("vredsum", F3_VMOV, 2, (XD, VS1), "xd <- the sum of vs1's 32-bit lanes")
_add("vredmax", F3_VMOV, 3, (XD, VS1), "xd <- the signed max of vs1's 32-bit lanes")

# ------------------------------------------------------------------ permute
# funct7 is op<<3 | idx here, not op<<2 | et: `vsldw` needs a lane index and an
# R-type has no field left for one, so the permute group spends three funct7
# bits on it and folds the element type into the opcode instead.
for _i in range(8):
    _add("vsldw%d" % _i, F3_VPRM, (0 << 3) | _i, (VD, VS1, VS2),
         "vd <- 32-bit lanes %d.. of the concatenation {vs2, vs1} -- the "
         "misaligned-neighbour primitive a stencil needs" % _i)
_add("vpack.s16", F3_VPRM, (1 << 3), (VD, VS1, VS2),
     "vd <- {vs2, vs1} narrowed int16 -> int8 with signed saturation")
_add("vpack.s32", F3_VPRM, (2 << 3), (VD, VS1, VS2),
     "vd <- {vs2, vs1} narrowed int32 -> int16 with signed saturation")
_add("vunpkl.s8", F3_VPRM, (3 << 3), (VD, VS1), "vd <- vs1's low int8s, widened to int16")
_add("vunpkh.s8", F3_VPRM, (4 << 3), (VD, VS1), "vd <- vs1's high int8s, widened to int16")
_add("vunpkl.s16", F3_VPRM, (5 << 3), (VD, VS1), "vd <- vs1's low int16s, widened to int32")
_add("vunpkh.s16", F3_VPRM, (6 << 3), (VD, VS1), "vd <- vs1's high int16s, widened to int32")

# ------------------------------------------------------------- the float tier
# Custom-1, and held in its own module so the integer table stays readable.
import rv_dsp_isa_f                                                  # noqa: E402

rv_dsp_isa_f.register(_add, {"AD": AD, "VS1": VS1, "VS2": VS2,
                             "VD": VD, "AS1": AS1, "XD": XD})


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
        if o.kind == "vreg" and not 0 <= v < VREGS:
            raise ISAError("%s: %s is v%d, but this build has %d vector "
                           "registers" % (name, o.name, v, VREGS))
        if o.kind == "areg" and not 0 <= v < NACC:
            raise ISAError("%s: %s is a%d, but this build has %d accumulators"
                           % (name, o.name, v, NACC))
        fields[o.field] = v
    for f in op.fmt.settable():
        fields.setdefault(f, 0)
    return op.fmt.encode(**fields)


def decode(word: int):
    """``(name, {operand: value})``, or None if no KohakuDSP format claims it."""
    opc = word & 0x7F
    if opc not in (OPC_KHD, OPC_KHF):
        return None
    f3 = (word >> 12) & 7
    f7 = (word >> 25) & 0x7F
    for name, op in ISA.items():
        # The opcode major is part of the match, not an assumption: custom-0 and
        # custom-1 both start their funct3 groups at zero, so a float `vfmacc`
        # and an integer `vld` are the same (f3, f7) and different instructions.
        if op.opcode != opc or op.group != f3:
            continue
        if op.funct7 is not None and op.funct7 != f7:
            continue
        raw = op.fmt.decode(word)
        return name, {o.name: raw[o.field] for o in op.operands}
    return None


def is_khd(word: int) -> bool:
    """Any KohakuDSP instruction, integer tier or float."""
    return (word & 0x7F) in (OPC_KHD, OPC_KHF)


def groups():
    """Group name per funct3, for the generated headers."""
    return ["VLD", "VST", "VINT", "VBIT", "VSHI", "VMAC", "VMOV", "VPRM"]
