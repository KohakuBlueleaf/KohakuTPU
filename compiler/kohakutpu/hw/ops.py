"""The level-1 operator kinds a vector program is built from.

Lifted out of the retired `src/ktpu.ir.graph`, which carried a whole graph
IR around them. The compiler only ever wanted the enum and its sets.
"""

from enum import Enum


class OpKind(Enum):
    INPUT = "input"
    CONST = "const"

    NEG = "neg"
    ABS = "abs"
    RECIP = "recip"
    RSQRT = "rsqrt"
    SQRT = "sqrt"
    EXP2 = "exp2"
    LOG2 = "log2"
    RELU = "relu"

    ADD = "add"
    SUB = "sub"
    MUL = "mul"
    DIV = "div"
    MAX = "max"
    MIN = "min"
    CMPLT = "cmplt"
    CMPGT = "cmpgt"
    CMPEQ = "cmpeq"

    FMA = "fma"
    SELECT = "select"

    SUM = "sum"
    RMAX = "rmax"
    RMIN = "rmin"
    SUMSQ = "sumsq"

    MATMUL = "matmul"

    RESHAPE = "reshape"
    PERMUTE = "permute"
    EXPAND = "expand"
    SLICE = "slice"
    PAD = "pad"
    CONCAT = "concat"

    CAST = "cast"


ELEMENTWISE = frozenset(
    {
        OpKind.NEG,
        OpKind.ABS,
        OpKind.RECIP,
        OpKind.RSQRT,
        OpKind.SQRT,
        OpKind.EXP2,
        OpKind.LOG2,
        OpKind.RELU,
        OpKind.ADD,
        OpKind.SUB,
        OpKind.MUL,
        OpKind.DIV,
        OpKind.MAX,
        OpKind.MIN,
        OpKind.CMPLT,
        OpKind.CMPGT,
        OpKind.CMPEQ,
        OpKind.FMA,
        OpKind.SELECT,
    }
)

REDUCTION = frozenset({OpKind.SUM, OpKind.RMAX, OpKind.RMIN, OpKind.SUMSQ})

VIEW = frozenset(
    {
        OpKind.RESHAPE,
        OpKind.PERMUTE,
        OpKind.EXPAND,
        OpKind.SLICE,
        OpKind.PAD,
        OpKind.CONCAT,
    }
)

ARITY = {
    **{
        k: 1
        for k in (
            OpKind.NEG,
            OpKind.ABS,
            OpKind.RECIP,
            OpKind.RSQRT,
            OpKind.SQRT,
            OpKind.EXP2,
            OpKind.LOG2,
            OpKind.RELU,
            OpKind.CAST,
            *REDUCTION,
            *(VIEW - {OpKind.CONCAT}),
        )
    },
    **{
        k: 2
        for k in (
            OpKind.ADD,
            OpKind.SUB,
            OpKind.MUL,
            OpKind.DIV,
            OpKind.MAX,
            OpKind.MIN,
            OpKind.MATMUL,
            OpKind.CMPLT,
            OpKind.CMPGT,
            OpKind.CMPEQ,
        )
    },
    **{k: 3 for k in (OpKind.FMA, OpKind.SELECT)},
    OpKind.INPUT: 0,
    OpKind.CONST: 0,
}
