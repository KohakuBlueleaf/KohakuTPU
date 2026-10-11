"""A reference interpreter for packages: the firmware's semantics in Python.

`firmware/kohakuaccel/package/interp.c` is the implementation that runs on a
node; this is the same step semantics over any `Mailbox`, so a package can be
checked end to end against unit models with no simulator. Where the two could
differ -- credit, acknowledgements, relocation -- this follows the C.

A Mailbox sends one payload and reports completions as they arrive:

    send(x, y, payload) -> None
    drain() -> list[(x, y, code, arg)]
"""

from dataclasses import dataclass, field
from typing import Protocol

from kohakuaccel.package import engine
from kohakuaccel.package.format import (
    F_POSTED,
    MOVER_SKIP,
    Op,
    Package,
    PackageError,
    apply_reloc,
    bind_addresses,
    signature,
)

MASK32, MASK64 = (1 << 32) - 1, (1 << 64) - 1

SIG_DATA_RECEIVED = 0x03
SIG_FAULT = 0x04

#: Status codes, as firmware/kohakuaccel/include/ka/package/status.h.
OK, BAD_SIGNATURE, UNIT_FAULT, TIMEOUT, BAD_STEP = 0x00, 0x12, 0x20, 0x30, 0x14


class Mailbox(Protocol):
    def send(self, x: int, y: int, payload: int) -> None: ...

    def drain(self) -> list[tuple[int, int, int, int]]: ...


@dataclass
class Result:
    status: int = OK
    detail: int = 0
    step: int = 0
    value: int = 0
    sent: int = 0
    signals: list = field(default_factory=list)


class Interpreter:
    """Runs packages over `mailbox`; `units` is the machine's unit words (the
    boot table) for the signature check, `cq_depth` the mailbox's depth."""

    def __init__(
        self, mailbox: Mailbox, units=(), cq_depth: int = 16, mover=None, memory=None
    ):
        self.mb = mailbox
        self.machine_sig = signature(units) if units else 0
        self.cq_depth = cq_depth
        #: Called with a MOVER step's (register, value) writes; None refuses them.
        self.mover = mover
        #: ``memory(addr, nbytes) -> bytes`` for FETCH; None refuses them.
        self.memory = memory

    def fetched(self, addr: int, n: int) -> list[int]:
        if self.memory is None:
            raise PackageError("this interpreter was given no memory to fetch from")
        raw = self.memory(addr, n * engine.PAYLOAD)
        return [
            int.from_bytes(raw[k : k + engine.PAYLOAD], "little")
            for k in range(0, len(raw), engine.PAYLOAD)
        ]

    def run(
        self, pkg: Package | bytes, bindings=None, max_polls: int = 1_000_000
    ) -> Result:
        if isinstance(pkg, (bytes, bytearray)):
            pkg = Package.from_bytes(bytes(pkg))
        res = Result()
        if pkg.signature and self.machine_sig and pkg.signature != self.machine_sig:
            res.status, res.value = BAD_SIGNATURE, self.machine_sig
            return res
        addrs = bind_addresses(pkg.buffers, bindings)
        words = list(pkg.payloads)
        for r in pkg.relocs:
            words[r.payload] = apply_reloc(words[r.payload], r, addrs[r.buffer])
        cap = max(1, self.cq_depth - pkg.ack_reserve)
        credit = [max(1, min(u.credit or cap, cap)) for u in pkg.units]
        inflight = [0] * len(pkg.units)
        expected = [0] * len(pkg.units)
        received = [0] * len(pkg.units)
        retired = [0] * len(pkg.units)
        where = {(u.x, u.y): i for i, u in enumerate(pkg.units)}

        def drain() -> None:
            for x, y, code, arg in self.mb.drain():
                i = where.get((x, y))
                if i is None:
                    continue
                received[i] += 1
                if code in (0x00, 0x01):
                    retired[i] += 1
                if code != SIG_DATA_RECEIVED and inflight[i]:
                    inflight[i] -= 1
                if code == SIG_FAULT and not res.status:
                    res.status, res.detail, res.value = UNIT_FAULT, i, arg

        def wait(done) -> bool:
            for _ in range(max_polls):
                drain()
                if res.status:
                    return False
                if done():
                    return True
            res.status = TIMEOUT
            return False

        # Posted moves run here only when the package waits for them -- an
        # MWAIT reaching them, the next MOVER (the walkers are not queued), a
        # barrier or the end -- so a package that reads a move's result
        # without waiting for it reads what was there before.
        posted: list = []  # [(gos, writes)]
        moves_done = 0

        def run_moves(upto=None) -> None:
            nonlocal moves_done
            while posted and (upto is None or moves_done < upto):
                gos, writes = posted.pop(0)
                self.mover(writes)
                moves_done += gos

        for n, s in enumerate(pkg.steps):
            res.step = n
            if s.op == Op.END:
                break
            if s.op in (Op.DISPATCH, Op.FETCH):
                u = pkg.units[s.unit]
                got = (
                    self.fetched(s.arg, s.count)
                    if s.op == Op.FETCH
                    else words[s.arg : s.arg + s.count]
                )
                for w in got:
                    if not wait(
                        lambda i=s.unit: inflight[i] < credit[i] and sum(inflight) < cap
                    ):
                        return res
                    self.mb.send(u.x, u.y, w)
                    inflight[s.unit] += 1
                    res.sent += 1
            elif s.op == Op.REPEAT:
                u = pkg.units[s.unit]
                first, repeats, nincs = (
                    s.arg & MASK32,
                    (s.arg >> 32) & 0xFFFF,
                    s.arg >> 48,
                )
                incs = []
                for k in range(nincs):
                    w = words[first + s.count + k // 2] >> (128 * (k % 2))
                    head, delta = w & MASK64, (w >> 64) & MASK64
                    incs.append(
                        (head & MASK32, (head >> 32) & 0xFF, (head >> 40) & 0xFF, delta)
                    )
                for r in range(repeats):
                    for j in range(s.count):
                        w = words[first + j]
                        for index, bit, width, delta in incs:
                            if index == j:
                                mask = (1 << width) - 1
                                field = (((w >> bit) & mask) + r * delta) & mask
                                w = (w & ~(mask << bit)) | field << bit
                        if not wait(
                            lambda i=s.unit: inflight[i] < credit[i]
                            and sum(inflight) < cap
                        ):
                            return res
                        self.mb.send(u.x, u.y, w)
                        inflight[s.unit] += 1
                        res.sent += 1
            elif s.op == Op.AWAIT:
                expected[s.unit] += s.count
                if not wait(lambda i=s.unit: received[i] >= expected[i]):
                    return res
            elif s.op == Op.BARRIER:
                if not wait(
                    lambda: not any(inflight)
                    and all(r >= e for r, e in zip(received, expected, strict=True))
                ):
                    return res
                run_moves()
            elif s.op == Op.MWAIT:
                run_moves(s.count)
            elif s.op == Op.MOVER:
                writes = []
                for k in range(s.count):
                    w = words[s.arg + k]
                    for half in (0, 128):
                        reg, val = (w >> half) & ((1 << 64) - 1), (w >> (half + 64)) & (
                            (1 << 64) - 1
                        )
                        if reg != MOVER_SKIP:
                            writes.append((reg, val))
                if self.mover is None:
                    raise PackageError("this interpreter was given no mover")
                run_moves()
                gos = sum(1 for r, v in writes if r == 0 and v >> 16 & 1)
                if s.flags & F_POSTED:
                    posted.append((gos, writes))
                else:
                    self.mover(writes)
                    moves_done += gos
            elif s.op == Op.ENGINE:
                regs: dict = {}
                rel = False
                moving: list = []
                for code, value in engine.entries(pkg, s):
                    reg = code & 0x3F
                    if reg == engine.ARG3:
                        rel = bool(code & engine.REL)
                    if reg == engine.WAIT:
                        src, want = value >> 56, value & engine.CTR_MASK
                        if src == engine.SRC_MOVER:
                            continue  # moves run at their GO, below
                        if not wait(
                            lambda i=src, w=want: ((retired[i] - w) & engine.CTR_MASK)
                            < (engine.CTR_MASK + 1) // 2
                        ):
                            return res
                    elif reg < 8:
                        regs[reg] = value
                        if reg == engine.GO:
                            dst = regs.get(engine.DST, 0)
                            if dst & engine.TYPED_MEM_RD_REQ:
                                arg2, arg3 = regs[engine.ARG2], regs[engine.ARG3]
                                yx, n = (arg2 >> 40) & 0xFF, arg3 & 0xFF
                                if rel:
                                    first = (arg3 >> 24) // engine.PAYLOAD
                                    got = words[first : first + n]
                                else:
                                    got = self.fetched(arg3 >> 24, n)
                                for w in got:
                                    self.mb.send(yx & 0xF, yx >> 4, w)
                                    res.sent += 1
                            else:
                                w = sum(
                                    regs.get(engine.ARG0 + j, 0) << (64 * j)
                                    for j in range(4)
                                )
                                self.mb.send(dst & 0xF, (dst >> 8) & 0xF, w)
                                res.sent += 1
                    elif reg < 24:
                        moving.append(((reg - 8) * 8, value))
                        if reg == 8 and value >> 16 & 1:
                            if self.mover is None:
                                raise PackageError(
                                    "this interpreter was given no mover"
                                )
                            self.mover(moving)
                            moves_done += 1
                            moving = []
            elif s.op == Op.SIGNAL:
                res.signals.append((n, s.count, s.arg))
            elif s.op in (Op.SETTLE, Op.RING, Op.WAIT_BELL):
                continue
            else:
                res.status, res.detail = BAD_STEP, int(s.op)
                return res
            if res.status:
                return res
        if wait(lambda: not any(inflight)):
            res.step = len(pkg.steps)
        run_moves()
        return res


class LocalNode:
    """A node queue's `run`, answered in-process by an :class:`Interpreter`.

    What `Runtime.node` takes, for a machine of unit models: the same packages
    a card's node runs, checked with nothing attached. Keeps every package it
    ran, so a caller can see what reuse found.
    """

    def __init__(
        self, mailbox: Mailbox, units=(), cq_depth: int = 16, mover=None, memory=None
    ) -> None:
        self.interp = Interpreter(mailbox, units, cq_depth, mover, memory)
        self.ran: list[tuple[bytes, list]] = []

    def run(self, package: bytes, bindings=None) -> Result:
        """Raises :class:`PackageError` naming the status of a failed package."""
        self.ran.append((package, list(bindings or [])))
        res = self.interp.run(package, bindings)
        if res.status:
            raise PackageError(
                f"package failed: status {res.status:#x} detail {res.detail} "
                f"at step {res.step}, value {res.value:#x}"
            )
        return res
