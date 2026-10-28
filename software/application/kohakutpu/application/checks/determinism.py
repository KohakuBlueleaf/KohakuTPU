"""Bounded byte-exact repetition at schedule and runtime submission boundaries.

This is a determinism probe, not an accuracy oracle. Readbacks serialize execution
and can hide concurrency races; a pass never proves the uninstrumented run safe.
"""

import hashlib
import re
from contextlib import ExitStack
from unittest.mock import patch

import numpy as np
from tinygrad.uop import Ops

from kohakuaccel.driver.node.queue.client import FirmwareFatal, NodeError, OverBudget
from kohakutpu.application.checks.kernels import op_summary
from kohakutpu.application.ktpugrad import device as KD
from kohakutpu.application.ktpugrad import fold, lower, run
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import forms as FM
from kohakutpu.application.runtime.storage import FLAT
from kohakutpu.stack.buffer import Buffer
from kohakutpu.stack.kernel import arena_name

DEVICE_ERRORS = (NodeError, OverBudget, FirmwareFatal)


class RepeatComplete(Exception):
    """The requested single package was measured; do not run later packages."""


def byte_difference(reference, observed, dtype="u1", limit=8):
    """Compare bits, including signed zeros/NaN payloads; indices are physical."""
    if len(reference) != len(observed):
        raise ValueError("repeat output extent changed")
    dt = np.dtype(dtype).newbyteorder("<")
    if len(reference) % dt.itemsize:
        raise ValueError("partial output element")
    a = np.frombuffer(reference, np.uint8).reshape(-1, dt.itemsize)
    b = np.frombuffer(observed, np.uint8).reshape(-1, dt.itemsize)
    indices = np.flatnonzero(np.any(a != b, axis=1))
    av, bv = np.frombuffer(reference, dt), np.frombuffer(observed, dt)
    return dict(
        identical=not len(indices),
        elements=len(a),
        differing_elements=len(indices),
        dtype=dt.str,
        first_differences=[
            dict(
                index=int(i),
                reference_bits=bytes(a[i]).hex(),
                observed_bits=bytes(b[i]).hex(),
                reference_value=str(av[i]),
                observed_value=str(bv[i]),
            )
            for i in indices[:limit]
        ],
    )


class Observations:
    """Bounded retained data: one baseline and a digest/count per distinct output."""

    def __init__(self, row):
        self.row, self.baseline, self.variants = row, None, {}
        row.update(
            attempts=0,
            completed=0,
            stalls=0,
            node_errors=0,
            stopped_on_error=False,
            identical=None,
            distinct_outputs=0,
            variant_counts={},
            comparisons=[],
        )

    def add(self, outputs, repeat):
        if self.baseline is None:
            self.baseline = outputs
        if list(outputs) != list(self.baseline):
            raise ValueError("repeat output bindings changed")
        digest = hashlib.sha256()
        diffs = []
        for name, (raw, dtype) in outputs.items():
            digest.update(len(raw).to_bytes(8, "little"))
            digest.update(raw)
            diff = byte_difference(self.baseline[name][0], raw, dtype)
            if not diff["identical"]:
                diffs.append(dict(output=name, **diff))
        key = digest.hexdigest()
        self.variants[key] = self.variants.get(key, 0) + 1
        self.row.update(
            completed=self.row["completed"] + 1,
            identical=(self.row["identical"] is not False and not diffs),
            distinct_outputs=len(self.variants),
            variant_counts=dict(self.variants),
        )
        # Bound JSON size in long stall-rate loops, but never truncate counts.
        if diffs and len(self.row["comparisons"]) < 16:
            self.row["comparisons"].append(dict(repeat=repeat, outputs=diffs))
        self.row["differing_repeats"] = self.row["completed"] - next(
            iter(self.variants.values())
        )
        self.row["max_differing_elements"] = max(
            self.row.get("max_differing_elements", 0),
            sum(d["differing_elements"] for d in diffs),
        )

    def error(self, error, repeat):
        node = isinstance(error, NodeError)
        # Node timeout status 0x30 carries the engine/blocking diagnostic.
        status = getattr(getattr(error, "completion", None), "status", None)
        stall = node and status == 0x30
        self.row.update(
            stopped_on_error=True,
            identical=False,
            node_errors=int(node),
            stalls=int(stall),
            error=dict(
                repeat=repeat,
                type=type(error).__name__,
                message=str(error),
                completion_status=status,
            ),
            stall_rate_observed=int(stall) / max(1, self.row["attempts"]),
        )


class StorageSnapshot:
    """All existing forms and metadata, including aliases and in-place outputs."""

    def __init__(self, device, buffers):
        self.device = device
        self.states = []
        seen = set()
        for buffer in buffers.values():
            st, _ = FM.base_of(buffer)
            if id(st) in seen:
                continue
            seen.add(id(st))
            self.states.append(
                (
                    st,
                    {k: self._form_bytes(f) for k, f in st.forms.items()},
                    set(st.valid),
                    st.host,
                    st.dtype,
                    set(st.zeroed),
                    st.site,
                )
            )

    def _form_bytes(self, form):
        entry, _ = self.device.mmu.resolve(form.addr, form.nbytes)
        return form.nbytes, self.device.read(entry.virtual, entry.extent)

    def restore(self):
        for st, forms, valid, host, dtype, zeroed, site in self.states:
            # Recreate destinations allocated/grown by the previous execution.
            # Selected-schedule allocations are zero-initialized on EVERY trial.
            for key, form in list(st.forms.items()):
                if key not in forms or form.nbytes != forms[key][0]:
                    if form.owned:
                        self.device.free(form.addr)
                    elif key in forms:
                        raise ValueError("non-owned snapshot form changed extent")
                    del st.forms[key]
            for key, (nbytes, raw) in forms.items():
                form = st.form(key, nbytes)
                entry, _ = self.device.mmu.resolve(form.addr, nbytes)
                if len(raw) != entry.extent:
                    raise ValueError("snapshot allocation extent changed")
                self.device.write(entry.virtual, raw)
            st.valid, st.host, st.dtype, st.zeroed, st.site = (
                set(valid),
                host,
                dtype,
                set(zeroed),
                site,
            )


class DeterminismChecker:
    """Repeat scheduled ASTs K times; independently repeat their first-pass packages."""

    def __init__(self, device, repeats, kernel_filter=None, package_prefix=None):
        if repeats < 2:
            raise ValueError("repeat count must be at least two")
        self.device, self.repeats, self.package_prefix = device, repeats, package_prefix
        self.filter = re.compile(kernel_filter) if kernel_filter else None
        self.active, self.seen, self.graph_replays = False, 0, 0
        self.rows, self.packages, self.asts = [], [], {}
        self.stack = ExitStack()
        self.current, self.observe_packages, self.package_found = None, False, False
        self.initialize_allocations = False

    def __enter__(self):
        original_plan, original_execute = lower.plan, run.execute
        original_graph, original_submit = KD.KTPUGraph.__call__, D.Device.submit
        original_alloc = D.Device.alloc

        def alloc(device, nbytes, space="dram", node=None, owner="tensor"):
            at = original_alloc(device, nbytes, space, node, owner)
            if device is self.device and self.initialize_allocations:
                entry, _ = device.mmu.resolve(at)
                device.write(at, bytes(entry.extent))
            return at

        def plan(ast):
            p = original_plan(ast)
            self.asts[id(p)] = ast
            p._check_original_ast = ast
            return p

        def submit(device, compiled, bindings, kind="dsl", info=None):
            if device is not self.device or not self.observe_packages:
                return original_submit(device, compiled, bindings, kind=kind, info=info)
            if self.package_prefix and not compiled.name.startswith(
                self.package_prefix
            ):
                return original_submit(device, compiled, bindings, kind=kind, info=info)
            self.package_found = True
            row = dict(
                schedule_index=self.current,
                name=compiled.name,
                kind=kind,
                requested=self.repeats,
                boundary="Device.submit compiled package group",
                output_scope="all bound buffers, including input-only ports and padding; scratch compared as bytes",
            )
            self.packages.append(row)
            if device.kind == "offline":
                row.update(
                    identical=None,
                    measured=False,
                    reason="offline shape only",
                    attempts=1,
                )
                result = original_submit(
                    device, compiled, bindings, kind=kind, info=info
                )
                if self.package_prefix:
                    raise RepeatComplete()
                return result
            obs = Observations(row)
            binds, allocated = dict(bindings), []
            try:
                device.drain()
                # Make hidden scratch explicit, stable and restorable across repeats.
                for arena in compiled.scratch or ():
                    key = arena_name(arena)
                    if key not in binds:
                        at = device.alloc(
                            arena["bytes"], space=arena["space"], node=arena.get("node")
                        )
                        binds[key] = at
                        allocated.append(at)
                    for member, offset in arena.get("members", ()):
                        binds.setdefault(member, binds[key] + offset)
                for name, ty in compiled.l1.params(compiled.name, "l1"):
                    if name not in binds:
                        layout = Buffer.of(ty, device.card.machine_json)
                        at = device.alloc(
                            layout.nbytes, layout.space, layout.layout.get("node")
                        )
                        binds[name] = at
                        allocated.append(at)
                protected = set(allocated) | {
                    binds[arena_name(a)] for a in compiled.scratch or ()
                }
                ports = self._ports(compiled, binds)
                row["ports"] = [
                    dict(name=name, address=at, nbytes=n, dtype=np.dtype(dt).str)
                    for name, at, n, dt in ports
                ]
                # Snapshot allocation extents, not overlapping subviews: host writes
                # round to lines and must not destroy adjacent alias bytes.
                spans = {}
                for _, at, n, _ in ports:
                    entry, _ = device.mmu.resolve(at, n)
                    spans[entry.virtual] = entry.extent
                saved = {at: device.read(at, n) for at, n in spans.items()}
                first_state, first_submission = None, None
                for repeat in range(self.repeats):
                    obs.row["attempts"] += 1
                    try:
                        if repeat:
                            for at, raw in saved.items():
                                device.write(at, raw)
                        # Keep descriptor metadata intact (alias fallback consults
                        # scratch). Reuse its explicitly allocated scratch arenas;
                        # defer their ordinary collect-time frees until this loop ends.
                        arenas = iter(compiled.scratch or ())
                        underlying_alloc, underlying_free = (
                            D.Device.alloc,
                            D.Device.free,
                        )

                        def fixed_alloc(
                            dev,
                            nbytes,
                            space="dram",
                            node=None,
                            owner="tensor",
                            *,
                            _arenas=arenas,
                            _alloc=underlying_alloc,
                        ):
                            if dev is device and owner == f"scratch:{compiled.name}":
                                arena = next(_arenas)
                                if (nbytes, space, node) != (
                                    arena["bytes"],
                                    arena["space"],
                                    arena.get("node"),
                                ):
                                    raise ValueError(
                                        "scratch allocation sequence changed"
                                    )
                                return binds[arena_name(arena)]
                            return _alloc(dev, nbytes, space, node, owner)

                        def deferred_free(dev, at, _free=underlying_free):
                            if dev is not device or at not in protected:
                                return _free(dev, at)
                            return None

                        with (
                            patch.object(D.Device, "alloc", fixed_alloc),
                            patch.object(D.Device, "free", deferred_free),
                        ):
                            result = original_submit(
                                device, compiled, binds, kind=kind, info=info
                            )
                            device.collect(result)
                        outputs = {
                            name: (device.read(at, n), dt) for name, at, n, dt in ports
                        }
                        obs.add(outputs, repeat)
                        if self.package_prefix and (repeat + 1) % 1000 == 0:
                            print(
                                f"package {compiled.name}: {repeat + 1}/{self.repeats}, "
                                f"{row['distinct_outputs']} distinct outputs",
                                flush=True,
                            )
                        if repeat == 0:
                            first_submission = result
                            first_state = {
                                at: device.read(at, n) for at, n in spans.items()
                            }
                    except DEVICE_ERRORS as error:
                        obs.error(error, repeat)
                        raise
                # Subsequent packages see exactly the first execution's bytes.
                for at, raw in first_state.items():
                    device.write(at, raw)
                row["measured"] = True
                row["stall_rate_observed"] = 0.0
                print(
                    f"  package {compiled.name} ({kind}) repeats={row['completed']} "
                    f"distinct={row['distinct_outputs']} max_diff={row['max_differing_elements']} "
                    f"{'IDENTICAL' if row['identical'] else 'NONDETERMINISTIC'}",
                    flush=True,
                )
                if self.package_prefix:
                    raise RepeatComplete()
                return first_submission
            finally:
                # Never release spans still owned by a failed/uncertain submission.
                if not row.get("stopped_on_error"):
                    for at in allocated:
                        device.free(at)

        def execute(device, p, buffers):
            if device is not self.device or not self.active:
                return original_execute(device, p, buffers)
            index = self.seen
            self.seen += 1
            if self.filter and not self.filter.search(f"{index}:{p.kind}:{p.name}"):
                return original_execute(device, p, buffers)
            self.current = index
            ast = self.asts.get(id(p), getattr(p, "_check_original_ast", None))
            if ast is None:
                raise RuntimeError(f"missing schedule AST for {index}:{p.name}")
            row = dict(
                index=index,
                name=p.name,
                kind=p.kind,
                ops=op_summary(ast),
                requested=self.repeats,
                launches=[],
                launches_by_repeat=[],
                launch_sequence_stable=True,
            )
            self.rows.append(row)
            if device.kind == "offline" or self.package_prefix:
                row.update(
                    identical=None,
                    measured=False,
                    reason=(
                        "offline shape only"
                        if device.kind == "offline"
                        else "package-only mode"
                    ),
                )
                self.observe_packages = True
                try:
                    return original_execute(device, p, buffers)
                finally:
                    self.observe_packages = False
            obs = Observations(row)
            device.drain()
            saved = StorageSnapshot(device, buffers)
            self.initialize_allocations = True
            outputs = {
                u.src[0].src[0].arg.slot: u.src[1].dtype
                for u in ast.toposort()
                if u.op is Ops.STORE
            }
            if not outputs:
                raise ValueError(f"{p.name}: no observable schedule outputs")
            row["output_scope"] = (
                "raw valid output forms; flat indices use AST storage dtype, other layouts use physical byte indices"
            )
            first_state, first_result = None, None
            for repeat in range(self.repeats):
                obs.row["attempts"] += 1
                try:
                    if repeat:
                        saved.restore()
                    self.observe_packages = repeat == 0
                    result = original_execute(device, p, buffers)
                    sequence = [dict(name=r.name, kind=r.kind) for r in result]
                    if repeat == 0:
                        row["launches"] = sequence
                    elif sequence != row["launches"]:
                        row["launch_sequence_stable"] = False
                    if repeat < 16:
                        row["launches_by_repeat"].append(sequence)
                    self.observe_packages = False
                    device.drain()
                    raw_outputs = {}
                    for slot, dt in outputs.items():
                        dtype = np.dtype(fold.np_dtype(dt))
                        view = buffers[slot]
                        st, offset = FM.base_of(view)
                        if not st.valid:
                            raise ValueError(
                                f"{p.name}: output {slot} has no valid form"
                            )
                        for key in sorted(st.valid, key=repr):
                            form = st.forms[key]
                            if key == FLAT:
                                raw = device.read(form.addr + offset, view.nbytes)
                                raw_outputs[f"{slot}:{key!r}"] = raw, dtype
                            else:
                                # Never unpack/convert a packed result before its
                                # bit check: conversion can erase NaN/padding differences.
                                raw_outputs[f"{slot}:{key!r}"] = (
                                    device.read(form.addr, form.nbytes),
                                    "u1",
                                )
                    obs.add(raw_outputs, repeat)
                    if repeat == 0:
                        first_state, first_result = (
                            StorageSnapshot(device, buffers),
                            result,
                        )
                except DEVICE_ERRORS as error:
                    obs.error(error, repeat)
                    raise
                finally:
                    self.observe_packages = False
            first_state.restore()
            self.initialize_allocations = False
            row["measured"] = True
            print(
                f"{index:4d} {p.kind:8s} {p.name:30.30s} repeats={obs.row['completed']} "
                f"{'IDENTICAL' if row['identical'] else 'NONDETERMINISTIC'}",
                flush=True,
            )
            return first_result

        def graph(graph, inputs, var_vals, wait=False):
            if not self.active or graph.dev.rt is not self.device:
                return original_graph(graph, inputs, var_vals, wait)
            self.graph_replays += 1
            graph._release_logs()
            graph.prev = graph.log = None
            graph._plan(inputs, slots=False)
            if wait:
                self.device.drain()
            return None

        self.stack.enter_context(patch.object(D.Device, "alloc", alloc))
        self.stack.enter_context(patch.object(lower, "plan", plan))
        self.stack.enter_context(patch.object(run, "execute", execute))
        self.stack.enter_context(patch.object(D.Device, "submit", submit))
        self.stack.enter_context(patch.object(KD.KTPUGraph, "__call__", graph))
        return self

    def _ports(self, compiled, bindings):
        ports = []
        specs = dict(compiled.l1.params(compiled.name, "l1"))
        for name, value in bindings.items():
            spec = specs.get(name)
            at = int(value)
            if spec is not None:
                layout = Buffer.of(spec, self.device.card.machine_json)
                dt = {"f16": "<f2", "f32": "<f4", "u32": "<u4", "i32": "<i4"}.get(
                    layout.dtype, "u1"
                )
                ports.append((name, at, layout.nbytes, dt))
            else:
                entry, _ = self.device.mmu.resolve(at)
                ports.append((name, at, entry.extent - (at - entry.virtual), "u1"))
        if not ports:
            raise ValueError(
                f"{compiled.name}: no readable bound outputs; cannot certify determinism"
            )
        return ports

    def __exit__(self, *exc):
        self.initialize_allocations = self.observe_packages = False
        return self.stack.__exit__(*exc)

    def report(self):
        measured = self.packages if self.package_prefix else self.rows + self.packages
        failed = any(r.get("identical") is False for r in measured)
        return dict(
            mode="determinism",
            repeat=self.repeats,
            repeat_package=self.package_prefix,
            seen_kernels=self.seen,
            checked_kernels=len(self.rows),
            rows=self.rows,
            packages=self.packages,
            graph_replays=self.graph_replays,
            package_found=self.package_found,
            package_stalls=sum(r.get("stalls", 0) for r in self.packages),
            schedule_stalls=sum(r.get("stalls", 0) for r in self.rows),
            full_step_coverage=not self.package_prefix
            and bool(self.rows)
            and len(self.rows) == self.seen
            and not any(r.get("stopped_on_error") for r in self.rows)
            and (
                self.device.kind == "offline"
                or all(r.get("measured") for r in self.rows)
            ),
            nondeterministic=sum(r.get("distinct_outputs", 0) > 1 for r in measured),
            boundary="schedule AST and independently repeated constituent submissions",
            caveat="readbacks serialize execution; fixed external bytes do not reset internal engine state; clean repeats cannot exclude a silicon race",
            allocation_policy="new schedule destinations/scratch zero-initialized on each trial; preexisting forms including padding restored byte-for-byte; package scratch fixed for its loop",
            passed=(
                None
                if self.device.kind == "offline"
                else bool(measured)
                and not failed
                and all(r.get("measured") for r in measured)
            ),
        )
