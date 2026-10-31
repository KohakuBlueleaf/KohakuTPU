"""Load and run a program on a node's RV64 processor through its load window.

The window is `rv64_load_win` behind the node's control port at +0x8000: 4 KB,
64-bit registers. [0x00..0x7F] mirror the processor's host-control registers
at their own offsets; the rest are the window's own.

    from kohakuaccel.driver.device import rv64load
    win = rv64load.LoadWindow(transport, ctrl_base + 0x8000)
    win.load_elf("build/rv64/hello_kohakuaccel.elf")
    result = win.run(expect="Nice to meet you")

A 64-bit write to LOAD_DATA streams one word into the region LOAD_CTL named
(imem: 4 bytes, spad: 8) and the pointer walks itself, so an 8 KB image is
2,048 writes and needs no mapped window. Console bytes queue in the window
(256 deep) and are read back one per access at CONS -- a write pops.

The window's registers ignore byte strobes. Over a 32-bit control path
(``transport.control_bytes == 4``, AXI-Lite) each half of a word is a write
of its own, and the station copies a 32-bit write into both halves of its
beat (`sb_nmu` place mode): a register takes {v, v}. BOOT, LOAD_CTL, STDIN
and CONS read a low field, so they are written their low half alone; PC is
the whole word, so only 0 can be written. A scratchpad word is two half
writes, each after LOAD_CTL re-aims the pointer: a half write still walks it
8, and the scratchpad keeps the strobes (0x0F, then 0xF0).
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
        self.narrow = getattr(transport, "control_bytes", 8) == 4

    def _w(self, off: int, v: int) -> None:
        """One register write; over a narrow path its low half alone."""
        if not self.narrow:
            self.t.write64(self.base + off, v)
        elif v >> 32 or (off == R_PC and v):
            raise ValueError(
                f"{v:#x} at +{off:#x} needs a 64-bit write: a 32-bit path "
                f"writes {{v, v}} (link the image at 0)"
            )
        else:
            self.t.write32(self.base + off, v)

    def _spad_words(self, offset: int, words: list) -> None:
        """Scratchpad words from byte `offset` (LOADC[23:8] is the start)."""
        if not self.narrow:
            self._w(R_LOADC, REGION_SPAD | (offset << 8))
            self.t.write_seq(self.base + R_LOADD, words)
            return
        for k, w in enumerate(words):
            at = REGION_SPAD | ((offset + 8 * k) << 8)
            self.t.write32(self.base + R_LOADC, at)
            self.t.write32(self.base + R_LOADD, w & 0xFFFF_FFFF)
            self.t.write32(self.base + R_LOADC, at)
            self.t.write32(self.base + R_LOADD + 4, w >> 32)

    # ---------------------------------------------------------------- ELF
    def load_elf(self, path) -> dict:
        info = self._parse(path)
        self._stream(REGION_IMEM, 0, info["text"], 4)
        self._stream(REGION_SPAD, SPAD_BASE, info["spad"], 8)
        return info

    def reload_spad(self, path) -> dict:
        """Re-stream only the scratchpad bytes the image's file holds (boot
        block, read-only data, initialised data); the instruction memory and
        everything crt0 clears or leaves uninitialised stay as they are. A warm
        restart after a reset that kept the arrays (a wizard relock)."""
        info = self._parse(path)
        self._stream(REGION_SPAD, SPAD_BASE, info["spad_file"], 8)
        return info

    def burn_elf(self, path, sim, cpu_scope: str) -> dict:
        """Write the image straight into a simulated node's imem and spad
        arrays (`sim.poke`); `cpu_scope` is its rv64_syscore scope."""
        info = self._parse(path)
        text = bytes(self._byte(a) for a in range((info["text"] + 3) & ~3))
        spad = bytes(self._byte(SPAD_BASE + a) for a in range((info["spad"] + 7) & ~7))
        if text:
            sim.poke(f"{cpu_scope}.u_imem.u_ram", "mem", 0, text)
        if spad:
            sim.poke(cpu_scope, "spad", 0, spad)
        return info

    def _parse(self, path) -> dict:
        b = pathlib.Path(path).read_bytes()
        if b[:4] != b"\x7fELF" or b[4] != 2:
            raise ValueError(f"{path}: not a 64-bit ELF")
        self.entry = struct.unpack_from("<Q", b, 24)[0]
        phoff = struct.unpack_from("<Q", b, 32)[0]
        phentsize, phnum = struct.unpack_from("<HH", b, 54)
        self.image = {}
        spad_file = 0
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
            if SPAD_BASE <= va < NODE_BASE and fsz:
                spad_file = max(spad_file, va + fsz - SPAD_BASE)
        text = max((a + 1 for a in self.image if a < SPAD_BASE), default=0)
        spad = max(
            (a + 1 - SPAD_BASE for a in self.image if SPAD_BASE <= a < NODE_BASE),
            default=0,
        )
        dram = {a: v for a, v in self.image.items() if a >= NODE_BASE}
        return {
            "entry": self.entry,
            "text": text,
            "spad": spad,
            "spad_file": spad_file,
            "dram_bytes": len(dram),
        }

    def _byte(self, a: int) -> int:
        return self.image.get(a, 0)

    def _stream(self, region: int, base: int, nbytes: int, width: int) -> None:
        if nbytes == 0:
            return
        words = []
        for a in range(0, nbytes, width):
            w = 0
            for j in range(width):
                w |= self._byte(base + a + j) << (8 * j)
            words.append(w)
        if region == REGION_SPAD:
            self._spad_words(0, words)
            return
        self._w(R_LOADC, region | (0 << 8))
        if self.narrow:
            for w in words:
                self.t.write32(self.base + R_LOADD, w)
        else:
            self.t.write_seq(self.base + R_LOADD, words)

    # ----------------------------------------------------------------- run
    def stdin(self, text: str) -> None:
        """Queue bytes for the program to read at R_STDIN, before boot."""
        for ch in text.encode():
            self._w(R_STDIN, ch)

    def write_spad(self, offset: int, data: bytes) -> None:
        """Stream `data` into the scratchpad from byte `offset` (8-byte words).
        Only while the core is stopped: both share the scratchpad's write port."""
        if offset % 8 or len(data) % 8:
            raise ValueError("the scratchpad is streamed in whole 8-byte words")
        self._spad_words(
            offset,
            [int.from_bytes(data[a : a + 8], "little") for a in range(0, len(data), 8)],
        )

    def stop(self) -> None:
        """Hold the core in reset (HR_BOOT = 0)."""
        self._w(R_BOOT, 0)

    def boot(self) -> None:
        self._w(R_PC, self.entry)
        self._w(R_BOOT, 1)

    def status(self) -> int:
        return self.t.read64(self.base + R_STATUS)

    def console(self) -> str:
        out = []
        while True:
            w = self.t.read64(self.base + R_CONS)
            if not (w >> 8) & 1:
                break
            out.append(chr(w & 0xFF))
            self._w(R_CONS, 0)  # pop
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
