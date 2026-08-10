"""The XDMA backend against a fake Win32, because the card cannot be a test.

Everything below the ctypes boundary is faked: a dict standing in for the
card's memory, and a kernel32 whose four calls can be made to fail on demand.
What that leaves testable is exactly what the C header earns its keep for --
64-bit seeks, the 8 MB split, and the win32-1359 retry -- plus the property the
whole design rests on, that a block through this backend and a word loop
through any other put the same bytes at the same addresses.

The device itself is never touched. When one is present these same assertions
are what a bring-up run should reproduce.
"""

import ctypes

import pytest

from ktpu.hw import device as dev
from ktpu.hw import xdma

PAGE = 4096


class FakeCard:
    """A byte-addressed card, plus the failures the real driver produces.

    Paged, because the address map puts the control window at 0x4_0080_0000
    and a flat buffer reaching there is 16 GB of zeros.

    `fail_first` reproduces the measured behaviour that matters most: a small
    transfer that fails n times with 1359 and then works.
    """

    def __init__(self, fail_below=0, fail_first=0, hard_fail=False):
        self.pages = {}
        self.pos = 0
        self.seeks = []
        self.sizes = []
        self.attempts = 0
        self.fail_below = fail_below
        self.fail_first = fail_first
        self.hard_fail = hard_fail
        self.closed = 0

    def store(self, addr, data):
        off = 0
        while off < len(data):
            page, lo = divmod(addr + off, PAGE)
            n = min(PAGE - lo, len(data) - off)
            self.pages.setdefault(page, bytearray(PAGE))[lo : lo + n] = data[
                off : off + n
            ]
            off += n

    def load(self, addr, nbytes):
        out = bytearray()
        while len(out) < nbytes:
            page, lo = divmod(addr + len(out), PAGE)
            n = min(PAGE - lo, nbytes - len(out))
            out += self.pages.setdefault(page, bytearray(PAGE))[lo : lo + n]
        return bytes(out)

    # ---- the kernel32 surface ------------------------------------------
    def CreateFileW(self, path, access, share, sa, disp, flags, tmpl):
        self.opened = path
        return 0x1234 if "h2c" in path else 0x5678

    def SetFilePointerEx(self, handle, distance, new_pos, whence):
        self.pos = distance
        self.seeks.append(distance)
        return 1

    def CloseHandle(self, handle):
        self.closed += 1
        return 1

    def _io(self, chunk, nbytes, got, write):
        self.attempts += 1
        if self.hard_fail:
            return 0
        if self.attempts <= self.fail_first or nbytes < self.fail_below:
            ctypes.set_last_error(xdma.ERROR_INTERNAL_ERROR)
            return 0
        self.sizes.append(nbytes)
        if write:
            self.store(self.pos, bytes(chunk))
        else:
            chunk[:] = self.load(self.pos, nbytes)
        got.contents.value = nbytes
        return 1

    def WriteFile(self, handle, chunk, nbytes, got, ovl):
        return self._io(chunk, nbytes, got, write=True)

    def ReadFile(self, handle, chunk, nbytes, got, ovl):
        return self._io(chunk, nbytes, got, write=False)


@pytest.fixture
def card(monkeypatch):
    c = FakeCard()
    monkeypatch.setattr(xdma, "_api", (c, None))
    monkeypatch.setattr(xdma, "RETRY_PAUSE", 0.0)
    return c


def transport(**kw):
    return xdma.XdmaTransport(path=r"\\?\fake", **kw)


# ---- discovery ----------------------------------------------------------
def test_the_module_imports_off_windows_and_says_so(monkeypatch):
    """Absence must be catchable, so the import can never be the failure."""
    monkeypatch.setattr(xdma, "_api", None)
    monkeypatch.setattr(xdma.sys, "platform", "linux")
    with pytest.raises(dev.TransportUnavailable, match="Windows driver"):
        xdma.find()
    assert xdma.available() is False


def test_the_interface_structs_are_the_sizes_windows_expects():
    """SetupAPI rejects a cbSize it does not recognise, silently returning 0."""
    assert ctypes.sizeof(xdma._GUID) == 16
    assert ctypes.sizeof(xdma._IfaceData) == (
        32 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
    )


# ---- the transport ------------------------------------------------------
def test_a_word_round_trips(card):
    t = transport()
    t.write64(0x40, 0x0011_2233_4455_6677)
    assert t.read64(0x40) == 0x0011_2233_4455_6677
    assert card.seeks == [0x40, 0x40]


def test_the_base_is_added_to_every_address(card):
    """The board offset lives in the transport, not in the address map."""
    transport(base=0x4_0080_0000).write64(0x58, 1)
    assert card.seeks == [0x4_0080_0058]


def test_a_64_bit_offset_survives(card):
    """xdma_rw.exe truncates here and cannot reach past the first DDR bank."""
    transport().write64(0x3_0000_0008, 0xDEAD)
    assert card.seeks == [0x3_0000_0008]


def test_a_block_is_the_same_bytes_as_the_word_loop(card):
    payload = bytes((i * 37 + 11) & 0xFF for i in range(256))
    t = transport()
    t.write_block(0x1000, payload)
    assert t.read_block(0x1000, len(payload)) == payload
    assert card.load(0x1000, 256) == payload


def test_write64_and_write_block_agree(card):
    t = transport()
    t.write64(0x80, 0xAABB_CCDD_EEFF_0011)
    t.write_block(0x88, (0xAABB_CCDD_EEFF_0011).to_bytes(8, "little"))
    assert card.load(0x80, 8) == card.load(0x88, 8)


def test_partial_words_are_refused(card):
    with pytest.raises(ValueError, match="whole number"):
        transport().write_block(0, b"1234")


# ---- the driver's three quirks -----------------------------------------
def test_a_large_transfer_is_split_at_the_ceiling(card):
    """8 MB succeeds and 16 MB fails outright, so the split is not tuning."""
    t = transport()
    t.write_block(0, bytearray(xdma.MAX_XFER + 4096))
    assert card.sizes == [xdma.MAX_XFER, 4096]
    assert card.seeks == [0, xdma.MAX_XFER]


def test_each_chunk_reseeks(card):
    transport().write_block(0x2_0000_0000, bytearray(2 * xdma.MAX_XFER))
    assert card.seeks == [0x2_0000_0000, 0x2_0000_0000 + xdma.MAX_XFER]


def test_1359_is_retried_and_counted(monkeypatch):
    c = FakeCard(fail_first=2)
    monkeypatch.setattr(xdma, "_api", (c, None))
    monkeypatch.setattr(xdma, "RETRY_PAUSE", 0.0)
    t = transport()
    t.write64(0x10, 0xFEED)
    assert t.retries == 2, "the retries must be counted, not papered over"
    assert c.attempts == 3


def test_retries_run_out_and_the_address_is_named(monkeypatch):
    c = FakeCard(fail_first=99)
    monkeypatch.setattr(xdma, "_api", (c, None))
    monkeypatch.setattr(xdma, "RETRY_PAUSE", 0.0)
    with pytest.raises(xdma.XdmaError, match="0x1234"):
        transport().write64(0x1234, 1)
    assert c.attempts == xdma.RETRIES


def test_a_failure_that_is_not_1359_is_not_retried(monkeypatch):
    """Retrying a real error only delays the report."""
    c = FakeCard(hard_fail=True)
    monkeypatch.setattr(xdma, "_api", (c, None))
    ctypes.set_last_error(5)
    with pytest.raises(xdma.XdmaError):
        transport().read64(0)
    assert c.attempts == 1


# ---- lifecycle ----------------------------------------------------------
def test_close_releases_both_handles(card):
    t = transport()
    t.close()
    assert card.closed == 2
    t.close()
    assert card.closed == 2, "closing twice must not close a handle twice"


def test_it_is_a_context_manager(card):
    with transport() as t:
        t.write64(0, 1)
    assert card.closed == 2


def test_it_reports_what_it_moved(card):
    t = transport()
    t.write_block(0, bytearray(64))
    t.read_block(0, 32)
    assert t.bytes_out == 64 and t.bytes_in == 32
    assert "retries=0" in repr(t)


# ---- the contract, against the reference implementation ----------------
def test_a_program_lands_the_same_bytes_as_the_memory_fake(card):
    """The point of the whole exercise: swapping the backend changes nothing.

    The same program is pushed through XDMA's real block path and through the
    in-process fake, and the two images are compared word for word.
    """
    prog = dev.Program()
    for slot in range(6):
        body = bytes((slot * 36 + i + 1) & 0xFF for i in range(36))
        prog.stage_flit(slot, int.from_bytes(body, "little"))
    prog.wr(0x58, 3).done(0xC0DE)

    ref = dev.MemoryTransport()
    prog.execute(ref)
    prog.execute(transport())

    for addr, word in ref.mem.items():
        assert int.from_bytes(card.load(addr, 8), "little") == word
