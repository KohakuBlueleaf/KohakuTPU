"""Physical write-line accesses shared by queue admission and recorded graphs."""

import json
from functools import lru_cache

from kohakutpu.application.runtime.graph.native import port_bytes


@lru_cache(maxsize=4096)
def contracts(source, machine):
    graph = json.loads(source)
    return tuple(
        (p["name"], port_bytes(p, machine), p.get("role") != "in")
        for p in (*graph["ports"], *graph["scratch"])
    )


def bound_accesses(device, compiled, bindings):
    """Ports and bound scratch, in their physical memory space, rounded to lines.

    Raw L1 programs have no graph port contract and use allocation-wide admission.
    Lowered scratch arenas not present in the graph own their entire window.
    """
    if compiled.traced is None or not compiled.traced.graph:
        return None
    return bind_contracts(
        device,
        contracts(compiled.traced.graph_json, device.card.machine_json),
        bindings,
    )


def bind_contracts(device, contract, bindings):
    """Bind a cached or composed graph contract in canonical physical spaces."""
    line = device.line_bytes
    result, named = [], set()
    for name, size, write in contract:
        named.add(name)
        if name not in bindings or not size:
            continue
        entry, at = device.mmu.resolve(bindings[name])
        result.append((entry.arena, at // line, (at + size + line - 1) // line, write))
    for name in bindings.keys() - named:
        entry, _ = device.mmu.resolve(bindings[name])
        result.append(
            (
                entry.arena,
                entry.physical // line,
                (entry.physical + entry.extent + line - 1) // line,
                True,
            )
        )
    return tuple(result)


def conflicts(left, right):
    return any(
        space_a is space_b and lo_a < hi_b and lo_b < hi_a and (write_a or write_b)
        for space_a, lo_a, hi_a, write_a in left
        for space_b, lo_b, hi_b, write_b in right
    )
