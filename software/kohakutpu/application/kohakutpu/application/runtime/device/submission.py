"""Launches: admission, the submit path, collection."""

import json
import time
from contextlib import contextmanager
from dataclasses import replace

from kohakuaccel.driver.node.queue import NodeError
from kohakutpu.application.runtime import generated, launch_boundary
from kohakutpu.application.runtime.binding import (
    burst_line,
    burst_unaligned,
    cluster_burst,
    unaligned,
)
from kohakutpu.application.runtime.class_library import ClassLibrary
from kohakutpu.application.runtime.device.common import (
    KERNELS_KEPT,
    Parts,
    Run,
    Submitted,
)
from kohakutpu.application.runtime.eager_batch import Deferred
from kohakutpu.application.runtime.graph.access import bound_accesses, conflicts
from kohakutpu.application.runtime.graph.leases import launch_slots
from kohakutpu.application.runtime.memory.arena import OutOfMemory
from kohakutpu.application.runtime.packages import Prebuilt, placed
from kohakutpu.stack import rs
from kohakutpu.stack.buffer import Buffer
from kohakutpu.stack.kernel import arena_name


def _entry(s: Submitted) -> int:
    """The queue entry `s` holds: runs deferred into one eager list share it."""
    p = s.pending
    if isinstance(p, Deferred):
        return id(p.group if p.group is not None else p.owner)
    return id(s)


class Submission(Parts):
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
    def submission_scope(self, name: str, readonly=(), writable=()):
        """A tinygrad plan's generated runs share its name and access contract."""
        old = self._scope
        self._scope = (name, readonly, writable)
        try:
            yield
        finally:
            self._scope = old
            if old is None and self.eager_flush:
                for queue in getattr(self.card, "queues", {}).values():
                    queue.flush()

    def _write_refs(self, references):
        if self._scope is None:
            return [r for r in references if r not in self.constants]
        _, readonly, writable = self._scope
        refs = set()
        for addr in readonly() if callable(readonly) else readonly:
            entry, _ = self.mmu.resolve(addr)
            refs.add((entry.arena, entry.physical))
        for addr in writable() if callable(writable) else writable:
            entry, _ = self.mmu.resolve(addr)
            refs.discard((entry.arena, entry.physical))
        return [r for r in references if r not in refs and r not in self.constants]

    def _fence(self, nodes, references, writes, accesses=None, slots=()) -> None:
        """Collect conflicting runs not ordered by one common FIFO queue.

        A multi-node launch can move data between queues, even when the next
        launch uses the same grant. Scratch windows participate as writes.
        """
        reads, wrote = set(references), set(writes)
        slots = frozenset(slots)
        observed = False
        for pending in list(self.inflight):
            if len(nodes) == 1 and pending.nodes == nodes:
                continue
            conflict = (
                conflicts(accesses, pending.accesses)
                if accesses is not None and pending.accesses is not None
                else not wrote.isdisjoint(pending.references)
                or not reads.isdisjoint(pending.writes)
            )
            conflict |= not slots.isdisjoint(pending.info.get("slot_leases", ()))
            if not self.node_overlap or conflict:
                self._observe_one(pending)
                observed = True
        while observed and self.inflight and self.inflight[0].result is not None:
            self._collect_one(self.inflight[0])

    def _admit(self, nodes, references):
        """Bound each node's uncollected runs and the union of pinned spans."""
        queues = getattr(self.card, "queues", {})
        while True:
            held = set(references)
            for s in self.inflight:
                held.update(s.references)
            over_bytes = sum(a.live[at] for a, at in held) > self.queue_bytes
            over_nodes = any(
                len({_entry(s) for s in self.inflight if n in s.nodes})
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
        # Deferred members a launch reads go first, recorded first too.
        launch_boundary.before_submit(self)
        if self._recipe_recording is not None:
            return self._recipe_recording.on_submit(
                self._submitted, compiled, binds, kind, info
            )
        return self._submitted(compiled, binds, kind, info)

    def _submitted(
        self, compiled, binds: dict, kind: str, info, *, pinned=False
    ) -> Submitted:
        launch_boundary.before_submit(self)
        self._healthy()
        if self.closed:
            raise RuntimeError("device is closed")
        if self.max_inflight is not None and self.max_inflight < 1:
            raise ValueError("max_inflight must be positive or None")
        params = compiled.l1.params(compiled.name, "l1")
        full, scratch = dict(binds), []
        submitted_compiled = compiled
        references = self._retain(binds.values(), pinned=pinned)
        record_mark = len(self._record_pins)
        submitting = False
        publication = s = None
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
            line = burst_line(self.card.machine_json)
            if off := unaligned(full, line, compiled.line_ports):
                raise ValueError(
                    f"silicon flaw F-001: {compiled.name} binds {off} off a "
                    f"{line}-byte base, which L1.MOV.019's mover proof assumes"
                )
            if compiled.burst_ports:
                burst = cluster_burst(self.card.machine_json)
                if off := burst_unaligned(full, burst, compiled.burst_ports):
                    raise ValueError(
                        f"silicon flaw F-016: {compiled.name} binds {off} off a "
                        f"{burst}-byte burst within its 4 KiB page"
                    )
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
                    progs = compiled.programs(phys, self.home)
            else:
                with rs.compiler_owner(self):
                    progs = compiled.programs(full, self.home)
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
                    with rs.compiler_owner(self):
                        programs = candidate.programs(full, self.home)
                    if all(p.matches_environment(self.mmu) for p in programs.values()):
                        submitted_compiled = candidate
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
                    output = rs.library_get(
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
                submitted_compiled = exact
                with rs.compiler_owner(self):
                    programs = exact.programs(full, self.home)
                progs = {
                    n: replace(p, translations=self.mmu.translations_for(p.bindings))
                    for n, p in programs.items()
                }
                self.counters["class_hits"] -= 1
                self.counters["exact_hits"] += 1
            use_lower = self.resident_mode.stateless
            for program in progs.values():
                if not isinstance(program, Prebuilt) or program.recipe is not None:
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
            writes = self._write_refs(references)
            accesses = bound_accesses(self, submitted_compiled, full)
            slots = tuple(sorted(launch_slots(progs, self.card.nodes)))
            self._fence(nodes, references, writes, accesses, slots)
            self._admit(nodes, references)
            card = progs[None] if single else progs
            s = Submitted(
                compiled.name,
                kind,
                {
                    **(info or {}),
                    **getattr(compiled, "selection", {}),
                    "slot_leases": slots,
                },
                t0,
                references=references,
                writes=writes,
                nodes=nodes,
                accesses=accesses,
            )
            if self._scope is not None:
                s.info["plan"] = self._scope[0]
            if self._recipe_capture is not None:
                s.extents = self._extents(spans, windowed)
                self._recipe_capture(compiled, full, progs, s)
            publication = getattr(self.card, "publication_epoch", None)
            submitting = True
            if hasattr(self.card, "submit_launch"):
                s.pending = self.card.submit_launch(
                    card,
                    submitted_compiled,
                    lambda: {k: self.mmu.resolve(v)[1] for k, v in full.items()},
                )
                if getattr(s.pending, "completed", False):
                    # A terminal completion cannot leave device work running.
                    submitting = False
                    s.result = self.card.collect(s.pending)
            elif hasattr(self.card, "submit"):
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
            for at in scratch:
                self.free(at)
            for arena, at in references:
                arena.unretain(at)
            if not self.inflight:
                self._release_windows()
            raise
        self.inflight.append(s)
        if self.recording:
            self._record_accesses.append(accesses)
            self._record_templates.append((submitted_compiled, tuple(full.items())))
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

    def _observe_one(self, s):
        """Wait for a dependency without exposing success ahead of earlier faults."""
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
        return got

    def _collect_one(self, s: Submitted) -> Run:
        """Retire one observed completion, releasing only its own references."""
        if s.run is not None:
            return s.run
        if s.error is not None:
            raise s.error
        got = self._observe_one(s)
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
