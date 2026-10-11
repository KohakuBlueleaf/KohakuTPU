"""The framework's L2 (`kohakuaccel.ir.l2`) with no project: derived
dependences, the checker's refusals, and the L2 -> L1 compiler's sync and node
order on a generic machine with toy lowerers.

The sync witness walks the emitted L1 steps itself: for every cross-unit
dependence, the producer's completing word must be awaited before the
consumer's first word is sent.
"""

import random
from dataclasses import dataclass

import pytest
from kohakuaccel.ir.l2 import Schedule, ScheduleError
from kohakuaccel.ir.l2.lower import Chunk, compile
from kohakuaccel.machinespec import MachineSpec
from kohakuaccel.package.units import unit_types

MACHINE = MachineSpec(
    name="tiny", units={"UA": ((1, 0), (2, 0)), "UB": ((3, 0),)}, inst_depth=64
)
UA0, UA1, UB = (1, 0), (2, 0), (3, 0)


@dataclass(frozen=True)
class Word:
    """An L1 op of one word: the item it belongs to, and which of its words."""

    item: int
    n: int = 0

    def flits(self) -> list[int]:
        return [self.item * 100 + self.n]


class Toy:
    """One chunk an item: `params["words"]` words, `params["cycles"]` long. With
    `hold`, an item's last word goes out with the NEXT item (a held result)."""

    def __init__(self, coord) -> None:
        self.held = None

    def lower(self, i, item) -> list:
        own = [Word(i, n) for n in range(item.params.get("words", 1))]
        ops, done = [], []
        if self.held is not None:
            ops.append(self.held[0])
            done.append(self.held[1])
            self.held = None
        if item.params.get("hold"):
            self.held, own = (own[-1], i), own[:-1]
        else:
            done.append(i)
        return [Chunk(ops + own, tuple(done), item.params.get("cycles", 10.0))]

    def finish(self) -> list:
        if self.held is None:
            return []
        out = [Chunk([self.held[0]], (self.held[1],), 1.0)]
        self.held = None
        return out


LOWER = {"UA": Toy, "UB": Toy}


def witness(schedule, prog):
    """Raise unless every cross-unit dependence is awaited before its consumer."""
    sent: dict = {}
    awaited: dict = {}
    marks: dict = {}
    where: dict = {}  # item -> (unit, position after its last word)
    firstsend: dict = {}  # item -> {unit: awaited counts at its first word}
    moved = [i for i, it in enumerate(schedule.items) if it.unit == "mover"]
    posts = 0
    for step in prog.steps:
        if step[0] == "send":
            u = step[1]
            for w in step[2]:
                item = w // 100
                if item not in firstsend:
                    firstsend[item] = dict(awaited)
                sent[u] = sent.get(u, 0) + 1
                where[item] = (u, sent[u])
        elif step[0] == "mark":
            marks[step[2]] = sent.get(step[1], 0)
        elif step[0] == "wait":
            upto = sent.get(step[1], 0) if step[2] is None else marks[step[2]]
            awaited[step[1]] = max(awaited.get(step[1], 0), upto)
        elif step[0] == "post":
            # mover items post in schedule order, one post each
            item = moved[posts]
            posts += 1
            firstsend[item] = dict(awaited)
            where[item] = ("mover", posts)
        elif step[0] == "wait_moves":
            awaited["mover"] = max(awaited.get("mover", 0), step[1] + 1)
        elif step[0] in ("barrier", "move"):
            awaited = dict(sent) | {"mover": posts}
    for b, ds in enumerate(schedule.deps()):
        for a in ds:
            ua, ub = schedule.items[a].at, schedule.items[b].at
            if ua == ub and schedule.items[a].unit != "mover":
                continue
            unit, pos = where[a]
            if unit == "mover" and schedule.items[b].unit == "mover":
                continue
            assert (
                firstsend[b].get(unit, 0) >= pos
            ), f"item {b} sent before item {a} is awaited"


def buf(s, name, n=64):
    return s.buffer(name, n, base=0x1000 * (len(s.buffers) + 1))


def test_dependences_are_raw_war_waw_on_overlapping_bytes():
    s = Schedule()
    x, y = buf(s, "x"), buf(s, "y")
    a = s.add("p", "UA", writes=[x.view(0, 32)], at=UA0)
    b = s.add("c", "UB", reads=[x.view(16, 16)], at=UB)  # RAW on a
    c = s.add("p", "UA", writes=[x.view(32, 32)], at=UA1)  # disjoint bytes
    d = s.add("w", "UA", writes=[x.view(0, 24)], at=UA0)  # WAW a, WAR b
    e = s.add("q", "UB", reads=[y.view()], at=UB)  # another buffer
    deps = s.deps()
    assert (
        deps[b] == {a} and deps[c] == set() and deps[d] == {a, b} and deps[e] == set()
    )


@pytest.mark.parametrize(
    "case,match",
    [
        ("outside", "outside"),
        ("unplaced", "not placed"),
        ("type", "is not one"),
        ("package", "later package"),
        ("alias", "overlap in memory"),
        ("nested", "x and z overlap"),
        ("local", "touches"),
    ],
)
def test_the_checker_refuses(case, match):
    s = Schedule()
    x = buf(s, "x")
    if case == "outside":
        s.add("p", "UA", writes=[x.view(32, 64)], at=UA0)
    elif case == "unplaced":
        s.add("p", "UA", writes=[x.view()])
    elif case == "type":
        s.add("p", "UA", writes=[x.view()], at=UB)
    elif case == "package":
        s.add("p", "UA", writes=[x.view()], at=UA0, package=1)
        s.add("c", "UB", reads=[x.view()], at=UB, package=0)
    elif case == "alias":
        s.buffer("y", 64, base=x.base + 32)
    elif case == "nested":
        # x spans both: y sits past x's start, z past y's end but inside x.
        s.buffers.remove(x)
        x = s.buffer("x", 4096, base=0x1000)
        s.buffer("y", 64, base=0x1100)
        s.buffer("z", 64, base=0x1800)
    elif case == "local":
        loc = s.buffer("l1", 64, space=("local", UA0))
        s.add("p", "UA", writes=[loc.view()], at=UA1)
    with pytest.raises(ScheduleError, match=match):
        s.verify(unit_types(MACHINE))


def test_a_consumer_waits_for_its_producer_and_independent_work_goes_first():
    """UB's independent item is dispatched before the node blocks on UA."""
    s = Schedule()
    x, y = buf(s, "x"), buf(s, "y")
    a = s.add("p", "UA", {"cycles": 100}, writes=[x.view()], at=UA0)
    s.add("c", "UB", reads=[x.view()], at=UB)
    s.add("i", "UB", writes=[y.view()], at=UA1 and UB)
    (prog,) = compile(s, MACHINE, LOWER)
    witness(s, prog)
    kinds = [st[0] for st in prog.steps]
    assert kinds.index("wait") > kinds.index("mark")
    sends_before_wait = [
        st for st in prog.steps[: kinds.index("wait")] if st[0] == "send"
    ]
    assert any(st[1] == UA0 for st in sends_before_wait)
    assert a == 0


def test_waits_follow_the_producers_finish_order():
    """Two producers; the faster one's consumer is unblocked first."""
    s = Schedule()
    x, y = buf(s, "x"), buf(s, "y")
    s.add("slow", "UA", {"cycles": 5000}, writes=[x.view()], at=UA0)
    s.add("fast", "UA", {"cycles": 50}, writes=[y.view()], at=UA1)
    s.add("cx", "UB", reads=[x.view()], at=UB)
    s.add("cy", "UB", reads=[y.view()], at=UB)
    (prog,) = compile(s, MACHINE, LOWER)
    witness(s, prog)
    waits = [st[1] for st in prog.steps if st[0] == "wait" and st[2] is not None]
    # Per-unit order keeps cx before cy on UB, so the node must wait on the
    # slow producer first however fast the other is -- program order wins.
    assert waits[0] == UA0


def test_a_held_result_is_marked_where_it_completes():
    """With `hold`, item 0 completes in item 1's chunk: the mark follows that."""
    s = Schedule()
    x, y = buf(s, "x"), buf(s, "y")
    s.add("p", "UA", {"hold": True, "words": 2}, writes=[x.view()], at=UA0)
    s.add("p2", "UA", {"words": 2}, writes=[y.view()], at=UA0)
    s.add("c", "UB", reads=[x.view()], at=UB)
    (prog,) = compile(s, MACHINE, LOWER)
    witness(s, prog)


def test_a_mover_item_waits_for_its_producers_and_is_no_barrier():
    """The move is posted after its producer; its consumer waits for it; work
    that does not touch its bytes goes out while it runs."""
    s = Schedule()
    x, y, z = buf(s, "x"), buf(s, "y"), buf(s, "z")
    s.add("p", "UA", writes=[x.view()], at=UA0)
    s.add("mv", "mover", reads=[x.view()], writes=[y.view()])
    s.add("c", "UB", reads=[y.view()], at=UB)
    s.add("i", "UA", writes=[z.view()], at=UA1)
    (prog,) = compile(s, MACHINE, LOWER, mover=lambda item: [(0, 1 << 16)])
    witness(s, prog)
    kinds = [st[0] for st in prog.steps]
    m = kinds.index("post")
    assert "wait" in kinds[:m], "the move waits for its producer"
    assert "wait_moves" in kinds[m : kinds.index("send", m)], "the consumer waits"
    assert "move" not in kinds and kinds.count("barrier") == 1
    assert any(
        st[0] == "send" and st[1] == UA1
        for st in prog.steps[: kinds.index("wait_moves")]
    )


def _random_schedule(seed: int) -> Schedule:
    rng = random.Random(seed)
    s = Schedule()
    bufs = [buf(s, f"b{i}") for i in range(4)]
    units = [("UA", UA0), ("UA", UA1), ("UB", UB)]
    for _ in range(30):
        kind, at = rng.choice(units)
        r = [
            rng.choice(bufs).view(rng.randrange(0, 32), 16)
            for _ in range(rng.randrange(0, 3))
        ]
        w = [
            rng.choice(bufs).view(rng.randrange(0, 32), 16)
            for _ in range(rng.randrange(0, 2))
        ]
        s.add(
            "x",
            kind,
            {"cycles": rng.choice((5, 50, 500)), "hold": rng.random() < 0.3},
            reads=r,
            writes=w,
            at=at,
        )
    return s


@pytest.mark.parametrize("seed", range(24))
def test_random_schedules_keep_every_cross_unit_dependence(seed):
    """Held results, mixed costs, dense byte overlaps: no deadlock, no race."""
    s = _random_schedule(seed)
    (prog,) = compile(s, MACHINE, LOWER)
    witness(s, prog)


def test_the_witness_catches_a_missing_wait():
    s = _random_schedule(0)
    (prog,) = compile(s, MACHINE, LOWER)
    assert any(st[0] == "wait" for st in prog.steps)
    prog.steps = [st for st in prog.steps if st[0] != "wait"]
    with pytest.raises(AssertionError, match="before item"):
        witness(s, prog)
