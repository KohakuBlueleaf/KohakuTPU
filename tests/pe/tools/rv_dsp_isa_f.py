"""The float tier's instructions, on RISC-V custom-1.

Held apart from `rv_dsp_isa.py` so that the integer table stays readable and so
that a build without the float tier lacks these encodings **at the opcode
major** -- a program using one then faults on an illegal instruction rather than
landing in a decode case that computes something plausible.

Phase 1 is the accumulator and nothing else: zero it, multiply-accumulate into
it, seed it, read it, reduce it. The elementwise forms are a separate decision
with a separate cost, because they write a VECTOR REGISTER and a 14-cycle
datapath writing a register needs a scoreboard, where an accumulating
instruction needs only the accumulator's own busy shadow.

`register(add)` hands the table to whatever owns the instruction dictionary; the
callback signature is the integer table's `_add` with an `opcode` argument.
Nothing here constructs an `InstFormat` itself, so there is exactly one place
that knows how a KohakuDSP instruction is laid out.

## The accumulation order is CONTRACT, not implementation

An accumulator is `2*SIMD` slots of **NPART partials** in E8M15, and the `n`th
accumulate since `vfaccz` lands on partial `n mod NPART`. `vfaccrd` and
`vfredsum` combine the partials in index order.

That is not an implementation detail leaking out. The lane's FMA is fourteen
cycles deep, so `acc = a*b + acc` on one partial would issue at II = 14; the
vector core breaks the recurrence with sixteen rotating partials
(`vec_lanes.v` s7.3) and this tier does the same. **Float addition does not
associate**, so the rotation changes the answer — a machine that rotated by 8
and a model that rotated by 16 would disagree on ordinary data and be right by
their own lights. The count is therefore architectural and a build that changes
it changes results.
"""

OPC_KHF = 0x2B          # custom-1

F3_FMAC, F3_FRED, F3_FCVT = 0, 1, 2

#: Element type, in funct7[1:0] exactly as the integer tier does it.
FT_F16, FT_F32 = 0, 1
FT_NAME = {FT_F16: "f16", FT_F32: "f32"}

#: Phase 1 builds f16 only. f32 is a legal encoding that every current build
#: refuses, which is what makes "this variant has no FP32" checkable.
PHASE1_TYPES = (FT_F16,)


def register(add, ops):
    """Register the float tier. `add(name, group, funct7, operands, doc, opcode)`.

    `ops` is the operand vocabulary from the integer table -- the same `Operand`
    objects, so the C generator's `xreg`/`vreg`/`areg` distinction carries over
    unchanged.
    """
    AD, VS1, VS2, VD, AS1, XD = (ops["AD"], ops["VS1"], ops["VS2"],
                                 ops["VD"], ops["AS1"], ops["XD"])

    for ft in PHASE1_TYPES:
        n = FT_NAME[ft]
        add("vfmacc.%s" % n, F3_FMAC, (0 << 2) | ft, (AD, VS1, VS2),
            "facc[ad] += vs1 * vs2, elementwise over %s. Lands on the next "
            "rotating partial, so consecutive ones issue at II=1 despite a "
            "14-deep lane; the order is contract." % n, opcode=OPC_KHF)
        add("vfmsac.%s" % n, F3_FMAC, (1 << 2) | ft, (AD, VS1, VS2),
            "facc[ad][i] -= vs1[i] * vs2[i], elementwise over %s" % n,
            opcode=OPC_KHF)
        add("vfaccwr.%s" % n, F3_FMAC, (4 << 2) | ft, (AD, VS1),
            "facc[ad] <- vs1 -- how a bias vector seeds a float accumulation",
            opcode=OPC_KHF)
        add("vfaccrd.%s" % n, F3_FMAC, (3 << 2) | ft, (VD, AS1),
            "vd <- facc[as1], rounded and saturated back to %s" % n,
            opcode=OPC_KHF)
        add("vfredsum.%s" % n, F3_FRED, (0 << 2) | ft, (XD, AS1),
            "xd <- the sum of every slot of facc[as1]. Serial through one "
            "lane's adder: it runs once per kernel, and a float adder tree "
            "would be four normalisers and four rounders for that.",
            opcode=OPC_KHF)

    add("vfaccz", F3_FMAC, (2 << 2) | FT_F16, (AD,),
        "facc[ad] <- 0, every slot. Untyped: zero is zero in either format.",
        opcode=OPC_KHF)
