"""A 64-bit AXI window driven over JTAG, through Vivado's Tcl console.

One host's answer to "where is the card", so it is not imported by the package
:mod:`kohakuaccel.transport` -- a driver that imports every backend makes every
caller depend on every one.

The session must already have sourced the ``jaxi`` helpers, or `tcl` must name
the file. Vivado is spoken to over a socket: a length line, then the script; back
comes ``code n_result n_log`` and the two payloads.
"""

import os
import socket

from kohakuaccel.transport.base import (
    MASK64,
    WORD_BYTES,
    Transport,
    TransportUnavailable,
    require_words,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5570

#: A soft ceiling on one transfer, not a hardware limit. Raise it deliberately
#: for a bulk probe; it exists so an accidental huge read is not silently slow.
MAX_BLOCK = 1 << 18

#: How far the write queue is searched for a lag before calling it a fault.
MAX_SHIFT_BEATS = 8

#: A beat no operand produces, so a stale one is recognisable in a readback.
_PROBE = 0xC0FFEE5EED000000


def _beats(data: bytes) -> list:
    """`data` as 16-hex-digit little-endian beats, one per 64-bit word."""
    return [
        f"{int.from_bytes(data[i : i + WORD_BYTES], 'little'):016x}"
        for i in range(0, len(data), WORD_BYTES)
    ]


class JtagError(RuntimeError):
    """Vivado answered, and what it said was an error."""


def exchange(sock, script: str) -> str:
    """One request and its reply on an already-open connection.

    Raises :class:`JtagError` when the script itself failed. Tcl code 2 is a
    bare ``return`` at script level, which is not a failure.
    """
    payload = script.encode("utf-8")
    sock.sendall(str(len(payload)).encode("ascii") + b"\n" + payload)
    code, n_res, n_log = (int(v) for v in _line(sock).split())
    result = _exact(sock, n_res).decode("utf-8", "replace")
    _exact(sock, n_log)
    if code == 1:
        raise JtagError(result.strip())
    return result


def connect(host=DEFAULT_HOST, port=DEFAULT_PORT, timeout=600.0):
    """A connection to the Tcl server, or :class:`TransportUnavailable`."""
    try:
        sock = socket.create_connection((host, port), timeout=10)
    except OSError as exc:
        raise TransportUnavailable(
            f"no Vivado Tcl server on {host}:{port} ({exc})"
        ) from exc
    sock.settimeout(timeout)
    return sock


def evaluate(script, host=DEFAULT_HOST, port=DEFAULT_PORT, timeout=600.0) -> str:
    """Run Tcl in the live Vivado session on a connection of its own.

    ONE-SHOT. A driver doing thousands of accesses must not use this: a socket
    per access leaves that many in TIME_WAIT, and Windows runs out of ephemeral
    ports after a few minutes of real work. :class:`JtagTransport` keeps one
    connection open instead.
    """
    sock = connect(host, port, timeout)
    try:
        return exchange(sock, script)
    except OSError as exc:
        raise TransportUnavailable(f"lost the Tcl server ({exc})") from exc
    finally:
        sock.close()


def _line(sock) -> str:
    buf = bytearray()
    while not buf.endswith(b"\n"):
        chunk = sock.recv(1)
        if not chunk:
            raise OSError("connection closed while reading the header")
        buf += chunk
    return buf.decode("ascii").strip()


def _exact(sock, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise OSError(f"connection closed with {n - len(buf)} bytes outstanding")
        buf += chunk
    return bytes(buf)


class JtagTransport(Transport):
    """The card's AXI window, reached through Vivado.

    `base` is added to every address so the same driver code runs against this
    and against a PCIe backend without either carrying a board constant.
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
        scratch: int = 0x1F00_0000,
    ) -> None:
        self.base = base
        self.host, self.port = host, port
        self.timeout = timeout
        self.max_block = max_block
        self.tcl = tcl if tcl is not None else os.environ.get("KTPU_JAXI_TCL")
        self.calls = 0
        #: Beats returned ahead of a read. Measured against a known register, so
        #: it is never contaminated by a write lag. See `measure_read_skew`.
        self.read_skew = 0
        # Only `verify_write_path` may make this non-zero: nothing silently
        # rewrites data on its way to the card.
        self.write_shift = 0
        self.scratch = scratch
        self._sock = None
        self._connect()

    def _eval(self, script: str) -> str:
        """One Tcl call on the transport's OWN connection, reopened if dropped.

        Held open rather than dialled per access: a socket per access leaves
        that many in TIME_WAIT, and a run of any size then dies with
        `WinError 10048` -- ports exhausted, which reads as the server being
        gone. Reconnecting once covers a server that restarted between calls.
        """
        self.calls += 1
        for retry in (False, True):
            if self._sock is None:
                self._sock = connect(self.host, self.port, self.timeout)
            try:
                return exchange(self._sock, script)
            except OSError as exc:
                self.close()
                if retry:
                    raise TransportUnavailable(f"lost the Tcl server ({exc})") from exc
        raise AssertionError("unreachable")

    def close(self) -> None:
        """Drop the connection. The next call opens a fresh one."""
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:  # noqa: BLE001, S110 -- a finaliser must not raise
            pass

    def _connect(self) -> None:
        """Source the helpers if asked to, then check the master's width.

        The width check is not ceremony: a 32-bit master would make every beat
        half a word, and the driver would write a coherent-looking program into
        the wrong half of every register.
        """
        if self.tcl:
            self._eval(f"if {{![namespace exists jaxi]}} {{ source {{{self.tcl}}} }}")
        if self._eval("namespace exists jaxi").strip() != "1":
            raise TransportUnavailable(
                "jaxi is not sourced in this Vivado session; pass tcl= or set "
                "KTPU_JAXI_TCL"
            )
        width = int(self._eval("set ::jaxi::bytes_per_beat").strip())
        if width != WORD_BYTES:
            raise TransportUnavailable(
                f"the JTAG-AXI master is {width * 8} bits wide; this driver "
                f"addresses {WORD_BYTES * 8}-bit words"
            )

    def write64(self, addr: int, data: int) -> None:
        self._eval(f"jaxi::write {self.base + addr} {{{data & MASK64:016x}}}")

    def read64(self, addr: int) -> int:
        n = 1 + self.read_skew
        words = self._eval(f"jaxi::read {self.base + addr} {n}").split()
        return int(words[self.read_skew], 16)

    def measure_read_skew(self, addr: int, accept, window: int = MAX_SHIFT_BEATS):
        """Beats the master returns ahead of a read, from KNOWN contents.

        `accept` decides whether a word is the expected one. Nothing is written,
        which is the whole point: a single-beat write pairs its data with the
        previous address, so using one as the reference reports a write lag as a
        read skew. Returns the offset, or None if no word in `window` is accepted.
        """
        raw = self._eval(f"jaxi::read {self.base + addr} {window + 1}").split()
        for skew, w in enumerate(raw):
            if accept(int(w, 16)):
                return skew
        return None

    def write_block(self, addr: int, data: bytes) -> None:
        require_words(len(data))
        if self.write_shift:
            return self._write_shifted(addr, data)
        for off in range(0, len(data), self.max_block):
            self._burst(addr + off, _beats(data[off : off + self.max_block]))
        return None

    def read_block(self, addr: int, nbytes: int) -> bytes:
        require_words(nbytes)
        out = bytearray()
        skew = self.read_skew
        for off in range(0, nbytes, self.max_block):
            n = min(self.max_block, nbytes - off) // WORD_BYTES
            words = self._eval(
                f"jaxi::read {self.base + addr + off} {n + skew}"
            ).split()
            for w in words[skew : skew + n]:
                out += int(w, 16).to_bytes(WORD_BYTES, "little")
        return bytes(out)

    def _burst(self, addr: int, beats: list) -> None:
        self._eval(f"jaxi::write {self.base + addr} {{{' '.join(beats)}}}")

    def _write_shifted(self, addr: int, data: bytes) -> None:
        """Write through a queue that pairs each address with `write_shift` beats
        of earlier data, then confirm the head landed.

        The payload's first `d` beats go out against a throwaway address so the
        queue already carries them when the real burst's addresses arrive.
        Raises :class:`JtagError` if the head does not land where it was sent.
        """
        d = self.write_shift
        beats = _beats(data)
        self._burst(self.scratch, beats[:d])
        self._burst(addr, beats[d:] + [f"{_PROBE:016x}"] * d)
        got = self.read_block(addr, WORD_BYTES)
        if got != data[:WORD_BYTES]:
            raise JtagError(
                f"the write path shifted mid-transfer at {addr:#x}: the head reads "
                f"{int.from_bytes(got, 'little'):016x}, written "
                f"{int.from_bytes(data[:WORD_BYTES], 'little'):016x}"
            )

    def measure_write_shift(self, scratch: int | None = None) -> int:
        """Beats by which write DATA lags the write ADDRESS. 0 is a clean path.

        Writes a position-derived pattern twice -- twice so every address is
        written whatever the shift, since an unwritten line on this ECC DRAM
        faults rather than reading zero. Raises :class:`JtagError` if no offset
        under `MAX_SHIFT_BEATS` explains the readback, which is a different fault.
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
            f"the write path at {at:#x} matches no shift under {MAX_SHIFT_BEATS} "
            f"beats. Wrote {want[0]}..., read {got[0]}..., so this is not a stale "
            f"write queue and the diagnosis needs redoing."
        )

    def verify_write_path(self, scratch: int | None = None, realign=False) -> int:
        """Prove a write round trip is byte-exact. Returns the shift compensated.

        `realign=True` permits compensating a non-zero shift; the default REFUSES,
        because a shift means every operand is landing in the wrong place on the
        card while host round trips still look clean. Raises :class:`JtagError`
        if the path is skewed and not realigned.
        """
        if scratch is not None:
            self.scratch = scratch
        self.write_shift = 0
        d = self.measure_write_shift()
        if d == 0 or realign:
            self.write_shift = d
            return d
        raise JtagError(
            f"write data lags the write address by {d} beats, so every operand "
            f"lands shifted and the card reports success. Reload the bitstream to "
            f"flush the queue, or pass realign=True to compensate."
        )

    def __repr__(self) -> str:
        return f"JtagTransport(base={self.base:#x}, calls={self.calls})"
