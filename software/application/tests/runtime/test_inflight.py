"""Kernels submitted without waiting (L0.NQ.047): `Device.submit` holds each
run's scratch until `collect`, collects the oldest past `max_inflight`, and
frees what it held; `NodeCard` hands each run the report text its node
printed for it, in submission order, whatever order they are collected in."""

from types import SimpleNamespace

from kohakutpu.application.runtime import card as C
from kohakutpu.application.runtime.device import Device
from kohakutpu.application.runtime.packages import Prebuilt
from kohakutpu.compiler import _rs
from kohakutpu.language import machine as MF

MACHINE = (MF.MACHINES / "multimesh_v9t6.json").read_text(encoding="utf-8")
PACKAGE = _rs.package_build('{"credit": 32}', "[]", MACHINE, defaults=False)

PKGT = "PKGT head {0} copy 0 bind 1 steps 2 barrier 3 local 0 total {0}\n"


class Queue:
    """A node queue whose firmware prints `chunks` as they are read."""

    def __init__(self, chunks: list) -> None:
        self.chunks, self.tag = chunks, 0

    def submit(self, raw, bindings, **kw) -> int:
        self.tag += 1
        return self.tag

    def read_stdout(self) -> str:
        return self.chunks.pop(0) if self.chunks else ""


def node_card(chunks: list):
    c = object.__new__(C.NodeCard)
    c.machine_json = MACHINE
    c.queues = {0: Queue(chunks)}
    c._order, c._stdout, c._segs = {}, {}, {}
    return c


def test_each_run_gets_its_own_report_text_in_submission_order():
    # Three runs; the text arrives split across reads, the second's PKGT
    # line in two pieces.
    c = node_card(
        [
            "",
            PKGT.format(10) + "PKGS 0 0 5\nPKGT he",
            "ad 20 total 20\n",
            PKGT.format(30),
        ]
    )
    tags = [c._submit(0, PACKAGE, []) for _ in range(3)]
    # Collected out of order: the third, then the first, then the second.
    third = c._segment(0, tags[2])
    first = c._segment(0, tags[0])
    second = c._segment(0, tags[1])
    assert "head 10" in first and "head 20" in second and "head 30" in third
    assert "PKGS 0 0 5" in second
    assert c._result(first, SimpleNamespace(cycles=1), None)["phases"]["head"] == 10


def test_text_of_runs_nothing_waits_for_is_not_the_next_runs():
    # A replay's or direct `q.run`'s text is read before the next submit.
    c = node_card([PKGT.format(99), PKGT.format(1)])
    tag = c._submit(0, PACKAGE, [])
    assert "head 1 " in c._segment(0, tag)


class Card:
    """A card that queues runs and completes them when collected."""

    def __init__(self) -> None:
        self.queued, self.collected = [], []

    def submit(self, progs):
        self.queued.append(progs)
        return len(self.queued)

    def collect(self, p):
        self.collected.append(p)
        return {"cycles": 100 * p}


def compiled(name: str, scratch: int):
    arena = {"space": "dram", "node": None, "bytes": scratch, "members": [("h0", 0)]}
    return SimpleNamespace(
        name=name,
        l1=SimpleNamespace(
            body=lambda n, level: SimpleNamespace(params=[]), params=lambda n, level: []
        ),
        scratch=[arena],
        programs=lambda binds: {
            None: Prebuilt(PACKAGE, (), MACHINE, 0, (), False, None)
        },
    )


def test_submit_holds_scratch_until_collect_and_bounds_runs_in_flight():
    dev = Device(kind="offline", board="multimesh_v9t6")
    dev.card = Card()
    dev.max_inflight = 2
    used = dev.memory.dram.used
    a = dev.submit(compiled("a", 4096), {})
    b = dev.submit(compiled("b", 4096), {})
    assert dev.card.collected == [] and len(dev.inflight) == 2
    assert dev.memory.dram.used > used
    # A third past max_inflight (2) collects the oldest first.
    c = dev.submit(compiled("c", 4096), {})
    assert dev.card.collected == [1] and dev.inflight == [b, c]
    assert dev.collect(a).cycles == 100
    assert [dev.collect(s).cycles for s in (b, c)] == [200, 300]
    assert dev.inflight == [] and dev.memory.dram.used == used
    assert [r.name for r in dev.runs] == ["a", "b", "c"]


def test_run_is_submit_then_collect():
    dev = Device(kind="offline", board="multimesh_v9t6")
    dev.card = Card()
    r = dev.run(compiled("k", 1024), {})
    assert r.cycles == 100 and dev.inflight == []
