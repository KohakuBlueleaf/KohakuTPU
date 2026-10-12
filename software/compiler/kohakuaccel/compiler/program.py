"""An L1 program: per-unit streams and the node's sync points, lowered to an L0
package (docs/spec/package-format.md).

`send` queues words for a unit (any op with a ``flits()``); `wait` holds the
node until that unit has completed everything sent to it, one completion per
word; `barrier` until every unit has; `move` is a mover step. Nothing here
chooses anything: the order, the units and the words are the program's.

A project names, per unit type, a `lowerings` hook ``f(words, state, coord)``
that rewrites a unit's stream against what the unit already holds -- `state`
is the caller's dict, kept across packages, e.g. a vector core's resident
instruction memory.
"""

from dataclasses import dataclass, field
from typing import ClassVar

from kohakuaccel.compiler.package.build import PackageBuilder
from kohakuaccel.compiler.package.format import REGS, Alu, Cond, PackageError
from kohakuaccel.compiler.package.units import unit_types

#: The register a field patch computes its value in: a program names r0..r14.
TMP = REGS - 1


@dataclass(frozen=True)
class Patch:
    """Word `word` of an op's flits gets, when the node reaches it, ``(register
    `reg` + `const`) >> shift`` into each ``(bit, width, shift)`` of
    `segments` -- a field whose value the program computes at run time."""

    word: int
    segments: tuple
    reg: int
    const: int = 0


@dataclass
class Program:
    machine: object
    #: ``("send", coord, words, ops)`` / ``("mark", coord, token)`` /
    #: ``("wait", coord, token)`` / ``("barrier",)`` / ``("move", writes, ops)``
    #: / ``("post", writes, ops, token)`` / ``("wait_moves", token)`` /
    #: ``("fetch", coord, addr, count)``
    steps: list = field(default_factory=list)
    #: unit type -> ``f(words, state, coord) -> words``; see the module docstring.
    lowerings: ClassVar[dict] = {}

    def send(self, coord, *ops) -> "Program":
        """Append the ops' words to `coord`'s stream. An op's ``patches`` (a list
        of :class:`Patch`, word index within its own flits) set fields from
        registers when the node reaches the words."""
        words, patches = [], []
        for op in ops:
            got = op.flits()
            patches += [
                Patch(len(words) + p.word, p.segments, p.reg, p.const)
                for p in getattr(op, "patches", ())
            ]
            words += got
        if words:
            self.steps.append(("send", tuple(coord), words, tuple(ops), tuple(patches)))
        return self

    # ------------------------------------------------- interlink and memory
    def ring(self, mesh: int, slot: int = 0, amount: int = 1, fence: bool = True):
        """Ring mesh `mesh`'s signal slot `slot` by `amount`; fenced, the ring
        leaves once this node's mover and outbound writes are done."""
        self.steps.append(("ring", mesh, slot, amount, fence))
        return self

    def wait_bell(self, mesh: int, count: int = 1):
        """Hold until `count` more rings from `mesh` have arrived."""
        self.steps.append(("wait_bell", mesh, count))
        return self

    def wait_slot(self, slot: int, count: int, consume: bool = False):
        """Hold until signal slot `slot` holds `count`; `consume` takes it."""
        self.steps.append(("wait_slot", slot, count, consume))
        return self

    def store(self, pairs, from_regs: bool = False):
        """64-bit stores ``[(address, value)]``; `from_regs`: values name registers."""
        self.steps.append(("store", list(pairs), from_regs))
        return self

    def ctrl(self, pairs, from_regs: bool = False):
        """Control-region writes ``[(offset, value)]``."""
        self.steps.append(("ctrl", list(pairs), from_regs))
        return self

    def load(
        self, reg: int, address: int, index: int | None = None, ctrl: bool = False
    ):
        """Register `reg` := the 64 bits at `address` (+ register `index`), or
        control register `address` with `ctrl`."""
        self.steps.append(("load", reg, address, index, ctrl))
        return self

    def alu(self, rd: int, op: Alu, ra: int = 0, b: int = 0, imm: bool = True):
        self.steps.append(("alu", rd, Alu(op), ra, b, imm))
        return self

    def label(self, name: str):
        """A jump target here. Every unit's words up to here are sent first."""
        if any(s[0] == "label" and s[1] == name for s in self.steps):
            raise ValueError(f"label {name} twice")
        self.steps.append(("label", name))
        return self

    def jump(
        self,
        name: str,
        cond: Cond = Cond.ALWAYS,
        ra: int = 0,
        b: int = 0,
        imm: bool = True,
    ):
        """Go to label `name` when ra COND b (b an immediate, or a register)."""
        self.steps.append(("jump", name, Cond(cond), ra, b, imm))
        return self

    def signal(self, value32: int = 0, value64: int = 0):
        """Tell the host now (a progress completion)."""
        self.steps.append(("signal", value32, value64))
        return self

    def settle(self, cycles: int):
        self.steps.append(("settle", cycles))
        return self

    def send_raw(self, coord, flit_type: int, words, ack: bool = False):
        """Flits of `flit_type` to `coord`, outside credit and completions."""
        self.steps.append(("send_raw", tuple(coord), flit_type, list(words), ack))
        return self

    def push(self, coord, descriptor: int, address: int, words: int, ack: bool = False):
        """A CU_DATA burst from the node: `descriptor`, then `words` words read
        from `address`."""
        self.steps.append(("push", tuple(coord), descriptor, address, words, ack))
        return self

    def recv(self, address: int, count: int, coord=None):
        """`count` RX flits (from `coord`), payloads to `address` + 32 i."""
        self.steps.append(
            ("recv", address, count, None if coord is None else tuple(coord))
        )
        return self

    def mark(self, coord) -> int:
        """A point in `coord`'s stream: everything sent to it so far. Returns
        the token a `wait` names."""
        token = sum(1 for s in self.steps if s[0] == "mark")
        self.steps.append(("mark", tuple(coord), token))
        return token

    def wait(self, coord, token: int | None = None) -> "Program":
        """Hold the node until `coord` has completed everything sent to it --
        or, given a `mark` token of that unit, everything sent up to the mark,
        whatever was sent after it."""
        if token is not None:
            at = next(
                (s[1] for s in self.steps if s[0] == "mark" and s[2] == token), None
            )
            if at != tuple(coord):
                raise ValueError(f"token {token} marks {at}, not {tuple(coord)}")
        self.steps.append(("wait", tuple(coord), token))
        return self

    def barrier(self) -> "Program":
        self.steps.append(("barrier",))
        return self

    def fetch(self, coord, addr: int, count: int, patches=()) -> "Program":
        """`count` words for `coord` that its fetch port reads from `addr` when
        the node gets here -- words a unit wrote while the package ran. They
        count as sent to `coord`; no lowering hook sees them. `patches` set
        the step's own address (step bits [127:64]) from registers; a count is
        static, since the node accounts credit by it."""
        self.steps.append(("fetch", tuple(coord), addr, count, tuple(patches)))
        return self

    def move(self, *ops) -> "Program":
        """A mover step: ops with ``writes()`` (register writes), a list of
        them, or one list of raw ``(register, value)`` writes. The node waits
        for the moves, then for every unit. An op's ``patches`` (word = the
        index of its write) set fields of the values from registers."""
        writes, ops, patches = _writes(ops)
        self.steps.append(("move", writes, ops, patches))
        return self

    def post(self, *ops) -> int:
        """A mover step the node does not wait for: it goes on while the moves
        run. Returns the token a `wait_moves` names."""
        token = sum(1 for s in self.steps if s[0] == "post")
        writes, ops, patches = _writes(ops)
        self.steps.append(("post", writes, ops, token, patches))
        return token

    def wait_moves(self, token: int) -> "Program":
        """Hold the node until the moves of `post` `token`, and every move
        before them, are done."""
        if not any(s[0] == "post" and s[3] == token for s in self.steps):
            raise ValueError(f"no post {token}")
        self.steps.append(("wait_moves", token))
        return self

    def units(self, kind: str) -> list:
        """The coordinates of every unit of `kind` on this machine."""
        types = unit_types(self.machine)
        return sorted(c for c, n in types.items() if n == kind)

    def build(
        self, fetch=None, resident=None, addresses=None, spans=None
    ) -> PackageBuilder:
        """The package. `fetch` is the memory port units stream their words
        from; `resident` is the state the `lowerings` hooks keep across
        packages (None: no hook runs, every word is sent). With `addresses`
        (``f(word, unit_type)``, the project's address fields) and `spans`
        (``{base: size}``), every address field inside a span is relocated: the
        package runs at whatever addresses it is bound to."""
        types = unit_types(self.machine)
        b = PackageBuilder(
            types=types,
            credit=self.machine.inst_depth,
            mesh=self.machine.default,
            fetch=fetch,
        )
        if spans:
            b.spans_from(spans)
        # Words dispatched to and awaited from each unit, cumulative: an AWAIT
        # raises a unit's expected count (package-format.md), so a wait up to a
        # mark is the difference.
        sent: dict = {}
        awaited: dict = {}
        marks: dict = {}
        # Sends between two sync points are unordered across units, so they go
        # out as few dispatches as the node allows: a fetch-port request costs
        # ~1k node cycles whatever its length (MEASURED on card_v9_1n). The node
        # sends a fetched step whole, waiting on that unit's credit, before the
        # next step, so a stream past its unit's credit is cut at the credit and
        # the units' pieces interleave: no unit waits idle behind another's
        # credit (MEASURED: 16 vector epilogues, 973 words a core, 411k cycles as
        # one step a core, 310k split by package). A stream within the credit
        # stays one step: cut at 255 anyway, add 262144 ran 93.9k -> 107.8k.
        pending: dict = {}
        #: unit -> [Patch], word indices into its pending words
        patched: dict = {}
        # Half the credit: a dispatch's `last` flit keeps one credit (isa.md §8),
        # so a second full-credit piece to one unit never fits.
        piece = self.machine.inst_depth // 2
        # The package's moves so far (its GOs), and the count each post ends at.
        gos, posts = 0, {}
        labels: dict = {}
        jumps: list = []
        # Past a label a step may run again: a patched word may still be
        # streaming to its unit from the pass before, so its unit is awaited.
        looping = False

        def flush(only=None) -> None:
            # `only`: that unit and every unit queued before it, in queue order.
            order = list(pending)
            ready = []
            for u in (
                order[: order.index(only) + 1]
                if only in pending
                else order if only is None else []
            ):
                words = pending.pop(u, [])
                fixes = patched.pop(u, [])
                coord = (b.units[u].x, b.units[u].y)
                lower = self.lowerings.get(types.get(coord))
                # A patched word's value is the node's at run time: no hook may
                # drop it as already held.
                if resident is not None and lower is not None and not fixes:
                    words = lower(words, resident, coord)
                if words:
                    if fixes and looping:
                        await_to(u, sent.get(u, 0))
                    ready.append((u, words, fixes))
                    sent[u] = sent.get(u, 0) + len(words)
            for k in range(0, max((len(w) for _, w, _ in ready), default=0), piece):
                for u, words, fixes in ready:
                    chunk = words[k : k + piece]
                    if not chunk:
                        continue
                    first = len(b.payloads)
                    for p in fixes:
                        if k <= p.word < k + len(chunk):
                            patch(p, first + p.word - k)
                    b.dispatch(u, chunk, addresses)

        def patch(p: Patch, index: int, steps: bool = False, base: int = 0) -> None:
            """PATCH steps for `p` into word `index`, its bits offset by `base`."""
            reg = p.reg
            if p.const:
                b.alu(TMP, Alu.ADD, p.reg, p.const)
                reg = TMP
            for bit, width, shift in p.segments:
                b.patch(reg, index, base + bit, width, shift, steps)

        def mover_patches(patches) -> None:
            """A MOVER step's payload pair i is word first + i // 2, its value
            at bit (i % 2) * 128 + 64."""
            first = len(b.payloads)
            for p in patches:
                patch(p, first + p.word // 2, base=(p.word % 2) * 128 + 64)

        def await_to(u, upto: int) -> None:
            if upto > awaited.get(u, 0):
                b.await_(u, upto - awaited.get(u, 0))
                awaited[u] = upto

        for step in self.steps:
            match step[0]:
                case "send":
                    u = b.unit(step[1])
                    have = len(pending.get(u, []))
                    pending.setdefault(u, []).extend(step[2])
                    patched.setdefault(u, []).extend(
                        Patch(have + p.word, p.segments, p.reg, p.const)
                        for p in step[4]
                    )
                case "ring":
                    flush()
                    b.ring(step[1], step[2], step[3], step[4])
                case "wait_bell":
                    flush()
                    b.wait_bell(step[1], step[2])
                case "wait_slot":
                    flush()
                    b.wait_slot(step[1], step[2], step[3])
                case "store":
                    flush()
                    b.store(step[1], step[2])
                case "ctrl":
                    flush()
                    b.ctrl(step[1], step[2])
                case "load":
                    flush()
                    b.load(step[1], step[2], step[3], step[4])
                case "alu":
                    flush()
                    b.alu(*step[1:])
                case "label":
                    flush()
                    labels[step[1]] = len(b.steps)
                    looping = True
                case "jump":
                    flush()
                    jumps.append(
                        (b.jump(0, step[2], step[3], step[4], step[5]), step[1])
                    )
                case "signal":
                    flush()
                    b.signal(step[1], step[2])
                case "settle":
                    flush()
                    b.settle(step[1])
                case "send_raw":
                    flush()
                    b.send(b.unit(step[1]), step[2], step[3], step[4])
                case "push":
                    flush()
                    b.push(b.unit(step[1]), step[2], step[3], step[4], step[5])
                case "recv":
                    flush()
                    unit = 0xFFFF if step[3] is None else b.unit(step[3])
                    b.recv(step[1], step[2], unit)
                case "mark":
                    # Every queued word goes out now, the units interleaved, so
                    # the count a wait names is what the unit is actually sent.
                    u = b.unit(step[1])
                    flush()
                    marks[step[2]] = sent.get(u, 0)
                case "wait":
                    u = b.unit(step[1])
                    flush()
                    await_to(u, sent.get(u, 0) if step[2] is None else marks[step[2]])
                case "barrier":
                    flush()
                    for u in sorted(sent):
                        await_to(u, sent[u])
                    b.barrier()
                case "move":
                    flush()
                    mover_patches(step[3])
                    b.mover(step[1], addresses)
                    gos += _gos(step[1])
                    b.barrier()
                case "post":
                    flush()
                    mover_patches(step[4])
                    b.mover(step[1], addresses, posted=True)
                    gos += _gos(step[1])
                    posts[step[3]] = gos
                case "wait_moves":
                    if looping:
                        raise PackageError(
                            "`wait moves` past a label: its count is the package's "
                            "moves so far, which a step run again changes; use a "
                            "blocking move inside a loop"
                        )
                    flush()
                    b.mwait(posts[step[1]])
                case "fetch":
                    # After the unit's queued words: a fetch keeps its stream order.
                    u = b.unit(step[1])
                    flush(u)
                    start = len(b.steps)
                    for p in step[4]:
                        patch(p, 0, steps=True)
                    b.fetch_from(u, step[2], step[3])
                    b.repoint(start, len(b.steps) - 1)
                    sent[u] = sent.get(u, 0) + step[3]
        flush()
        for u in sorted(sent):
            await_to(u, sent[u])
        b.barrier()
        for at, name in jumps:
            if name not in labels:
                raise PackageError(f"goto {name}: no such label")
            b.retarget(at, labels[name])
        return b


def _writes(ops) -> tuple:
    """``(writes, ops, patches)`` of a move's arguments: ops with
    ``writes()``, a list of them, or one list of raw ``(register, value)``
    writes; each op's patches renumbered to the whole list."""
    if len(ops) == 1 and isinstance(ops[0], list):
        if all(isinstance(w, tuple) for w in ops[0]):
            return list(ops[0]), (), ()
        ops = tuple(ops[0])
    writes, patches = [], []
    for op in ops:
        got = op.writes()
        patches += [
            Patch(len(writes) + p.word, p.segments, p.reg, p.const)
            for p in getattr(op, "patches", ())
        ]
        writes += got
    return writes, tuple(ops), tuple(patches)


def _gos(writes) -> int:
    """Moves a mover step starts: its writes to the control register with GO."""
    return sum(1 for r, v in writes if r == 0 and v >> 16 & 1)


__all__ = ["Program"]
