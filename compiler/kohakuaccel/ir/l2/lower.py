"""The L2 -> L1 compiler (docs/spec/l2-schedule.md §4): one L1 `Program` a
package.

A project gives one LOWERER factory per unit type -- ``factory(coord)`` returns
an object with ``lower(index, item) -> [Chunk]`` and ``finish() -> [Chunk]``,
keeping that unit's state across items and packages -- and, for mover items,
``mover(item) -> writes``. Everything about sync and node order is here.
"""

from dataclasses import dataclass, field

from kohakuaccel.ir.l1.program import Program
from kohakuaccel.ir.l2.schedule import Schedule, ScheduleError
from kohakuaccel.package.units import unit_types

#: Node cycles a mark's dispatch costs before the next step (a fetch-port
#: request, MEASURED ~0.6k on card_v9_1n); it only orders the waits.
SEND_CYCLES = 600.0


@dataclass
class Chunk:
    """L1 ops for one unit; `done` the items complete once they have run;
    `cycles` the unit time they take, estimated."""

    ops: list
    done: tuple = ()
    cycles: float = 0.0


@dataclass
class _Part:
    chunk: Chunk
    item: int
    requires: set = field(default_factory=set)


def compile(schedule: Schedule, machine, lowerers: dict, mover=None, program=Program):
    """L1 programs, one a package, for a placed `schedule`; raises
    `ScheduleError` for one the checker refuses."""
    schedule.verify(unit_types(machine))
    deps = schedule.deps()
    units: dict = {}
    for it in schedule.items:
        if it.unit != "mover" and it.at not in units:
            if it.unit not in lowerers:
                raise ScheduleError(f"no lowerer for unit type {it.unit!r}")
            units[it.at] = lowerers[it.unit](it.at)
    out = []
    for items in schedule.packages():
        prog = program(machine)
        segment: list = []
        for i in items:
            if schedule.items[i].unit == "mover":
                _segment(schedule, segment, deps, units, prog, i)
                if mover is None:
                    raise ScheduleError("a mover item and no `mover` to lower it")
                prog.move(mover(schedule.items[i]))
                segment = []
            else:
                segment.append(i)
        _segment(schedule, segment, deps, units, prog, None)
        prog.barrier()
        out.append(prog)
    return out


def _segment(schedule, items, deps, units, prog, closer) -> None:
    """Send `items` (no mover among them) to their units with the waits their
    cross-unit dependences need; then, for a mover item `closer`, wait for its
    producers. Every lowerer finishes: the segment ends at a barrier."""
    streams: dict = {}
    here = set(items)

    def flush(at) -> None:
        tail = units[at].finish()
        if tail:
            last = streams[at][-1].item if streams.get(at) else -1
            streams.setdefault(at, []).extend(_Part(c, last) for c in tail)

    for i in items:
        it = schedule.items[i]
        # A result a lowerer holds back goes out before an item that waits on
        # another unit: that unit may be waiting on the held result, and the
        # node, blocked on the item, would never send it.
        if any(a in here and schedule.items[a].at != it.at for a in deps[i]):
            flush(it.at)
        streams.setdefault(it.at, []).extend(
            _Part(c, i) for c in units[it.at].lower(i, it)
        )
    for at in units:
        flush(at)
    done_at: dict = {}
    first: dict = {}
    for at, parts in streams.items():
        for k, p in enumerate(parts):
            first.setdefault(p.item, (at, k))
            for i in p.chunk.done:
                done_at[i] = (at, k)
    for at, parts in streams.items():
        for k, p in enumerate(parts):
            if first.get(p.item) != (at, k):
                continue
            for a in deps[p.item]:
                if a not in here:
                    continue  # an earlier segment or package: behind a barrier
                if a not in done_at:
                    raise ScheduleError(f"item {a} never completes on its unit")
                if done_at[a][0] != at:
                    p.requires.add(done_at[a])
    closing = (
        {done_at[a] for a in deps[closer] if a in done_at}
        if closer is not None
        else set()
    )
    needed = {
        r for parts in streams.values() for p in parts for r in p.requires
    } | closing

    head = {at: 0 for at in streams}
    tokens: dict = {}
    waited: dict = {}
    unit_free = {at: 0.0 for at in streams}
    finish: dict = {}
    node_t = 0.0

    def satisfied(req) -> bool:
        return waited.get(req[0], -1) >= req[1]

    def send(at) -> None:
        nonlocal node_t
        k = head[at]
        p = streams[at][k]
        prog.send(at, *p.chunk.ops)
        unit_free[at] = max(node_t, unit_free[at]) + p.chunk.cycles
        finish[(at, k)] = unit_free[at]
        if (at, k) in needed:
            tokens[(at, k)] = prog.mark(at)
            node_t += SEND_CYCLES
        head[at] = k + 1

    def wait(req) -> None:
        nonlocal node_t
        prog.wait(req[0], tokens[req])
        node_t = max(node_t, finish[req])
        waited[req[0]] = max(waited.get(req[0], -1), req[1])

    def pending(at) -> bool:
        return head[at] < len(streams[at])

    while any(pending(at) for at in streams):
        ready = [
            at
            for at in streams
            if pending(at) and all(satisfied(r) for r in streams[at][head[at]].requires)
        ]
        if ready:
            for at in ready:
                send(at)
            continue
        blocking = {
            r
            for at in streams
            if pending(at)
            for r in streams[at][head[at]].requires
            if not satisfied(r)
        }
        sent = [r for r in blocking if r in finish]
        if not sent:
            raise ScheduleError("the node would wait on a chunk never sent")
        wait(min(sent, key=lambda r: finish[r]))
    for r in sorted(closing, key=lambda r: finish[r]):
        if not satisfied(r):
            wait(r)
