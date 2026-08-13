"""Where every buffer of a WHOLE MODEL sits, decided before anything runs.

`Compiled.footprint` prices one call; a block is tens and the peak is across
them. Weights are PINNED in declaration order and only activations are packed,
so adding a layer moves no weight already uploaded over a link three orders of
magnitude slower than the arithmetic.

DECLARE EACH BUFFER WHERE THE RUNNER ISSUES IT: step order is the only thing
lifetimes come from. Declaring in another order verifies an execution that never
happens -- measured, a per-head block scored 1.38e-01 against 1.07e-03 with
`verify()` passing.
"""

from dataclasses import dataclass, field

from kohakuaccel.lifetime import Span, conflicts, lifetimes, pack

#: What a buffer is for. Only `act` is ever placed on top of anything else.
KINDS = ("weight", "act", "input", "output")


class PlanError(ValueError):
    """A plan that cannot be run, and which rule it broke."""


@dataclass(frozen=True)
class Held:
    """One named buffer: what it costs and what it is for."""

    name: str
    nbytes: int
    kind: str
    note: str = ""


@dataclass(frozen=True)
class Step:
    """One kernel launch, naming BUFFERS rather than addresses.

    That indirection is the mechanism: a step is written once against names and
    runs against whatever the placement decided, so two steps naming one buffer
    cannot disagree about where it is.
    """

    name: str
    reads: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()
    host_in: int = 0
    host_out: int = 0


class Plan:
    """A model as buffers and steps, with nothing placed yet."""

    def __init__(self, arena_bytes: int, align: int = 256, base: int = 0) -> None:
        self.arena_bytes, self.align, self.base = (
            int(arena_bytes),
            int(align),
            int(base),
        )
        self.held: dict[str, Held] = {}
        self.steps: list[Step] = []

    def add(self, name: str, nbytes: int, kind: str, note: str = "") -> str:
        if name in self.held:
            raise PlanError(f"buffer {name!r} is declared twice")
        if kind not in KINDS:
            raise PlanError(
                f"buffer {name!r}: unknown kind {kind!r}, not one of {KINDS}"
            )
        self.held[name] = Held(name, int(nbytes), kind, note)
        return name

    def weight(self, name, nbytes, note="") -> str:
        return self.add(name, nbytes, "weight", note)

    def act(self, name, nbytes, note="") -> str:
        return self.add(name, nbytes, "act", note)

    def input(self, name, nbytes, note="") -> str:
        return self.add(name, nbytes, "input", note)

    def output(self, name, nbytes, note="") -> str:
        return self.add(name, nbytes, "output", note)

    def step(self, name, reads=(), writes=(), host_in=0, host_out=0) -> Step:
        """Append a step. Raises for a buffer it names that is not declared."""
        for n in (*reads, *writes):
            if n not in self.held:
                raise PlanError(
                    f"step {name!r} names {n!r}, which is not declared. Every "
                    f"buffer is declared before the step that touches it -- a "
                    f"name invented at step time has no size and no lifetime"
                )
        made = Step(name, tuple(reads), tuple(writes), host_in, host_out)
        self.steps.append(made)
        return made

    def solve(self, reuse: bool = True) -> "Placement":
        """Give every buffer an address.

        `reuse=False` gives each activation its own, which is not only a
        debugging aid: a plan that never frees has no aliasing bug to have, and
        the peak it reports says whether that is affordable.

        Raises :class:`PlanError` if the result does not fit the arena.
        """
        sizes = {n: _up(h.nbytes, self.align) for n, h in self.held.items()}
        at, top = {}, self.base
        for h in self.held.values():
            if h.kind == "weight":
                at[h.name] = top
                top += sizes[h.name]
        weights = top - self.base

        acts = [n for n, h in self.held.items() if h.kind != "weight"]
        if not reuse:
            for n in acts:
                at[n] = top
                top += sizes[n]
            peak = top - self.base - weights
        else:
            life = lifetimes(self._touches(), sizes, keep=[])
            spots, peak = pack(acts, sizes, life, align=self.align, base=top)
            at.update(spots)
            top += peak

        used = top - self.base
        if used > self.arena_bytes:
            raise PlanError(
                f"this plan needs {used:,} bytes and the arena holds "
                f"{self.arena_bytes:,}: weights {weights:,}, peak live "
                f"activations {peak:,}"
            )
        return Placement(self, at, sizes, weights, peak, used)

    def _touches(self) -> list:
        """Per step, `(incoming reads, writes)` -- what `lifetimes` walks.

        A read of something the step itself writes is NOT incoming: counting it
        would start the lifetime one step early and pin a span nothing holds.
        """
        out = []
        for s in self.steps:
            made = set(s.writes)
            out.append((set(s.reads) - made, made))
        return out


@dataclass
class Placement:
    """A solved plan: one address per buffer, and what it cost."""

    plan: Plan
    at: dict
    sizes: dict
    weight_bytes: int
    peak_act_bytes: int
    total_bytes: int
    #: Bytes the host moves, filled by :meth:`traffic`.
    _moved: dict = field(default_factory=dict, init=False)

    def verify(self, limit: int = 20) -> list[str]:
        """Every way this placement is unsound. Empty means it is safe to run.

        THE OVERLAP CHECK IS THE POINT: reuse aliasing is the one failure mode
        of a lifetime allocator and it is completely silent on the machine.
        """
        life = lifetimes(self.plan._touches(), self.sizes, keep=[])
        spans = [
            Span(n, self.at[n], self.sizes[n], life.get(n, (-1, len(self.plan.steps))))
            for n in self.at
        ]
        why = [
            f"{a.name} [{a.offset},{a.end}) live {a.life} overlaps "
            f"{b.name} [{b.offset},{b.end}) live {b.life}"
            for a, b in conflicts(spans, limit)
        ]
        for n, h in self.plan.held.items():
            first, last = life.get(n, (0, 0))
            if h.kind == "act" and last <= first:
                why.append(f"{n} is written at step {first} and never read")
        return why[:limit]

    def traffic(self, up_bps: float, down_bps: float, weight_bps=None) -> dict:
        """Host bytes and the seconds they take.

        Weights count ONCE -- uploaded before the run and never moved -- while a
        step's `host_in`/`host_out` count every execution. `weight_bps` prices
        them on a different transport, which is the point of the split: a bulk
        weight upload needs no control plane and can go over PCIe where dispatch
        cannot.
        """
        kind = {n: h.kind for n, h in self.plan.held.items()}
        wb = sum(h.nbytes for h in self.plan.held.values() if h.kind == "weight")
        ib = sum(h.nbytes for h in self.plan.held.values() if h.kind == "input")
        ob = sum(h.nbytes for h in self.plan.held.values() if h.kind == "output")
        sin = sum(s.host_in for s in self.plan.steps)
        sout = sum(s.host_out for s in self.plan.steps)
        del kind
        secs = (
            wb / (weight_bps or up_bps) + (ib + sin) / up_bps + (ob + sout) / down_bps
        )
        return {
            "weight_bytes": wb,
            "up_bytes": wb + ib + sin,
            "down_bytes": ob + sout,
            "seconds": secs,
        }

    def report(self) -> str:
        mib = 1 << 20
        return (
            f"{len(self.plan.held)} buffers, {len(self.plan.steps)} steps\n"
            f"  weights {self.weight_bytes / mib:,.1f} MiB   peak acts "
            f"{self.peak_act_bytes / mib:,.1f} MiB   total "
            f"{self.total_bytes / mib:,.1f} MiB of "
            f"{self.plan.arena_bytes / mib:,.0f} MiB"
        )


def _up(v: int, q: int) -> int:
    return -(-v // q) * q if q > 1 else v
