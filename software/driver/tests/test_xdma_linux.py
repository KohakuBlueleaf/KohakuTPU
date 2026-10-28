"""XdmaLinux's access shaping, against an engine that takes whole lines only.

`LineEngine` replaces the device files: its H2C / C2H refuse any transfer that
is not whole aligned 64-byte lines (what the card's engines need) and its
AXI-Lite records each 32-bit access in order. The bytes are checked against a
`MemoryTransport` given the same accesses as words -- the word-for-word
equivalence `Transport` promises -- so a widening or merge that moves a byte
fails here. The load window's narrow mode is checked access by access.
"""

import random
import struct
import types

import pytest

from kohakuaccel.driver.device import rv64load
from kohakuaccel.driver.transport.memory import MemoryTransport
from kohakuaccel.driver.transport.xdma_linux import LINE, XdmaLinux, load_module

LITE = 1 << 16
DRAM = 1 << 40


class Lite32:
    """A 32-bit register file that logs every access in order."""

    def __init__(self) -> None:
        self.mem: dict[int, int] = {}
        self.log: list = []

    def __setitem__(self, i, v):
        self.log.append(("w", 4 * i, v))
        self.mem[i] = v

    def __getitem__(self, i):
        self.log.append(("r", 4 * i))
        return self.mem.get(i, 0)

    def release(self):
        pass


class LineEngine(XdmaLinux):
    def __init__(self, fail_reads: int = 0, **kw) -> None:
        self.lines: dict[int, bytes] = {}
        self.xfers: list = []
        self.fail_reads = fail_reads
        self.opened = 0
        super().__init__(lite_bytes=LITE, max_xfer=4 * LINE, **kw)

    def _open(self) -> None:
        self.opened += 1
        self._u32 = Lite32()
        self._lite = types.SimpleNamespace(close=lambda: None)
        self._fds = []

    def _whole(self, at, n):
        assert at % LINE == 0 and n % LINE == 0 and n, (hex(at), n)

    def _h2c_once(self, at, n):
        self._whole(at, n)
        self.xfers.append(("h2c", at, n))
        for k in range(0, n, LINE):
            self.lines[at + k] = bytes(self._staging[k : k + LINE])

    def _c2h_once(self, at, n):
        self._whole(at, n)
        if self.fail_reads:
            self.fail_reads -= 1
            raise OSError(62, "C2H timed out")
        self.xfers.append(("c2h", at, n))
        for k in range(0, n, LINE):
            self._staging[k : k + LINE] = self.lines.get(at + k, bytes(LINE))


def test_memory_accesses_are_whole_lines_and_byte_exact():
    rng = random.Random(7)
    x, ref = LineEngine(), MemoryTransport()
    span = 64 * LINE
    for _ in range(400):
        at = DRAM + 8 * rng.randrange(span // 8 - 64)
        n = 8 * rng.randrange(1, 40)
        op = rng.randrange(4)
        if op == 0:
            data = rng.randbytes(n)
            x.write_block(at, data)
            ref.write_block(at, data)
        elif op == 1:
            v = rng.getrandbits(64)
            x.write64(at, v)
            ref.write64(at, v)
        elif op == 2:
            assert x.read_block(at, n) == ref.read_block(at, n)
        else:
            assert x.read64(at) == ref.read64(at)
    assert x.read_block(DRAM, span) == ref.read_block(DRAM, span)
    # Long transfers split at max_xfer, and only partial ends were merged.
    assert max(n for _, _, n in x.xfers) == 4 * LINE
    assert x.counts["rmw"] > 0
    aligned_write = len(x.xfers)
    x.write_block(DRAM + LINE, bytes(2 * LINE))
    assert x.xfers[aligned_write:] == [("h2c", DRAM + LINE, 2 * LINE)]


def test_a_partial_write_merges_one_line_read_once():
    x = LineEngine()
    x.write_block(DRAM, bytes(range(64)))
    x.xfers.clear()
    x.write_block(DRAM + 16, b"\xff" * 16)
    assert x.xfers == [("c2h", DRAM, LINE), ("h2c", DRAM, LINE)]
    want = bytes(range(16)) + b"\xff" * 16 + bytes(range(32, 64))
    assert x.read_block(DRAM, LINE) == want


def test_control_words_are_two_lite_writes_low_first():
    x = LineEngine()
    x.write64(0x8008, 0x1122334455667788)
    assert x._u32.log == [("w", 0x8008, 0x55667788), ("w", 0x800C, 0x11223344)]
    assert x.read64(0x8008) == 0x1122334455667788
    x.write32(0x9004, 0xDEADBEEF)
    assert x._u32.log[-1] == ("w", 0x9004, 0xDEADBEEF)
    assert x.xfers == []
    with pytest.raises(ValueError, match="straddles"):
        x.read_block(LITE - 8, 16)


def test_a_failed_transfer_recovers_and_retries_once():
    calls = []
    x = LineEngine(fail_reads=1, recover=lambda: calls.append("reset"))
    x.write_block(DRAM, bytes(range(64)))
    assert x.read_block(DRAM, LINE) == bytes(range(64))
    assert calls == ["reset"] and x.opened == 2 and x.counts["recovered"] == 1
    y = LineEngine(fail_reads=1)
    y.write_block(DRAM, bytes(64))
    with pytest.raises(OSError, match="timed out"):
        y.read_block(DRAM, LINE)


def _elf(text: bytes, spad: bytes, entry: int = 0) -> bytes:
    """A minimal ELF64 of two PT_LOAD segments: imem at 0, spad at SPAD_BASE."""
    ph = 64
    off_t = ph + 2 * 56
    off_s = off_t + len(text)
    hdr = bytearray(64)
    hdr[:5] = b"\x7fELF\x02"
    struct.pack_into("<QQ", hdr, 24, entry, ph)
    struct.pack_into("<HH", hdr, 54, 56, 2)
    segs = b""
    for va, off, raw in ((0, off_t, text), (rv64load.SPAD_BASE, off_s, spad)):
        segs += struct.pack("<IIQQQQQQ", 1, 7, off, va, va, len(raw), len(raw), 8)
    return bytes(hdr) + segs + text + spad


def test_a_link_reset_loads_the_driver_only_when_it_unloaded(tmp_path):
    calls = []
    module = tmp_path / "xdma"
    module.mkdir()
    assert not load_module("xdma.ko", ["c2h_timeout=3"], module, calls.append)
    assert calls == []
    module.rmdir()
    assert load_module(
        "xdma.ko", ["c2h_timeout=3"], module, lambda c, check: calls.append(c)
    )
    assert calls == [["insmod", "xdma.ko", "c2h_timeout=3"]]


def test_the_load_window_writes_halves_alone_over_lite(tmp_path):
    x = LineEngine()
    win = rv64load.LoadWindow(x, 0x8000)
    assert win.narrow
    elf = tmp_path / "t.elf"
    word = 0x0123456789ABCDEF
    elf.write_bytes(
        _elf((0x13).to_bytes(4, "little") * 2, word.to_bytes(8, "little"), 0)
    )
    win.load_elf(elf)
    win.boot()
    win.stdin("A")
    ld, lc = 0x8000 + rv64load.R_LOADD, 0x8000 + rv64load.R_LOADC
    assert x._u32.log == [
        ("w", lc, 0),
        ("w", ld, 0x13),
        ("w", ld, 0x13),
        ("w", lc, 1),
        ("w", ld, 0x89ABCDEF),
        ("w", lc, 1),
        ("w", ld + 4, 0x01234567),
        ("w", 0x8000 + rv64load.R_PC, 0),
        ("w", 0x8000 + rv64load.R_BOOT, 1),
        ("w", 0x8000 + rv64load.R_STDIN, 0x41),
    ]
    # The station copies a 32-bit write into both halves: PC only at 0.
    win.entry = 0x40
    with pytest.raises(ValueError, match="64-bit write"):
        win.boot()
    with pytest.raises(ValueError, match="64-bit write"):
        win._w(rv64load.R_LOADC, 1 << 32)
