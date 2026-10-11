"""The static checker and the dispatch scheduler
(docs/projects/kohakutpu/verify.md).

    pkgs = verify.schedule({0: prog}, machine, fetch, residents)
    findings = verify.check({0: prog}, machine)

`schedule` builds each node's package and orders each segment's dispatches
on the abstract machine: deadlock-free by construction, or ScheduleError.
`check` schedules a program's first run and its rerun (the vector cores'
programs then resident) and runs both on the machine. Finding kinds:
deadlock, rest (a count left for the next run), port (bursts into one unit
not ordered, or queued behind a shared fill), bounds (a burst the receiver
rejects), route (aimed where no unit is), loop (a node loop on data, run a
bounded number of trips), model (what the model does not decide).
"""

import bisect

from kohakutpu.compiler import imem
from kohakutpu.compiler import target as T
from kohakutpu.compiler.isa.vector import ISA as VEC
from kohakutpu.compiler.verify.card import Card
from kohakutpu.compiler.verify.model import Finding, Machine
from kohakutpu.compiler.verify.node import Node


class ScheduleError(Exception):
    def __init__(self, findings: list) -> None:
        self.findings = findings
        super().__init__("no dispatch order completes:\n" + "\n".join(str(f) for f in findings[:20]))


def _nodes(programs) -> dict:
    if not isinstance(programs, dict):
        return {0: programs}
    return {0 if n is None else n: p for n, p in programs.items()}


def _held(residents: dict, mesh: int) -> list:
    """``[(coord, imem, desc)]``: what `residents` says mesh's vector cores hold."""
    out = []
    for coord, r in residents.get(mesh, {}).items():
        desc = {k: v | hi << VEC.cfg.desc_value_bits for k, (v, hi) in r.desc.items()}
        out.append((tuple(coord), dict(r.mem), desc))
    return out


def schedule(programs, machine=None, fetch=None, residents=None) -> dict:
    """``{mesh: (Package, bindings)}``: each node's package, built against
    `residents` (``{mesh: imem.Cores}``, updated as the card will be), its
    segments in an order the abstract machine runs to the end. Raises
    :class:`ScheduleError` when no order does."""
    programs = _nodes(programs)
    machine = machine or T.machine(nodes=max(programs) + 1)
    residents = residents if residents is not None else {}
    held = {mesh: _held(residents, mesh) for mesh in programs}
    built = {}
    for mesh, prog in programs.items():
        b = prog.build(fetch, residents.setdefault(mesh, imem.Cores(imem.IMEM_WORDS)))
        built[mesh] = (b.build(defaults=False), b.bindings())
    m = Machine()
    card = Card(m, machine)
    for mesh, cores in held.items():
        for coord, mem, desc in cores:
            u = card.unit((mesh, *coord))
            if u is not None and hasattr(u, "imem"):
                u.imem.update(mem)
                u.desc.update(desc)
    nodes = {mesh: Node(card, mesh, [pkg], free=True) for mesh, (pkg, _) in sorted(built.items())}
    m.run()
    stuck = [f for f in m.findings if f.kind == "deadlock"]
    if stuck:
        raise ScheduleError(stuck)
    return {mesh: (nodes[mesh].packages()[0], built[mesh][1]) for mesh in built}


def check(programs, machine=None, fetch=None, runs: int = 2) -> list:
    """Every finding for `programs` (a Program, or ``{node: Program}``) run
    `runs` times on a fresh card of `machine`."""
    programs = _nodes(programs)
    machine = machine or T.machine(nodes=max(programs) + 1)
    residents: dict = {}
    pkgs: dict = {mesh: [] for mesh in programs}
    for _ in range(runs):
        try:
            got = schedule(programs, machine, fetch, residents)
        except ScheduleError as e:
            return e.findings
        for mesh, (pkg, _) in got.items():
            pkgs[mesh].append(pkg)
    first, _ = run(pkgs, machine)
    m, card = run(pkgs, machine, first.tally())
    out = list(m.findings)
    if any(f.kind == "deadlock" for f in out):
        return out
    return out + _rest(m, card) + _ports(card)


def run(pkgs: dict, machine, totals=None) -> tuple:
    m = Machine(totals=totals)
    card = Card(m, machine)
    for mesh, ps in sorted(pkgs.items()):
        Node(card, mesh, ps)
    m.run()
    return m, card


def _rest(m: Machine, card: Card) -> list:
    out = []
    for u in card.units.values():
        out += [Finding("rest", u.name, t) for t in u.rest()]
    for node in card.nodes.values():
        out += [Finding("rest", node.name, t) for t in node.rest()]
    for name, c in m.counters.items():
        kind, _, rest = name.partition(":")
        if kind not in ("bell", "slot"):
            continue
        mesh, _, which = rest.partition(":")
        key = (int(mesh), (kind, int(which)))
        taken = card.taken.get(key, 0)
        # A count no wait of the program reads only grows: the program's
        # reruns cannot tell.
        if key in card.waited and c.value != taken:
            out.append(Finding("rest", f"node {mesh}", f"{kind} {which}: {c.value - taken} never taken"))
    # A DATA_RECEIVED belongs to the package whose step sent the burst it
    # answers; the next package counts completions from zero.
    for node, dst, sent, landed in card.acks:
        at = sent.v.get(node.ix, 0)
        end = next((c for n, c in node.ends if n >= at), None)
        port = dst.port.ix
        if end is None or end.v.get(port, 0) < landed.v.get(port, 0):
            out.append(Finding("rest", node.name, f"a DATA_RECEIVED from {dst.name} may arrive after its package ends"))
    return out


def _ports(card: Card) -> list:
    """P1: bursts from different senders into one unit land one after the
    other (each landing happens before the next sender's send). P2: a burst
    into a sharer of a shared fill lands before the lead's arrival or is sent
    after the sharer's own."""
    out = []
    for u in card.units.values():
        port = u.port.ix
        prev = None
        for owner, sent, landed in u.landings:
            if prev is not None and prev[0] != owner and prev[2].v.get(port, 0) > sent.v.get(port, 0):
                out.append(
                    Finding(
                        "port",
                        u.name,
                        f"a burst from {card.m.procs[owner].name} is not ordered after "
                        f"the one from {card.m.procs[prev[0]].name}: their flits can interleave",
                    )
                )
            prev = (owner, sent, landed)
    out += _collisions(card)
    for u, group, lead, k, mine, me in card.windows:
        lead_at, _ = card.lead_arrival(group, lead, k)
        if lead_at is None:
            continue
        port = u.port.ix
        for owner, sent, landed in u.landings:
            before = landed.v.get(port, 0) <= lead_at.v.get(port, 0)
            after = mine.v.get(me, 0) <= sent.v.get(me, 0)
            if not (before or after):
                out.append(
                    Finding(
                        "port",
                        u.name,
                        f"a burst from {card.m.procs[owner].name} may queue behind the "
                        f"responses of shared fill {k} before this unit reaches it",
                    )
                )
    return out


def _collisions(card: Card) -> list:
    """P3: an L1 burst into a cluster lands before a memory FILL's responses
    can start, or is sent after the FILL completed. A cluster's FILLs are in
    its sequencer's order, so the FILLs completed before a burst's sending are
    a prefix; the rest must all start after the burst landed."""
    out = []
    for u in card.units.values():
        windows = getattr(u, "fill_windows", None)
        if not windows:
            continue
        seq, port = u.seq.ix, u.port.ix
        ends = [end.v.get(seq, 0) for _, end in windows]
        # A FILL of this cluster's own starts at its sequencer's previous
        # event: those starts only grow. A shared FILL's starts at its lead's.
        own = [start.v.get(seq, 0) >= end.v.get(seq, 0) - 1 for start, end in windows]
        shared = [i for i, o in enumerate(own) if not o]
        for landed, sent, what in u.l1_bursts:
            at = landed.v.get(port, 0)
            first = bisect.bisect_right(ends, sent.v.get(seq, 0))
            bad = None
            for i in range(first, len(windows)):
                if at > windows[i][0].v.get(port, 0):
                    bad = i
                    break
                if own[i]:
                    later = shared[bisect.bisect_right(shared, i):]
                    bad = next((j for j in later if at > windows[j][0].v.get(port, 0)), None)
                    break
            if bad is not None:
                out.append(Finding("port", u.name, f"{what} may land while FILL {bad}'s responses arrive"))
    return out


__all__ = ["Finding", "ScheduleError", "check", "schedule"]
