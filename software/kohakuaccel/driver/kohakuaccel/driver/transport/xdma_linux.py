"""A 64-bit AXI window over PCIe on Linux, through the Xilinx XDMA driver
(dma_ip_drivers XDMA/linux-kernel): AXI-Lite for the control space, the
H2C / C2H engines for memory. Not imported by the package (one host's answer).

Measured on multimesh_v9t6 (x4 Gen3, 2026-10-19), which is what the code obeys:

* AXI-Lite reaches control-space ``[0, 16 MB)`` in 32-bit accesses, so a word
  is two writes, low first; a strobe-blind register (the load window) takes
  each half as a write of its own (`control_bytes`; `rv64load` writes alone).
* The engines move whole 64-byte lines only (narrower: wrong lanes, a zeroed
  line rest, or a hung engine), so every transfer is widened to lines; a
  partial write reads its end lines and writes them back merged.
* H2C 0.80 / 1.46 / 2.07 GB/s at the low / mid / ship profiles from 1 MB up;
  a 64-byte C2H read ~28 us. A C2H read can hang at random: the driver's
  `c2h_timeout` ends it, `recover` resets the link, the transfer retries once.

Uninitialised ECC DRAM answers SLVERR and wedges the path, and a widened read
covers whole lines: the caller paints every line it may read.
"""

import ctypes
import mmap
import os
import pathlib
import subprocess
import threading
import time

from kohakuaccel.driver.transport.base import (
    MASK32,
    MASK64,
    WORD_BYTES,
    Transport,
    TransportUnavailable,
    aligned,
    require_words,
)

#: The engines' transfer granule.
LINE = 64
#: The user BAR: the control space the AXI-Lite master reaches.
LITE_BYTES = 16 << 20
#: One engine transfer's ceiling; the page-aligned staging buffer's size.
MAX_XFER = 16 << 20
#: PCI bridge control register, and its secondary-bus-reset bit.
BRIDGE_CONTROL, SBR = 0x3E, 0x40


class XdmaLinux(Transport):
    """The card's AXI window over PCIe on a Linux host.

    `index` names ``/dev/xdma<index>_*``. An access below `lite_bytes` goes to
    the AXI-Lite master, any other to the engines. `recover` runs after an
    engine transfer fails (the transport is closed around it and reopened
    after), then the transfer is retried once; None raises at once. Raises
    :class:`TransportUnavailable` where the device files cannot be opened.
    """

    bulk = True
    #: Every engine transfer is whole lines of this many bytes.
    line = LINE
    #: The control path's access width (`rv64load` writes strobe-blind
    #: registers one 32-bit half at a time when it is 4).
    control_bytes = 4

    def __init__(
        self,
        index: int = 0,
        lite_bytes: int = LITE_BYTES,
        max_xfer: int = MAX_XFER,
        recover=None,
    ) -> None:
        if max_xfer % LINE or lite_bytes % mmap.PAGESIZE:
            raise ValueError("max_xfer is whole lines, lite_bytes whole pages")
        self.index = index
        self.lite_bytes, self.max_xfer = lite_bytes, max_xfer
        self.recover = reset_link(index) if recover == "auto" else recover
        self.counts = dict.fromkeys(
            (
                "lite_w",
                "lite_r",
                "h2c",
                "c2h",
                "h2c_bytes",
                "c2h_bytes",
                "rmw",
                "recovered",
            ),
            0,
        )
        #: Every failed engine transfer: kind, address, bytes, error, seconds
        #: the attempt took, and the counts before it.
        self.faults: list = []
        # Page-aligned: the driver pins these pages for every transfer.
        self._staging = mmap.mmap(-1, max_xfer)
        self._pread = None
        self._fds: list = []
        self._native_lock = threading.RLock()
        self.fd_generation = 0
        self._open()

    # ------------------------------------------------------------ lifetime
    def _open(self) -> None:
        with self._native_lock:
            if self._pread is None:
                try:
                    libc = ctypes.CDLL(None, use_errno=True)
                except (OSError, TypeError) as exc:
                    raise TransportUnavailable(f"not a Linux host ({exc})") from exc
                # Plain pread into the staging pages: preadv faults the 2025.2
                # driver (kernel BUG at fs/read_write.c on 6.8).
                self._pread = libc.pread
                self._pread.restype = ctypes.c_ssize_t
                self._pread.argtypes = [
                    ctypes.c_int,
                    ctypes.c_void_p,
                    ctypes.c_size_t,
                    ctypes.c_longlong,
                ]
                self._addr = ctypes.addressof(ctypes.c_char.from_buffer(self._staging))
            dev = f"/dev/xdma{self.index}"
            try:
                self._user = os.open(f"{dev}_user", os.O_RDWR | os.O_SYNC)
                self._h2c = os.open(f"{dev}_h2c_0", os.O_WRONLY)
                self._c2h = os.open(f"{dev}_c2h_0", os.O_RDONLY)
            except OSError as exc:
                self.close()
                raise TransportUnavailable(
                    f"cannot open {dev}_*: is xdma.ko loaded and the card enumerated? ({exc})"
                ) from exc
            self._fds = [self._user, self._h2c, self._c2h]
            self.fd_generation += 1
            self._lite = mmap.mmap(
                self._user,
                self.lite_bytes,
                mmap.MAP_SHARED,
                mmap.PROT_READ | mmap.PROT_WRITE,
            )
            # One 32-bit store per access: a bytewise copy is four AXI-Lite writes.
            self._u32 = memoryview(self._lite).cast("I")

    def close(self) -> None:
        with self._native_lock:
            if self._fds:
                self.fd_generation += 1
            u32 = getattr(self, "_u32", None)
            if u32 is not None:
                u32.release()
                self._u32 = None
                self._lite.close()
            for fd in getattr(self, "_fds", []):
                try:
                    os.close(fd)
                except OSError:
                    pass
            self._fds = []

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:  # noqa: BLE001, S110 -- a finaliser must not raise
            pass

    # ------------------------------------------------------------ control
    def _lite_span(self, addr: int, nbytes: int) -> bool:
        """Whether [addr, addr + nbytes) is the control space; raises when it
        straddles the boundary."""
        if addr + nbytes <= self.lite_bytes:
            return True
        if addr < self.lite_bytes:
            raise ValueError(
                f"[{addr:#x}, {addr + nbytes:#x}) straddles the control space's end "
                f"at {self.lite_bytes:#x}"
            )
        return False

    def write32(self, addr: int, data: int) -> None:
        """One 32-bit AXI-Lite write."""
        if addr % 4 or not self._lite_span(addr, 4):
            raise ValueError(f"{addr:#x} is not a 32-bit control-space address")
        self._u32[addr >> 2] = data & MASK32
        self.counts["lite_w"] += 1

    def read32(self, addr: int) -> int:
        """One 32-bit AXI-Lite read."""
        if addr % 4 or not self._lite_span(addr, 4):
            raise ValueError(f"{addr:#x} is not a 32-bit control-space address")
        self.counts["lite_r"] += 1
        return self._u32[addr >> 2]

    def write64(self, addr: int, data: int) -> None:
        aligned(addr)
        if self._lite_span(addr, WORD_BYTES):
            self.write32(addr, data)
            self.write32(addr + 4, (data & MASK64) >> 32)
            return
        self.write_block(addr, (data & MASK64).to_bytes(WORD_BYTES, "little"))

    def read64(self, addr: int) -> int:
        aligned(addr)
        if self._lite_span(addr, WORD_BYTES):
            return self.read32(addr) | self.read32(addr + 4) << 32
        return int.from_bytes(self.read_block(addr, WORD_BYTES), "little")

    # ------------------------------------------------------------- memory
    def write_block(self, addr: int, data: bytes) -> None:
        require_words(len(data))
        if self._lite_span(addr, len(data)):
            super().write_block(addr, data)
            return
        end = addr + len(data)
        lo, hi = addr - addr % LINE, -(-end // LINE) * LINE
        if lo == addr and hi == end:
            self._h2c_lines(lo, data)
            return
        self.counts["rmw"] += 1
        buf = bytearray(hi - lo)
        if lo < addr:
            buf[:LINE] = self._c2h_lines(lo, LINE)
        if hi > end and (lo == addr or hi - lo > LINE):
            buf[-LINE:] = self._c2h_lines(hi - LINE, LINE)
        buf[addr - lo : addr - lo + len(data)] = data
        self._h2c_lines(lo, bytes(buf))

    def read_block(self, addr: int, nbytes: int) -> bytes:
        require_words(nbytes)
        if self._lite_span(addr, nbytes):
            return super().read_block(addr, nbytes)
        lo = addr - addr % LINE
        hi = -(-(addr + nbytes) // LINE) * LINE
        return self._c2h_lines(lo, hi - lo)[addr - lo : addr - lo + nbytes]

    def _h2c_lines(self, at: int, data: bytes) -> None:
        for off in range(0, len(data), self.max_xfer):
            n = min(self.max_xfer, len(data) - off)
            self._staging[:n] = data[off : off + n]
            self._retry("h2c", at + off, n, self._h2c_once)
            self.counts["h2c"] += 1
            self.counts["h2c_bytes"] += n

    def _h2c_once(self, at: int, n: int) -> None:
        got = os.pwrite(self._h2c, memoryview(self._staging)[:n], at)
        if got != n:
            raise OSError(f"short H2C write at {at:#x}: {got} of {n}")

    def _c2h_lines(self, at: int, nbytes: int) -> bytes:
        out = bytearray()
        for off in range(0, nbytes, self.max_xfer):
            n = min(self.max_xfer, nbytes - off)
            self._retry("c2h", at + off, n, self._c2h_once)
            out += self._staging[:n]
            self.counts["c2h"] += 1
            self.counts["c2h_bytes"] += n
        return bytes(out)

    def _c2h_once(self, at: int, n: int) -> None:
        got = self._pread(self._c2h, self._addr, n, at)
        if got != n:
            err = ctypes.get_errno()
            raise OSError(
                err, f"C2H read at {at:#x}: {got} of {n} ({os.strerror(err)})"
            )

    def _retry(self, kind: str, at: int, n: int, xfer) -> None:
        with self._native_lock:
            t0 = time.monotonic()
            try:
                xfer(at, n)
            except OSError as exc:
                self.faults.append(
                    {
                        "kind": kind,
                        "addr": at,
                        "bytes": n,
                        "error": str(exc),
                        "seconds": time.monotonic() - t0,
                        "after": dict(self.counts),
                    }
                )
                if self.recover is None:
                    raise
                self.close()
                self.recover()
                self._open()
                self.counts["recovered"] += 1
                xfer(at, n)

    def __repr__(self) -> str:
        return f"XdmaLinux(xdma{self.index}, {self.counts})"


def load_module(
    ko: str, args, module=pathlib.Path("/sys/module/xdma"), run=subprocess.run
) -> bool:
    """Load the driver from `ko` unless it is still loaded: an rmmod refused
    while a handle was open leaves it loaded, and the rescan re-probes the
    device under it. Whether it loaded the module."""
    if module.exists():
        return False
    run(["insmod", ko, *args], check=True)
    return True


def reset_link(index: int = 0, ko: str | None = None, params: str | None = None):
    """A recovery for `XdmaLinux(recover=...)`: unload the driver, take the
    endpoint off the tree, pulse a secondary-bus reset on its root port,
    remove and rescan the port (its bridge window is re-sized), load the
    driver again. Root only.

    The endpoint and the port are found now, while ``/dev/xdma<index>`` is
    bound; the module from `ko` (else ``$XDMA_KO``) with `params` (else
    ``$XDMA_PARAMS``). Raises :class:`TransportUnavailable` without them.
    """
    user = pathlib.Path(f"/sys/class/xdma/xdma{index}_user/device")
    if not user.exists():
        raise TransportUnavailable(f"no {user}: the xdma driver is not bound")
    dev = user.resolve()
    port = dev.parent
    ko = ko or os.environ.get("XDMA_KO")
    if not ko:
        raise TransportUnavailable(
            "reset_link needs the module's path: ko= or $XDMA_KO"
        )
    args = (params if params is not None else os.environ.get("XDMA_PARAMS", "")).split()
    pci = pathlib.Path("/sys/bus/pci")

    def run() -> None:
        subprocess.run(["rmmod", "xdma"], check=False)
        if (pci / "devices" / dev.name).exists():
            (pci / "devices" / dev.name / "remove").write_text("1")
        cfg = os.open(port / "config", os.O_RDWR)
        try:
            bc = int.from_bytes(os.pread(cfg, 2, BRIDGE_CONTROL), "little")
            os.pwrite(cfg, (bc | SBR).to_bytes(2, "little"), BRIDGE_CONTROL)
            time.sleep(0.2)
            os.pwrite(cfg, bc.to_bytes(2, "little"), BRIDGE_CONTROL)
        finally:
            os.close(cfg)
        time.sleep(1.5)
        (pci / "devices" / port.name / "remove").write_text("1")
        time.sleep(1.0)
        (pci / "rescan").write_text("1")
        time.sleep(2.0)
        if not (pci / "devices" / dev.name).exists():
            raise TransportUnavailable(f"{dev.name} did not come back after the reset")
        load_module(ko, args)
        for _ in range(50):
            if pathlib.Path(f"/dev/xdma{index}_c2h_0").exists():
                return
            time.sleep(0.1)
        raise TransportUnavailable(f"/dev/xdma{index}_* did not reappear")

    return run


__all__ = ["LINE", "LITE_BYTES", "XdmaLinux", "reset_link"]
