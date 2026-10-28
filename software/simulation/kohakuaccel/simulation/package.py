"""A reference interpreter for packages: the firmware's semantics in Python.

`software/firmware/kohakuaccel/package/interp.c` is the implementation that
runs on a node; this is the same step semantics over any `Mailbox`, so a
package can be checked end to end against unit models with no simulator. Where
the two could differ -- credit, acknowledgements, relocation -- this follows
the C.

A Mailbox sends one payload and reports completions as they arrive:

    send(x, y, payload) -> None
    drain() -> list[(x, y, code, arg)]
"""

from dataclasses import dataclass, field
from enum import IntEnum
from types import SimpleNamespace
from typing import Protocol

PackageError = ValueError


class Reader(Protocol):
    """Package tables, format codes and unit signatures supplied by the caller."""

    format: dict

    def read(self, raw: bytes, bindings=None) -> dict: ...

    def signature(self, units) -> int: ...


@dataclass
class Step:
    op: int
    unit: int
    count: int
    arg: int
    flags: int = 0


MASK32, MASK64 = (1 << 32) - 1, (1 << 64) - 1

SIG_DATA_RECEIVED = 0x03
SIG_FAULT = 0x04

#: Status codes, as software/firmware/kohakuaccel/include/ka/package/status.h.
OK, BAD_SIGNATURE, UNIT_FAULT, TIMEOUT, BAD_STEP = 0x00, 0x12, 0x20, 0x30, 0x14
BAD_UNIT = 0x17


def _step_word(s: Step) -> int:
    """A step as its 128 stored bits."""
    w0 = (
        int(s.op)
        | (s.flags & 0xFF) << 8
        | (s.unit & 0xFFFF) << 16
        | (s.count & MASK32) << 32
    )
    return w0 | (s.arg & MASK64) << 64


def _word_step(w: int, Op) -> Step:
    w0 = w & MASK64
    return Step(Op(w0 & 0xFF), (w0 >> 16) & 0xFFFF, w0 >> 32, w >> 64, (w0 >> 8) & 0xFF)


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
    #: The package's registers at its end.
    regs: list = field(default_factory=list)
    #: ``(step, op, unit, count, arg)`` of every interlink / control step, in
    #: the order the node reached them.
    events: list = field(default_factory=list)
    #: ``(x, y, type, payload)`` of every SEND flit.
    flits: list = field(default_factory=list)


def _alu(op: int, a: int, b: int, Alu) -> int:
    m = (1 << 64) - 1
    return {
        Alu.MOV: b,
        Alu.ADD: a + b,
        Alu.SUB: a - b,
        Alu.MUL: a * b,
        Alu.AND: a & b,
        Alu.OR: a | b,
        Alu.XOR: a ^ b,
        Alu.SHL: a << (b & 63),
        Alu.SHR: a >> (b & 63),
        Alu.MIN: min(a, b),
        Alu.MAX: max(a, b),
        Alu.DIVU: a // b if b else m,
        Alu.REMU: a % b if b else a,
    }[Alu(op)] & m


def _taken(cond: int, a: int, b: int, Cond) -> bool:
    sa, sb = (a ^ 1 << 63) - (1 << 63), (b ^ 1 << 63) - (1 << 63)
    return {
        Cond.ALWAYS: True,
        Cond.EQ: a == b,
        Cond.NE: a != b,
        Cond.LTU: a < b,
        Cond.GEU: a >= b,
        Cond.LT: sa < sb,
        Cond.GE: sa >= sb,
    }[Cond(cond)]


class Interpreter:
    """Runs packages over `mailbox`; `units` is the machine's unit words (the
    boot table) for the signature check, `cq_depth` the mailbox's depth.

    `store(addr, value)` takes a STORE's 64-bit writes and `ctrl(offset)` /
    `rx()` answer a control-register LOAD and a RECV; each None refuses its
    step."""

    def __init__(
        self,
        mailbox: Mailbox,
        units=(),
        cq_depth: int = 16,
        mover=None,
        memory=None,
        store=None,
        ctrl=None,
        rx=None,
        reader=None,
    ):
        self.mb = mailbox
        if hasattr(units, "read"):
            reader, units = units, ()
        if reader is None:
            raise ValueError("a machine-bound package reader is required")
        self.reader = reader
        self.machine_sig = reader.signature(units) if units else 0
        self.cq_depth = cq_depth
        #: Called with a MOVER step's (register, value) writes; None refuses them.
        self.mover = mover
        #: ``memory(addr, nbytes) -> bytes`` for FETCH; None refuses them.
        self.memory = memory
        self.store = store
        self.ctrl = ctrl
        self.rx = rx

    def fetched_bytes(self, addr: int, nbytes: int) -> bytes:
        if self.memory is None:
            raise PackageError("this interpreter was given no memory to read")
        return self.memory(addr, nbytes)

    def fetched(self, addr: int, n: int) -> list[int]:
        raw = self.fetched_bytes(addr, n * self.reader.format["PAYLOAD"])
        return [
            int.from_bytes(raw[k : k + self.reader.format["PAYLOAD"]], "little")
            for k in range(0, len(raw), self.reader.format["PAYLOAD"])
        ]

    def run(self, pkg: bytes, bindings=None, max_polls: int = 1_000_000) -> Result:
        raw = bytes(pkg) if isinstance(pkg, (bytes, bytearray)) else pkg.to_bytes()
        tables = self.reader.read(raw, bindings)
        f = self.reader.format
        Op, Alu, Cond = (IntEnum(k, f[k]) for k in ("op", "alu", "cond"))
        engine = SimpleNamespace(**f)
        ANY_UNIT, MOVER_SKIP, REGS = (f[k] for k in ("ANY_UNIT", "MOVER_SKIP", "REGS"))
        F_CTRL, F_IMM, F_INDEXED, F_MEM, F_POSTED, F_REG = (
            f[k] for k in ("F_CTRL", "F_IMM", "F_INDEXED", "F_MEM", "F_POSTED", "F_REG")
        )
        pkg = SimpleNamespace(
            signature=tables["signature"],
            ack_reserve=tables["ack_reserve"],
            units=[SimpleNamespace(**u) for u in tables["units"]],
            steps=[
                Step(Op(op), unit, count, arg, flags)
                for op, flags, unit, count, arg in tables["steps"]
            ],
        )
        res = Result(regs=[0] * REGS)
        if pkg.signature and self.machine_sig and pkg.signature != self.machine_sig:
            res.status, res.value = BAD_SIGNATURE, self.machine_sig
            return res
        words = list(tables.get("bound", tables["payloads"]))
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

        steps = list(pkg.steps)
        regs = res.regs
        pc = 0
        while pc < len(steps):
            n, s = pc, steps[pc]
            pc += 1
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
                engine_regs: dict = {}
                rel = False
                moving: list = []
                for code, value in dict(tables["engines"])[n]:
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
                        engine_regs[reg] = value
                        if reg == engine.GO:
                            dst = engine_regs.get(engine.DST, 0)
                            if dst & engine.TYPED_MEM_RD_REQ:
                                arg2, arg3 = (
                                    engine_regs[engine.ARG2],
                                    engine_regs[engine.ARG3],
                                )
                                yx, count = (arg2 >> 40) & 0xFF, arg3 & 0xFF
                                if rel:
                                    first = (arg3 >> 24) // engine.PAYLOAD
                                    got = words[first : first + count]
                                else:
                                    got = self.fetched(arg3 >> 24, count)
                                for w in got:
                                    self.mb.send(yx & 0xF, yx >> 4, w)
                                    res.sent += 1
                            else:
                                w = sum(
                                    engine_regs.get(engine.ARG0 + j, 0) << (64 * j)
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
                    else:
                        res.events.append((n, "il", 0xC0 + 8 * (reg - 24), 0, value))
            elif s.op == Op.SIGNAL:
                res.signals.append((n, s.count, s.arg))
            elif s.op in (Op.SETTLE, Op.RING, Op.WAIT_BELL, Op.WAIT_SLOT):
                res.events.append((n, s.op.name, s.unit, s.count, s.arg))
            elif s.op in (Op.STORE, Op.CTRL):
                for k in range(s.count):
                    w = words[(s.arg & MASK32) + k]
                    for half in (0, 128):
                        at, v = (w >> half) & MASK64, (w >> (half + 64)) & MASK64
                        if at == MOVER_SKIP:
                            continue
                        v = regs[v & (REGS - 1)] if s.flags & F_REG else v
                        if s.op == Op.CTRL:
                            res.events.append((n, "CTRL", at, 0, v))
                        elif self.store is None:
                            raise PackageError("this interpreter was given no store")
                        else:
                            self.store(at, v)
            elif s.op == Op.LOAD:
                at = words[s.arg & MASK32] & MASK64
                if s.flags & F_INDEXED:
                    at = (at + regs[s.count & (REGS - 1)]) & MASK64
                if s.flags & F_CTRL:
                    if self.ctrl is None:
                        raise PackageError("this interpreter reads no control register")
                    regs[s.unit] = self.ctrl(at) & MASK64
                else:
                    regs[s.unit] = int.from_bytes(self.fetched_bytes(at, 8), "little")
            elif s.op == Op.ALU:
                b = s.arg if s.flags & F_IMM else regs[(s.count >> 12) & 15]
                regs[s.unit] = _alu(s.count & 0xFF, regs[(s.count >> 8) & 15], b, Alu)
            elif s.op == Op.JUMP:
                b = s.arg if s.flags & F_IMM else regs[(s.count >> 12) & 15]
                if _taken(s.count & 0xFF, regs[(s.count >> 8) & 15], b, Cond):
                    pc = s.unit
            elif s.op == Op.PATCH:
                bit, width = s.arg & 0xFF, (s.arg >> 8) & 0xFF
                shift, on_steps, add = (
                    (s.arg >> 16) & 0xFF,
                    s.arg >> 24 & 1,
                    s.arg >> 26 & 1,
                )
                mask = (1 << width) - 1
                old = _step_word(steps[s.count]) if on_steps else words[s.count]
                v = regs[s.unit] >> shift
                if add:
                    v += (old >> bit) & mask
                new = (old & ~(mask << bit)) | (v & mask) << bit
                if on_steps:
                    steps[s.count] = _word_step(new, Op)
                else:
                    words[s.count] = new
            elif s.op == Op.SEND:
                u, first = pkg.units[s.unit], s.arg & MASK32
                kind = (s.arg >> 32) & 0xF
                got = words[first : first + (1 if s.flags & F_MEM else s.count)]
                if s.flags & F_MEM:
                    got += self.fetched(words[first + 1] & MASK64, s.count)
                res.flits += [(u.x, u.y, kind, w) for w in got]
            elif s.op == Op.RECV:
                if self.rx is None or self.store is None:
                    raise PackageError("this interpreter was given no RX queue")
                to = words[s.arg & MASK32] & MASK64
                for k in range(s.count):
                    x, y, payload = self.rx()
                    if s.unit != ANY_UNIT:
                        want = pkg.units[s.unit]
                        if (x, y) != (want.x, want.y):
                            res.status, res.detail = BAD_UNIT, s.unit
                            return res
                    for j in range(4):
                        self.store(to + 32 * k + 8 * j, (payload >> (64 * j)) & MASK64)
            else:
                res.status, res.detail = BAD_STEP, int(s.op)
                return res
            if res.status:
                return res
        if wait(lambda: not any(inflight)):
            res.step = pc
        run_moves()
        return res


class LocalNode:
    """A node queue's `run`, answered in-process by an :class:`Interpreter`.

    What `Runtime.node` takes, for a machine of unit models: the same packages
    a card's node runs, checked with nothing attached. Keeps every package it
    ran, so a caller can see what reuse found.
    """

    def __init__(
        self,
        mailbox: Mailbox,
        units=(),
        cq_depth: int = 16,
        mover=None,
        memory=None,
        *,
        reader: Reader,
    ) -> None:
        self.interp = Interpreter(
            mailbox, units, cq_depth, mover, memory, reader=reader
        )
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
