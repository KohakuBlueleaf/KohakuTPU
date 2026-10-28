"""Runs that must not lean on an earlier run.

`poisoned`: on every card the harness holds (the session's and those it
`track`s), allocations start as POISON, buffers land at an offset none of
the previous `PADS` - 1 runs used (a node register or core memory still
holding an earlier run's address meets other addresses: tier-2 ring under
`l0.bind/clobber`, where each vector's runs come in pairs),
debug regions are POISON before each run, and allocations left POISON are
reported. A tier-1 clause runs under it.

`fresh`: a mutant's run, `poisoned`, with every cache of compiled kernels,
built programs, schedules, checker findings, relayout plans and uploaded
constants empty (in process and on disk), tinygrad's schedule, program and
runner caches and the KTPU seam's plans (`ktpugrad.plans.PLANS`) empty, and
every held card's vector cores holding no resident image -- a mutant's code
runs, and what it made is dropped afterwards. A DSL kernel object's own
trace cache (`Kernel._cache`) is kept.
"""

import importlib
import os
from collections import OrderedDict
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field

from tinygrad.engine import realize as TR
from tinygrad.helpers import Context

from harness.registry import patched
from kohakuaccel.driver.node.queue import client as QC
from kohakutpu.application.kernels import attention as KA
from kohakutpu.application.kernels import convert as KC
from kohakutpu.application.kernels import lanes as KL
from kohakutpu.application.kernels import matmul as KM
from kohakutpu.application.kernels import transpose as KT
from kohakutpu.application.kernels import vector as KV
from kohakutpu.application.ktpugrad import plans as KP
from kohakutpu.application.runtime import card as RC
from kohakutpu.application.runtime import forms as FM
from kohakutpu.application.runtime.arena import Arena
from kohakutpu.compiler import _rs
from kohakutpu.stack import kernel as K

#: The DSL kernel module (the package attribute `kernel` is a function).
DK = importlib.import_module("kohakutpu.language.dsl.kernel")
#: tinygrad's scheduler package (its `schedule_cache`).
TS = importlib.import_module("tinygrad.schedule")
#: tinygrad's in-process caches: schedules by graph, programs by kernel AST,
#: runners and launch sizes by program.
TINYGRAD_CACHES = (
    (TS, "schedule_cache"),
    (TR, "to_program_cache"),
    (TR, "runtime_cache"),
    (TR, "local_size_cache"),
    (TR, "first_run_cache"),
)
#: The byte unwritten card memory holds under `poisoned`: every f16 and f32
#: element NaN, every integer all ones.
POISON = 0xFF
#: Cards `poisoned` and `fresh` hold besides the session's (`track`).
CARDS: list = []
#: Pads `poisoned` cycles through: an arena's buffers sit at an offset none
#: of the previous PADS - 1 runs used (fewer in an arena too small for them,
#: `pad`), whatever pattern of vectors and mutants the runs follow.
PADS = 64
#: Vectors run under `poisoned` so far: the pad cycles by it.
_RUNS = [0]


def pad(arena, run: int) -> int:
    """The bytes `poisoned` holds below `arena`'s allocations on run `run`:
    one of `PADS` (at least 2) multiples of its alignment, using at most a
    sixteenth of its free bytes."""
    n = max(2, min(PADS, arena.free // (16 * arena.align)))
    return arena.align * (1 + run % n)


def track(card):
    """`card` held by `poisoned` and `fresh` from now on; returns it."""
    if all(c is not card for c in CARDS):
        CARDS.append(card)
    return card


def held(session) -> list:
    """Every card the harness holds: the session's first, then `CARDS`."""
    first = [session.dev.card] if session is not None else []
    return first + [c for c in CARDS if all(c is not f for f in first)]


@contextmanager
def environ(name: str, value: str):
    """``os.environ[name]`` set to `value` for the duration."""
    keep = os.environ.get(name)
    os.environ[name] = value
    try:
        yield
    finally:
        if keep is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = keep


@dataclass
class Poisoned:
    """What `poisoned` saw: ``(card index, address, bytes)`` of each
    poisoned allocation still all POISON when it was freed or at the end."""

    untouched: list = field(default_factory=list)


def poison_debug(card, node: int) -> None:
    """Node `node`'s debug region POISON past its header, except the
    records the host has not read (the firmware writes its header at ready
    and the per-unit counts at each run's start)."""
    base, size = card.debug_regions[node]
    head = card.mem.read_block(base, RC.DBG_LASTS)
    h = [int.from_bytes(head[i : i + 8], "little") for i in range(0, RC.DBG_LASTS, 8)]
    rec, cap, wr, rd = h[4], h[5], h[6], h[8]
    if not cap or rec + cap > size:
        return
    card.mem.write_block(base + RC.DBG_LASTS, bytes([POISON]) * (rec - RC.DBG_LASTS))
    for start, stop in outside(rd % cap, min(wr - rd, cap), cap):
        card.mem.write_block(base + rec + start, bytes([POISON]) * (stop - start))


def outside(lo: int, n: int, cap: int) -> list:
    """``[(start, stop)]``: a ring of `cap` bytes outside the `n` bytes
    from `lo`, wrapping."""
    hi = lo + n
    if hi <= cap:
        return [s for s in ((0, lo), (hi, cap)) if s[0] < s[1]]
    return [(hi - cap, lo)] if hi - cap < lo else []


@contextmanager
def poisoned(session):
    """Every held card's allocations start as POISON, its buffers land past
    a pad other than the previous vector's, and each debug region is
    POISON before each run (`poison_debug`); yields a `Poisoned`."""
    cards = held(session)
    seen = Poisoned()
    if not cards:
        yield seen
        return
    private = list(session.dev.private.values()) if session is not None else []
    owner = {}
    for k, card in enumerate(cards):
        for a in card.memory.arenas():
            owner[id(a)] = k
    for a in private:
        owner[id(a)] = 0
    keep_alloc, keep_top, keep_release = Arena.alloc, Arena.alloc_top, Arena.release
    keep_submit, keep_q_submit = RC.NodeCard.submit, QC.NodeQueue.submit
    live: dict = {}
    by_id = {id(c): c for c in cards}
    q_owner = {
        id(q): (card, n)
        for card in cards
        for n, q in getattr(card, "queues", {}).items()
    }

    def mark(self, at):
        k = owner.get(id(self))
        if k is not None:
            n = self.live[at]
            cards[k].mem.write_block(at, bytes([POISON]) * n)
            live[(id(self), at)] = (k, n)
        return at

    def alloc(self, nbytes, **kw):
        return mark(self, keep_alloc(self, nbytes, **kw))

    def alloc_top(self, nbytes, **kw):
        return mark(self, keep_top(self, nbytes, **kw))

    def check(self, at):
        k, n = live.pop((id(self), at))
        if cards[k].mem.read_block(at, n) == bytes([POISON]) * n:
            seen.untouched.append((k, hex(at), n))

    def release(self, at):
        if (id(self), at) in live:
            check(self, at)
        keep_release(self, at)

    def submit(self, program, *a, **kw):
        if id(self) in by_id and self.debug:
            nodes = program.keys() if isinstance(program, dict) else [None]
            for n in nodes:
                n = self.nodes[0] if n is None else n
                q = getattr(self, "queues", {}).get(n)
                if (
                    n in self.debug_regions
                    and not self._order.get(n)
                    and not isinstance(q, QC.NodeQueue)
                ):
                    poison_debug(self, n)
        return keep_submit(self, program, *a, **kw)

    def q_submit(self, package=None, *a, **kw):
        got = q_owner.get(id(self))
        if (
            got is not None
            and package is not None
            and got[0].debug
            and got[1] in got[0].debug_regions
            and not self._outstanding
        ):
            poison_debug(got[0], got[1])
        return keep_q_submit(self, package, *a, **kw)

    _RUNS[0] += 1
    pads = []
    for a in [*(a for c in cards for a in c.memory.arenas()), *private]:
        pads.append((a, keep_alloc(a, pad(a, _RUNS[0]))))
    try:
        with ExitStack() as es:
            es.enter_context(patched(Arena, "alloc", alloc))
            es.enter_context(patched(Arena, "alloc_top", alloc_top))
            es.enter_context(patched(Arena, "release", release))
            es.enter_context(patched(RC.NodeCard, "submit", submit))
            es.enter_context(patched(QC.NodeQueue, "submit", q_submit))
            yield seen
            arenas = {id(a): a for a, _ in pads}
            for aid, at in list(live):
                if aid in arenas:
                    check(arenas[aid], at)
    finally:
        for a, at in pads:
            keep_release(a, at)


@contextmanager
def emptied(container):
    """A dict or set emptied in place for the duration, its contents back
    after (other modules hold the same object)."""
    keep = container.copy()
    container.clear()
    try:
        yield
    finally:
        container.clear()
        container.update(keep)


def forget_residents(card) -> None:
    """The host's record of the vector cores' images emptied: the next run
    sends every image it uses again."""
    card.residents = {i: _rs.Cores(card.machine_json) for i in card.residents}
    card.resident = card.residents[card.nodes[0]]


@contextmanager
def local_trace_caches():
    """Scope each encountered kernel instance's memo, including imported kernels."""
    saved = {}
    original = DK.Kernel._trace

    def trace(kernel, *args, **kwargs):
        if kernel not in saved:
            saved[kernel] = kernel._cache
            kernel._cache = {}
        return original(kernel, *args, **kwargs)

    with patched(DK.Kernel, "_trace", trace):
        try:
            yield
        finally:
            for kernel, cache in saved.items():
                kernel._cache = cache


@contextmanager
def fresh(session):
    """Empty caches, no resident images on any held card and `poisoned`
    memory, for the duration; the uploaded constants made meanwhile are
    freed after. Yields `poisoned`'s `Poisoned`."""
    with ExitStack() as es:
        es.enter_context(patched(K, "_cache", OrderedDict()))
        es.enter_context(patched(K, "_PROGS", OrderedDict()))
        es.enter_context(patched(DK, "_TRACES", {}))
        es.enter_context(local_trace_caches())
        es.enter_context(patched(FM, "_plans", OrderedDict()))
        es.enter_context(patched(KL, "_plans", OrderedDict()))
        for mod in (KA, KC, KL, KM, KT, KV):
            es.enter_context(patched(mod, "_kernels", OrderedDict()))
        for mod, name in TINYGRAD_CACHES:
            es.enter_context(emptied(getattr(mod, name)))
        es.enter_context(emptied(KP.PLANS))
        es.enter_context(Context(SCACHE=0, CCACHE=0))
        for card in held(session):
            es.enter_context(patched(card, "checked", {}))
            forget_residents(card)
            es.callback(forget_residents, card)
        tables: dict = {}
        if session is not None:
            es.enter_context(patched(session.dev, "tables", tables))
        seen = es.enter_context(poisoned(session))
        try:
            yield seen
        finally:
            for st in tables.values():
                st.free()


__all__ = [
    "CARDS",
    "PADS",
    "POISON",
    "Poisoned",
    "environ",
    "forget_residents",
    "fresh",
    "held",
    "pad",
    "poison_debug",
    "poisoned",
    "track",
]
