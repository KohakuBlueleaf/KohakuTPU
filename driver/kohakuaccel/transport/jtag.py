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


def _beats(data: bytes, bb: int = WORD_BYTES) -> list:
    """`data` as little-endian hex beats, one per `bb`-byte bus beat."""
    return [
        f"{int.from_bytes(data[i : i + bb], 'little'):0{bb * 2}x}"
        for i in range(0, len(data), bb)
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
        # Off until a bitstream is shown to return consecutive words for a
        # multi-beat read; `burst_read_ok` is that proof and the board carries
        # the answer. Measured on v7: 8 KB reads 1.0 KB/s single-beat against
        # 41.1 KB/s burst, so this is the difference between a 2.7 s kernel and
        # a 0.2 s one.
        self.prefer_burst_read = False
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

    def tck_hz(self, hz: int | None = None) -> int:
        """The cable's JTAG clock. Returns the rate in effect after any change.

        THIS IS THE LINK SPEED AND NOTHING ELSE BOUNDS BULK TRANSFER once bursts
        are used: at 8 KB per exchange the round trip is amortised and what is
        left is bits on the wire. Measured 2026-08-23 at the inherited default:
        8 KB in 0.885 s, about 74 kbit/s of payload.

        Raising it is the cheapest bandwidth there is, and the ceiling is the
        cable and the board's signal integrity, not the design.
        """
        tgt = "[current_hw_target]"
        if hz is not None:
            self._eval(f"set_property PARAM.FREQUENCY {int(hz)} {tgt}")
        return int(self._eval(f"get_property PARAM.FREQUENCY {tgt}").strip())

    def program(self, bitstream: str, probes: str | None = None) -> None:
        """Reprogram the FPGA: the only recovery for a fabric-wedged port.

        Every MMCM returns to its BUILT configuration — retune the clocks
        immediately after, before touching anything else.
        """
        script = (
            f"set d [current_hw_device]; set_property PROGRAM.FILE {{{bitstream}}} $d; "
        )
        if probes:
            script += f"set_property PROBES.FILE {{{probes}}} $d; "
        script += "program_hw_devices $d; refresh_hw_device $d"
        self._eval(script)
        self._eval("jaxi::reset_cache; jaxi::connect")

    def reset_core(self) -> None:
        """Reset the JTAG-AXI master, clearing a wedged channel.

        An oversized burst leaves the port with a transaction it can never
        finish, and every later access fails with it. This is the recovery
        that does not cost a reprogram; it does not touch the design.
        """
        self._eval("reset_hw_axi [get_hw_axis]; jaxi::reset_cache")

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
                f"the JTAG-AXI master is {width * 8} bits wide; only the "
                f"64-bit manager is supported (the 32-bit manager was the "
                f"v6.5 era and is retired)"
            )
        self.beat_bytes = width

    def write32(self, addr: int, data: int) -> None:
        """One 32-bit register, by 64-bit word read-merge-write.

        Native 64-bit beats are the only access this core has: Vivado 2024.2
        ignores sub-width transactions outright. The neighbor register in the
        same word is rewritten with its just-read value, so W1C/kick-style
        registers packed 32-bit apart must be written through write64 with
        the whole word composed.
        """
        base = addr & ~0x7
        word = self.read64(base)
        if addr & 4:
            word = (word & 0xFFFFFFFF) | ((data & 0xFFFFFFFF) << 32)
        else:
            word = (word & (0xFFFFFFFF << 32)) | (data & 0xFFFFFFFF)
        self.write64(base, word)

    def read32(self, addr: int) -> int:
        """One 32-bit register, extracted from its 64-bit word."""
        word = self.read64(addr & ~0x7)
        return (word >> (32 if addr & 4 else 0)) & 0xFFFFFFFF

    def write64(self, addr: int, data: int) -> None:
        beats = _beats((data & MASK64).to_bytes(WORD_BYTES, "little"), self.beat_bytes)
        self._eval(f"jaxi::write {self.base + addr} {{{' '.join(beats)}}}")

    def read64(self, addr: int) -> int:
        bpw = WORD_BYTES // self.beat_bytes
        n = bpw + self.read_skew
        words = self._eval(f"jaxi::read {self.base + addr} {n}").split()
        sel = words[self.read_skew : self.read_skew + bpw]
        raw = b"".join(int(w, 16).to_bytes(self.beat_bytes, "little") for w in sel)
        return int.from_bytes(raw, "little")

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
        self._burst(addr, _beats(data, self.beat_bytes))
        return None

    def read_burst(self, addr: int, nbytes: int) -> bytes:
        """One multi-beat read, beats concatenated in address order.

        8x fewer JTAG transactions than `read_block`: 8 KB is 4 exchanges rather
        than 1,024 single-beat reads. Valid only where `burst_read_ok` says so.
        """
        require_words(nbytes)
        out = bytearray()
        step = max(1, self.max_block // self.beat_bytes)
        for off in range(0, nbytes, step * self.beat_bytes):
            n = min(step, (nbytes - off) // self.beat_bytes)
            at = self.base + addr + off
            for w in self._eval(f"jaxi::read {at} {n}").split():
                out += int(w, 16).to_bytes(self.beat_bytes, "little")
        return bytes(out)

    def burst_read_ok(self, addr: int, nbytes: int = 256) -> bool:
        """Whether a multi-beat read returns CONSECUTIVE words on this bitstream.

        On the pre-v7 bus it did not — beat k returned one lane of word k — which
        is why `read_block` is single-beat. That was a property of the fabric, not
        of AXI, so it must be re-measured per bitstream rather than assumed.
        """
        want = bytes((i * 37 + 5) & 0xFF for i in range(nbytes))
        self.write_block(addr, want)
        return self.read_burst(addr, nbytes) == want

    def read_block(self, addr: int, nbytes: int) -> bytes:
        """Address-exact block read: single-beat accesses, batched per round trip.

        A multi-beat read on a wide endpoint STRIDES BY THE ENDPOINT WORD per
        beat (measured: beat k returns one lane of word k), so a burst cannot
        return consecutive bytes — every access is one beat. But the SESSION
        cost is per Tcl round trip, so one eval runs a whole loop of reads and
        returns the values together. `burst_read_ok` says whether this bitstream
        still needs that; where it does not, `read_burst` is 8x fewer exchanges.
        """
        if self.prefer_burst_read:
            return self.read_burst(addr, nbytes)
        require_words(nbytes)
        out = bytearray()
        batch = 256 * self.beat_bytes
        for off in range(0, nbytes, batch):
            n = min(batch, nbytes - off) // self.beat_bytes
            script = (
                f"set o {{}}; for {{set i 0}} {{$i < {n}}} {{incr i}} {{ "
                f"lappend o [lindex [jaxi::read "
                f"[expr {{{self.base + addr + off} + $i * {self.beat_bytes}}}] 1] 0] "
                f"}}; join $o"
            )
            for w in self._eval(script).split():
                out += int(w, 16).to_bytes(self.beat_bytes, "little")
        return bytes(out)

    def _burst(self, addr: int, beats: list) -> None:
        """One write, split so no single burst exceeds `max_block`.

        Split HERE, not only in `write_block`: the calibration paths build
        their beats by hand, and a burst past the manager port's FIFO depth
        wedges it with no error until the next access times out.
        """
        step = max(1, self.max_block // self.beat_bytes)
        for off in range(0, len(beats), step):
            chunk = beats[off : off + step]
            at = self.base + addr + off * self.beat_bytes
            self._eval(f"jaxi::write {at} {{{' '.join(chunk)}}}")

    def _write_shifted(self, addr: int, data: bytes) -> None:
        """Write through a queue that pairs each address with `write_shift` beats
        of earlier data, then confirm the head landed.

        The payload's first `d` beats go out against a throwaway address so the
        queue already carries them when the real burst's addresses arrive.
        Raises :class:`JtagError` if the head does not land where it was sent.
        """
        d = self.write_shift
        beats = _beats(data, self.beat_bytes)
        filler = _beats(_PROBE.to_bytes(WORD_BYTES, "little"), self.beat_bytes)[0]
        self._burst(self.scratch, beats[:d])
        self._burst(addr, beats[d:] + [filler] * d)
        got = self.read_block(addr, WORD_BYTES)
        if got != data[:WORD_BYTES]:
            raise JtagError(
                f"the write path shifted mid-transfer at {addr:#x}: the head reads "
                f"{int.from_bytes(got, 'little'):016x}, written "
                f"{int.from_bytes(data[:WORD_BYTES], 'little'):016x}"
            )

    def calibrate_max_block(self, addr: int, top: int = 1 << 14) -> int:
        """Raise `max_block` to the largest burst this port actually completes.

        THE ROUND TRIP IS THE COST, NOT THE BYTES. One Vivado exchange is ~12 ms
        whatever it carries, so a 12 KB upload at the inherited 64-byte cap is
        192 exchanges and 2.3 s for work the silicon does in microseconds. Every
        doubling here halves the wall time of every upload the driver ever does.

        Walks up powers of two from the current setting, writing and verifying at
        each, and leaves `max_block` at the largest that round-tripped exactly.
        `addr` must be scribble-safe and granule-aligned.

        A burst past what the port can hold does not fail — IT WEDGES THE LINK,
        and only a reprogram clears it. So this prints each size BEFORE trying it:
        if the session dies, the last line named the size that did it.
        """
        best = self.max_block
        size = best
        while size <= top:
            print(f"calibrate_max_block: trying {size} B", flush=True)
            want = bytes((i * 31 + size) & 0xFF for i in range(size))
            self.max_block = size
            self.write_block(addr, want)
            if self.read_block(addr, size) != want:
                break
            best = size
            size *= 2
        self.max_block = best
        return best

    def measure_write_shift(self, scratch: int | None = None) -> int:
        """Beats by which write DATA lags the write ADDRESS. 0 is a clean path.

        Writes a position-derived pattern twice -- twice so every address is
        written whatever the shift, since an unwritten line on this ECC DRAM
        faults rather than reading zero. Raises :class:`JtagError` if no offset
        under `MAX_SHIFT_BEATS` explains the readback, which is a different fault.
        """
        at = self.scratch if scratch is None else scratch
        n = 2 * MAX_SHIFT_BEATS
        patt = b"".join(
            (_PROBE | (i + 1)).to_bytes(WORD_BYTES, "little") for i in range(n)
        )
        want = _beats(patt, self.beat_bytes)
        self._burst(at, want)
        self._burst(at, want)
        got = _beats(self.read_block(at, n * WORD_BYTES), self.beat_bytes)
        for d in range(MAX_SHIFT_BEATS):
            if got[d:] == want[: len(want) - d]:
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
