"""Two dispatchers talking: a mover push across the interlink, then a doorbell.

    python scripts/py/sw_two_node.py [--build build/sw/vlt_card_v8t8_2n]

Boots the dispatcher firmware on nodes 0 and 1, each with its own queue, and
runs two packages built from the framework's cross-node steps:
  node 1:  WAIT_BELL(mesh 0, 1), SIGNAL           -- submitted first, so it waits
  node 0:  MOVER(copy SRC -> mesh 1's DST), RING(mesh 1)
The ring is issued only once node 0's mover is idle, so when node 1's package
completes the bytes must already be at DST; the host checks them, and that node
1's completion came after the ring and not before it.
"""

import argparse
import pathlib
import sys
import time

from kohakuaccel.device import mover
from kohakuaccel.node.boot import BootArgs, NodeBoot
from kohakuaccel.node.queue import NodeQueue
from kohakuaccel.package.build import PackageBuilder
from kohakuaccel.transport.rebase import UnitGlobal
from kohakuaccel.transport.verilator import VerilatorTransport
from kohakutpu.clock.card import load_board
from kohakutpu.host import board_map

ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC, DST = 0x0060_0000, 0x0070_0000
WORDS = 32  # 32-byte words moved


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", default=str(ROOT / "build/sw/vlt_card_v8t8_2n"))
    ap.add_argument("--fw", default=str(ROOT / "build/fw/kohakutpu_node.elf"))
    a = ap.parse_args()
    m = board_map(load_board("multimesh_v8t8"))
    t0 = time.monotonic()
    t = VerilatorTransport(build_dir=a.build)
    mem = UnitGlobal(t, m["dram"], m["mem"], m["size"], staging=True)
    fails = 0

    queues = []
    for i in (0, 1):
        # Each node's queue in its own staging store (bit 39, mesh in [37:36]).
        q = NodeQueue(mem, 1 << 39 | i << 36, idle=lambda: t.run(600))
        q.init()
        NodeBoot(t, m["ctrl"][i], i, sim=t).load(
            a.fw, BootArgs(queue=q.base, mesh=i), "burn"
        )
        queues.append(q)
    for q in queues:
        q.wait_ready()
    print(f"  both nodes ready ({time.monotonic() - t0:.1f}s)")

    blob = bytes((k * 37 + 11) & 0xFF for k in range(WORDS * 32))
    mem.write_block(m["dram"][0] + SRC, blob)
    mem.write_block(m["dram"][1] + DST, bytes(len(blob)))

    wait = PackageBuilder()
    wait.wait_bell(mesh=0, count=1)
    wait.signal(1, 0xB)
    push = PackageBuilder()
    walk = [(WORDS, 32)]
    push.mover(
        mover.copy(
            mover.Walker(m["dram"][0] + SRC, dims=walk),
            mover.Walker(m["dram"][1] + DST, dims=walk),
            ewidth=mover.W32,
        )
    )
    push.ring(mesh=1, tag=7)

    t1 = time.monotonic()
    waiting = queues[1].submit(wait.build().to_bytes())
    queues[1].poll()
    early = queues[1].progress[:]
    pushed = queues[0].run(push.build().to_bytes())
    got_wait = queues[1].wait(waiting)
    secs = time.monotonic() - t1
    landed = mem.read_block(m["dram"][1] + DST, len(blob)) == blob
    print(f"  node 0 push+ring: {pushed.describe()}")
    print(
        f"  node 1 wait_bell: {got_wait.describe()}  (no progress before the push: {not early})"
    )
    print(
        f"  {len(blob)} bytes at mesh 1 DST after node 1's completion: {'ok' if landed else 'MISSING'}"
    )
    fails += not (pushed.ok and got_wait.ok and landed and not early)
    for i, q in enumerate(queues):
        out = q.read_stdout().strip()
        if out:
            print(f"  node {i}: {out.splitlines()[-1]}")
        q.stop()
    s = t.status()
    print(
        f"  {secs:.1f}s for the exchange; DECERR {s['decerr']:#x}; {s['sys']} sys cycles"
    )
    fails += s["decerr"] != 0
    t.close()
    print("PASS" if not fails else f"FAIL ({fails})")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
