"""The machine's number formats and the conversions between them.

E8M15, ACC24 and MXFP7 exist in no external type system, so precision reasoning
happens here or not at all.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Kind(Enum):
    FLOAT = "float"
    BLOCK = "block"
    INT = "int"


class DTypeError(TypeError):
    """Operand formats with no common form on any engine."""


@dataclass(frozen=True)
class DType:
    """`exp`/`man` are stored field widths; the significand adds the implicit 1."""

    name: str
    bits: int
    kind: Kind
    exp: int = 0
    man: int = 0
    bias: int = 0
    subnormal: bool = True
    block: int = 0
    scale: Optional["DType"] = None
    value: Optional["DType"] = None

    def __str__(self) -> str:
        return self.name

    @property
    def significand(self) -> int:
        return self.man + 1 if self.kind is Kind.FLOAT else self.bits

    @property
    def max_exp(self) -> int:
        # all-ones exponent is reserved for inf/NaN
        return (1 << self.exp) - 2 - self.bias

    @property
    def min_exp(self) -> int:
        return 1 - self.bias

    @property
    def max_finite(self) -> float:
        if self.kind is not Kind.FLOAT:
            raise TypeError(f"{self.name} is not a float")
        return (2.0 - 2.0**-self.man) * 2.0**self.max_exp

    @property
    def eps(self) -> float:
        """Half-ulp relative error."""
        if self.kind is not Kind.FLOAT:
            raise TypeError(f"{self.name} is not a float")
        return 2.0 ** -(self.man + 1)

    @property
    def smallest_positive(self) -> float:
        """Smallest magnitude that survives, subnormals included if any."""
        if self.kind is not Kind.FLOAT:
            raise TypeError(f"{self.name} is not a float")
        floor = 2.0**self.min_exp
        return floor * 2.0**-self.man if self.subnormal else floor

    def contains_range_of(self, other: "DType") -> bool:
        """Every finite magnitude of `other` fits without overflow.

        Range only: ACC24 -> E8M15 passes while still rounding a mantissa bit.
        """
        if self.kind is not Kind.FLOAT or other.kind is not Kind.FLOAT:
            return False
        return self.max_exp >= other.max_exp and self.min_exp <= other.min_exp

    def is_exact_from(self, other: "DType") -> bool:
        return self.contains_range_of(other) and self.man >= other.man

    def may_saturate_from(self, other: "DType") -> bool:
        return not self.contains_range_of(other)


def value_dtype(d: DType) -> DType:
    """What a format decodes to. A block type encodes a value, it is not one."""
    return d.value if d.kind is Kind.BLOCK and d.value is not None else d


def converge(*dtypes: DType) -> DType:
    """The format an op's operands must agree on.

    Not promotion: asks what a value must HOLD, and never invents a format.
    Mixing floats is ordinary -- matmul(FP16, MXFP7) is pre-quantised weights
    against online-quantised activations, add(FP32, FP16) is native to the
    vector core. The compute format is the engine's, decided at level 2.
    """
    if not dtypes:
        raise DTypeError("converge() needs at least one dtype")
    vals = [value_dtype(d) for d in dtypes]

    if len({v.kind for v in vals}) != 1:
        names = ", ".join(str(d) for d in dtypes)
        raise DTypeError(f"no common format for ({names}): mixed float and integer")

    if vals[0].kind is Kind.INT:
        if len(set(vals)) != 1:
            names = ", ".join(str(d) for d in dtypes)
            raise DTypeError(f"integer formats must match exactly, got ({names})")
        return vals[0]

    holds_all = [c for c in vals if all(c.contains_range_of(v) for v in vals)]
    if not holds_all:
        names = ", ".join(str(d) for d in dtypes)
        raise DTypeError(f"no operand of ({names}) holds every other's range")
    return max(holds_all, key=lambda c: (c.man, c.exp))


FP32 = DType("fp32", 32, Kind.FLOAT, exp=8, man=23, bias=127)
FP16 = DType("fp16", 16, Kind.FLOAT, exp=5, man=10, bias=15)

# vector core, src/kohakutpu/vector/vec_alu.v
E8M15 = DType("e8m15", 24, Kind.FLOAT, exp=8, man=15, bias=127, subnormal=False)

# matmul accumulator, src/kohakutpu/matmul/mx_fpacc.v at MW=16
ACC24 = DType("acc24", 24, Kind.FLOAT, exp=7, man=16, bias=63, subnormal=False)

# block scale, src/kohakumas/mx_quant.v: scale = 2^(E-20) * (1+M/8)
E5M3 = DType("e5m3", 8, Kind.FLOAT, exp=5, man=3, bias=20)

# int7 with one E5M3 scale per 32 along K. `value` is FP16 because the scale
# spans FP16's range, so quantising FP32 clamps to FP16's range, not FP32's.
MXFP7 = DType("mxfp7", 7, Kind.BLOCK, block=32, scale=E5M3, value=FP16)

INT8 = DType("int8", 8, Kind.INT)
INT16 = DType("int16", 16, Kind.INT)
INT32 = DType("int32", 32, Kind.INT)

HOST_DTYPES = (FP32, FP16)
VECTOR_DTYPES = (E8M15,)

ALL = (FP32, FP16, E8M15, ACC24, E5M3, MXFP7, INT8, INT16, INT32)
BY_NAME = {d.name: d for d in ALL}
