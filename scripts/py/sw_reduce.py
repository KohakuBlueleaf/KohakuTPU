"""A two-node reduce, run by the nodes: matmul halves, a push, a doorbell, an add.

    python scripts/py/sw_reduce.py [--build build/sw/vlt_card_v8t8_2n]

Boots the dispatcher on nodes 0 and 1 (each with its queue in its own staging,
units self-enumerated), splits a (32 x 128) @ (128 x 32) matmul on K across
the two, and runs `MeshGroup.matmul_node_reduce`: node 1 computes its partial,
pushes it across the interlink into a buffer on node 0 and rings node 0;
node 0 computes its partial, waits for the ring and adds. The host submits
both packages and reads one result. Checked against numpy and against the unit
models running the same packages.
"""

import argparse
import pathlib
import sys
import time

import numpy as np
from kohakuaccel.node.boot import BootArgs, NodeBoot
from kohakuaccel.node.queue import NodeQueue
from kohakuaccel.package.format import Package
from kohakuaccel.package.interp import LocalNode
from kohakuaccel.package.lower import machine_units
from kohakuaccel.sim.machine import MEM_BASE
from kohakuaccel.sim.mailbox import SimMailbox
from kohakuaccel.transport.rebase import UnitGlobal
from kohakuaccel.transport.verilator import VerilatorTransport
from kohakutpu.clock.card import load_board
from kohakutpu.host import Card, board_map
from kohakutpu.isa.fields import FIELDS
from kohakutpu.meshes import MeshGroup
from kohakutpu.model import SimDevice, run_move
from kohakutpu.rt import Device

ROOT = pathlib.Path(__file__).resolve().parents[2]
BOARD = "multimesh_v8t8"
#: Per-node arenas, apart: the model's 16 MB of DRAM is flat across meshes.
ARENAS = (0x0040_0000, 0x0080_0000)
ARENA_BYTES = 0x0040_0000


class Routed:
    """The models' interlink: one memory, an address to whichever arena holds it."""

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


def models() -> MeshGroup:
    devs = [
        SimDevice(
            mg=((1, 1),),
            vc=((1, 0),),
            agent=(0, 1),
            base=MEM_BASE + b,
            size=ARENA_BYTES,
        )
        for b in ARENAS
    ]
    routed = Routed(devs)
    for d in devs:
        d.node = LocalNode(
            SimMailbox(d.card),
            machine_units(d.machine),
            mover=lambda wr: run_move(wr, routed),
        )
        d.fields = FIELDS
    return MeshGroup(devs)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", default=str(ROOT / "build/sw/vlt_card_v8t8_2n"))
    ap.add_argument("--fw", default=str(ROOT / "build/fw/kohakutpu_node.elf"))
    a = ap.parse_args()
    m = board_map(load_board(BOARD))
    t_all = time.monotonic()
    t = VerilatorTransport(build_dir=a.build)
    card = Card.from_board(
        BOARD, transport=t, which=[0, 1], verify=False, reply_at_agent=True
    )
    mem = UnitGlobal(t, m["dram"], m["mem"], m["size"], staging=True)
    devs = []
    for i in (0, 1):
        mesh = card.select(i)
        q = NodeQueue(mem, mesh.stage_addr(0), idle=lambda: t.run(600))
        q.init()
        dev = Device(card, base=ARENAS[i], size=ARENA_BYTES, node=q)
        dev.keep_packages = True
        NodeBoot(t, m["ctrl"][i], i, sim=t).load(
            a.fw, BootArgs(queue=q.base, mesh=i, scan=mesh.caps.grid_hi + 1), "burn"
        )
        devs.append(dev)
    for d in devs:
        d.node.wait_ready()
    print(f"  both nodes ready ({time.monotonic() - t_all:.1f}s)", flush=True)

    rng = np.random.default_rng(13)
    x = (rng.standard_normal((32, 128)) * 0.5).astype(np.float16)
    w = (rng.standard_normal((32, 128)) * 0.5).astype(np.float16)
    g = MeshGroup(devs)
    t0 = time.monotonic()
    got = g.matmul_node_reduce(g.split(x, 1), g.split(w, 1), into=0).numpy()
    secs = time.monotonic() - t0
    r = models()
    mod = r.matmul_node_reduce(r.split(x, 1), r.split(w, 1), into=0).numpy()
    want = x.astype(np.float32) @ w.astype(np.float32).T
    err = float(np.abs(got - want).max() / np.abs(want).max())
    scale = 2.0 ** (np.floor(np.log2(float(np.abs(mod).max()))) - 10)
    ulp = float(np.abs(got - mod).max() / scale)
    steps = {
        i: [s.op.name for p in d.packages for s in Package.from_bytes(p).steps]
        for i, d in enumerate(devs)
    }
    shape_ok = "RING" in steps[1] and "WAIT_BELL" in steps[0] and "MOVER" in steps[1]
    ok = err <= 0.05 and ulp <= 4 and shape_ok
    print(f"  node 1 ran {steps[1]}")
    print(f"  node 0 ran {steps[0]}")
    print(
        f"  {'ok  ' if ok else 'FAIL'} (x @ w.T) split on K over nodes 0+1: rel err vs "
        f"numpy {err:.2e}, {ulp:.1f} scale-ulp vs models "
        f"({int(np.sum(got != mod))} of {got.size} differ); {secs:.1f}s",
        flush=True,
    )
    s = t.status()
    for d in devs:
        d.node.stop()
    t.close()
    fails = (not ok) + (s["decerr"] != 0)
    print(
        f"  total {time.monotonic() - t_all:.1f}s, {s['sys']} sys cycles, DECERR {s['decerr']:#x}"
    )
    print("PASS" if not fails else f"FAIL ({fails})")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
