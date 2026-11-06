"""Dependency frontiers over canonical allocation write lines."""

from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class Frontier:
    writer: int | None = None
    readers: set[int] = field(default_factory=set)


def dependencies(launches, addresses, line_bytes=64):
    """RAW, WAR and WAW predecessors, with transitive predecessors omitted.

    Coordinate compression splits only at access endpoints, never at every byte
    or line. Addresses locate allocation-relative views within write lines.
    """
    endpoints = defaultdict(set)
    regions = []
    for launch in launches:
        current = []
        for span, write in (
            *((span, False) for span in launch.inputs),
            (launch.output, True),
        ):
            if not span.nbytes:
                continue
            start = addresses[span.allocation] + span.offset
            lo = start // line_bytes
            hi = (start + span.nbytes + line_bytes - 1) // line_bytes
            endpoints[span.allocation].update((lo, hi))
            current.append((span.allocation, lo, hi, write))
        regions.append(current)
    positions = {
        allocation: {point: index for index, point in enumerate(sorted(points))}
        for allocation, points in endpoints.items()
    }
    frontiers = {
        allocation: [Frontier() for _ in range(len(points) - 1)]
        for allocation, points in endpoints.items()
    }
    result = []
    for index, current in enumerate(regions):
        touched = {}
        for allocation, lo, hi, write in current:
            positions_ = positions[allocation]
            for part in range(positions_[lo], positions_[hi]):
                key = allocation, part
                touched[key] = touched.get(key, False) or write
        conflicts = set()
        for (allocation, part), write in touched.items():
            frontier = frontiers[allocation][part]
            if write and frontier.readers:
                conflicts.update(frontier.readers)
            elif frontier.writer is not None:
                conflicts.add(frontier.writer)
        parents = []
        while conflicts:
            previous = max(conflicts)
            conflicts.remove(previous)
            parents.append(previous)
            todo = list(result[previous]) if conflicts else []
            visited = set()
            while todo and conflicts:
                ancestor = todo.pop()
                if ancestor in visited or ancestor < min(conflicts):
                    continue
                visited.add(ancestor)
                conflicts.discard(ancestor)
                todo.extend(result[ancestor])
        result.append(tuple(sorted(parents)))
        for (allocation, part), write in touched.items():
            frontier = frontiers[allocation][part]
            if write:
                frontier.writer = index
                frontier.readers.clear()
            else:
                frontier.readers.add(index)
    return tuple(result)
