"""The float groups, on RISC-V custom-1.

Held apart from `rv_simd_isa.py` so the integer table stays readable and so a
build without float lacks these encodings **at the opcode major** -- a program
using one faults rather than landing in a decode case that computes something
plausible.

## One dtype configuration, and it is not a knob

```
    FP32 or FP16 operands in  ->  E8M15 compute  ->  FP32 or FP16 out
```

`funct7[1:0]` picks the operand width per instruction, exactly as the integer
tier's element type does, and it reaches the conversion at the lane's edge and
nothing below it. **Every group below is registered at both widths, including
the optional ones.** There is no parameter anywhere that removes either width
and no build computes in anything but E8M15.

## The SIMD PE is a CPU, so FALU is the base and the rest are additions

An RV32I core plus a SIMD extension is a CPU with SIMD, and every CPU SIMD ISA
-- SSE, AVX, NEON, RVV -- ships multiply, add, subtract, fused multiply-add,
min, max and compare as the *base* set. Dot-product accumulators arrived later
as additions to that base: ARM `SDOT`, x86 `VPDPBUSD`. This table has the same
shape, and the shape is what the RTL parameters follow.

| group | what | built |
|---|---|---|
| `FALU` | mul, add, sub, fma, min, max, compare | **always, wherever float exists** |
| `FCVT` | float <-> int32, f16 <-> f32 | parameter, on by default |
| `FSFU` | exp2, log2, rcp, rsqrt | parameter, on by default |
| `FMAC` | the rotating accumulator and its fold | parameter, **off** by default |

`FMAC` is off by default because it is the SIMD PE's *extra*, not its floor: a
vertex transform accumulating into E8M15 partials, an int8-style float dot, or a
long reduction justify it, and a shader doing elementwise colour work does not.
The SIMT PE has no equivalent and does not want one. Being a parameter is also
what lets the eight SIMD PEs in a mesh carry different feature sets, which makes
the feature mix an axis of the balance study rather than one global choice.

## FALU packs; the accumulator does not

A 256-bit register is **16 FP16 elements or 8 FP32 elements** under `FALU`,
which is the integer tier's own rule (32 int8 / 16 int16 / 8 int32) and every
CPU SIMD ISA's. So FP16 gets twice FP32's throughput for the cost of an operand
mux rather than for lanes.

`FMAC` keeps its own packing because a partial-sum machine sizes itself by the
accumulator rather than by the operand.

The SIMT PE places one element per 32-bit slot in both formats instead, because
there a slot is a *thread*. The arithmetic is identical either way -- same lane,
same E8M15, same conversions -- so a SIMD float result and a SIMT float result
agree element for element. Only the addressing differs.

## Compares need no mask register

`vfcmplt` and friends write **all-ones or all-zeros per element** into an
ordinary vector register, so the integer tier's `vand` / `vandn` / `vor` do the
blend. That is SSE's shape, and it means a branchless conditional costs no new
architectural state and no select instruction.

## The accumulation order is CONTRACT, not implementation

An accumulator slot holds **NPART partials** in E8M15, and the `n`th accumulate
since `vfaccz` lands on partial `n mod NPART`. `vfaccrd` and `vfredsum` combine
them in index order.

That is not a detail leaking out. The lane is fifteen cycles deep, so
`acc = a*b + acc` on one partial issues at II = 15; the vector core breaks the
recurrence with rotating partials (`vec_lanes.v` s7.3) and this does the same.
**Float addition does not associate**, so the rotation changes the answer -- a
machine rotating by 8 and a model rotating by 16 would disagree on ordinary data
and each be right by its own lights. The count is architectural.

`FALU` carries no such contract: every instruction is one pass of independent
elements, so its answer does not depend on how many lanes were built.

`register(add)` hands the table to whatever owns the instruction dictionary.
Nothing here builds an `InstFormat`, so exactly one place knows the layout.
"""

OPC_KHF = 0x2B          # custom-1

F3_FMAC, F3_FRED, F3_FCVT, F3_FALU, F3_FSFU = 0, 1, 2, 3, 4

#: Operand width, in funct7[1:0] exactly as the integer tier does it.
FT_F16, FT_F32 = 0, 1
FT_NAME = {FT_F16: "f16", FT_F32: "f32"}

#: Registered at BOTH widths, in every group. This is the dtype rule, spelled
#: once: an optional feature is optional in its presence, never in its formats.
FLOAT_TYPES = (FT_F16, FT_F32)

FALU_VFMUL, FALU_VFADD, FALU_VFSUB, FALU_VFMA = 0, 1, 2, 3
FALU_VFMIN, FALU_VFMAX = 4, 5
FALU_VFCMPLT, FALU_VFCMPGT, FALU_VFCMPEQ = 6, 7, 8

FCVT_F2I, FCVT_I2F, FCVT_F2F = 0, 1, 2

FSFU_EXP2, FSFU_LOG2, FSFU_RCP, FSFU_RSQRT = 0, 1, 2, 3

#: `vec_alu`'s own opcodes, which the lane forwards unchanged. Every FALU and
#: FSFU instruction maps onto one of these, so the mapping lives beside the
#: instruction table rather than in the RTL where it could drift from it.
VEC_OP = {"MOV": 0, "NEG": 1, "ABS": 2, "ADD": 3, "SUB": 4, "MUL": 5,
          "FMA": 6, "FNMA": 7, "MAX": 8, "MIN": 9, "SEL": 10,
          "CMPLT": 11, "CMPGT": 12, "CMPEQ": 13,
          "EXP2": 16, "LOG2": 17, "INV": 18, "RSQRT": 19}

#: instruction stem -> the vec_alu opcode that implements it.
FOP_OF = {"vfmul": "MUL", "vfadd": "ADD", "vfsub": "SUB", "vfma": "FMA",
          "vfmin": "MIN", "vfmax": "MAX",
          "vfcmplt": "CMPLT", "vfcmpgt": "CMPGT", "vfcmpeq": "CMPEQ",
          "vfexp2": "EXP2", "vflog2": "LOG2", "vfrcp": "INV",
          "vfrsqrt": "RSQRT"}

_PACK = ("A 256-bit register is 16 %s elements; the element count is the "
         "register width over the operand width, not the lane count.")


def register(add, ops):
    """Register the float groups. `add(name, group, funct7, operands, doc, opcode)`."""
    AD, VS1, VS2, VD, AS1, XD = (ops["AD"], ops["VS1"], ops["VS2"],
                                 ops["VD"], ops["AS1"], ops["XD"])

    # ------------------------------------------- FALU: the elementwise base
    for ft in FLOAT_TYPES:
        n = FT_NAME[ft]
        pack = _PACK % n if ft == FT_F16 else (
            "A 256-bit register is 8 f32 elements.")

        add("vfmul.%s" % n, F3_FALU, (FALU_VFMUL << 2) | ft, (VD, VS1, VS2),
            "vd[i] <- vs1[i] * vs2[i] over %s. The lane's addend is forced to "
            "zero rather than a second multiplier being built. %s" % (n, pack),
            opcode=OPC_KHF)
        add("vfadd.%s" % n, F3_FALU, (FALU_VFADD << 2) | ft, (VD, VS1, VS2),
            "vd[i] <- vs1[i] + vs2[i] over %s. The lane's multiplier is forced "
            "to 1.0 rather than a second adder being built. %s" % (n, pack),
            opcode=OPC_KHF)
        add("vfsub.%s" % n, F3_FALU, (FALU_VFSUB << 2) | ft, (VD, VS1, VS2),
            "vd[i] <- vs1[i] - vs2[i] over %s. vs2's SIGN BIT is inverted and "
            "the add proceeds: negating a float is one bit, not a subtractor. "
            "%s" % (n, pack), opcode=OPC_KHF)
        add("vfma.%s" % n, F3_FALU, (FALU_VFMA << 2) | ft, (VD, VS1, VS2),
            "vd[i] <- vs1[i] * vs2[i] + vd[i] over %s, rounded ONCE. vd is read "
            "as the addend and then written, which is what makes this one fused "
            "operation rather than two instructions. %s" % (n, pack),
            opcode=OPC_KHF)
        add("vfmin.%s" % n, F3_FALU, (FALU_VFMIN << 2) | ft, (VD, VS1, VS2),
            "vd[i] <- min(vs1[i], vs2[i]) over %s. The winner is selected at "
            "the lane's first cycle and sent through as winner*1.0 + 0, which "
            "is bit-exact. %s" % (n, pack), opcode=OPC_KHF)
        add("vfmax.%s" % n, F3_FALU, (FALU_VFMAX << 2) | ft, (VD, VS1, VS2),
            "vd[i] <- max(vs1[i], vs2[i]) over %s. %s" % (n, pack),
            opcode=OPC_KHF)

        for op, sym, word in ((FALU_VFCMPLT, "<", "lt"),
                              (FALU_VFCMPGT, ">", "gt"),
                              (FALU_VFCMPEQ, "==", "eq")):
            add("vfcmp%s.%s" % (word, n), F3_FALU, (op << 2) | ft,
                (VD, VS1, VS2),
                "vd[i] <- all ones if vs1[i] %s vs2[i] else all zeros, over %s. "
                "A MASK IN AN ORDINARY VECTOR REGISTER, so vand/vandn/vor do the "
                "blend and a branchless conditional needs no new architectural "
                "state. NaN compares false in every form. %s" % (sym, n, pack),
                opcode=OPC_KHF)

    # ------------------------------------------------ FCVT: the conversions
    for ft in FLOAT_TYPES:
        n = FT_NAME[ft]
        add("vfcvt.f2i.%s" % n, F3_FCVT, (FCVT_F2I << 2) | ft, (VD, VS1),
            "vd[i] <- (int32)vs1[i], truncating toward zero, where vs1 holds %s "
            "elements. Saturates at int32's bounds; a NaN gives zero." % n,
            opcode=OPC_KHF)
        add("vfcvt.i2f.%s" % n, F3_FCVT, (FCVT_I2F << 2) | ft, (VD, VS1),
            "vd[i] <- (%s)(int32)vs1[i], round to nearest even." % n,
            opcode=OPC_KHF)
        add("vfcvt.f2f.%s" % n, F3_FCVT, (FCVT_F2F << 2) | ft, (VD, VS1),
            "vd <- vs1 converted to %s: the element type names the DESTINATION, "
            "so .f32 widens f16->f32 (exact -- E8 is FP32's exponent verbatim) "
            "and .f16 narrows f32->f16 (rounds, and a finite overflow saturates "
            "rather than becoming an infinity)." % n, opcode=OPC_KHF)

    # ------------------------------- FSFU: the four seeds the lane already has
    # `vec_alu` computes all four at FULL RATE, II = 1, sharing the FMA's own
    # normaliser and rounder -- a real GPU's special-function unit runs them at
    # a quarter rate. They cost LUT here only because exposing them stops the
    # tool constant-folding the lane's operation select.
    for ft in FLOAT_TYPES:
        n = FT_NAME[ft]
        for op, mn, doc in (
                (FSFU_EXP2, "exp2", "2 raised to vs1[i]"),
                (FSFU_LOG2, "log2", "the base-2 logarithm of vs1[i]"),
                (FSFU_RCP, "rcp", "1 / vs1[i]"),
                (FSFU_RSQRT, "rsqrt", "1 / sqrt(vs1[i])")):
            add("vf%s.%s" % (mn, n), F3_FSFU, (op << 2) | ft, (VD, VS1),
                "vd[i] <- %s, over %s. Full rate, II=1, through the same "
                "normaliser and rounder the FMA uses." % (doc, n),
                opcode=OPC_KHF)

    # ------------------------------------------ FMAC: the accumulator, optional
    for ft in FLOAT_TYPES:
        n = FT_NAME[ft]
        add("vfmacc.%s" % n, F3_FMAC, (0 << 2) | ft, (AD, VS1, VS2),
            "facc[ad] += vs1 * vs2, elementwise over %s. Lands on the next "
            "rotating partial, so consecutive ones issue at II=1 despite a "
            "15-deep lane; the order is contract." % n, opcode=OPC_KHF)
        add("vfmsac.%s" % n, F3_FMAC, (1 << 2) | ft, (AD, VS1, VS2),
            "facc[ad][i] -= vs1[i] * vs2[i], elementwise over %s" % n,
            opcode=OPC_KHF)
        add("vfaccwr.%s" % n, F3_FMAC, (4 << 2) | ft, (AD, VS1),
            "facc[ad] <- vs1 -- how a bias vector seeds a float accumulation",
            opcode=OPC_KHF)
        add("vfaccrd.%s" % n, F3_FMAC, (3 << 2) | ft, (VD, AS1),
            "vd <- facc[as1], %s" % ("rounded and saturated back to f16"
                                     if ft == FT_F16 else
                                     "widened to f32 exactly"),
            opcode=OPC_KHF)
        add("vfredsum.%s" % n, F3_FRED, (0 << 2) | ft, (XD, AS1),
            "xd <- the sum of every slot of facc[as1]. Serial through one "
            "lane's adder: it runs once per kernel, and a float adder tree "
            "would be four normalisers and four rounders for that.",
            opcode=OPC_KHF)

    add("vfaccz", F3_FMAC, (2 << 2) | FT_F16, (AD,),
        "facc[ad] <- 0, every slot. Untyped: zero is zero in either format.",
        opcode=OPC_KHF)
