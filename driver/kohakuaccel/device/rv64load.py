"""Load and run a program on a node's RV64 processor through its load window.

The window is `rv64_load_win` behind the node's control port at +0x8000: 4 KB,
64-bit registers. [0x00..0x7F] mirror the processor's host-control registers
at their own offsets; the rest are the window's own.

    from kohakuaccel.device import rv64load
    win = rv64load.LoadWindow(transport, ctrl_base + 0x8000)
    win.load_elf("build/rv64/hello_kohakuaccel.elf")
    result = win.run(expect="Nice to meet you")

A 64-bit write to LOAD_DATA streams one word into the region LOAD_CTL named
(imem: 4 bytes, spad: 8) and the pointer walks itself, so an 8 KB image is
2,048 writes and needs no mapped window. Console bytes queue in the window
(256 deep) and are read back one per access at CONS -- a write pops.
"""

import pathlib
import struct
import time

# processor host-control registers, mirrored at their own offsets
R_BOOT, R_PC, R_STATUS, R_EXIT, R_HALTPC, R_STDIN = 0x00, 0x08, 0x18, 0x20, 0x28, 0x40
# the window's own
R_LOADC, R_LOADD, R_CONS = 0x80, 0x88, 0x90
REGION_IMEM, REGION_SPAD = 0, 1
SPAD_BASE = 0x0001_0000
NODE_BASE = 0x1000_0000

WINDOW_OFFSET = 0x8000  # inside the node's 64 KB control window


class LoadWindow:
    def __init__(self, transport, base: int) -> None:
        self.t = transport
        self.base = base
        self.image: dict[int, int] = {}
        self.entry = 0

    # ---------------------------------------------------------------- ELF
    def load_elf(self, path) -> dict:
        b = pathlib.Path(path).read_bytes()
        if b[:4] != b"\x7fELF" or b[4] != 2:
            raise ValueError(f"{path}: not a 64-bit ELF")
        self.entry = struct.unpack_from("<Q", b, 24)[0]
        phoff = struct.unpack_from("<Q", b, 32)[0]
        phentsize, phnum = struct.unpack_from("<HH", b, 54)
        self.image = {}
        for i in range(phnum):
            p = phoff + i * phentsize
            ptype = struct.unpack_from("<I", b, p)[0]
            if ptype != 1:
                continue
            off, va, fsz, msz = struct.unpack_from(
                "<QQ", b, p + 8
            ) + struct.unpack_from("<QQ", b, p + 32)
            for k in range(fsz):
                self.image[va + k] = b[off + k]
            for k in range(fsz, msz):
                self.image[va + k] = 0
        text = max((a + 1 for a in self.image if a < SPAD_BASE), default=0)
        spad = max(
            (a + 1 - SPAD_BASE for a in self.image if SPAD_BASE <= a < NODE_BASE),
            default=0,
        )
        dram = {a: v for a, v in self.image.items() if a >= NODE_BASE}
        self._stream(REGION_IMEM, 0, text, 4)
        self._stream(REGION_SPAD, SPAD_BASE, spad, 8)
        return {
            "entry": self.entry,
            "text": text,
            "spad": spad,
            "dram_bytes": len(dram),
        }

    def _byte(self, a: int) -> int:
        return self.image.get(a, 0)

    def _stream(self, region: int, base: int, nbytes: int, width: int) -> None:
        if nbytes == 0:
            return
        self.t.write64(self.base + R_LOADC, region | (0 << 8))
        for a in range(0, nbytes, width):
            w = 0
            for j in range(width):
                w |= self._byte(base + a + j) << (8 * j)
            self.t.write64(self.base + R_LOADD, w)

    # ----------------------------------------------------------------- run
    def stdin(self, text: str) -> None:
        """Queue bytes for the program to read at R_STDIN, before boot."""
        for ch in text.encode():
            self.t.write64(self.base + R_STDIN, ch)

    def boot(self) -> None:
        self.t.write64(self.base + R_PC, self.entry)
        self.t.write64(self.base + R_BOOT, 1)

    def status(self) -> int:
        return self.t.read64(self.base + R_STATUS)

    def console(self) -> str:
        out = []
        while True:
            w = self.t.read64(self.base + R_CONS)
            if not (w >> 8) & 1:
                break
            out.append(chr(w & 0xFF))
            self.t.write64(self.base + R_CONS, 0)  # pop
        return "".join(out)

    def run(self, expect: str | None = None, timeout: float = 600.0, poll=None) -> dict:
        """Boot, drain the console until the processor halts or exits."""
        self.boot()
        text = ""
        t0 = time.monotonic()
        st = 0
        while time.monotonic() - t0 < timeout:
            text += self.console()
            st = self.status()
            if st & 0xC:
                break
            if poll is not None:
                poll()
        text += self.console()
        return {
            "status": st,
            "halted": bool(st & 0x4),
            "exit": self.t.read64(self.base + R_EXIT),
            "halt_pc": self.t.read64(self.base + R_HALTPC),
            "console": text,
            "ok": (expect is None) or (expect in text),
            "seconds": time.monotonic() - t0,
        }
