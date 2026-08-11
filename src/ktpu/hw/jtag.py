"""The JTAG-AXI backend, through a live Vivado Tcl session.

CONTROL PLANE AND BRING-UP ONLY. JTAG-to-AXI is ~0.08 MB/s against XDMA's
~1.8 GB/s, so an 8 MB operand image is a hundred seconds; a block past
`max_block` is refused rather than taking minutes quietly. It is worth having
because it needs no PCIe -- it works before the host has enumerated the card --
and because both masters are mapped identically and verified byte-exact
(JTAG-DMA-test docs/XDMA_WINDOWS.md s3.1), so a pointer means the same thing
here as over PCIe and this is a debugger for PCIe-side work.

It talks to `tools/vivado_tcl_server.tcl` over a length-prefixed socket,
reimplemented here so ktpu does not depend on a file in another repo, and
drives `tools/jtag_axi.tcl`, which presents beats in ADDRESS ORDER already.
"""

import os
import socket

from ktpu.hw.device import (
    MASK64,
    WORD_BYTES,
    Transport,
    TransportUnavailable,
    require_words,
)

DEFAULT_PORT = 5570
DEFAULT_HOST = "127.0.0.1"

# ~0.08 MB/s measured, so this is about three seconds. A ceiling in BYTES with
# a known rate is the only honest way to say "this will not be quick".
MAX_BLOCK = 1 << 18
JTAG_BYTES_PER_SECOND = 80_000

# Measured at 8 on ship_3x2. `reset_hw_axi` restores STATUS bits only, so
# nothing short of reloading the bitstream flushes the queue that causes it.
MAX_SHIFT_BEATS = 64
_PROBE = 0xC0DE_0000_0000_0000


class JtagError(RuntimeError):
    """Vivado answered, and what it said was an error."""


def evaluate(script, host=DEFAULT_HOST, port=DEFAULT_PORT, timeout=600.0):
    """Run Tcl in the live Vivado session; returns its result string.

    Raises :class:`TransportUnavailable` when the server is not listening and
    :class:`JtagError` when the script itself failed. Tcl code 2 is a bare
    `return` at script level, which is not a failure.
    """
    payload = script.encode("utf-8")
    try:
        with socket.create_connection((host, port), timeout=10) as sock:
            sock.settimeout(timeout)
            sock.sendall(str(len(payload)).encode("ascii") + b"\n" + payload)
            code, n_res, n_log = (int(v) for v in _line(sock).split())
            result = _exact(sock, n_res).decode("utf-8", "replace")
            _exact(sock, n_log)
    except OSError as exc:
        raise TransportUnavailable(
            f"no Vivado Tcl server on {host}:{port} ({exc}). Start one with "
            "tools/vivado_tcl_server.tcl in the hardware project."
        ) from exc
    if code == 1:
        raise JtagError(result.strip())
    return result


def _beats(data: bytes) -> list:
    """Bytes as the zero-padded hex beats `jaxi::write` takes, in address order."""
    return [
        f"{int.from_bytes(data[i : i + WORD_BYTES], 'little'):016x}"
        for i in range(0, len(data), WORD_BYTES)
    ]


def _line(sock):
    buf = bytearray()
    while not buf.endswith(b"\n"):
        chunk = sock.recv(1)
        if not chunk:
            raise OSError("connection closed while reading the header")
        buf += chunk
    return buf.decode("ascii").strip()


def _exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise OSError(f"connection closed with {n - len(buf)} bytes outstanding")
        buf += chunk
    return bytes(buf)


class JtagTransport(Transport):
    """A 64-bit AXI window driven over JTAG, through Vivado's Tcl console.

    ``base`` is added to every address, as on the PCIe backend, so the two are
    interchangeable and neither carries a board constant.

    ``tcl`` is the path to `jtag_axi.tcl`; without it the session is expected
    to have sourced it already. `KTPU_JAXI_TCL` sets it from the environment.
    """

    bulk = True

    def __init__(
        self,
        base: int = 0,
        tcl=None,
        host=DEFAULT_HOST,
        port=DEFAULT_PORT,
        max_block: int = MAX_BLOCK,
        timeout: float = 600.0,
        scratch: int = 0,
    ):
        self.base = base
        self.host, self.port = host, port
        self.timeout = timeout
        self.max_block = max_block
        # Only `verify_write_path` may make this non-zero: nothing silently
        # rewrites data on its way to the card.
        self.write_shift = 0
        self.scratch = scratch
        self.tcl = tcl if tcl is not None else os.environ.get("KTPU_JAXI_TCL")
        self.calls = 0
        self._connect()

    def _eval(self, script):
        self.calls += 1
        return evaluate(script, self.host, self.port, self.timeout)

    def _connect(self):
        """Source the helpers if asked to, attach to the master, check its width.

        The width check is not ceremony: a 32-bit JTAG-AXI master would make
        every beat half a word, and the driver would write a coherent-looking
        program to the wrong half of every register.
        """
        boot = "if {![namespace exists jaxi]} { %s }\njaxi::require_connected\n" % (
            f"source {{{self.tcl}}}"
            if self.tcl
            else 'error "jaxi is not sourced in this session; pass tcl= or set '
            'KTPU_JAXI_TCL"'
        )
        self._eval(boot)
        width = int(self._eval("set ::jaxi::bytes_per_beat").strip())
        if width != WORD_BYTES:
            raise TransportUnavailable(
                f"the JTAG-AXI master is {width * 8} bits wide; this driver "
                f"addresses {WORD_BYTES * 8}-bit words"
            )

    # ---- the transport --------------------------------------------------
    def write64(self, addr: int, data: int) -> None:
        self._eval(f"jaxi::write {self.base + addr} {{{data & MASK64:016x}}}")

    def read64(self, addr: int) -> int:
        return int(self._eval(f"jaxi::read {self.base + addr} 1").split()[0], 16)

    def write_block(self, addr: int, data: bytes) -> None:
        require_words(len(data))
        self._check_size(len(data))
        if self.write_shift:
            return self._write_shifted(addr, data)
        self._burst(addr, _beats(data))
        return None

    def _burst(self, addr, beats):
        self._eval(f"jaxi::write {self.base + addr} {{{' '.join(beats)}}}")

    def _write_shifted(self, addr, data):
        """Write through a queue that pairs each address with `write_shift` beats
        of earlier data, then confirm the head landed.

        The payload's first `d` beats go out against throwaway addresses so the
        queue carries them when the real burst's addresses arrive. Verification is
        the HEAD ONLY and that is exact, not a sample: the shift is one scalar
        over the whole stream, so a head at its expected address means a shift of
        zero, and a zero shift applies to every beat of the burst.

        Raises :class:`JtagError` if the head does not land.
        """
        d = self.write_shift
        beats = _beats(data)
        self._burst(self.scratch, beats[:d])
        self._burst(addr, beats[d:] + [f"{_PROBE:016x}"] * d)
        got = self.read_block(addr, WORD_BYTES)
        if got != data[:WORD_BYTES]:
            raise JtagError(
                f"the write path shifted mid-transfer at {addr:#x}: the head "
                f"reads {int.from_bytes(got, 'little'):016x} and was written "
                f"{int.from_bytes(data[:WORD_BYTES], 'little'):016x}. Every byte "
                "of this block is suspect. Re-run verify_write_path()."
            )

    def measure_write_shift(self, scratch=None) -> int:
        """Beats by which write DATA lags the write ADDRESS, measured not assumed.

        Writes a position-derived pattern twice -- twice so every address is
        written whatever the shift, since an unwritten line on this ECC DRAM
        faults rather than reading zero -- and finds the one offset that explains
        the readback. Returns 0 for a clean path.

        Raises :class:`JtagError` if no offset under `MAX_SHIFT_BEATS` explains
        it, which is a different fault and must not be papered over.
        """
        at = self.scratch if scratch is None else scratch
        n = 2 * MAX_SHIFT_BEATS
        want = [f"{(_PROBE | (i + 1)):016x}" for i in range(n)]
        self._burst(at, want)
        self._burst(at, want)
        got = _beats(self.read_block(at, n * WORD_BYTES))
        for d in range(MAX_SHIFT_BEATS):
            if got[d:] == want[: n - d]:
                return d
        raise JtagError(
            f"the write path at {at:#x} matches no shift under "
            f"{MAX_SHIFT_BEATS} beats. Wrote {want[0]}..., read {got[0]}..., so "
            "this is not a stale write queue and the diagnosis needs redoing."
        )

    def verify_write_path(self, scratch=None, realign=False) -> int:
        """Preflight: prove a write round trip is byte-exact. Returns the shift
        being compensated for, 0 when the path is clean.

        `realign=True` permits compensating a non-zero shift and re-proving the
        round trip through it; the default REFUSES, because a shift means every
        operand and weight image is landing in the wrong place and the honest
        answer is to say so rather than to quietly correct it.

        Raises :class:`JtagError` if the path is skewed and not realigned, or if
        realignment does not converge.
        """
        if scratch is not None:
            self.scratch = scratch
        self.write_shift = 0
        d = self.measure_write_shift()
        if d == 0:
            return 0
        if not realign:
            raise JtagError(
                f"write data lags the write address by {d} beats, so every "
                "operand lands shifted and the card reports success. Reload the "
                "bitstream to flush the queue, or pass realign=True to "
                "compensate and have every result say so."
            )
        self.write_shift = d
        if not self._roundtrip_exact():
            self.write_shift = 0
            raise JtagError(
                f"compensating for {d} beats did not produce an exact round "
                "trip, so the shift is not a fixed queue depth. Reload the "
                "bitstream; do not guess."
            )
        return d

    def _roundtrip_exact(self) -> bool:
        """Write a block through the compensated path and compare every byte."""
        n = 2 * MAX_SHIFT_BEATS
        want = b"".join((_PROBE | (i + 1)).to_bytes(WORD_BYTES, "little")
                        for i in range(n))
        try:
            self.write_block(self.scratch, want)
        except JtagError:
            return False
        return self.read_block(self.scratch, len(want)) == want

    def read_block(self, addr: int, nbytes: int) -> bytes:
        require_words(nbytes)
        self._check_size(nbytes)
        n = nbytes // WORD_BYTES
        beats = self._eval(f"jaxi::read {self.base + addr} {n}").split()
        if len(beats) != n:
            raise JtagError(f"asked for {n} beats and got back {len(beats)}")
        return b"".join(int(b, 16).to_bytes(WORD_BYTES, "little") for b in beats)

    def _check_size(self, nbytes):
        if nbytes > self.max_block:
            raise ValueError(
                f"{nbytes} bytes over JTAG is about "
                f"{nbytes / JTAG_BYTES_PER_SECOND:.0f}s at the measured "
                f"{JTAG_BYTES_PER_SECOND / 1e3:.0f} kB/s. This is a control-plane "
                f"transport; use XDMA for operands, or raise max_block."
            )

    def __repr__(self):
        return f"JtagTransport(base={self.base:#x}, calls={self.calls})"
