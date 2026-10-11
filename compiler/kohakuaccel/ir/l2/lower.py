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
    if any(it.unit == "mover" for it in schedule.items):
        if mover is None:
            raise ScheduleError("a mover item and no `mover` to lower it")
        units[MOVER] = _MoverLowerer(mover)
    out = []
    for items in schedule.packages():
        prog = program(machine)
        _segment(schedule, list(items), deps, units, prog)
        prog.barrier()
        out.append(prog)
    return out


#: The stream key of mover items: the mover is one more unit to the node
#: order, its work posted (the node goes on while a move runs) and waited for
#: only by the items that read or overwrite what it touches.
MOVER = "mover"

#: Node cycles of a mover item a byte moved, for the node order (MODEL).
MOVE_BYTES_A_CYCLE = 30.0


class _MoverLowerer:
    def __init__(self, mover) -> None:
        self.mover = mover

    def lower(self, index, item) -> list:
        writes = self.mover(item)
        nbytes = item.params.get("nbytes") or item.params.get("entries", 0) * 384
        return [Chunk(writes, (index,), nbytes / MOVE_BYTES_A_CYCLE)]

    def finish(self) -> list:
        return []


def _note(prog, item: int) -> None:
    """Tell a program that keeps provenance (a ``note(item)`` method) which
    item the ops it is sent next come from."""
    note = getattr(prog, "note", None)
    if note is not None:
        note(item)


def _segment(schedule, items, deps, units, prog) -> None:
    """Send `items` to their units -- mover items to the mover, posted -- with
    the waits their cross-unit dependences need. Every lowerer finishes: the
    segment ends at a barrier."""
    streams: dict = {}
    here = set(items)

    def at_of(it):
        return MOVER if it.unit == "mover" else it.at

    def flush(at) -> None:
        tail = units[at].finish()
        if tail:
            last = streams[at][-1].item if streams.get(at) else -1
            streams.setdefault(at, []).extend(_Part(c, last) for c in tail)

    for i in items:
        it = schedule.items[i]
        at = at_of(it)
        # A result a lowerer holds back goes out before an item that waits on
        # another unit: that unit may be waiting on the held result, and the
        # node, blocked on the item, would never send it.
        if any(a in here and at_of(schedule.items[a]) != at for a in deps[i]):
            flush(at)
        streams.setdefault(at, []).extend(_Part(c, i) for c in units[at].lower(i, it))
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
    needed = {r for parts in streams.values() for p in parts for r in p.requires}

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
        _note(prog, p.item)
        unit_free[at] = max(node_t, unit_free[at]) + p.chunk.cycles
        finish[(at, k)] = unit_free[at]
        if at == MOVER:
            tokens[(at, k)] = prog.post(p.chunk.ops)
            node_t += SEND_CYCLES
        else:
            prog.send(at, *p.chunk.ops)
            if (at, k) in needed:
                tokens[(at, k)] = prog.mark(at)
                node_t += SEND_CYCLES
        head[at] = k + 1

    def wait(req) -> None:
        nonlocal node_t
        if req[0] == MOVER:
            prog.wait_moves(tokens[req])
        else:
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
        # Wait for the blocked unit that can start soonest: its producers done
        # and the unit itself free. Waiting on the soonest producer alone, ties
        # broken arbitrarily, let one core's waits queue ahead of another
        # core's ready work (MEASURED: 16 tile epilogues on two cores).
        blocked = []
        for at in streams:
            if not pending(at):
                continue
            unsat = [r for r in streams[at][head[at]].requires if not satisfied(r)]
            if not unsat:
                continue
            if any(r not in finish for r in unsat):
                continue
            start = max(max(finish[r] for r in unsat), unit_free[at])
            blocked.append((start, min(finish[r] for r in unsat), unsat))
        if not blocked:
            raise ScheduleError("the node would wait on a chunk never sent")
        _, _, unsat = min(blocked, key=lambda b: (b[0], b[1]))
        wait(min(unsat, key=lambda r: finish[r]))
