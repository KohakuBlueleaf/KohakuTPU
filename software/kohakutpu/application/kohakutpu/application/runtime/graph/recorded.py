"""Opt-in native preparation of eager recordings, preserving host-effect boundaries."""

import json
import os
import time
from dataclasses import dataclass, field

from kohakutpu.application.runtime.graph.firmware import FirmwareRunner
from kohakutpu.application.runtime.graph.native import (
    NativeLaunch,
    fuse,
    local_scratch,
    port_bytes,
)
from kohakutpu.stack.kernel import arena_name


@dataclass
class NativeRecording:
    device: object
    segments: list
    allocations: list
    before: int
    after: int
    rules: list
    build_seconds: float
    prepared: list = field(default_factory=list)
    busy: bool = False

    def run(self):
        if self.busy:
            raise RuntimeError("native recording has unresolved work")
        dev = self.device
        if not self.prepared:
            prepared = []
            try:
                for kind, payload in self.segments:
                    prepared.append(
                        FirmwareRunner(dev.card.queues[0]).prepare(payload, ())
                        if kind == "run"
                        else None
                    )
            except BaseException:
                for item in prepared:
                    if item is not None:
                        item.close()
                raise
            self.prepared = prepared
        self.busy = True
        cycles = 0
        for (kind, payload), prepared in zip(self.segments, self.prepared, strict=True):
            if kind == "write":
                dev.card.write(*payload)
            else:
                result = prepared.submit()
                if not result.ok:
                    raise RuntimeError(result.describe())
                cycles += result.cycles
        self.busy = False
        return cycles

    def close(self):
        if self.busy and not self.device.closed:
            raise RuntimeError(
                "stop device before releasing an unresolved native replay"
            )
        for prepared in self.prepared:
            if prepared is not None:
                prepared.close()
        for address in self.allocations:
            if not self.device.closed:
                self.device.memory.free(address)
        self.allocations.clear()


def prepare(
    log,
    *,
    exports=None,
    quant=True,
    elementwise=True,
    norm=False,
    layout=False,
    local=False,
    hoist=True,
):
    """Prepare under log's lifetime; log must outlive this borrowed native replay.

    Live allocation owners are exports; released pinned temporaries may disappear.
    """
    dev = log.owner
    if dev.nodes != 1:
        raise ValueError("native recording placement currently supports one node")
    if any(event[0] not in ("run", "write") for event in log):
        raise ValueError("native replay requires a recording without host reads")
    start = time.perf_counter()
    machine = dev.machine.machine_json
    if exports is None:
        exports = [
            (entry.physical, entry.extent)
            for entry in dev.mmu.entries.values()
            if entry.live and entry.physical not in entry.arena.released
        ]
    # Every address crossing a host effect is observable to both segments.
    host_spans = [(event[1], len(event[2])) for event in log if event[0] == "write"]
    templates = iter(zip(log.templates, log.runs, strict=True))
    groups, current, prefix, prior = [], [], [], []
    for event in log:
        if event[0] == "write":
            at, data = event[1:]
            hazard = any(at < hi and lo < at + len(data) for lo, hi in prior)
            prior.append((at, at + len(data)))
            if hoist and not hazard:
                prefix.append(("write", event[1:]))
                continue
            if current:
                groups.append(("run", current))
                current = []
            groups.append(("write", event[1:]))
        else:
            (compiled, bindings), metadata = next(templates)
            ports = {p["name"] for p in compiled.traced.graph["ports"]}
            bound = {
                name: dev.mmu.resolve(at)[1] for name, at in bindings if name in ports
            }
            launch = NativeLaunch(compiled.traced.graph, bound, metadata[1])
            current.append(launch)
            prior.extend(
                (bound[p["name"]], bound[p["name"]] + port_bytes(p, machine))
                for p in launch.graph["ports"]
            )
    if current:
        groups.append(("run", current))
    groups = prefix + groups
    # Other segments' accesses are exports, never removed by a local liveness query.
    all_ports = [
        (g, at, p)
        for g, (kind, body) in enumerate(groups)
        if kind == "run"
        for launch in body
        for p in launch.graph["ports"]
        for at in (launch.bindings[p["name"]],)
    ]
    # Original private scratch stays pinned by log. Reuse its largest isolated
    # span instead of reserving a second copy or invalidating fallback replay.
    visible = [
        *exports,
        *host_spans,
        *((at, port_bytes(p, machine)) for _, at, p in all_ports),
    ]
    reusable = {}
    for arena, at in log.references:
        size = arena.live[at]
        if at in arena.released and not any(
            at < lo + n and lo < at + size for lo, n in visible
        ):
            key = (
                ("dram", 0)
                if arena is dev.memory.dram
                else (
                    "stage",
                    next(n for n, a in dev.memory.staging.items() if a is arena),
                )
            )
            if size > reusable.get(key, (0, 0))[1]:
                reusable[key] = (at, size)
    stage_budget = max(
        dev.memory.staging[0].largest, reusable.get(("stage", 0), (0, 0))[1]
    )
    prepared, rules, allocations, need = [], [], [], {}
    before = after = 0
    for group, (kind, body) in enumerate(groups):
        if kind == "write":
            prepared.append((kind, body))
            continue
        outside = [(at, port_bytes(p, machine)) for g, at, p in all_ports if g != group]
        selected, applied = fuse(
            body,
            [*exports, *host_spans, *outside],
            machine,
            quant=quant,
            elementwise=elementwise,
            norm=norm,
            layout=layout,
            stage_bytes=stage_budget,
            recursive=True,
            producer_quant=True,
        )
        before += len(body)
        after += len(selected)
        rules.extend(applied)
        entries = []
        for launch in selected:
            if local:
                launch = local_scratch(launch, machine, stage_budget)
            compiled = dev.kernel(json.dumps(launch.graph))
            for arena in compiled.scratch or ():
                key = (arena["space"], arena["node"] or 0)
                need[key] = max(need.get(key, 0), arena["bytes"])
            entries.append((compiled, launch.bindings))
        prepared.append(("run", entries))
    base = {}
    try:
        for key, size in need.items():
            space, node = key
            if key in reusable and reusable[key][1] >= size:
                base[key] = reusable[key][0]
                continue
            base[key] = (
                dev.memory.stage(size, node)
                if space == "stage"
                else dev.memory.alloc(size)
            )
            allocations.append(base[key])
        segments = []
        for kind, body in prepared:
            if kind == "write":
                segments.append((kind, body))
                continue
            packets = []
            for compiled, bindings in body:
                full = dict(bindings)
                for arena in compiled.scratch or ():
                    at = base[arena["space"], arena["node"] or 0]
                    full[arena_name(arena)] = at
                    full.update(
                        {name: at + offset for name, offset in arena["members"]}
                    )
                programs = compiled.programs(full)
                packets.append(next(iter(programs.values())).bound(True))
            segments.append(("run", packets))
        return NativeRecording(
            dev,
            segments,
            allocations,
            before,
            after,
            rules,
            time.perf_counter() - start,
        )
    except BaseException:
        for at in allocations:
            dev.memory.free(at)
        raise


def from_environment(log):
    return prepare(
        log,
        **{
            key: os.environ.get("KTPU_GRAPH_" + key.upper(), default) == "1"
            for key, default in (
                ("quant", "1"),
                ("elementwise", "1"),
                ("norm", "0"),
                ("layout", "0"),
                ("local", "0"),
                ("hoist", "1"),
            )
        },
    )
