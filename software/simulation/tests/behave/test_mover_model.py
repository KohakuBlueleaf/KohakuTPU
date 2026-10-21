"""The behaviour model's mover on its own: moves as the package writes them
(`package.mover`), their bytes against numpy."""

import numpy as np

from kohakuaccel.compiler.package import mover as PM
from kohakutpu.simulation.behave.mover import Mover


class _Mem:
    def __init__(self, n: int) -> None:
        self.b = bytearray(n)

    def read_block(self, addr: int, n: int) -> bytes:
        return bytes(self.b[addr : addr + n])

    def write_block(self, addr: int, data: bytes) -> None:
        self.b[addr : addr + len(data)] = data


def test_a_negative_stride_walks_the_words_backwards():
    """Source from its last word back (stride -32): the words reversed."""
    mem = _Mem(1 << 12)
    words = np.arange(16 * 8, dtype=np.uint16).reshape(8, 16)
    mem.write_block(0, words.tobytes())
    m = Mover(mem)
    for reg, value in PM.move(PM.COPY, (7 * 32, [(8, -32)]), (1024, [(8, 32)])):
        m.write(reg, value)
    got = np.frombuffer(mem.read_block(1024, 256), np.uint16).reshape(8, 16)
    assert np.array_equal(got, words[::-1])
