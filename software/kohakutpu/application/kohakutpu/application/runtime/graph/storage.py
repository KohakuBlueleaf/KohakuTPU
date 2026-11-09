"""Capacity-gated private scratch placement and retained window borrowing."""

import json
from math import lcm

from kohakutpu.application.runtime.memory.arena import OutOfMemory
from kohakutpu.language.dsl import TensorSpec
from kohakutpu.stack.buffer import Buffer
from kohakutpu.stack.kernel import arena_name


class GraphScratchRefused(ValueError):
    """Preparation cannot meet the compiled placement/lifetime contract."""

    stage = "bind"
    clause = "GRAPH.SCRATCH"

    def __init__(self, key, required, available, reason):
        self.key, self.required, self.available = key, required, available
        super().__init__(
            f"{self.clause}: {key} needs {required} bytes, available {available}: {reason}"
        )


def scratch_bytes(port, machine):
    spec = port["spec"]
    layout = {k: next(iter(v.values())) for k, v in spec["layout"]}
    tensor = TensorSpec.make(spec["shape"], spec["dtype"], layout, spec["space"])
    return Buffer.of(tensor.text(), machine).nbytes


def stage_alignment(machine):
    facts = json.loads(machine)["machine"]
    return lcm(
        256,
        facts["card"]["dram"]["line_bytes"],
        facts["mg"]["wburst"] * facts["die"]["word_bytes"],
    )


def stage_graph(graph, machine, budget):
    """Relocate private DRAM scratch only when its whole footprint fits."""
    scratch = graph["scratch"]
    align = stage_alignment(machine)
    need = sum(-(-scratch_bytes(port, machine) // align) * align for port in scratch)
    if need > budget or not any(p["spec"]["space"] == "dram" for p in scratch):
        return graph
    return dict(
        graph,
        scratch=[
            (
                dict(port, spec=dict(port["spec"], space="stage"))
                if port["spec"]["space"] == "dram"
                else port
            )
            for port in scratch
        ],
    )


def borrow_scratch(dev, compiled, full, limits=None):
    """Preflight all windows, then borrow the eager path's exact storage.

    `limits` is the capture's existing window envelope, not a new free-memory
    budget. A retained replay must not grow that envelope. Standalone graph
    preparation may establish a window, but never changes its compiled space.
    """
    named = set(full)
    spans = []
    for arena in compiled.scratch or ():
        name = arena_name(arena)
        named.add(name)
        named.update(member for member, _ in arena.get("members", ()))
        if name not in full:
            spans.append((name, arena["bytes"], arena["space"], arena.get("node"), ()))
    for name, spec in compiled.l1.params(compiled.name, "l1"):
        if name not in named:
            buf = Buffer.of(spec, dev.card.machine_json)
            spans.append((name, buf.nbytes, buf.space, buf.layout.get("node"), ()))
    # Mirror Allocation._windowed exactly, including multiple arenas that map
    # to the same DRAM window or unqualified staging owner.
    line = max(dev.line_bytes, 256)
    need = {}
    for _, size, space, node, _ in spans:
        key = (space, dev._node_of(node) if space == "stage" else dev.home)
        need[key] = need.get(key, 0) + -(-int(size) // line) * line
    dev.drain()
    for key, size in need.items():
        if limits is not None and size > limits.get(key, 0):
            raise GraphScratchRefused(
                key, size, limits.get(key, 0), "exceeds eager capture envelope"
            )
        held = dev.windows.get(key)
        if held is not None and held[1] >= size:
            root, _ = dev.mmu.resolve(held[0])
            for address in full.values():
                external, _ = dev.mmu.resolve(address)
                if (external.arena, external.physical) == (root.arena, root.physical):
                    raise GraphScratchRefused(
                        key, size, 0, "scratch window aliases an external binding"
                    )
            continue
        arena = dev.memory.staging.get(key[1]) if key[0] == "stage" else dev.memory.dram
        available = 0 if arena is None else arena.largest
        # Do not destroy a pinned old window in an attempt to grow it. Capture
        # and fallback replay still own it; a second allocation is not reuse.
        if held is not None:
            raise GraphScratchRefused(
                key, size, held[1], "existing scratch window is too small"
            )
        if size > available:
            raise GraphScratchRefused(
                key, size, available, "compiled space has no fitting span"
            )
    try:
        return dev._windowed(spans)
    except OutOfMemory as error:
        # Even an intervening allocator failure remains a typed preparation
        # refusal. Never fall back from priced staging to unpriced DRAM.
        raise GraphScratchRefused(
            "windows", sum(need.values()), 0, str(error)
        ) from error
