"""The saxpy unit's instruction set, declared rather than packed.

Compare ``driver/examples/saxpy/sw/isa.py``, which hand-rolls the same layout
with three helper functions and a pair of shift constants per field. This is the
same instruction and the same bits, and it also gets range checking, a
disassembler and a round-trip test for free.

Payload only: the routing header belongs to the driver.
"""

from kohakuaccel.backend import Field, InstFormat, InstSet

OP_SAXPY = 0x01

SAXPY = InstFormat(
    "saxpy",
    [
        Field("op", 8, const=OP_SAXPY),
        Field("n", 24, doc="elements, so one instruction covers up to 16M"),
        Field("a", 32, doc="the scalar, as float32 bits"),
        Field("x_addr", 64),
        Field("y_addr", 64),
    ],
    width=256,
    doc="y[0:n] = a*x[0:n] + y[0:n]",
)

# A second instruction, so the set has something to dispatch between and the
# disassembler has to actually choose.
FILL = InstFormat(
    "fill",
    [
        Field("op", 8, const=0x02),
        Field("n", 24),
        Field("value", 32, doc="float32 bits"),
        Field("addr", 64),
    ],
    width=256,
    doc="y[0:n] = value",
)

ISA = InstSet("saxpy", [SAXPY, FILL])
