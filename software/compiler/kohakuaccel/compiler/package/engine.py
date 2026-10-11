"""Packages lowered for a node's dispatch engine (docs/spec/dispatch-engine.md).

Every step the engine can carry becomes the control-register writes and WAITs
it stands for, packed into ENGINE steps: the node copies those entries into the
engine without decoding them, and the engine issues them at one a cycle while
counting each unit's completions itself. Steps the engine cannot carry
(WAIT_BELL, SIGNAL, SETTLE) stay steps; the node lets the engine drain first.

An ENGINE step's payloads hold three entries each: word 0 is their codes, one
byte each (0xFF for none), words 1..3 their values. A code is the engine's
(0..31 a register, 32 a WAIT) plus REL (0x40): the value is a fetch request's
ARG3 and the node adds the package's payload address, shifted 24, to it.
"""

from kohakuaccel.compiler.package.format import (
    F_POSTED,
    MOVER_SKIP,
    Op,
    Package,
    PackageError,
    Step,
)

WAIT = 32
REL = 0x40
NONE = 0xFF
#: Mailbox registers DST, ARG0..ARG3, GO.
DST, ARG0, ARG1, ARG2, ARG3, GO = range(6)
#: A WAIT's source past the unit counters: moves done since the package began.
SRC_MOVER = 16
UNITS = 16
CTR_MASK = 0xFFF
#: A unit's instruction queue, the most one fetch run may put in flight.
FETCH_DEPTH = 512
#: The interlink's doorbell register, from the control region's 0xC0 window.
IL_RING = 0x10
TYPED_MEM_RD_REQ = 1 << 24
PAYLOAD = 32


def mover_code(reg: int) -> int:
    return 8 + (reg >> 3)


def il_code(reg: int) -> int:
    return 24 + (reg >> 3)


def wait_value(src: int, want: int) -> int:
    return src << 56 | (want & CTR_MASK)


class _Stream:
    """The entries of one package, cut into ENGINE steps."""

    def __init__(self, pkg: Package) -> None:
        self.pkg = pkg
        self.payloads = list(pkg.payloads)
        self.steps: list[Step] = []
        self.entries: list[tuple[int, int]] = []
        self.sent = [0] * len(pkg.units)
        self.exp = [0] * len(pkg.units)
        self.gos = 0
        self.mb: dict = {}

    def put(self, code: int, value: int) -> None:
        self.entries.append((code, value & ((1 << 64) - 1)))

    def wait(self, src: int, want: int) -> None:
        if want > 0:
            self.put(WAIT, wait_value(src, want))

    def mailbox(self, reg: int, value: int) -> None:
        if self.mb.get(reg) != value:
            self.mb[reg] = value
            self.put(reg, value)

    def cut(self) -> None:
        """The entries so far as one ENGINE step."""
        if not self.entries:
            return
        first = len(self.payloads)
        for k in range(0, len(self.entries), 3):
            group = self.entries[k : k + 3]
            word = 0
            for j in range(3):
                code, value = group[j] if j < len(group) else (NONE, 0)
                word |= code << (8 * j) | value << (64 * (j + 1))
            self.payloads.append(word)
        self.steps.append(Step(Op.ENGINE, 0, len(self.entries), first))
        self.entries = []
        self.mb = {}

    # ------------------------------------------------------------- sends
    def credit(self, u: int) -> int:
        return self.pkg.units[u].credit or (1 << 31)

    def fetch(self, u: int, at: int, count: int, rel: int) -> None:
        """`count` words from `at` by `u`'s fetch port: a payload offset under
        `rel` REL, an absolute address under 0."""
        unit = self.pkg.units[u]
        room = min(self.credit(u), FETCH_DEPTH)
        most = max(1, min(room, 255))
        while count:
            n = min(count, most)
            self.wait(u, self.sent[u] + n - room)
            self.mailbox(DST, TYPED_MEM_RD_REQ | (unit.fetch & 0xFFFF))
            self.mailbox(ARG0, 0)
            self.mailbox(ARG1, 0)
            self.mailbox(ARG2, ((unit.y << 4) | unit.x) << 40 | 1 << 30)
            self.mb.pop(ARG3, None)
            self.put(ARG3 | rel, at << 24 | 0x60 << 8 | n)
            self.put(GO, 1)
            self.sent[u] += n
            at += n * PAYLOAD
            count -= n

    def words(self, u: int, first: int, count: int) -> None:
        unit = self.pkg.units[u]
        if unit.fetch >> 16:
            self.fetch(u, first * PAYLOAD, count, REL)
            return
        for k in range(count):
            w = self.payloads[first + k]
            self.wait(u, self.sent[u] + 1 - self.credit(u))
            self.mailbox(DST, unit.y << 8 | unit.x)
            for j in range(4):
                self.mailbox(ARG0 + j, (w >> (64 * j)) & ((1 << 64) - 1))
            self.put(GO, 1)
            self.sent[u] += 1

    def repeat(self, s: Step) -> None:
        """A REPEAT's words, spelled out as payloads and sent as a DISPATCH."""
        first, repeats, nincs = s.arg & 0xFFFF_FFFF, (s.arg >> 32) & 0xFFFF, s.arg >> 48
        incs = []
        for k in range(nincs):
            w = self.payloads[first + s.count + k // 2] >> (128 * (k % 2))
            head, delta = w & 0xFFFF_FFFF_FFFF_FFFF, (w >> 64) & 0xFFFF_FFFF_FFFF_FFFF
            incs.append(
                (head & 0xFFFF_FFFF, (head >> 32) & 0xFF, (head >> 40) & 0xFF, delta)
            )
        at = len(self.payloads)
        for r in range(repeats):
            for j in range(s.count):
                w = self.payloads[first + j]
                for index, bit, width, delta in incs:
                    if index == j:
                        mask = (1 << width) - 1
                        field = (((w >> bit) & mask) + r * delta) & mask
                        w = (w & ~(mask << bit)) | field << bit
                self.payloads.append(w)
        self.words(s.unit, at, repeats * s.count)

    # ------------------------------------------------------------- waits
    def barrier(self) -> None:
        for u in range(len(self.pkg.units)):
            self.wait(u, max(self.sent[u], self.exp[u]))
        self.wait(SRC_MOVER, self.gos)

    def mover(self, s: Step) -> None:
        for k in range(s.count):
            w = self.payloads[s.arg + k]
            for half in (0, 128):
                reg = (w >> half) & ((1 << 64) - 1)
                val = (w >> (half + 64)) & ((1 << 64) - 1)
                if reg == MOVER_SKIP:
                    continue
                if reg >= 0x80 or reg & 7:
                    raise PackageError(f"mover register {reg:#x} is not one")
                self.put(mover_code(reg), val)
                if reg == 0 and val >> 16 & 1:
                    self.gos += 1
        if not s.flags & F_POSTED:
            self.wait(SRC_MOVER, self.gos)


def lower(pkg: Package) -> Package:
    """`pkg` for a node with a dispatch engine. Raises :class:`PackageError`
    for one it cannot carry: relocations (the entries are final values), or
    more units than the engine counts."""
    if pkg.relocs:
        raise PackageError("an engine package is bound: it has no relocations")
    if len(pkg.units) > UNITS:
        raise PackageError(f"{len(pkg.units)} units; the engine counts {UNITS}")
    st = _Stream(pkg)
    for s in pkg.steps:
        match s.op:
            case Op.END:
                break
            case Op.DISPATCH:
                st.words(s.unit, s.arg, s.count)
            case Op.REPEAT:
                st.repeat(s)
            case Op.AWAIT:
                st.exp[s.unit] += s.count
                st.wait(s.unit, st.exp[s.unit])
            case Op.BARRIER:
                st.barrier()
            case Op.MOVER:
                st.mover(s)
            case Op.MWAIT:
                st.wait(SRC_MOVER, s.count)
            case Op.FETCH:
                st.fetch(s.unit, s.arg, s.count, 0)
            case Op.RING:
                st.put(il_code(IL_RING), (s.count & 0xFF) << 8 | (s.unit & 3))
            case Op.ENGINE:
                raise PackageError("this package is lowered already")
            case _:
                st.cut()
                st.steps.append(s)
    st.barrier()
    st.cut()
    return Package(
        units=list(pkg.units),
        buffers=list(pkg.buffers),
        steps=st.steps,
        relocs=[],
        payloads=st.payloads,
        signature=pkg.signature,
        ack_reserve=pkg.ack_reserve,
        checksum=pkg.checksum,
        meta=dict(pkg.meta),
    )


def entries(pkg: Package, step: Step) -> list[tuple[int, int]]:
    """An ENGINE step's ``(code, value)`` entries, as the node reads them."""
    out = []
    for k in range(step.count):
        w = pkg.payloads[step.arg + k // 3]
        j = k % 3
        out.append(((w >> (8 * j)) & 0xFF, (w >> (64 * (j + 1))) & ((1 << 64) - 1)))
    return out
