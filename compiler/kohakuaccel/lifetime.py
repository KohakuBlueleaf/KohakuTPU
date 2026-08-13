"""Lifetimes, span reuse, and the aliasing check that makes reuse safe.

Level 2, beside the arena. An :class:`~kohakuaccel.memory.Arena` is a heap and
needs somebody to say when a span dies; a compiled kernel already knows, because
its stages ARE an order.

THE FAILURE THIS EXISTS FOR IS SILENT: a buffer read after something sharing its
bytes overwrote it returns plausible numbers and no fault. :func:`conflicts`
proves a placement sound before it runs; :class:`Writes` catches what a
placement cannot see -- an issue order disagreeing with the step order the
lifetimes were read off -- AT THE READ. A lifetime the schedule cannot establish
is not a lifetime, so an unexplained buffer gets the whole run.
"""

import itertools
from dataclasses import dataclass, field


class AliasError(RuntimeError):
    """A buffer is read after something sharing its bytes overwrote it."""


@dataclass(frozen=True)
class Span:
    """One buffer's bytes, and the steps over which it holds them.

    `life` is ``(first, last)`` step indices, both inclusive. -1 is "already
    written when the run starts" and a `last` past the final step is "still
    wanted when it ends".
    """

    name: str
    offset: int
    nbytes: int
    life: tuple = (-1, 0)

    @property
    def end(self) -> int:
        return self.offset + self.nbytes

    def __str__(self) -> str:
        return f"{self.name} [{self.offset},{self.end}) live {self.life}"


def lifetimes(steps, names, keep=()) -> dict:
    """``name -> (first, last)`` step indices, read off the order steps run in.

    `steps` is one ``(reads, writes)`` pair of names per step, in execution
    order, where `reads` are the INCOMING ones -- what the step reads that it
    did not itself write. `first` is the step that writes the name, `last` the
    final step that touches it.

    Four schedules do not explain a buffer and get the whole run instead: no
    step mentions it, it is written and never read, it is read and never
    written, or its first incoming read is not AFTER its first write. The last
    includes same-step, which is still a read of what was there before.

    Returns the map. Raises nothing -- an unexplained buffer is refused reuse,
    not refused outright.
    """
    end = len(steps)
    wrote: dict = {}
    read: dict = {}
    for i, (reads, writes) in enumerate(steps):
        for n in writes:
            wrote.setdefault(n, []).append(i)
        for n in reads:
            read.setdefault(n, []).append(i)

    out = {}
    for n in names:
        born, seen = wrote.get(n, ()), read.get(n, ())
        if n in keep or not born or not seen or seen[0] <= born[0]:
            out[n] = (-1, end)
            continue
        out[n] = (born[0], max(born[-1], seen[-1]))
    return out


def _grouped(names, sizes, life, align: int, groups):
    """`groups` folded into synthetic entries, plus how to split them again.

    A group is one allocation, so its members land consecutive and in order --
    which is what lets one transfer cover them all. Its life spans every
    member's. Returns ``(names, sizes, life, expand)``.
    """
    held = {n: g for g in groups for n in g}
    if not held:
        return names, sizes, life, {}
    out = [n for n in names if n not in held]
    sizes, life = dict(sizes), dict(life)
    expand: dict = {}
    for at, g in enumerate(groups):
        tag = f"__band{at}"
        out.append(tag)
        sizes[tag] = sum(_up(sizes[n], align) for n in g)
        life[tag] = (min(life[n][0] for n in g), max(life[n][1] for n in g))
        expand[tag] = g
    return out, sizes, life, expand


def pack(names, sizes, life, align: int = 1, base: int = 0, groups=()) -> tuple:
    """Offsets for `names`, letting spans with disjoint lifetimes share bytes.

    Walks the steps in order, taking every buffer a step writes BEFORE returning
    what dies at that step. That order is correctness, not taste: freeing first
    would let a step's output land on an operand it has not finished reading,
    since the two are live at the same instant even though one dies here.

    Returns ``(name -> offset, high-water bytes)``. Raises :class:`KeyError` for
    a name with no size or no lifetime.
    """
    names, sizes, life, expand = _grouped(names, sizes, life, align, groups)
    born: dict = {}
    dies: dict = {}
    for n in names:
        first, last = life[n]
        born.setdefault(first, []).append(n)
        dies.setdefault(last, []).append(n)

    free: list = []
    top = high = base
    offset: dict = {}
    held: dict = {}

    def take(want: int) -> int:
        nonlocal top, high
        for i, (at, have) in enumerate(free):
            if have >= want:
                free.pop(i)
                if have > want:
                    _give(free, at + want, have - want)
                return at
        at, top = top, top + want
        high = max(high, top)
        return at

    for step in sorted({*born, *dies}):
        for n in born.get(step, ()):
            held[n] = _up(sizes[n], align)
            offset[n] = take(held[n])
        for n in dies.get(step, ()):
            if n in offset:
                _give(free, offset[n], held[n])
    for tag, group in expand.items():
        at = offset.pop(tag)
        for n in group:
            offset[n] = at
            at += _up(sizes[n], align)
    return offset, high - base


def conflicts(spans, limit: int = 20) -> list:
    """Every pair of co-live spans whose bytes intersect. Empty means sound.

    THIS IS THE ONE FAILURE MODE OF A LIFETIME ALLOCATOR and it is completely
    silent on the machine. Exact, and near linear rather than quadratic: at each
    step the live set is sorted by address and only ADJACENT pairs are compared,
    which is exhaustive -- if spans i<j overlap then so do i and i+1.

    Returns at most `limit` pairs, as ``(Span, Span)``.
    """
    births: dict = {}
    edges: set = set()
    for s in spans:
        births.setdefault(s.life[0], []).append(s)
        edges |= {s.life[0], s.life[1]}

    out: list = []
    live: dict = {}
    for step in sorted(edges):
        for s in births.pop(step, ()):
            live[s.name] = s
        for name in [n for n, s in live.items() if s.life[1] < step]:
            del live[name]
        ordered = sorted(live.values(), key=lambda s: (s.offset, s.name))
        for a, b in itertools.pairwise(ordered):
            if b.offset < a.end:
                out.append((a, b))
                if len(out) >= limit:
                    return out
    return out


def aliases(spans) -> dict:
    """``name -> the set of names sharing any byte with it``, lifetimes ignored.

    Empty sets throughout mean nothing was reused, which is what makes
    :class:`Writes` free on a placement that gave every buffer its own span.
    Found by one sweep over address-sorted spans rather than pairwise.
    """
    ordered = sorted(spans, key=lambda s: (s.offset, s.name))
    out: dict = {s.name: set() for s in spans}
    for i, a in enumerate(ordered):
        for b in ordered[i + 1 :]:
            if b.offset >= a.end:
                break
            out[a.name].add(b.name)
            out[b.name].add(a.name)
    return out


class Writes:
    """Which buffer each shared span currently holds, checked at every READ.

    :func:`conflicts` proves a PLACEMENT self-consistent and cannot see that the
    caller issues in a different order from the one the lifetimes were read off.
    Reuse is decided from step order, so a buffer written early and read late
    gets bytes something else has since taken, and the number that comes back is
    plausible.
    """

    def __init__(self, alias: dict, valid=()) -> None:
        self.alias = dict(alias)
        self.valid: set = set(valid)

    @property
    def sharing(self) -> bool:
        """Whether anything aliases anything. False makes every check free."""
        return any(self.alias.values())

    def wrote(self, *names) -> None:
        """Record that these buffers now hold their own contents."""
        for n in names:
            self.valid.difference_update(self.alias.get(n, ()))
            self.valid.add(n)

    def check(self, where, *names) -> None:
        """Raise :class:`AliasError` for a read of a buffer since overwritten.

        `where` names the step, for the message. A buffer nothing aliases is
        never wrong, so it is passed without a lookup.
        """
        for n in names:
            if n in self.valid or not self.alias.get(n):
                continue
            clash = sorted(self.alias[n] & self.valid)
            raise AliasError(
                f"{where}: {n!r} is read here but was overwritten by "
                f"{clash or 'nothing yet -- it was never written'}, which the "
                f"placement puts on the same bytes. The placement is "
                f"self-consistent, so the ISSUE ORDER disagrees with the step "
                f"order its lifetimes were read off"
            )


@dataclass(frozen=True)
class Footprint:
    """What one call needs resident on a device, before anything runs.

    `sizes` is every buffer and the bytes it costs, alignment included; `peak`
    is what has to be resident at the busiest moment, which is smaller than
    their sum exactly when temps shared spans. `offsets` places the temps inside
    one block and is empty when each got its own.
    """

    name: str
    align: int
    sizes: dict = field(default_factory=dict)
    roles: dict = field(default_factory=dict)
    life: dict = field(default_factory=dict)
    offsets: dict = field(default_factory=dict)
    scratch: int = 0
    reused: bool = False
    #: Which mesh's 4 GB this is measured against. One mesh does not decode
    #: another's addresses, so "does it fit" is a question per mesh.
    mesh: int | None = None

    @property
    def peak(self) -> int:
        """Bytes resident at the busiest moment of the call."""
        return self.held + self.scratch

    @property
    def held(self) -> int:
        """Bytes in buffers nothing may share: operands, results, tables."""
        return sum(v for n, v in self.sizes.items() if n not in self.offsets)

    @property
    def saved(self) -> int:
        """Bytes the reuse took off the peak. Zero when nothing was shared."""
        return sum(self.sizes[n] for n in self.offsets) - self.scratch

    def by_role(self) -> dict:
        """Role -> the bytes it accounts for, in a settled order."""
        out: dict = {}
        for n, v in self.sizes.items():
            out[self.roles.get(n, "?")] = out.get(self.roles.get(n, "?"), 0) + v
        return dict(sorted(out.items()))

    def fits(self, nbytes: int) -> bool:
        """Whether this call's peak is within `nbytes` of free memory."""
        return self.peak <= nbytes

    def report(self) -> str:
        """The footprint as a table: every buffer, its role, size and lifetime."""
        widest = max((len(n) for n in self.sizes), default=1)
        where = "" if self.mesh is None else f" on mesh_{self.mesh}"
        lines = [f"{self.name}{where}: peak {self.peak / 2**20:,.3f} MiB"]
        for n, v in sorted(self.sizes.items(), key=lambda kv: (-kv[1], kv[0])):
            first, last = self.life.get(n, (-1, -1))
            at = f" @+{self.offsets[n]:#x}" if n in self.offsets else ""
            lines.append(
                f"  {n:<{widest}}  {self.roles.get(n, '?'):<6} "
                f"{v / 1024:>10,.1f} KiB  life {first}..{last}{at}"
            )
        for role, v in self.by_role().items():
            lines.append(f"  {role:>8} total {v / 2**20:,.3f} MiB")
        if self.reused:
            lines.append(
                f"  scratch {self.scratch / 2**20:,.3f} MiB, reuse saved "
                f"{self.saved / 2**20:,.3f} MiB"
            )
        return "\n".join(lines)


def _give(free: list, at: int, nbytes: int) -> None:
    """Return a span and coalesce, so a long run does not fragment into dust."""
    free.append((at, nbytes))
    free.sort()
    merged: list = []
    for w, n in free:
        if merged and merged[-1][0] + merged[-1][1] == w:
            merged[-1] = (merged[-1][0], merged[-1][1] + n)
        else:
            merged.append((w, n))
    free[:] = merged


def _up(v: int, q: int) -> int:
    return -(-v // q) * q if q > 1 else v
