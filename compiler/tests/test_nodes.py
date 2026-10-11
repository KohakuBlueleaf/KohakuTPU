"""`kohakutpu.nodes.Nodes`: how a stage is cut over nodes and where they wait.

The devices here record what they are handed and build real packages, so the
assertions read the steps a node would run: which instances each node got, and
the RING / WAIT_BELL / BARRIER steps that order one node's reads after another
node's writes on shared memory.
"""

import pytest
from kohakuaccel.machinespec import MachineSpec
from kohakuaccel.package.build import PackageBuilder
from kohakuaccel.package.format import Op
from kohakutpu.nodes import Nodes

from kohakutpu import tiling

MG = ((1, 0), (2, 0))
VC = ((1, 1),)


class FakeNode:
    def __init__(self):
        self.submitted = []

    def submit(self, pkg, bindings):
        self.submitted.append(pkg)
        return len(self.submitted)

    def wait(self, tag):
        return tag


class FakeDevice:
    """A node-dispatched device that appends each stage to its open package."""

    def __init__(self, mesh: int):
        self.machine = MachineSpec(name="t", units={"MG": MG, "VC": VC}, default=mesh)
        self.node = FakeNode()
        self.transport = self.ctrl = None
        self.keep_packages = False
        self.engine_packages = False
        self.packages = []
        self.counters = {}
        self._pending = None
        self.calls = []

    def _package(self):
        if self._pending is None:
            types = {c: "MG" for c in MG} | {c: "VC" for c in VC}
            self._pending = PackageBuilder(types=types, mesh=self.machine.default)
        return self._pending

    def dispatch(self, payloads, unit, name="kernel", nodes=None, acks=None):
        b = self._package()
        self.calls.append((sorted(payloads), unit, nodes, acks))
        coords = nodes or self.machine.coords(unit)
        for i, key in enumerate(sorted(payloads)):
            b.dispatch(b.unit(coords[i % len(coords)]), payloads[key])
        b.barrier()
        return 1

    def move(self, writes, name="move"):
        self.calls.append(("move",))


def steps(dev) -> list:
    """`(op, unit or mesh)` of every step in the device's open package."""
    return [(s.op, s.unit) for s in dev._package().steps]


def group(n: int = 2):
    devs = [FakeDevice(m) for m in range(n)]
    return Nodes(devs, base=0x40_0000, size=0x40_0000), devs


def stage(n: int) -> dict:
    return {(i,): [i + 1] for i in range(n)}


def test_a_stage_is_cut_into_contiguous_shares_one_per_node():
    g, (a, b) = group()
    g.dispatch(stage(5), "MG")
    assert [c[0] for c in a.calls] == [[(0,), (1,)]]
    assert [c[0] for c in b.calls] == [[(2,), (3,), (4,)]]
    assert g.machine.dispatchers == 2 and g.counters["barriers"] == 0


def test_a_stage_after_a_shared_one_waits_for_every_other_writer():
    g, (a, b) = group()
    g.dispatch(stage(4), "MG")
    g.dispatch(stage(4), "MG")
    # Each node: its first stage, a ring to the other, a wait for the other,
    # a barrier, then its second stage.
    for dev, other in ((a, 1), (b, 0)):
        got = steps(dev)
        ring = got.index((Op.RING, other))
        wait = got.index((Op.WAIT_BELL, other))
        later = [i for i, s in enumerate(got) if s[0] == Op.DISPATCH][-1]
        assert ring < wait < later
    assert g.counters["barriers"] == 1


def test_a_single_reader_waits_only_for_the_other_writers():
    g, (a, b) = group()
    g.dispatch(stage(2), "MG")
    g.dispatch(stage(1), "MG")
    assert (Op.WAIT_BELL, 1) in steps(a) and (Op.RING, 0) not in steps(a)
    assert (Op.RING, 0) in steps(b) and (Op.WAIT_BELL, 0) not in steps(b)


def test_a_node_reading_only_its_own_writes_does_not_wait():
    g, (a, b) = group()
    g.dispatch(stage(1), "MG")
    g.dispatch(stage(1), "MG")
    assert not [s for s in steps(a) if s[0] in (Op.RING, Op.WAIT_BELL)]
    assert b._pending is None


def test_flush_runs_every_node_and_clears_the_order():
    g, (a, b) = group()
    g.dispatch(stage(4), "MG")
    g.flush()
    assert len(a.node.submitted) == len(b.node.submitted) == 1
    g.dispatch(stage(4), "MG")
    assert g.counters["barriers"] == 0


def test_a_pinned_pair_runs_whole_on_one_node():
    g, (a, b) = group()
    g.dispatch(stage(2), "MG", acks={(0,): ((1, 1), 1)})
    g.dispatch(stage(1), "VC", nodes=VC)
    assert len(a.calls) == 2 and not b.calls
    g.dispatch(stage(2), "MG", acks={(0,): ((1, 1), 1)})
    assert len(b.calls) == 1


def test_meshes_that_differ_are_refused():
    a, b = FakeDevice(0), FakeDevice(1)
    b.machine = MachineSpec(name="t", units={"MG": MG[:1], "VC": VC}, default=1)
    with pytest.raises(ValueError, match="carries"):
        Nodes([a, b], base=0x40_0000, size=0x40_0000)


def test_the_tiler_prices_a_share_per_dispatcher():
    one = tiling.cost(16, 16, 16, 1, 4, 4, 4, 2)
    two = tiling.cost(16, 16, 16, 1, 4, 4, 4, 2, dispatchers=2)
    assert two.words == one.words and two.cycles < one.cycles


def test_fetched_words_cost_memory_not_firmware():
    """64 small instances: the mailbox's per-word cost dominates; streamed by a
    fetch port the same plan is bound by fills and sweeps."""
    sent = tiling.cost(64, 64, 16, 1, 4, 8, 8, 2)
    fetched = tiling.cost(64, 64, 16, 1, 4, 8, 8, 2, word=tiling.FETCH_WORD_CYCLES)
    assert fetched.cycles < sent.cycles / 2
