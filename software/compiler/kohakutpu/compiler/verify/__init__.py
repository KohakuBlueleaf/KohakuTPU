"""The static checker and the dispatch scheduler
(docs/projects/kohakutpu/verify.md).

    pkgs = verify.schedule({0: prog}, machine, fetch, residents)
    findings = verify.check({0: prog}, machine)
    verify.gate({0: prog}, machine, mode="refuse")   # raises Refused

`schedule` builds each node's package and orders each segment's dispatches
on the abstract machine: deadlock-free by construction, or ScheduleError.
`check` schedules a program's first run and its rerun (the vector cores'
programs then resident) and runs both on the machine. Finding kinds:
deadlock, rest (a count left for the next run), port (bursts into one unit
not ordered, or queued behind a shared fill), bounds (a burst the receiver
rejects), route (aimed where no unit is), loop (a node loop on data, run a
bounded number of trips), model (what the model does not decide). `gate`
is `check` before a run: it refuses one with a finding of a REFUSE kind.
"""

import bisect
import collections
import functools
import hashlib
import json
import os
import pathlib
import pickle

from kohakutpu.compiler import imem
from kohakutpu.compiler import target as T
from kohakutpu.compiler.isa.vector import ISA as VEC
from kohakutpu.compiler.l2 import banks
from kohakutpu.compiler.verify.card import Card
from kohakutpu.compiler.verify.model import Finding, Machine
from kohakutpu.compiler.verify.node import Node

#: Finding kinds a run is refused for: each names a way the run can go wrong.
#: `model` (what the abstract machine does not decide) and `loop` (a node loop
#: on data, run a bounded number of trips) are reported, not refused.
REFUSE = ("deadlock", "rest", "port", "race", "bounds", "route")
GATES = ("refuse", "warn", "off")


class ScheduleError(Exception):
    def __init__(self, findings: list) -> None:
        self.findings = findings
        super().__init__(
            "no dispatch order completes:\n" + "\n".join(str(f) for f in findings[:20])
        )


class Refused(Exception):
    """A run the checker finds a REFUSE kind of finding in."""

    def __init__(self, findings: list) -> None:
        self.findings = findings
        bad = [f for f in findings if f.kind in REFUSE]
        super().__init__(
            f"the static checker refuses this run ({len(bad)} findings; "
            "check='warn' runs it anyway):\n" + "\n".join(str(f) for f in bad[:20])
        )


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
    cores = {
        mesh: residents.setdefault(mesh, imem.Cores(imem.IMEM_WORDS))
        for mesh in programs
    }
    # A build is a function of the program and its cores' whole state (IMEM
    # placement and eviction order, descriptors): the same pair seen again
    # returns its packages and leaves the cores as that build did.
    bkey = (
        repr(machine),
        repr(fetch),
        tuple((m, id(p), cores_digest(cores[m])) for m, p in sorted(programs.items())),
    )
    hit = _BUILT.get(bkey)
    if hit is not None and all(hit[0][m] is p for m, p in programs.items()):
        _BUILT.move_to_end(bkey)
        for mesh, snap in hit[2].items():
            cores_restore(cores[mesh], snap)
        return dict(hit[1])
    held = {mesh: _held(residents, mesh) for mesh in programs}
    built = {}
    for mesh, prog in programs.items():
        b = prog.build(fetch, residents.setdefault(mesh, imem.Cores(imem.IMEM_WORDS)))
        built[mesh] = (b.build(defaults=False), b.bindings())
    # The order is a function of the machine, the fetch port, what the cores
    # hold and the packages' bytes (bindings are outside them): a package
    # sent again with new buffers is not run on the model again.
    h = hashlib.sha256(f"{machine!r}|{fetch!r}|{held!r}".encode())
    for mesh, (pkg, _) in sorted(built.items()):
        h.update(f"|{mesh}|".encode() + pkg.to_bytes())
    k = h.digest()
    got = _SCHEDULED.get(k)
    if got is not None:
        _SCHEDULED.move_to_end(k)
        return _keep(bkey, programs, cores, got, built)
    got = _order_stored(k)
    if got is not None:
        _SCHEDULED[k] = got
        return _keep(bkey, programs, cores, got, built)
    m = Machine()
    card = Card(m, machine)
    for mesh, units in held.items():
        for coord, mem, desc in units:
            u = card.unit((mesh, *coord))
            if u is not None and hasattr(u, "imem"):
                u.imem.update(mem)
                u.desc.update(desc)
    nodes = {
        mesh: Node(card, mesh, [pkg], free=True)
        for mesh, (pkg, _) in sorted(built.items())
    }
    try:
        m.run()
        stuck = [f for f in m.findings if f.kind == "deadlock"]
        if stuck:
            raise ScheduleError(stuck)
        got = {mesh: nodes[mesh].packages()[0] for mesh in built}
    finally:
        card.close()
    _SCHEDULED[k] = got
    if len(_SCHEDULED) > SCHEDULED_MAX:
        _SCHEDULED.popitem(last=False)
    _order_store(k, got)
    return _keep(bkey, programs, cores, got, built)


#: Orders `schedule` found, on disk by what decides them and this checker's
#: source; the variable names another directory, empty turns the store off.
ORDER_ENV = "KTPU_SCHEDULE_CACHE"
ORDER_DIR = pathlib.Path(__file__).resolve().parents[5] / "build" / "schedule"


def _order_path(k: bytes) -> pathlib.Path | None:
    where = os.environ.get(ORDER_ENV, str(ORDER_DIR))
    if not where:
        return None
    return pathlib.Path(where) / hashlib.sha256(_source_digest() + k).hexdigest()[:40]


def _order_stored(k: bytes) -> dict | None:
    path = _order_path(k)
    if path is None or not path.exists():
        return None
    return pickle.loads(path.read_bytes())


def _order_store(k: bytes, got: dict) -> None:
    path = _order_path(k)
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_bytes(pickle.dumps(got))
    tmp.replace(path)


#: Orders `schedule` found, by what decides them, and whole builds by program
#: and cores' state (each holds a copy of every core's IMEM and its programs:
#: ~5 MB of host memory an entry of a convolution net's step, measured); the
#: least recently used past SCHEDULED_MAX / BUILT_MAX are dropped.
SCHEDULED_MAX = 4096
BUILT_MAX = 256
_SCHEDULED: collections.OrderedDict = collections.OrderedDict()
_BUILT: collections.OrderedDict = collections.OrderedDict()


def _keep(bkey, programs, cores, got, built) -> dict:
    """`schedule`'s result, kept under `bkey` with the cores' state after it
    (the programs held, so an id in the key is never another program's)."""
    out = {mesh: (got[mesh], built[mesh][1]) for mesh in built}
    after = {mesh: cores_snapshot(c) for mesh, c in cores.items()}
    _BUILT[bkey] = (dict(programs), out, after)
    if len(_BUILT) > BUILT_MAX:
        _BUILT.popitem(last=False)
    return dict(out)


def cores_snapshot(cores: imem.Cores) -> dict:
    return {
        coord: (tuple(r.slots.items()), r.last, dict(r.desc), dict(r.mem))
        for coord, r in cores.items()
    }


def cores_restore(cores: imem.Cores, snap: dict) -> None:
    for coord, (slots, last, desc, mem) in snap.items():
        r = cores[coord]
        r.slots = collections.OrderedDict(slots)
        r.last, r.desc, r.mem = last, dict(desc), dict(mem)


def cores_digest(cores: imem.Cores) -> bytes:
    """A digest of every core's whole state: slots in eviction order, the
    last base, descriptors and IMEM words."""
    h = hashlib.sha256()
    for coord, r in sorted(cores.items()):
        h.update(
            repr(
                (coord, tuple(r.slots.items()), r.last, sorted(r.desc.items()))
            ).encode()
        )
        h.update(repr(sorted(r.mem.items())).encode())
    return h.digest()


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
    first, first_card = run(pkgs, machine)
    first_card.close()
    m, card = run(pkgs, machine, first.tally())
    try:
        out = list(m.findings)
        if any(f.kind == "deadlock" for f in out):
            return out
        return out + _rest(m, card) + _ports(card)
    finally:
        card.close()


def gate(programs, machine=None, fetch=None, mode: str = "refuse", cache=None) -> list:
    """`check`'s findings for a run about to start. ``mode``: "refuse" raises
    :class:`Refused` when any finding is of a REFUSE kind, "warn" returns
    them all, "off" checks nothing and returns ``[]``. With `cache` (a
    directory), the findings are kept in ``<cache>/<key>.json`` and read back
    for the same programs, machine and checker."""
    if mode not in GATES:
        raise ValueError(f"check mode {mode!r}: one of {GATES}")
    if mode == "off":
        return []
    path = None
    if cache is not None:
        path = pathlib.Path(cache) / f"{key(programs, machine, fetch)}.json"
    rows = _load(path) if path is not None else None
    if rows is not None:
        found = [Finding(*r) for r in rows]
    else:
        found = check(programs, machine, fetch)
        if path is not None:
            rows = [[f.kind, f.where, f.text] for f in found]
            _store(path, json.dumps(rows))
    if mode == "refuse" and any(f.kind in REFUSE for f in found):
        raise Refused(found)
    return found


def _load(path: pathlib.Path):
    """A cache entry's rows; None when there is none, or another process
    holds it while it is being renamed in (Windows): a miss, computed again
    to the same rows."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, PermissionError):
        return None


def _store(path: pathlib.Path, text: str) -> None:
    """Write a cache entry whole or not at all: the bytes reach the disk in a
    sibling file before it is renamed over `path`, so a power loss leaves the
    old entry or none, never a truncated one that every later gate reads. An
    entry's rows are its key's, so one another process already stored is
    kept (a rename onto it may be refused while that process reads it)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    try:
        os.replace(tmp, path)
    except PermissionError:
        if not path.exists():
            raise
        tmp.unlink()


def key(programs, machine=None, fetch=None) -> str:
    """A digest of what `check` decides from: the checker's own source, the
    machine, the fetch port, and each node's package as built against empty
    vector cores (every image it loads, every dispatch)."""
    programs = _nodes(programs)
    machine = machine or T.machine(nodes=max(programs) + 1)
    h = hashlib.sha256(_source_digest())
    h.update(repr(machine).encode())
    h.update(repr(fetch).encode())
    for mesh, prog in sorted(programs.items()):
        built = prog.build(fetch, imem.Cores(imem.IMEM_WORDS))
        h.update(str(mesh).encode())
        h.update(built.build(defaults=False).to_bytes())
    return h.hexdigest()[:32]


@functools.cache
def _source_digest() -> bytes:
    h = hashlib.sha256()
    for p in sorted(pathlib.Path(__file__).parent.glob("*.py")):
        h.update(p.read_bytes())
    return h.digest()


def lint(module, machine=None) -> list:
    """`bank` findings for every image of `module` on `machine`'s vector
    cores: math beats that hold or starve for want of register-file read
    copies (docs/projects/kohakutpu/verify.md s10)."""
    return banks.lint(module, T.vcopies(machine or T.machine()))


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
            out.append(
                Finding(
                    "rest",
                    f"node {mesh}",
                    f"{kind} {which}: {c.value - taken} never taken",
                )
            )
    # A DATA_RECEIVED belongs to the package whose step sent the burst it
    # answers; the next package counts completions from zero.
    for node, dst, sent, landed in card.acks:
        at = sent.at(node.ix)
        end = next((c for n, c in node.ends if n >= at), None)
        port = dst.port.ix
        if end is None or end.at(port) < landed.at(port):
            out.append(
                Finding(
                    "rest",
                    node.name,
                    f"a DATA_RECEIVED from {dst.name} may arrive after its package ends",
                )
            )
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
            if (
                prev is not None
                and prev[0] != owner
                and prev[2].at(port) > sent.at(port)
            ):
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
            before = landed.at(port) <= lead_at.at(port)
            after = mine.at(me) <= sent.at(me)
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
        ends = [end.at(seq) for _, end in windows]
        # A FILL of this cluster's own starts at its sequencer's previous
        # event: those starts only grow. A shared FILL's starts at its lead's.
        own = [start.at(seq) >= end.at(seq) - 1 for start, end in windows]
        shared = [i for i, o in enumerate(own) if not o]
        for landed, sent, what in u.l1_bursts:
            at = landed.at(port)
            first = bisect.bisect_right(ends, sent.at(seq))
            bad = None
            for i in range(first, len(windows)):
                if at > windows[i][0].at(port):
                    bad = i
                    break
                if own[i]:
                    later = shared[bisect.bisect_right(shared, i) :]
                    bad = next((j for j in later if at > windows[j][0].at(port)), None)
                    break
            if bad is not None:
                out.append(
                    Finding(
                        "port",
                        u.name,
                        f"{what} may land while FILL {bad}'s responses arrive",
                    )
                )
    return out


__all__ = [
    "GATES",
    "REFUSE",
    "Finding",
    "Refused",
    "ScheduleError",
    "check",
    "cores_digest",
    "cores_restore",
    "cores_snapshot",
    "gate",
    "key",
    "lint",
    "schedule",
]
