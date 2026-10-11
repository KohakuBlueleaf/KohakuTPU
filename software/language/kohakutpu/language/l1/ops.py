"""The V2 vector core's math mnemonics as L1 names them, and what each reads
and writes: the facts the L1 optimisers reason with. Their encodings are the
compiler's (`kohakutpu.compiler.encode.vector.OPS`, keyed by the same names)."""

#: One source (a).
UNARY = frozenset({"vmov", "vneg", "vabs", "vexp2", "vlog2", "vinv", "vrsqrt"})
#: Reads a second source (b).
READS_B = frozenset(
    {
        "vmul",
        "vfma",
        "vfnma",
        "vmax",
        "vmin",
        "vsel",
        "vcmplt",
        "vcmpgt",
        "vcmpeq",
        "vexp2d",
    }
)
#: Reads a third source (c).
READS_C = frozenset({"vadd", "vsub", "vfma", "vfnma", "vsel"})
#: Writes a predicate, not a register.
COMPARE = frozenset({"vcmplt", "vcmpgt", "vcmpeq"})
#: Every math mnemonic.
MATH = UNARY | READS_B | READS_C

__all__ = ["COMPARE", "MATH", "READS_B", "READS_C", "UNARY"]
