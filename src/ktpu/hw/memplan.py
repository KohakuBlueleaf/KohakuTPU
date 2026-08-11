"""Where every tensor of a whole network sits, decided before anything runs.

    p.weight("conv1.w", AENTRY(m=320, k=320)); p.act("y", CTILE(4096, 320))
    p.step("conv1", "matmul", reads=("x", "conv1.w"), writes=("y",))
    place = p.solve(); place.byte("y")

`Allocator` is a heap and needs somebody to know when each buffer dies; nothing
did. A forward pass is fixed before step 0, so lifetimes are graph properties
and computing them turns runtime aliasing into a static check.

Three mistakes this refuses, ALL SILENT on the machine: two co-live tensors
placed on top of each other; a tile-major operand image fed where a 4x4-sub-tile
drain was expected, permuting every element; a read one step before its write,
which on ECC DRAM is a bus fault.

DECLARE EACH BUFFER WHERE THE RUNNER ISSUES IT. Step order IS execution order
and lifetimes are computed from nothing else, so a plan that declares work in a
different order from the code that runs it is verified against an execution that
never happens -- `verify()` passes and the runner reads an address the allocator
has already given away. A per-head transformer block scored 1.38e-01 instead of
1.07e-03 exactly this way. :class:`Writes` catches it at the read.
"""

import itertools
from dataclasses import dataclass, field

from ktpu.hw import tensor as T
from ktpu.hw.bench import DRAIN_BURST
from ktpu.hw.board import MEM_WORD_BYTES

#: Every allocation starts on a drain burst, so a C drain can never straddle two
#: buffers. Same constant and same reason as `ktpu.hw.runtime.ALIGN_WORDS`.
ALIGN_WORDS = DRAIN_BURST
ALIGN_BYTES = ALIGN_WORDS * MEM_WORD_BYTES

#: Measured on the ship_3x2 card over `tools/jtag_axi.tcl`, both directions, at
#: block sizes from 8 kB to 1 MB. Reads are consistently slower than writes.
JTAG_WRITE_BPS = 100_000
JTAG_READ_BPS = 57_000


class PlanError(ValueError):
    """The plan is not runnable, and the message says which rule it broke."""


# ---- layouts ---------------------------------------------------------------
@dataclass(frozen=True)
class Layout:
    """A tensor's SHAPE AND ITS BYTE ORDER, which are not the same question.

    `kind` is what distinguishes an operand image from a drained result from a
    flat fp16 array. Two tensors of equal `nbytes` and different `kind` hold the
    same numbers in different places, and the whole point of carrying this is
    that the machine cannot tell them apart and this can.
    """

    kind: str
    rows: int
    cols: int
    nbytes: int
    preq: bool = False

    def __str__(self):
        q = "/mx" if self.preq else ""
        return f"{self.kind}{q}[{self.rows}x{self.cols}]"


def FLAT(rows, cols):
    """Row-major fp16. What a vector core VFILLs and VDRAINs, and what the host
    reads. The only layout whose element order is the obvious one."""
    return Layout("flat", rows, cols, rows * cols * 2)


def AENTRY(m, k, preq=False):
    """A cluster's A operand: tile-major ``4 lanes x 32 K`` L1 entries.

    `m` and `k` are the PADDED extents -- `entries` below refuses anything else,
    because a partial entry is not a smaller entry, it is a different address for
    everything after it.
    """
    return Layout("aentry", m, k, _entry_bytes(m, k, preq), preq)


def BENTRY(n, k, preq=False):
    """A cluster's B operand. Same packing as A; `n` counts B's rows, which are
    the output columns, because B is stored transposed."""
    return Layout("bentry", n, k, _entry_bytes(n, k, preq), preq)


def CTILE(m, n):
    """A cluster's drained C: one 256-bit word per 4x4 sub-tile.

    Rounded up to a whole drain burst, which is why this is not simply
    ``m*n*2``: the DRAIN writes DRAIN_BURST words at a time and the tail of the
    last burst lands past the last sub-tile.
    """
    words = _up((m // T.LANES) * (n // T.LANES), DRAIN_BURST)
    return Layout("ctile", m, n, words * MEM_WORD_BYTES)


def _entry_bytes(lanes, k, preq):
    if lanes % T.LANES or k % T.KBLOCK:
        raise PlanError(
            f"an operand image is whole {T.LANES}x{T.KBLOCK} entries; "
            f"{lanes}x{k} is not. Pad the shape before planning it -- padding "
            "after placement moves every address that follows."
        )
    per = T.MXFP7_ENTRY_BYTES if preq else T.FP16_ENTRY_BYTES
    return (lanes // T.LANES) * (k // T.KBLOCK) * per


def _up(v, q):
    return -(-v // q) * q


# ---- the plan --------------------------------------------------------------
@dataclass
class Tensor:
    """One named buffer. `kind` decides which arena it lands in and when it dies.

    ``weight``   uploaded once, live for the whole program, never reused.
    ``act``      produced by a step and dead after its last reader.
    ``input``    the host writes it before step 0; dead after its last reader.
    ``output``   the host reads it after the last step, so it is live to the end.
    """

    name: str
    layout: Layout
    kind: str
    note: str = ""

    @property
    def nbytes(self) -> int:
        return self.layout.nbytes

    @property
    def words(self) -> int:
        return _up(-(-self.nbytes // MEM_WORD_BYTES), ALIGN_WORDS)


@dataclass
class Step:
    """One kernel launch. Reads and writes are TENSOR NAMES, not addresses.

    That indirection is the whole mechanism: a step is written once against
    names and runs against whatever the placement decided, which is what makes
    "several kernels with matched addresses" a property the planner enforces
    rather than a convention the caller remembers.

    `wants` pins the layout a read must be in, per tensor name. A step that
    omits it accepts anything, which is right for an elementwise vector pass --
    those are permutation-agnostic and deliberately so.
    """

    name: str
    kind: str
    reads: tuple = ()
    writes: tuple = ()
    wants: dict = field(default_factory=dict)
    host_in: int = 0
    host_out: int = 0
    meta: dict = field(default_factory=dict)


class Plan:
    """A whole forward pass as tensors and steps, with nothing placed yet.

    Declaration order does not matter; step order is execution order and is the
    only thing lifetimes are computed from.
    """

    def __init__(self, arena_words: int, base: int = 0):
        self.arena_words, self.base = int(arena_words), int(base)
        self.tensors: dict[str, Tensor] = {}
        self.steps: list[Step] = []

    # ---- declaring ------------------------------------------------------
    def add(self, name, layout, kind, note="") -> str:
        if name in self.tensors:
            raise PlanError(f"tensor {name!r} is declared twice")
        if kind not in ("weight", "act", "input", "output"):
            raise PlanError(f"tensor {name!r}: unknown kind {kind!r}")
        self.tensors[name] = Tensor(name, layout, kind, note)
        return name

    def weight(self, name, layout, note="") -> str:
        return self.add(name, layout, "weight", note)

    def act(self, name, layout, note="") -> str:
        return self.add(name, layout, "act", note)

    def input(self, name, layout, note="") -> str:
        return self.add(name, layout, "input", note)

    def output(self, name, layout, note="") -> str:
        return self.add(name, layout, "output", note)

    def step(self, name, kind, reads=(), writes=(), wants=None, host_in=0,
             host_out=0, **meta) -> Step:
        for n in (*reads, *writes):
            if n not in self.tensors:
                raise PlanError(
                    f"step {name!r} names tensor {n!r}, which is not declared. "
                    "Declare every buffer before the step that touches it -- a "
                    "name invented at step time has no size and no lifetime."
                )
        s = Step(name, kind, tuple(reads), tuple(writes), dict(wants or {}),
                 host_in, host_out, meta)
        self.steps.append(s)
        return s

    # ---- solving --------------------------------------------------------
    def lifetimes(self) -> dict:
        """``name -> (first, last)`` step indices. Weights and inputs start at -1.

        `last` is the index of the final step that READS the tensor; an output
        is forced to the end because the host reads it after the program. A
        tensor nobody reads gets ``last == first``, which :meth:`solve` reports
        rather than silently reusing -- a computed-and-discarded intermediate is
        almost always a wiring mistake, not an intended one.
        """
        first = {n: (-1 if t.kind in ("weight", "input") else None)
                 for n, t in self.tensors.items()}
        last = dict.fromkeys(self.tensors, -1)
        for i, s in enumerate(self.steps):
            for n in s.writes:
                if first[n] is None:
                    first[n] = i
                last[n] = max(last[n], i)
            for n in s.reads:
                if first[n] is None:
                    raise PlanError(
                        f"step {i} ({s.name!r}) reads {n!r} before anything "
                        "writes it. On this DRAM an unwritten line is an "
                        "uncorrectable ECC error, not zeros."
                    )
                last[n] = max(last[n], i)
        end = len(self.steps)
        out = {}
        for n, t in self.tensors.items():
            if first[n] is None:
                raise PlanError(
                    f"tensor {n!r} is declared but never written and never read"
                )
            out[n] = (first[n], end if t.kind == "output" else last[n])
        return out

    def check_layouts(self) -> list[str]:
        """Every step that reads a tensor in a layout it cannot consume."""
        bad = []
        for i, s in enumerate(self.steps):
            for n, want in s.wants.items():
                if n not in (*s.reads, *s.writes):
                    bad.append(f"step {i} ({s.name}): wants {n!r}, which it "
                               "neither reads nor writes")
                    continue
                got = self.tensors[n].layout
                if got.kind != want:
                    bad.append(
                        f"step {i} ({s.name}): reads {n!r} as {want!r} but it "
                        f"holds {got}. These are the same bytes in a different "
                        "order; the machine will not notice."
                    )
        return bad

    def solve(self, reuse: bool = True) -> "Placement":
        """Assign an address to every tensor. Weights first, then activations.

        WEIGHTS ARE PINNED AND ACTIVATIONS ARE NOT, because that is what the two
        actually are: a weight is uploaded once over a link three orders of
        magnitude slower than the arithmetic and must never be moved, while an
        intermediate is cheap to place anywhere and there are many of them.
        Giving weights the bottom of the arena in declaration order also means
        adding a layer does not move any weight already loaded.

        `reuse=False` gives every activation its own address. That is not merely
        a debugging aid: with the intermediates of one UNet stage well under the
        free space, a plan that never frees has no lifetime bugs to have, and
        the peak it reports says whether that is affordable.
        """
        life = self.lifetimes()
        bad = self.check_layouts()
        if bad:
            raise PlanError("layout mismatch:\n  " + "\n  ".join(bad))

        addr, top = {}, self.base
        for t in self.tensors.values():
            if t.kind == "weight":
                addr[t.name] = top
                top += t.words
        weight_words = top - self.base

        acts = [t for t in self.tensors.values() if t.kind != "weight"]
        if not reuse:
            for t in acts:
                addr[t.name] = top
                top += t.words
            peak = top - self.base - weight_words
        else:
            top, peak = self._pack(acts, life, top, addr)
            peak -= self.base + weight_words

        used = top - self.base
        if used > self.arena_words:
            raise PlanError(
                f"the plan needs {used:,} words ({used * MEM_WORD_BYTES / 2**30:.3f} "
                f"GiB) and the arena has {self.arena_words:,} "
                f"({self.arena_words * MEM_WORD_BYTES / 2**30:.3f} GiB): weights "
                f"{weight_words * MEM_WORD_BYTES / 2**30:.3f} GiB, peak live "
                f"activations {peak * MEM_WORD_BYTES / 2**20:.1f} MiB"
            )
        return Placement(self, addr, life, weight_words, peak, top - self.base)

    def _pack(self, acts, life, top, addr):
        """Walk the steps, allocating a step's writes before freeing its dead.

        THE ORDER MATTERS AND IT IS NOT THE OBVIOUS ONE. Freeing first would let
        a step's output land on top of an input it has not finished reading --
        the two are live at the same instant even though one dies at this step.
        Allocating first costs one buffer of headroom and makes in-place
        aliasing impossible to express by accident.
        """
        free: list[tuple[int, int]] = []
        base, high = top, top
        live: dict[str, tuple[int, int]] = {}

        def take(words):
            nonlocal top, high
            for i, (w, have) in enumerate(free):
                if have >= words:
                    free.pop(i)
                    if have > words:
                        _give(free, w + words, have - words)
                    return w
            w, top = top, top + words
            high = max(high, top)
            return w

        for t in acts:
            if life[t.name][0] == -1:
                live[t.name] = (take(t.words), t.words)
                addr[t.name] = live[t.name][0]

        byname = {t.name: t for t in acts}
        for i, s in enumerate(self.steps):
            for n in s.writes:
                if n in live or n not in byname:
                    continue
                live[n] = (take(byname[n].words), byname[n].words)
                addr[n] = live[n][0]
            for n in {*s.reads, *s.writes}:
                if n in live and life[n][1] == i:
                    _give(free, *live.pop(n))
        return max(top, base), high

    # ---- convenience ----------------------------------------------------
    def __len__(self):
        return len(self.steps)

    def __repr__(self):
        return (f"<Plan {len(self.tensors)} tensors, {len(self.steps)} steps, "
                f"arena {self.arena_words:,} words>")


class Writes:
    """Which tensor each address currently holds, checked at every READ.

    :meth:`Placement.verify` proves THE PLAN is self-consistent and cannot see
    that the RUNNER issues in a different order. Reuse is decided from step
    order, so a buffer declared early and read late gets an address the
    allocator has since given away, and the number that comes back is plausible.

    MEASURED 2026-08-11: a per-head transformer block scored 1.38e-01 instead of
    1.07e-03 this way, with `verify()` passing.
    """

    def __init__(self, place: "Placement"):
        self.place = place
        self.alias = place.aliases()
        self.valid: set = set()

    def wrote(self, *names) -> None:
        for n in names:
            self.valid -= self.alias[n]
            self.valid.add(n)

    def check(self, *names) -> None:
        """Raise :class:`PlanError` for a read of a buffer since overwritten."""
        for n in names:
            if n in self.valid or not self.alias[n]:
                continue
            clash = sorted(self.alias[n] & self.valid)
            raise PlanError(
                f"{n!r} is read here but was overwritten by "
                f"{clash or 'nothing yet -- it was never written'}, which the "
                f"placement aliases to it. The plan is self-consistent; the "
                f"ISSUE ORDER disagrees with the DECLARATION ORDER it was "
                f"solved from. Declare each buffer where the runner writes it."
            )


def _give(free, word, words):
    """Return a span and coalesce, so a long run does not fragment into dust."""
    free.append((word, words))
    free.sort()
    merged = []
    for w, n in free:
        if merged and merged[-1][0] + merged[-1][1] == w:
            merged[-1] = (merged[-1][0], merged[-1][1] + n)
        else:
            merged.append((w, n))
    free[:] = merged


# ---- the answer ------------------------------------------------------------
@dataclass
class Placement:
    """A solved plan: one address per tensor, plus what it cost.

    Addresses are in 256-bit WORDS, which is what the machine's flits carry;
    :meth:`byte` converts for the transport. Both are kept rather than derived
    at the call site for the same reason `runtime.Buffer` keeps both -- confusing
    them writes an operand a factor of 32 away from where it was meant to go.
    """

    plan: Plan
    word: dict
    life: dict
    weight_words: int
    peak_act_words: int
    total_words: int

    def byte(self, name, board=None) -> int:
        """Byte address of `name`, on `board` if one is given."""
        w = self.word[name]
        return board.mem_byte(w) if board is not None else w * MEM_WORD_BYTES

    def span(self, name) -> tuple:
        """``(word, words)`` -- what the tensor occupies, alignment included."""
        return self.word[name], self.plan.tensors[name].words

    def bind(self, step) -> dict:
        """``tensor name -> word address`` for one step's operands.

        This is the address matching, in one call: a kernel builder asks the
        placement where its operands are instead of computing an offset, so two
        kernels that name the same tensor cannot disagree about where it is.
        """
        s = step if isinstance(step, Step) else self.plan.steps[step]
        return {n: self.word[n] for n in (*s.reads, *s.writes)}

    def verify(self, limit: int = 20) -> list[str]:
        """Every way the placement is unsound. Empty means it is safe to run.

        THE OVERLAP CHECK IS THE POINT: reuse aliasing is the one failure mode of
        a lifetime allocator and it is completely silent on the machine.

        Exact, and near linear rather than quadratic, because a whole UNet is
        tens of thousands of tensors and a pairwise sweep over those never
        finishes. At each step the LIVE set is sorted by address and only
        ADJACENT pairs are compared -- which is exhaustive: if intervals i<j
        overlap then `start[i+1] <= start[j] < end[i]`, so i and i+1 overlap too.
        """
        why = []
        for name, span in self._conflicts(limit):
            a, b = name
            why.append(
                f"{a} [{span[0]},{span[1]}) live {self.life[a]} overlaps "
                f"{b} [{span[2]},{span[3]}) live {self.life[b]}"
            )
        for n, (f, l) in self.life.items():
            if l == f and self.plan.tensors[n].kind == "act":
                why.append(f"{n} is written at step {f} and never read")
                if len(why) > limit:
                    break
        if self.total_words > self.plan.arena_words:
            why.append(f"{self.total_words} words placed in an arena of "
                       f"{self.plan.arena_words}")
        return why

    def _conflicts(self, limit):
        """Co-live tensors whose spans intersect, found by a per-step sweep."""
        births: dict[int, list] = {}
        for n, (f, l) in self.life.items():
            births.setdefault(f, []).append((n, l))
        out, live = [], {}
        for step in range(-1, len(self.plan.steps) + 1):
            for n, l in births.pop(step, ()):
                live[n] = l
            for n in [n for n, l in live.items() if l < step]:
                del live[n]
            spans = sorted((self.word[n], self.plan.tensors[n].words, n)
                           for n in live)
            for (w, k, a), (w2, k2, b) in itertools.pairwise(spans):
                if w2 < w + k:
                    out.append(((a, b), (w, w + k, w2, w2 + k2)))
                    if len(out) >= limit:
                        return out
        return out

    def aliases(self) -> dict:
        """``name -> the set of tensors sharing any word with it``.

        Empty for a plan solved with ``reuse=False``. Computed by one sweep over
        address-sorted spans rather than pairwise, for the same reason
        :meth:`verify` is.
        """
        spans = sorted((self.word[n], self.plan.tensors[n].words, n)
                       for n in self.word)
        out = {n: set() for n in self.word}
        for i, (w, k, a) in enumerate(spans):
            for w2, k2, b in spans[i + 1:]:
                if w2 >= w + k:
                    break
                out[a].add(b)
                out[b].add(a)
        return out

    def tracker(self) -> "Writes":
        return Writes(self)

    def traffic(self, wbps=JTAG_WRITE_BPS, rbps=JTAG_READ_BPS,
                weight_bps=None) -> dict:
        """Bytes the HOST must move, and the seconds that is at a link rate.

        Weights count once -- uploaded before the run and never moved -- while a
        step's `host_in`/`host_out` count every execution. `weight_bps` prices
        the weights on a DIFFERENT transport, which is the whole point of the
        split: weights are one bulk write of a known address map and need no
        control plane, so they can go over PCIe even where dispatch cannot.
        """
        wb = sum(t.nbytes for t in self.plan.tensors.values()
                 if t.kind == "weight")
        ib = sum(t.nbytes for t in self.plan.tensors.values()
                 if t.kind == "input")
        ob = sum(t.nbytes for t in self.plan.tensors.values()
                 if t.kind == "output")
        sin = sum(s.host_in for s in self.plan.steps)
        sout = sum(s.host_out for s in self.plan.steps)
        up, down = wb + ib + sin, ob + sout
        wsec = wb / (weight_bps or wbps)
        return {
            "weight_bytes": wb,
            "input_bytes": ib,
            "output_bytes": ob,
            "step_in_bytes": sin,
            "step_out_bytes": sout,
            "up_bytes": up,
            "down_bytes": down,
            "seconds": wsec + (ib + sin) / wbps + down / rbps,
            "weight_seconds": wsec,
            "step_seconds": (ib + sin) / wbps + down / rbps,
        }

    def report(self, limit=None) -> str:
        """The placement as a table: name, kind, layout, address, span, life."""
        rows = sorted(self.word, key=lambda n: self.word[n])
        out = [(f"{'tensor':<34} {'kind':<7} {'layout':<22} {'word':>11} "
                f"{'KiB':>9} {'life':>10}")]
        for n in rows[:limit]:
            t = self.plan.tensors[n]
            f, l = self.life[n]
            out.append(
                f"{n:<34} {t.kind:<7} {t.layout!s:<22} {self.word[n]:>11,} "
                f"{t.nbytes / 1024:>9.1f} {f:>4}..{l:<4}"
            )
        if limit and len(rows) > limit:
            out.append(f"... {len(rows) - limit} more")
        mb = MEM_WORD_BYTES / 2**20
        out.append(
            f"\nweights {self.weight_words * mb:,.1f} MiB   peak live acts "
            f"{self.peak_act_words * mb:,.1f} MiB   total "
            f"{self.total_words * mb:,.1f} MiB of "
            f"{self.plan.arena_words * mb:,.1f} MiB"
        )
        return "\n".join(out)
