"""The saxpy unit's instruction set. One instruction, and this is all of it.

The framework owns the 32-bit routing header and the memory request encoding.
This file spends the 256-bit payload, which is the only part a compute unit
gets to define -- see ``docs/spec/instruction-encoding.md``.

Fields are byte-aligned and generously sized because this is an example. A real
unit packs harder: 34 bits is enough for an address on the reference machine,
and the two 64-bit fields here waste 60 of them.
"""

import struct

from kohakuaccel.device import T_CU_INST, header

OP_SAXPY = 0x01

OP_HI, OP_WIDTH = 255, 8
N_HI, N_WIDTH = 247, 24
A_HI, A_WIDTH = 223, 32
X_HI, X_WIDTH = 191, 64
Y_HI, Y_WIDTH = 127, 64


def _place(value: int, hi: int, width: int) -> int:
    return (value & ((1 << width) - 1)) << (hi - width + 1)


def _take(payload: int, hi: int, width: int) -> int:
    return (payload >> (hi - width + 1)) & ((1 << width) - 1)


def encode(n: int, a: float, x_addr: int, y_addr: int) -> int:
    """The 256-bit payload for ``y[0:n] = a*x[0:n] + y[0:n]``.

    `a` is carried as its float32 bit pattern, so the unit and the host agree on
    a value rather than on a decimal rendering of one.
    """
    a_bits = struct.unpack("<I", struct.pack("<f", a))[0]
    return (
        _place(OP_SAXPY, OP_HI, OP_WIDTH)
        | _place(n, N_HI, N_WIDTH)
        | _place(a_bits, A_HI, A_WIDTH)
        | _place(x_addr, X_HI, X_WIDTH)
        | _place(y_addr, Y_HI, Y_WIDTH)
    )


def decode(payload: int) -> dict:
    """Unpack a payload into ``n``, ``a``, ``x_addr`` and ``y_addr``.

    Raises :class:`ValueError` if the opcode is not this instruction's, which is
    what turns the field into a validating tag rather than a comment.
    """
    op = _take(payload, OP_HI, OP_WIDTH)
    if op != OP_SAXPY:
        raise ValueError(f"opcode {op:#x} is not saxpy ({OP_SAXPY:#x})")
    a_bits = _take(payload, A_HI, A_WIDTH)
    return {
        "n": _take(payload, N_HI, N_WIDTH),
        "a": struct.unpack("<f", struct.pack("<I", a_bits))[0],
        "x_addr": _take(payload, X_HI, X_WIDTH),
        "y_addr": _take(payload, Y_HI, Y_WIDTH),
    }


def instruction(n: int, a: float, x_addr: int, y_addr: int, txn: int = 0) -> int:
    """A complete CU_INST flit.

    Destination and source are left zero: the dispatcher stamps both from
    PROG_DST and its own coordinates, and overwriting them here would be
    ignored.
    """
    return header(T_CU_INST, txn, True) | encode(n, a, x_addr, y_addr)
