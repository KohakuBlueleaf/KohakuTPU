"""Order between the stages of one node's program (docs/spec/l2-program.md
§4): a later stage waits for an earlier one only where both touch one thing.

An access is a range of a key: ``("mem", BUFFER)`` in bytes, a unit's own
memory (``("vc[1]", "l1")`` words, ``("mg[0]", "A")`` entries of both banks)
or a cluster's receive port (``("mg[0]", "rx")``: FILL responses and CU_DATA
share one entry register). Accesses conflict when one writes and the ranges
meet, unless one unit touches its own memory both times (its in-order run
orders them). The wait is on the earlier access's unit, up to a mark after
that stage. A unit-to-unit transfer's landing is seen by its receiver
(COUNTed bursts it waits for, or DATA_RECEIVED the node waits for), never
by a wait on its sender, whose words retire once their flits left. A `pipe`
group sends a later iteration's words ahead of this one's waits when they
need no wait and share no unit with the stages they pass. A streamed
transfer (`flow`: a ring under credits, arrivals counted by the receiver)
orders itself: its accesses never make the node wait for one another.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Access:
    unit: str  # "mg[0]", "vc[1]", "mover"
    write: bool
    key: tuple
    lo: int
    hi: int
    flow: bool = False  # a streamed transfer's ring: credits and counts order it


def mem(unit: str, write: bool, buf: str, lo: int, hi: int) -> Access:
    return Access(unit, write, ("mem", buf), lo, hi)


def _own(a: Access) -> bool:
    return a.key[0] == a.unit


def clash(a: Access, b: Access) -> bool:
    if a.key != b.key or not (a.write or b.write) or (a.flow and b.flow):
        return False
    if a.lo >= b.hi or b.lo >= a.hi:
        return False
    return not (a.unit == b.unit and _own(a) and _own(b))


@dataclass
class Stage:
    """One stage's node lines and effects. `units` are the units its lines
    send to (a mark after the stage names each); `post` the posted move's
    token name, for a mover stage."""

    lines: list
    units: list
    accesses: list
    group: tuple | None = None  # (loop id, iteration, pipe)
    post: str | None = None
    closes: bool = False  # a blocking move: everything is complete after it
    #: the lines as the segments the node may interleave with another stage's
    segs: list = field(default_factory=list)


@dataclass
class _State:
    out: list = field(default_factory=list)
    #: unit -> emission position of the latest stage waited on through
    done: dict = field(default_factory=dict)
    pos: dict = field(default_factory=dict)  # stage -> emission position
    order: list = field(default_factory=list)  # stages in emission order
    used: set = field(default_factory=set)  # mark names a wait names


def _mark(s: int, unit: str) -> str:
    return f"k{s}_{unit.replace('[', '').replace(']', '')}"


def schedule(stages: list) -> list:
    """The node's lines: the stages in program order, each preceded by the
    waits its conflicts need; a `pipe` group's later
    iterations sent ahead where they need no wait. Ends with a barrier."""
    st = _State()
    for s in range(len(stages)):
        if s in st.pos:
            continue
        if _needs(stages, st, s) and stages[s].group is not None:
            for j in _ahead(stages, s):
                if j not in st.pos and _hoistable(stages, st, j):
                    _emit(stages, st, j)
        _emit(stages, st, s)
    out = []
    for ln in st.out:
        if isinstance(ln, tuple):  # a mark slot
            name = ln[1]
            if name in st.used:
                out.append(f"{name} = mark {ln[2]}")
            continue
        out.append(ln)
    if st.order and stages[st.order[-1]].closes:
        return out
    posts = [stages[s].post for s in st.order if stages[s].post]
    if posts:
        out.append(f"wait moves {posts[-1]}")
    out.append("barrier")
    return out


def _needs(stages, st, s) -> list:
    """``[(earlier stage, access)]`` emitted, not yet complete, that `s` must
    wait for."""
    got = []
    for p in st.order:
        for a in stages[p].accesses:
            if _complete(stages, st, p, a.unit):
                continue
            if any(clash(a, b) for b in stages[s].accesses):
                got.append((p, a))
    return got


def _complete(stages, st, p, unit) -> bool:
    return st.done.get(unit, -1) >= st.pos[p]


def _ahead(stages, s) -> list:
    loop, it, pipe = stages[s].group
    return [
        j
        for j in range(s + 1, len(stages))
        if stages[j].group is not None
        and stages[j].group[0] == loop
        and stages[j].group[1] < it + pipe
    ]


def _hoistable(stages, st, j) -> bool:
    if _needs(stages, st, j):
        return False
    for k in range(j):
        if k in st.pos:
            continue
        if set(stages[k].units) & set(stages[j].units):
            return False
        for a in stages[k].accesses:
            for b in stages[j].accesses:
                if (
                    a.key == b.key
                    and (a.write or b.write)
                    and a.lo < b.hi
                    and b.lo < a.hi
                ):
                    return False
    return True


def _emit(stages, st, s) -> None:
    waits: dict = {}
    for p, a in _needs(stages, st, s):
        if a.unit == "mover":
            waits["mover"] = max(waits.get("mover", -1), st.pos[p])
        else:
            waits[a.unit] = max(waits.get(a.unit, -1), st.pos[p])
    for unit, at in sorted(waits.items(), key=lambda kv: kv[1]):
        p = st.order[at]
        if unit == "mover":
            st.out.append(f"wait moves {stages[p].post}")
            # A posted move's wait covers every move before it.
            for q in st.order[: at + 1]:
                if stages[q].post:
                    st.done["mover"] = max(st.done.get("mover", -1), st.pos[q])
            continue
        name = _mark(p, unit)
        st.used.add(name)
        st.out.append(f"wait {unit} upto={name}")
        st.done[unit] = max(st.done.get(unit, -1), at)
    st.pos[s] = len(st.order)
    st.order.append(s)
    st.out += stages[s].lines
    for u in stages[s].units:
        st.out.append(("mark", _mark(s, u), u))


__all__ = ["Access", "Stage", "clash", "mem", "schedule"]
