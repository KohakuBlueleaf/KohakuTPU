"""The two-node reduce as package steps: what each node runs, and the sum.

Two model devices stand in for two meshes; their movers write through one
router that sends an address to whichever device's arena holds it -- the part
of the interlink a model needs. Doorbells are the firmware's to order and the
reference interpreter runs packages one at a time, so RING and WAIT_BELL are
checked by STRUCTURE here and by sw_reduce.py on the card model.
"""

import numpy as np
from kohakuaccel.package.format import Op, Package
from kohakuaccel.package.interp import LocalNode
from kohakuaccel.package.lower import machine_units
from kohakuaccel.sim.machine import MEM_BASE
from kohakuaccel.sim.mailbox import SimMailbox
from kohakutpu.isa.fields import FIELDS
from kohakutpu.meshes import MeshGroup
from kohakutpu.model import SimDevice, run_move

from kohakutpu import ops


class Routed:
    """One memory over several devices, by which arena holds the address."""

    def __init__(self, devices) -> None:
        self.devices = devices

    def _mem(self, off: int):
        addr = off + MEM_BASE
        for d in self.devices:
            if d.arena.base <= addr < d.arena.base + d.arena.size:
                return d.card.mem
        raise KeyError(f"{addr:#x} is in no device's arena")

    def read(self, off: int, n: int) -> bytes:
        return self._mem(off).read(off, n)

    def write(self, off: int, data: bytes) -> None:
        self._mem(off).write(off, data)


def _group():
    devs = [
        SimDevice(
            mg=((1, 1),), vc=((1, 0),), agent=(0, 1), base=MEM_BASE + base, size=1 << 22
        )
        for base in (0x10_0000, 0x50_0000)
    ]
    routed = Routed(devs)
    for d in devs:
        d.node = LocalNode(
            SimMailbox(d.card),
            machine_units(d.machine),
            mover=lambda wr: run_move(wr, routed),
        )
        d.fields = FIELDS
        d.keep_packages = True
    return MeshGroup(devs)


def test_node_reduce_sums_the_partials_and_says_how():
    rng = np.random.default_rng(9)
    a = (rng.standard_normal((32, 128)) * 0.5).astype(np.float16)
    b = (rng.standard_normal((32, 128)) * 0.5).astype(np.float16)
    g = _group()
    got = g.matmul_node_reduce(g.split(a, 1), g.split(b, 1), into=0).numpy()

    one = SimDevice(mg=((1, 1),), vc=((1, 0),), agent=(0, 1))
    p0 = ops.matmul(one.tensor(a[:, :64]), one.tensor(b[:, :64]))
    p1 = ops.matmul(one.tensor(a[:, 64:]), one.tensor(b[:, 64:]))
    want = ops.residual(p0, p1).numpy()
    assert np.array_equal(got, want)

    sender = [Package.from_bytes(p) for p in g[1].packages]
    receiver = [Package.from_bytes(p) for p in g[0].packages]
    ops1 = [s.op for p in sender for s in p.steps]
    ops0 = [s.op for p in receiver for s in p.steps]
    assert ops1.index(Op.MOVER) < ops1.index(Op.RING)
    assert (
        ops0.index(Op.WAIT_BELL) < len(ops0) - 1
        and Op.DISPATCH in ops0[ops0.index(Op.WAIT_BELL) :]
    )
    ring = next(s for p in sender for s in p.steps if s.op == Op.RING)
    assert ring.unit == g[0].machine.default
