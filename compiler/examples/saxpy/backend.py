"""The saxpy backend: one method, because the defaults cover the rest."""

import struct

from kohakuaccel.backend import Backend, EncodeContext
from kohakuaccel.ir import Task

from .isa import SAXPY


class SaxpyBackend(Backend):
    """Turns a saxpy task's payload into one instruction word.

    The payload is a plain dict here. Nothing above ever looked inside it, which
    is why it can be whatever a frontend finds convenient.
    """

    def encode(self, task: Task, ctx: EncodeContext) -> list[int]:
        op = task.payload
        a_bits = struct.unpack("<I", struct.pack("<f", op["a"]))[0]
        return [
            SAXPY.encode(
                n=op["n"],
                a=a_bits,
                x_addr=op["x_addr"],
                y_addr=op["y_addr"],
            )
        ]
