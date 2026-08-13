"""The lower seam, and the toolkit for building one.

:mod:`~kohakuaccel.backend.slots` is the contract;
:mod:`~kohakuaccel.backend.isa` turns a field table into encoding, decoding,
validation and disassembly.
"""

from kohakuaccel.backend.isa import Field, InstFormat, InstSet, ISAError, roundtrip
from kohakuaccel.backend.slots import Backend, EncodeContext

__all__ = [
    "Backend",
    "EncodeContext",
    "Field",
    "ISAError",
    "InstFormat",
    "InstSet",
    "roundtrip",
]
