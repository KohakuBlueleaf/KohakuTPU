"""A `Window` must refuse the write its endpoint cannot perform.

Card memory ignores byte strobes: a write shorter than one 32-byte granule
paints the whole granule and ZERO-FILLS every byte the beat did not carry. Four
consecutive `write64` into one line leave only the last, and every one of them
reports success -- so nothing but a refusal can hold this.
"""

import pytest
from kohakuaccel.transport.base import Transport
from kohakuaccel.transport.rebase import Window


class Recorder(Transport):
    bulk = True

    def __init__(self) -> None:
        self.words: list[tuple[int, int]] = []
        self.blocks: list[tuple[int, bytes]] = []

    def write64(self, addr: int, data: int) -> None:
        self.words.append((addr, data))

    def write_block(self, addr: int, data: bytes) -> None:
        self.blocks.append((addr, data))

    def read64(self, addr: int) -> int:
        return 0

    def read_block(self, addr: int, nbytes: int) -> bytes:
        return bytes(nbytes)


def test_a_word_write_into_a_strobe_blind_window_is_refused():
    """`write64` is 32 bytes short of a granule, wherever it lands."""
    w = Window(Recorder(), base=0x1000, size=0x1000, granule=32)
    with pytest.raises(ValueError, match="ZEROED"):
        w.write64(0, 0xDEAD)
    with pytest.raises(ValueError, match="ZEROED"):
        w.write64(0x40, 0xDEAD)


def test_a_partial_or_misaligned_block_is_refused():
    w = Window(Recorder(), base=0x1000, size=0x1000, granule=32)
    with pytest.raises(ValueError, match="whole 32-byte write"):
        w.write_block(0, bytes(16))  # short
    with pytest.raises(ValueError, match="whole 32-byte write"):
        w.write_block(8, bytes(32))  # aligned length, misaligned start


def test_a_whole_granule_block_goes_through_mapped():
    t = Recorder()
    Window(t, base=0x1000, size=0x1000, granule=32).write_block(0x40, bytes(64))
    assert t.blocks == [(0x1040, bytes(64))]


def test_granule_zero_leaves_every_write_alone():
    """The default. An endpoint that honours strobes must not be handicapped."""
    t = Recorder()
    w = Window(t, base=0x1000, size=0x1000)
    w.write64(8, 0xBEEF)
    w.write_block(4, bytes(7))
    assert t.words == [(0x1008, 0xBEEF)]
    assert t.blocks == [(0x1004, bytes(7))]


def test_reads_are_never_refused():
    """Only WRITES carry collateral; a short read is exact."""
    w = Window(Recorder(), base=0x1000, size=0x1000, granule=32)
    assert w.read64(8) == 0
    assert w.read_block(8, 8) == bytes(8)


def test_the_bound_is_still_checked_with_a_granule_set():
    """A granule refusal must not displace the one that stops a run-off."""
    w = Window(Recorder(), base=0x1000, size=0x100, granule=32)
    with pytest.raises(ValueError, match="outside this window"):
        w.write_block(0xE0, bytes(64))


def test_repr_names_the_granule_so_a_session_can_see_it():
    w = Window(Recorder(), base=0x1000, size=0x1000, granule=32)
    assert "granule=32" in repr(w)
    assert "granule=" not in repr(Window(Recorder(), base=0x1000, size=0x1000))
