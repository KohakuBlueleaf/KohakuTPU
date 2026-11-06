"""Split recording preparation, with host effects kept between pure regions."""

import json
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from kohakutpu.application.runtime.graph.access import bind_contracts, contracts
from kohakutpu.application.runtime.graph.dependencies import access_dependencies
from kohakutpu.application.runtime.graph.native import NativeLaunch, fuse, port_bytes
from kohakutpu.application.runtime.graph.split import PreparedSplitGraph, SplitRunner
from kohakutpu.language.config import ENV


def contains_split(log):
    return any(
        info.get("active_nodes", 1) > 1 or len(nodes) > 1
        for _, _, info, _, _, nodes in log.runs
    )


@dataclass
class SplitRecording:
    device: Any
    segments: list
    pending: list
    rules: list = field(default_factory=list)
    before: int = 0
    after: int = 0

    def run(self, wait):
        # Each retained instance owns one epoch of its event slots and scratch.
        for submitted in self.pending:
            self.device.collect_through(submitted)
        self.pending.clear()
        for kind, payload in self.segments:
            if kind == "write":
                address, data = payload
                self.device.write(address, data)
            else:
                self.pending.append(payload.submit())
        if wait:
            result = [self.device.collect_through(s) for s in self.pending]
            self.pending.clear()
            return result
        return list(self.pending)

    def close(self):
        for kind, payload in self.segments:
            if kind == "run":
                payload.close()
        self.segments.clear()


def regions(log):
    """Partition graph-backed runs at explicit host or executable-L1 boundaries."""
    dev = log.owner
    templates = iter(zip(log.templates, log.runs, strict=True))
    groups, current = [], []
    for event in log:
        if event[0] == "write":
            if current:
                groups.append(("graph", current))
                current = []
            entry = next(
                entry
                for entry in dev.mmu.entries.values()
                if entry.live
                and entry.physical <= event[1] < entry.physical + entry.extent
            )
            address = entry.virtual + event[1] - entry.physical
            groups.append(("write", (address, event[2])))
            continue
        (compiled, bindings), metadata = next(templates)
        if (
            compiled.traced is None
            or not compiled.traced.graph
            or "executable_l1" in compiled.traced.graph
        ):
            if current:
                groups.append(("graph", current))
                current = []
            groups.append(("opaque", (compiled, dict(bindings))))
            continue
        graph = compiled.traced.graph
        ports = {p["name"] for p in graph["ports"]}
        bound = {name: at for name, at in bindings if name in ports}
        current.append(NativeLaunch(graph, bound, metadata[1]))
    if current:
        groups.append(("graph", current))
    return groups


def visible_regions(dev, groups):
    """Live owners and every access crossing a composition boundary are exports."""
    live = [
        (entry.virtual, entry.extent)
        for entry in dev.mmu.entries.values()
        if entry.live and entry.physical not in entry.arena.released
    ]
    accesses = []
    for index, (kind, body) in enumerate(groups):
        if kind == "write":
            accesses.append((index, body[0], len(body[1])))
        elif kind == "opaque":
            for address in body[1].values():
                entry, _ = dev.mmu.resolve(address)
                accesses.append((index, entry.virtual, entry.extent))
        else:
            accesses.extend(
                (
                    index,
                    packet.bindings[p["name"]],
                    port_bytes(p, dev.card.machine_json),
                )
                for packet in body
                for p in packet.graph["ports"]
            )
    return live, accesses


def prepare(log):
    """The log owns external allocations; each prepared region pins its bindings."""
    dev = log.owner
    if any(event[0] not in ("run", "write") for event in log):
        raise ValueError("split replay requires a recording without host reads")
    groups = regions(log)
    live, outside = visible_regions(dev, groups)
    result = SplitRecording(dev, [], [])
    try:
        for index, (kind, body) in enumerate(groups):
            if kind == "write":
                result.segments.append((kind, body))
                continue
            if kind == "opaque":
                compiled, bindings = body
                bound = MappingProxyType(bindings)
                references = dev._retain(bound.values(), pinned=True)
                prepared = PreparedSplitGraph(dev, compiled, bound, (), (), references)
                result.before += 1
                result.after += 1
            else:
                result.before += len(body)
                if ENV.get("KTPU_GRAPH_FUSE", "0") == "1":
                    body, rules = fuse(
                        body,
                        [
                            *live,
                            *(
                                (at, size)
                                for group, at, size in outside
                                if group != index
                            ),
                        ],
                        dev.card.machine_json,
                        **{
                            key: ENV.get("KTPU_GRAPH_" + key.upper(), default) == "1"
                            for key, default in (
                                ("quant", "1"),
                                ("elementwise", "1"),
                                ("norm", "0"),
                                ("layout", "0"),
                            )
                        },
                        stage_bytes=min(a.largest for a in dev.memory.staging.values()),
                        recursive=True,
                        producer_quant=True,
                        edge_space="dram",
                    )
                    result.rules.extend(rules)
                accesses = [
                    bind_contracts(
                        dev,
                        contracts(json.dumps(packet.graph), dev.card.machine_json),
                        packet.bindings,
                    )
                    for packet in body
                ]
                prepared = SplitRunner(dev, pinned=True).prepare(
                    body, access_dependencies(accesses)
                )
                result.after += len(body)
            result.segments.append(("run", prepared))
        return result
    except BaseException:
        result.close()
        raise
