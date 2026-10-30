"""Cold replay admission: capacity-derived pins with room for streamed packages."""

from collections import Counter

from kohakutpu.application.runtime.arena import OutOfMemory
from kohakutpu.language.dsl.kernel import _MUTANT

FRACTION_ENV = "KOHAKUTPU_REPLAY_PACKAGE_FRACTION"
DEFAULT_FRACTION = 0.5
OVERCOMMIT_ENV = "KOHAKUTPU_REPLAY_OVERCOMMIT"


def padded(nbytes, alignment=64):
    return (nbytes + alignment - 1) // alignment * alignment


def held_bytes(card):
    arena = card.memory.dram
    addresses = {at for q in card.queues.values() for at in q.prepared_addresses()}
    return sum(arena.live[at] for at in addresses)


def select(card, log, override, depth, fraction=DEFAULT_FRACTION, overcommit=False):
    if not 0.0 <= fraction <= 1.0:
        raise ValueError("replay package fraction must be in [0, 1]")
    counts = Counter(
        (node, raw)
        for event in log
        if event[0] == "run"
        for node, (raw, _) in event[1].items()
    )
    arena = card.memory.dram
    held = held_bytes(card)
    budget = held + int(arena.free * fraction) if override is None else override
    remaining = max(0, budget - held)
    spans = arena.free_spans()
    selected = {}
    for node, raw in sorted(counts, key=lambda key: (-counts[key], -len(key[1]))):
        queue = card.queues[node]
        if queue.has_prepared(raw):
            selected[node, raw] = None
            continue
        size = padded(len(raw), arena.align)
        if size > remaining:
            continue
        if not queue.is_cached(raw):
            index = next((i for i, free in enumerate(spans) if free >= size), None)
            if index is None:
                continue
            spans[index] -= size
        selected[node, raw] = None
        remaining -= size
    # Pins cannot consume the workspace the same recording needs to stream.
    while True:
        workspace = []
        for node, queue in card.queues.items():
            largest = max(
                (
                    padded(len(raw), arena.align)
                    for n, raw in counts
                    if n == node and (n, raw) not in selected
                ),
                default=0,
            )
            if largest:
                workspace.extend(
                    [largest] * min(depth or queue.sq_n, queue.sq_n, queue.cq_n)
                )
        if workspace and _MUTANT.get() != "app.replay/split_workspace":
            workspace = [sum(workspace)]
        allocations = [
            padded(len(raw), arena.align)
            for node, raw in selected
            if not card.queues[node].is_cached(raw)
        ]
        if arena.fits(allocations + workspace):
            break
        removable = [
            key for key in selected if not card.queues[key[0]].has_prepared(key[1])
        ]
        if not removable:
            if not overcommit:
                raise OutOfMemory("streamed replay workspace exceeds free DRAM")
            break
        selected.pop(removable[-1])
    if _MUTANT.get() == "app.replay/refuses_unpinned" and len(selected) != len(counts):
        raise OutOfMemory("aggregate packages exceed replay pin budget")
    return budget, selected, counts, workspace
