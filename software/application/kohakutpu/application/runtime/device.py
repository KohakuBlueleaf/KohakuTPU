"""The device tensors live on and kernels run on: the Verilated card by
default (``KOHAKUTPU_DEVICE=sim``, its model build from
``KOHAKUTPU_SIM_BUILD``), or the card itself (``KOHAKUTPU_DEVICE=card``)
over ``KOHAKUTPU_TRANSPORT`` (jtag, the default, or xdma on a Linux host),
or nothing attached (``KOHAKUTPU_DEVICE=offline``: every run compiled,
checked and encoded, nothing executed). ``KOHAKUTPU_BOARD`` names the board
each opens.

    dev = runtime.device()              # opened once, shared
    a = dev.alloc(nbytes)               # an address in the card's DRAM
    dev.write(a, raw); dev.read(a, n)
    dev.run(compiled, {"x": a, ...})    # scratch in its space's window

Every run is checked by the static verifier before the card sees it (the
card's `run` schedules each package through `verify.schedule`).
"""

import json
import os
import pathlib
import tempfile
import time
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace

from kohakuaccel.driver.node.queue import NodeError
from kohakuaccel.driver.runtime.mmu import SoftwareMMU
from kohakutpu.application.runtime import generated, launch_boundary
from kohakutpu.application.runtime.arena import OutOfMemory
from kohakutpu.application.runtime.card import TRANSPORTS, SiliconCard, VerilatorCard
from kohakutpu.application.runtime.class_library import ClassLibrary
from kohakutpu.application.runtime.offline import OfflineCard
from kohakutpu.application.runtime.packages import Prebuilt, placed
from kohakutpu.driver.clock.card import load_board
from kohakutpu.language import machine as MF
from kohakutpu.language.dsl.kernel import _const_key
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs
from kohakutpu.stack.buffer import Buffer
from kohakutpu.stack.kernel import arena_name

ROOT = pathlib.Path(__file__).resolve().parents[5]
DEVICE_ENV = "KOHAKUTPU_DEVICE"
BUILD_ENV = "KOHAKUTPU_SIM_BUILD"
#: How many nodes (sysnodes sharing memory through the Xache) the card has.
NODES_ENV = "KOHAKUTPU_SIM_NODES"
#: The required board a device opens (boards/<name>.json).
BOARD_ENV = "KOHAKUTPU_BOARD"
#: `sim` the Verilated card, `card` the card itself, `offline` nothing
#: attached (every run compiled, checked and encoded; nothing executed).
KINDS = ("sim", "card", "offline")
#: How a `card` device reaches the card (`card.TRANSPORTS`).
TRANSPORT_ENV = "KOHAKUTPU_TRANSPORT"
#: The board clock profile a `card` device requires (`idle`: the board's idle one).
PROFILE_ENV = "KOHAKUTPU_PROFILE"

#: The card models' build root: fast models (no probes) with V2 vector cores
#: and DRAM sized by the named machine's card.dram.bytes.
SIM_ROOT = "v9g"


def default_build(nodes: int) -> pathlib.Path:
    return ROOT / "build" / SIM_ROOT / f"vlt_card_v9_{nodes}n_v2"


DEFAULT_BUILD = default_build(1)
#: Compatibility value for RUL.020 observers; no allocation adds a tail.
TAIL = 0


#: Optional extra host limit; hardware limits come from each queue's layout.
MAX_INFLIGHT = None
#: Compiled kernels a device keeps by graph text (`Device.kernel`).
KERNELS_KEPT = 4096
QUEUE_ENV = "KOHAKUTPU_QUEUE"
QUEUE_BYTES_ENV = "KOHAKUTPU_QUEUE_BYTES"
QUEUE_DEPTH_ENV = "KOHAKUTPU_QUEUE_DEPTH"
PREPARED_REPLAY_ENV = "KOHAKUTPU_PREPARED_REPLAY"
REPLAY_PACKAGE_BYTES_ENV = "KOHAKUTPU_REPLAY_PACKAGE_BYTES"


class Recorded(list):
    """A replay log owning references to every allocation its events use."""

    def __init__(self, events, references, owner, runs=()):
        super().__init__(events)
        self.references = references
        self.runs = list(runs)
        self.owner = owner
        self.closed = False
        self.prepared = None

    def close(self):
        if not self.closed:
            if self.prepared is not None:
                self.prepared.close()
                self.prepared = None
            for arena, at in self.references:
                arena.unretain(at)
            self.references = []
            self.closed = True

    def __del__(self):
        self.close()


@dataclass
class Submitted:
    """A kernel run queued on the card (`Device.submit`): the spans it
    references (its scratch windows among them), held until it is collected,
    and the card's pending run or result."""

    name: str
    kind: str
    info: dict
    t0: float
    pending: object = None
    result: dict | None = None
    run: "Run | None" = None
    references: list = field(default_factory=list)
    writes: list = field(default_factory=list)
    nodes: tuple = ()
    error: Exception | None = None


@dataclass
class Run:
    """One kernel run: its name, the card's cycles, the wall time."""

    name: str
    cycles: int
    wall: float
    kind: str = "dsl"
    info: dict = field(default_factory=dict)


class Device:
    """A KohakuTPU card of one or more nodes sharing memory: kernel runs,
    counters. Each argument left None comes from its environment variable."""

    def __init__(
        self,
        kind: str | None = None,
        build=None,
        log=None,
        nodes: int | None = None,
        board: str | None = None,
        transport: str | None = None,
        queued: bool | None = None,
        queue_bytes: int | None = None,
        library_only: bool = False,
        library=None,
        classes: bool | None = None,
        queue_depth: int | None = None,
    ) -> None:
        class_mode = os.environ.get("KOHAKUTPU_LIBRARY_CLASSES", "1")
        if classes is None and class_mode not in ("0", "1"):
            raise ValueError("KOHAKUTPU_LIBRARY_CLASSES must be 0 or 1")
        self.classes = class_mode == "1" if classes is None else classes
        mode = os.environ.get(QUEUE_ENV, "1")
        if queued is None and mode not in ("0", "1"):
            raise ValueError(f"{QUEUE_ENV} must be 0 or 1")
        self.queued = mode == "1" if queued is None else queued
        budget = (
            queue_bytes if queue_bytes is not None else os.environ.get(QUEUE_BYTES_ENV)
        )
        budget = None if budget is None else int(budget)
        if budget is not None and budget < 1:
            raise ValueError("queue_bytes must be positive")
        depth = (
            queue_depth if queue_depth is not None else os.environ.get(QUEUE_DEPTH_ENV)
        )
        depth = MAX_INFLIGHT if depth is None else int(depth)
        if depth is not None and depth < 1:
            raise ValueError("queue_depth must be positive or None")
        prepared = os.environ.get(PREPARED_REPLAY_ENV, "1")
        if prepared not in ("0", "1"):
            raise ValueError(f"{PREPARED_REPLAY_ENV} must be 0 or 1")
        self.prepared_replay = prepared == "1"
        self.replay_package_bytes = int(
            os.environ.get(REPLAY_PACKAGE_BYTES_ENV, str(16 << 20))
        )
        if self.replay_package_bytes < 1:
            raise ValueError("replay package byte budget must be positive")
        kind = kind or os.environ.get(DEVICE_ENV, "sim")
        if kind not in KINDS:
            raise ValueError(f"{DEVICE_ENV}={kind!r}: one of {', '.join(KINDS)}")
        board = board or os.environ.get(BOARD_ENV)
        if not board:
            raise ValueError("RUL.011: name a board with board= or KOHAKUTPU_BOARD")
        self.line_bytes = MF.load(board).get("card.dram.line_bytes")
        # The card itself runs on every node its board has.
        whole = load_board(board)["meshes"] if kind == "card" else 1
        nodes = int(nodes or os.environ.get(NODES_ENV) or whole)
        self.kind, self.nodes = kind, nodes
        if kind == "offline":
            self.build = None
            self.card = OfflineCard(board, nodes=tuple(range(nodes)))
        elif kind == "card":
            self.build = None
            self.card = SiliconCard(
                board,
                transport=transport or os.environ.get(TRANSPORT_ENV, "jtag"),
                nodes=tuple(range(nodes)),
                profile=os.environ.get(PROFILE_ENV, "idle"),
            )
        else:
            build = pathlib.Path(
                build or os.environ.get(BUILD_ENV) or default_build(nodes)
            )
            if not build.exists():
                raise FileNotFoundError(
                    f"no card model at {build}; build one (scripts/py/card_model.py) "
                    f"or set {BUILD_ENV}"
                )
            log = (
                log or pathlib.Path(tempfile.mkdtemp(prefix="ktpu_dev_")) / "model.log"
            )
            self.build = build
            self.card = VerilatorCard(
                build, board=board, nodes=tuple(range(nodes)), log=log
            )
        self.machine = self.card.machine
        #: The card's memory, which every allocation here comes from.
        self.memory = self.card.memory
        self.mmu = SoftwareMMU()
        self.closed = False
        self.dram = self.memory.dram
        #: Physical capacity, before queue/scratch reservations.
        self.dram_bytes = self.card.dram_bytes
        #: Compatibility view: recordings use only the main arenas.
        self.private: dict = {}
        self.recording = False
        self._record_pins: list = []
        self._record_runs: list = []
        self.runs: list = []
        #: Runs submitted and not yet collected, oldest first (`submit`).
        self.inflight: list = []
        self._uncertain: list = []
        #: The most runs held in flight (None: bounded by memory alone).
        self.max_inflight: int | None = depth
        capacity = sum(a.hi - a.lo for a in self.memory.arenas())
        self.queue_bytes = capacity if budget is None else budget
        self._scope = None
        self._placement = None
        #: (space, node) -> (address, bytes) of a scratch window (`_window`)
        self.windows: dict = {}
        self._failure = None
        self.library_only = library_only
        self.library = (
            str(library or ROOT / "build" / "templates")
            if library_only or library is not None
            else None
        )
        self.counters = {
            "runs": 0,
            "cycles": 0,
            "bytes_in": 0,
            "bytes_out": 0,
            "library_hits": 0,
            "class_hits": 0,
            "exact_hits": 0,
            "library_misses": 0,
            "compile_calls": 0,
            "compile_seconds": 0.0,
            "kernel_reuses": 0,
        }
        #: (graph text, mutant, classes) -> (Compiled, hit kind), newest last
        #: (`kernel`)
        self.kernels: OrderedDict = OrderedDict()
        self.specialisations: OrderedDict = OrderedDict()
        #: folded constants uploaded once, by content (`forms.table`)
        self.tables: dict = {}
        for queue in getattr(self.card, "queues", {}).values():
            queue.spill = lambda size: self._take(
                lambda: self.memory.alloc(size, owner="package")
            )

    def kernel(self, source, graph=None, traced=None):
        """The `Compiled` of graph `source`, kept by its text: a launch seen
        before reuses it (its bound programs included), so a repeated eager
        step reads no library entry and builds nothing. A library entry's
        reuse still counts as a library hit (class or exact)."""
        mutant = rs._MUTANT.get()
        key = self._kernel_key(source, mutant, self.classes)
        got = self.kernels.get(key)
        if got is not None:
            self.kernels.move_to_end(key)
            compiled, hit_kind = got
            self.counters["kernel_reuses"] += 1
            if hit_kind == "class":
                self.counters["class_hits"] += 1
                self.counters["library_hits"] += 1
            elif hit_kind == "exact":
                self.counters["exact_hits"] += 1
                self.counters["library_hits"] += 1
            elif hit_kind == "library":
                self.counters["library_hits"] += 1
            return compiled
        c0 = self.counters["class_hits"]
        e0 = self.counters["exact_hits"]
        l0 = self.counters["library_hits"]
        output = self.compile_graph(source)
        if self.counters["class_hits"] > c0:
            hit_kind = "class"
        elif self.counters["exact_hits"] > e0:
            hit_kind = "exact"
        elif self.counters["library_hits"] > l0:
            hit_kind = "library"
        else:
            hit_kind = None
        compiled = generated.from_output(
            self,
            json.loads(source) if graph is None else graph,
            output,
            traced,
        )
        self.kernels[key] = (compiled, hit_kind)
        while len(self.kernels) > KERNELS_KEPT:
            self.kernels.popitem(last=False)
        return compiled

    def _kernel_key(self, source, mutant, classes):
        return (
            source,
            mutant,
            classes,
            rs.machine_json(self.machine),
            self.library,
            self.library_only,
        )

    def invalidate_library(self):
        """After replacing entries in the current library, discard compiled views."""
        self.kernels.clear()
        self.specialisations.clear()

    def compile_graph(self, source):
        """Look for a certified class launch before the exact-graph fallback."""
        machine = rs.machine_json(self.machine)
        if self.library is not None:
            if self.classes:
                output = ClassLibrary(self.library, machine).lookup(source)
                if output is not None:
                    self.counters["class_hits"] += 1
                    self.counters["library_hits"] += 1
                    return output
            mutant = rs._MUTANT.get()
            output = (
                rs._rs.library_get(self.library, source, machine)
                if mutant is None
                else rs._rs.library_get(self.library, source, machine, mutant)
            )
            if output is not None:
                self.counters["exact_hits"] += 1
                self.counters["library_hits"] += 1
                return output
            self.counters["library_misses"] += 1
            key = json.loads(rs._rs.library_key(source, machine))
            if self.library_only:
                raise RuntimeError(
                    f"library miss: class {key['class']}, machine {key['machine']}"
                )
        with rs.compiler_owner(self):
            return rs.compile(source, json.loads(source)["name"], self.machine)

    def specialise(self, kernel, given, **consts):
        cached = os.environ.get("KTPU_SPECIALISE_CACHE", "1") == "1"
        key = (
            kernel,
            tuple(sorted(given.items())),
            tuple(
                (k, _const_key(v)) for k, v in sorted((kernel.consts | consts).items())
            ),
            self._kernel_key(None, rs._MUTANT.get(), self.classes),
        )
        if cached and key in self.specialisations:
            self.specialisations.move_to_end(key)
            compiled, hit_kind = self.specialisations[key]
            self.counters["kernel_reuses"] += 1
            if hit_kind is not None:
                self.counters["library_hits"] += 1
                self.counters[f"{hit_kind}_hits"] += 1
            return compiled
        before = (self.counters["class_hits"], self.counters["exact_hits"])
        if self.library is None:
            with rs.compiler_owner(self):
                compiled = K.specialise(kernel, given, machine=self.machine, **consts)
        else:
            traced = kernel.trace(given, machine=K.dsl_machine(self.machine), **consts)
            compiled = self.kernel(traced.graph_json, traced.graph, traced)
        if cached:
            hit_kind = (
                "class"
                if self.counters["class_hits"] > before[0]
                else "exact" if self.counters["exact_hits"] > before[1] else None
            )
            self.specialisations[key] = (compiled, hit_kind)
            while len(self.specialisations) > KERNELS_KEPT:
                self.specialisations.popitem(last=False)
        return compiled

    # ------------------------------------------------------------ memory
    def alloc(
        self,
        nbytes: int,
        space: str = "dram",
        node: int | None = None,
        owner: str = "tensor",
    ) -> int:
        if self._placement is not None:
            pool, site, cursors = self._placement
            return pool.alloc(site, cursors, nbytes, space, node, owner)
        return self._alloc(nbytes, space, node, owner)

    def _alloc(self, nbytes, space, node, owner):
        line = self.line_bytes
        extent = max(line, (int(nbytes) + line - 1) // line * line)
        if space == "stage":
            n = self.card.nodes[0] if node is None else node
            arena = self.memory.staging[n]
            physical = self._take(lambda: self.memory.stage(extent, n, owner=owner))
        else:
            arena = self.dram
            physical = self._take(lambda: self.memory.alloc(extent, owner=owner))
        arena.requested[physical] = int(nbytes)
        try:
            return self.mmu.register(physical, extent, arena)
        except BaseException:
            arena.release(physical)
            raise

    def _window(self, key: tuple, nbytes: int) -> int:
        """The scratch window of `key`, `(space, node)`, at least `nbytes`:
        where every kernel run lays out its scratch in that space, a node's
        staging store each, DRAM one (`node` None). A node runs its queue in
        order and `_fence` keeps the runs in flight on one node, a run over
        several alone, so no two runs' scratch is live at once, eager or
        replayed, and a graph pins one window a space, not a span a site. A window too small is freed to
        whatever still pins it (runs in flight, recordings)."""
        held = self.windows.get(key)
        if held is not None and held[1] >= nbytes:
            return held[0]
        if held is not None:
            self.free(held[0])
        space, node = key
        at = self._alloc(nbytes, space, node, f"scratch:window:{space}:{node}")
        self.windows[key] = (at, nbytes)
        return at

    def _release_windows(self) -> None:
        """Every scratch window no recording pins freed, once no run is in
        flight (API.MEM.007: a collected run's scratch is not kept)."""
        for key, (at, _) in list(self.windows.items()):
            entry, physical = self.mmu.resolve(at, owner=True)
            if not entry.arena.references[physical]:
                self.free(at)
                del self.windows[key]

    def _windowed(self, spans) -> dict:
        """Each scratch span of one run, `(name, bytes, space, node,
        members)`, at its address in its window: laid out from the window's
        start in order, each at a whole line."""
        line = max(self.line_bytes, 256)
        offsets, ends = {}, {}
        for name, nbytes, space, node, _ in spans:
            if space == "stage":
                key = (space, self.card.nodes[0] if node is None else node)
            else:
                key = (space, None)
            offsets[name] = (key, ends.get(key, 0))
            ends[key] = offsets[name][1] + -(-int(nbytes) // line) * line
        bases = {key: self._window(key, end) for key, end in ends.items()}
        return {name: bases[key] + off for name, (key, off) in offsets.items()}

    def _take(self, alloc) -> int:
        """Retry after each oldest completion, then evict unused packages."""
        while True:
            try:
                return alloc()
            except OutOfMemory:
                if self.inflight:
                    self.collect(self.inflight[0])
                    continue
                self.card.drop_spilled()
                return alloc()

    def _retain(self, addresses) -> list:
        held = []
        try:
            for addr in set(addresses):
                entry, _ = self.mmu.resolve(addr, owner=True)
                arena, at = entry.arena, entry.physical
                if (arena, at) not in held:
                    arena.retain(at)
                    held.append((arena, at))
        except BaseException:
            for arena, at in held:
                arena.unretain(at)
            raise
        return held

    def _pin_record(self, addresses):
        if self.recording:
            for ref in self._retain(addresses):
                if ref in self._record_pins:
                    ref[0].unretain(ref[1])
                else:
                    self._record_pins.append(ref)

    def free(self, addr: int) -> None:
        if self.closed:
            return
        entry, physical = self.mmu.resolve(addr, owner=True)
        if physical != entry.physical:
            raise ValueError("RUL.023: only an allocation's base can be freed")
        entry.arena.release(physical)
        self.mmu.retire(addr)

    def write(self, addr: int, raw: bytes) -> None:
        self.wait_address(addr, write=True)
        self._pin_record([addr])
        raw = bytes(raw)
        # Whole host lines: the memory endpoint fills a short line's rest
        # (a JTAG write), or the host reads it to merge (an XDMA write).
        pad = -len(raw) % self.card.line
        entry, physical = self.mmu.resolve(addr, len(raw))
        if (
            physical - entry.physical + len(raw) + pad
            > entry.arena.live[entry.physical]
        ):
            raise ValueError("host write line exceeds allocation reservation")
        self.card.write(physical, raw + bytes(pad))
        self.counters["bytes_in"] += len(raw)

    def read(self, addr: int, nbytes: int) -> bytes:
        self.wait_address(addr)
        self._pin_record([addr])
        n = -(-int(nbytes) // self.card.line) * self.card.line
        self.counters["bytes_out"] += nbytes
        entry, physical = self.mmu.resolve(addr, nbytes)
        if physical - entry.physical + n > entry.arena.live[entry.physical]:
            raise ValueError("host read line exceeds allocation reservation")
        return bytes(self.card.get(physical, n))[:nbytes]

    def _healthy(self):
        if self._uncertain:
            raise RuntimeError(
                "a submission failed after publication began; close the device"
            )
        if self._failure is not None:
            raise RuntimeError(
                f"queued results discarded after {self._failure}; close the device"
            ) from self._failure

    def wait_address(self, addr: int, write: bool = False):
        """Host access waits through the last conflicting allocation use."""
        self._healthy()
        entry, _ = self.mmu.resolve(addr)
        ref = (entry.arena, entry.physical)
        conflicts = [
            s for s in self.inflight if ref in (s.references if write else s.writes)
        ]
        if conflicts:
            self.collect_through(conflicts[-1])

    def collect_through(self, target):
        """Preserve submission order when a host dependency needs a result."""
        if target.run is not None:
            return target.run
        for pending in list(self.inflight):
            self.collect(pending)
            if pending is target:
                break
        return self.collect(target)

    @contextmanager
    def placement_scope(self, pool, site):
        """One graph call site's allocation order, independent of other sites."""
        old = self._placement
        self._placement = None if pool is None else (pool, site, {})
        try:
            yield
        finally:
            self._placement = old

    @contextmanager
    def submission_scope(self, name: str, readonly=(), writable=()):
        """A tinygrad plan's generated runs share its name and access contract."""
        old = self._scope
        self._scope = (name, readonly, writable)
        try:
            yield
        finally:
            self._scope = old
            if old is None and os.environ.get("KOHAKUTPU_EAGER_FLUSH", "1") == "1":
                for queue in getattr(self.card, "queues", {}).values():
                    queue.flush()

    def _write_refs(self, references):
        if self._scope is None:
            return list(references)
        _, readonly, writable = self._scope
        refs = set()
        for addr in readonly() if callable(readonly) else readonly:
            entry, _ = self.mmu.resolve(addr)
            refs.add((entry.arena, entry.physical))
        for addr in writable() if callable(writable) else writable:
            entry, _ = self.mmu.resolve(addr)
            refs.discard((entry.arena, entry.physical))
        return [r for r in references if r not in refs]

    def _fence(self, nodes) -> None:
        """Every run in flight collected before a run on `nodes` is queued,
        where it is a run over more than one node or the runs in flight are
        on other nodes: one node's queue orders its runs, and nothing orders
        two nodes' queues. So the runs in flight are always on one node, in
        that node's order, and a run over several nodes runs alone; the
        scratch windows rest on this (`_window`)."""
        if len(nodes) > 1 or any(s.nodes != nodes for s in self.inflight):
            self.drain()

    def _admit(self, nodes, references):
        """Bound each node's uncollected runs and the union of pinned spans."""
        queues = getattr(self.card, "queues", {})
        while True:
            held = set(references)
            for s in self.inflight:
                held.update(s.references)
            over_bytes = sum(a.live[at] for a, at in held) > self.queue_bytes
            over_nodes = any(
                sum(n in s.nodes for s in self.inflight)
                >= min(queues[n].sq_n, queues[n].cq_n)
                for n in nodes
                if n in queues
            )
            if not over_bytes and not over_nodes:
                return
            if not self.inflight:
                raise OutOfMemory("one run exceeds the queued pinned-byte budget")
            self.collect(self.inflight[0])

    # --------------------------------------------------------------- run
    def run(
        self, compiled, binds: dict, kind: str = "dsl", info=None
    ) -> Run | Submitted:
        """Submit in a queued plan scope; otherwise submit and collect."""
        submitted = self.submit(compiled, binds, kind, info)
        if self._scope is not None and self.queued and submitted.result is None:
            return submitted
        return self.collect_through(submitted)

    def submit(self, compiled, binds: dict, kind: str = "dsl", info=None) -> Submitted:
        """`compiled` queued on the card with each parameter at the address
        in `binds`, not waited for: the card runs its queue in order, so the
        next kernel is queued while this one runs (L0.NQ.047). The L1 body's
        other parameters (scratch the L2 pipeline kept) are laid out in the
        scratch window of their space (`_window`), held until :meth:`collect`:
        one block per scratch arena the Rust stack laid out
        (`Compiled.scratch`) and not bound in `binds`, bound under the
        arena's name and each member at its offset, else one per parameter.
        At most `max_inflight` runs
        are held; past that the oldest is collected first, and when memory
        pressure collects the oldest and retries after each completion."""
        launch_boundary.before_submit(self)
        self._healthy()
        if self.closed:
            raise RuntimeError("device is closed")
        if self.max_inflight is not None and self.max_inflight < 1:
            raise ValueError("max_inflight must be positive or None")
        params = compiled.l1.params(compiled.name, "l1")
        full = dict(binds)
        references = self._retain(binds.values())
        record_mark = len(self._record_pins)
        submitting = False
        try:
            while (
                self.max_inflight is not None
                and len(self.inflight) >= self.max_inflight
            ):
                self.collect(self.inflight[0])
            arenas = getattr(compiled, "scratch", None) or ()
            named = set(full)
            for a in arenas:
                named.add(arena_name(a))
                named.update(p for p, _ in a.get("members", ()))
            # An arena the caller bound stays where it was bound.
            spans = [
                (
                    arena_name(a),
                    a["bytes"],
                    a["space"],
                    a.get("node"),
                    a.get("members", ()),
                )
                for a in arenas
                if arena_name(a) not in full
            ]
            for p, t in params:
                if p not in named:
                    b = Buffer.of(t, self.card.machine_json)
                    spans.append((p, b.nbytes, b.space, b.layout.get("node"), ()))
            windowed = self._windowed(spans)
            full.update(windowed)
            for a in arenas:
                for p, off in a.get("members", ()):
                    full.setdefault(p, full[arena_name(a)] + off)
            references.extend(self._retain(windowed.values()))
            self._pin_record(full.values())
            t0 = time.monotonic()
            apart = any(
                arena_name(a) not in full
                for a in getattr(compiled, "scratch", None) or ()
            )
            if getattr(compiled, "bound", None) is not None or apart:
                phys = {k: self.mmu.resolve(v)[1] for k, v in full.items()}
                with rs.compiler_owner(self):
                    progs = compiled.programs(phys)
            else:
                with rs.compiler_owner(self):
                    progs = compiled.programs(full)
                progs = {
                    n: (
                        replace(p, translations=self.mmu.translations_for(p.bindings))
                        if isinstance(p, Prebuilt)
                        else p
                    )
                    for n, p in progs.items()
                }
            if any(
                isinstance(p, Prebuilt) and not p.matches_environment(self.mmu)
                for p in progs.values()
            ):
                source = compiled.traced.graph_json
                library = ClassLibrary(self.library, rs.machine_json(self.machine))
                variant = 0
                while (output := library.lookup(source, variant=variant)) is not None:
                    variant += 1
                    if json.dumps(output["scratch"], sort_keys=True) != json.dumps(
                        compiled.scratch, sort_keys=True
                    ):
                        continue
                    candidate = generated.from_output(self, json.loads(source), output)
                    programs = candidate.programs(full)
                    if all(p.matches_environment(self.mmu) for p in programs.values()):
                        progs = {
                            n: replace(
                                p, translations=self.mmu.translations_for(p.bindings)
                            )
                            for n, p in programs.items()
                        }
                        break
            if any(
                isinstance(p, Prebuilt) and not p.matches_environment(self.mmu)
                for p in progs.values()
            ):
                # Class members retain the exact launch's allocation metadata.
                source = compiled.traced.graph_json
                exact_key = self._kernel_key(source, rs._MUTANT.get(), False)
                cached = self.kernels.get(exact_key)
                exact = cached[0] if cached is not None else None
                if exact is None:
                    output = rs._rs.library_get(
                        self.library,
                        source,
                        rs.machine_json(self.machine),
                        rs._MUTANT.get(),
                    )
                    if output is None:
                        raise RuntimeError(
                            "library miss: exact alias-environment fallback"
                        )
                    if output["bound"] is not None or json.dumps(
                        output["scratch"], sort_keys=True
                    ) != json.dumps(compiled.scratch, sort_keys=True):
                        raise ValueError(
                            "exact fallback allocation metadata differs from class member"
                        )
                    exact = generated.from_output(self, json.loads(source), output)
                    self.kernels[exact_key] = (exact, "exact")
                    while len(self.kernels) > KERNELS_KEPT:
                        self.kernels.popitem(last=False)
                progs = {
                    n: replace(p, translations=self.mmu.translations_for(p.bindings))
                    for n, p in exact.programs(full).items()
                }
                self.counters["class_hits"] -= 1
                self.counters["exact_hits"] += 1
            use_lower = os.environ.get("KOHAKUTPU_RESIDENT", "0") == "0"
            for program in progs.values():
                if not isinstance(program, Prebuilt):
                    continue
                try:
                    program.bound(use_lower)
                except Exception as error:
                    error.add_note(f"kernel {compiled.name}; bindings {full}")
                    raise
            single = list(progs) == [None]
            nodes = (
                tuple(placed(progs, self.card.nodes))
                if hasattr(self.card, "nodes")
                else tuple(progs)
            )
            self._fence(nodes)
            self._admit(nodes, references)
            card = progs[None] if single else progs
            s = Submitted(
                compiled.name,
                kind,
                dict(info or {}),
                t0,
                references=references,
                writes=self._write_refs(references),
                nodes=nodes,
            )
            if self._scope is not None:
                s.info["plan"] = self._scope[0]
            publication = getattr(self.card, "publication_epoch", None)
            submitting = True
            if hasattr(self.card, "submit"):
                s.pending = self.card.submit(card)
            else:
                s.result = self.card.run(card)
        except BaseException:
            if submitting and (
                publication is None or self.card.publication_epoch != publication
            ):
                # Any attempted queue publication can leave a node running.
                self._uncertain.append(s)
                raise
            for arena, at in self._record_pins[record_mark:]:
                arena.unretain(at)
            del self._record_pins[record_mark:]
            for arena, at in references:
                arena.unretain(at)
            if not self.inflight:
                self._release_windows()
            raise
        self.inflight.append(s)
        if self.recording:
            self._record_runs.append(
                (
                    s.name,
                    s.kind,
                    dict(s.info),
                    list(s.references),
                    list(s.writes),
                    s.nodes,
                )
            )
        return s

    def collect(self, s: Submitted) -> Run:
        """Collect through `s` in submission order; never skip an earlier fault."""
        if s.run is None and s.error is None:
            for earlier in list(self.inflight):
                if earlier is s:
                    break
                self._collect_one(earlier)
        return self._collect_one(s)

    def _collect_one(self, s: Submitted) -> Run:
        """Retire one observed completion, releasing only its own references."""
        if s.run is not None:
            return s.run
        if s.error is not None:
            raise s.error
        self._healthy()
        try:
            got = s.result if s.result is not None else self.card.collect(s.pending)
        except NodeError as error:
            label = f"kernel {s.info.get('plan', s.name)}; run {s.name}"
            error.args = (f"{label}: {error}",)
            s.error = self._failure = error
            for later in self.inflight[self.inflight.index(s) + 1 :]:
                later.error = RuntimeError(
                    f"kernel {later.info.get('plan', later.name)}; run {later.name}: "
                    f"result discarded after {label}: {error.completion.describe()}"
                )
            raise
        s.result = got
        if s in self.inflight:
            self.inflight.remove(s)
        for arena, at in s.references:
            arena.unretain(at)
        s.references = []
        if not self.inflight and not self.recording:
            self._release_windows()
        wall = time.monotonic() - s.t0
        s.run = Run(s.name, int(got["cycles"]), wall, s.kind, s.info)
        self.runs.append(s.run)
        self.counters["runs"] += 1
        self.counters["cycles"] += s.run.cycles
        return s.run

    def drain(self) -> list:
        """Every run in flight collected, oldest first: their runs."""
        return [self.collect(s) for s in list(self.inflight)]

    # ----------------------------------------------------- record / replay
    def record(self) -> None:
        """Record on the main heap, pinning the spans each event uses."""
        if self.recording:
            raise RuntimeError("a recording is already active")
        self.card.record()
        self.recording = True
        self._record_pins = []
        self._record_runs = []

    def recorded(self) -> Recorded:
        """Finish recording; the returned log owns its pins until closed."""
        if not self.recording:
            raise RuntimeError("no recording is active")
        log = Recorded(self.card.recorded(), self._record_pins, self, self._record_runs)
        self._record_pins = []
        self._record_runs = []
        self.recording = False
        return log

    def private_state(self) -> tuple:
        """Compatibility state; replay lifetimes belong to the log's pins."""
        return tuple(
            (k, tuple(a.free_list), tuple(sorted(a.live.items())))
            for k, a in sorted(self.private.items(), key=str)
        )

    def replay(self, log: list, wait: bool = True) -> list:
        """`log` run again, nothing recompiled or rebuilt: one `Run` a
        package run, its node cycles (no wall time of its own)."""
        if not isinstance(log, Recorded) or log.closed or log.owner is not self:
            raise ValueError("replay requires an open, pinned recording")
        self._healthy()
        if self.closed:
            raise RuntimeError("device is closed")
        if hasattr(self.card, "submit_recorded"):
            pending = self._replay_queued(log)
            return [self.collect_through(s) for s in pending] if wait else pending
        self.drain()
        references = list(log.references)
        for arena, at in references:
            arena.retain_pinned(at)
        publication = getattr(self.card, "publication_epoch", None)
        try:
            cycles = self.card.replay(log)
        except BaseException:
            if publication is None or self.card.publication_epoch != publication:
                self._uncertain.append(
                    Submitted(
                        "replay",
                        "replay",
                        {},
                        time.monotonic(),
                        references=references,
                    )
                )
            else:
                for arena, at in references:
                    arena.unretain(at)
            raise
        for arena, at in references:
            arena.unretain(at)
        out = []
        for c in cycles:
            r = Run("replay", int(c), 0.0, "replay")
            self.runs.append(r)
            self.counters["runs"] += 1
            self.counters["cycles"] += r.cycles
            out.append(r)
        return out

    def prepare_recorded(self, log):
        """Keep a recording's dispatch wrappers and uploads, when selected."""
        if not isinstance(log, Recorded) or log.closed or log.owner is not self:
            raise ValueError("preparation requires an open, pinned recording")
        if (
            self.prepared_replay
            and hasattr(self.card, "prepare_recorded")
            and log.prepared is None
        ):
            log.prepared = self.card.prepare_recorded(log, self.replay_package_bytes)

    def _replay_queued(self, log):
        if any(ev[0] == "read" for ev in log):
            raise ValueError("a log that reads the card cannot be replayed")
        self.prepare_recorded(log)
        prepared = log.prepared if self.prepared_replay else None
        if prepared is not None and not prepared.matches(log):
            raise ValueError("prepared recording was changed or closed")
        submit = (
            self.card.submit_recorded if prepared is None else self.card.submit_prepared
        )
        metadata = iter(log.runs)
        out = []
        for ev in log if prepared is None else prepared:
            if ev[0] == "write":
                # Log addresses are physical; the recording owns their spans.
                refs = [
                    (a, at) for a, at in log.references if at <= ev[1] < at + a.live[at]
                ]
                conflicts = [
                    s for s in self.inflight if any(r in s.references for r in refs)
                ]
                if conflicts:
                    self.collect_through(conflicts[-1])
                self.card.write(ev[1], ev[2])
                continue
            name, kind, info, refs, writes, nodes = next(metadata)
            self._fence(nodes)
            while (
                self.max_inflight is not None
                and len(self.inflight) >= self.max_inflight
            ):
                self.collect(self.inflight[0])
            self._admit(nodes, refs)
            for arena, at in refs:
                arena.retain_pinned(at)
            s = Submitted(
                name,
                kind,
                dict(info),
                time.monotonic(),
                references=list(refs),
                writes=list(writes),
                nodes=nodes,
            )
            publication = getattr(self.card, "publication_epoch", None)
            try:
                s.pending = submit(ev[1])
            except BaseException:
                if publication is None or self.card.publication_epoch != publication:
                    self._uncertain.append(s)
                else:
                    for arena, at in s.references:
                        arena.unretain(at)
                raise
            self.inflight.append(s)
            out.append(s)
        for queue in getattr(self.card, "queues", {}).values():
            if hasattr(queue, "flush"):
                queue.flush()
        return out

    def close(self) -> dict:
        if self.closed:
            return {}
        try:
            self.drain()
        finally:
            # No reservation is revoked until card shutdown succeeds.
            result = self.card.close()
            for queue in getattr(self.card, "queues", {}).values():
                if hasattr(queue, "discard_packages"):
                    queue.discard_packages()
            self.closed = True
            for arena in self.memory.arenas():
                arena.close()
            self.inflight.clear()
            self._uncertain.clear()
            self.mmu.entries.clear()
        return result

    def __repr__(self) -> str:
        return (
            f"Device({self.kind}: {self.nodes} node(s), {self.machine.count('MG')} MG, "
            f"{self.machine.count('VC')} VC, {self.dram.used:,} B used)"
        )


_device: Device | None = None


def device() -> Device:
    """The process's device, opened the first time it is asked."""
    global _device
    if _device is None:
        _device = Device()
    return _device


def attach(dev: Device) -> Device:
    """Make `dev` the process's device."""
    global _device
    _device = dev
    return dev


@contextmanager
def attached(dev: Device | None = None) -> Iterator[Device | None]:
    """`dev` the process's device while the block runs (None: whichever it
    is), and the process's device as it was once the block ends, however it
    ends or whatever the block attached."""
    global _device
    keep = _device
    try:
        if dev is not None:
            _device = dev
        yield _device
    finally:
        _device = keep


__all__ = [
    "BOARD_ENV",
    "BUILD_ENV",
    "DEVICE_ENV",
    "MAX_INFLIGHT",
    "NODES_ENV",
    "TRANSPORTS",
    "TRANSPORT_ENV",
    "Device",
    "Run",
    "Submitted",
    "arena_name",
    "attach",
    "attached",
    "default_build",
    "device",
]
