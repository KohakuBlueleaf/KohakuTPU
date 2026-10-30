"""Two dispatchers talking: a mover push across the interlink, then a doorbell.

    python software/firmware/tools/two_node.py [--build build/sw/vlt_card_v8t8_2n]

Boots the dispatcher firmware on nodes 0 and 1, each with its own queue, and
runs two packages built from the framework's cross-node steps:
  node 1:  WAIT_BELL(mesh 0, 1), SIGNAL           -- submitted first, so it waits
  node 0:  MOVER(copy SRC -> mesh 1's DST), RING(mesh 1)
The ring is issued only once node 0's mover is idle, so when node 1's package
completes the bytes must already be at DST; the host checks them, and that node
1's completion came after the ring and not before it.
"""

import argparse
import json
import pathlib
import sys
import time

from kohakuaccel.driver.device import mover
from kohakuaccel.driver.node.boot import BootArgs, NodeBoot
from kohakuaccel.driver.node.boot.args import REGION_QUEUE, idle_bound
from kohakuaccel.driver.node.queue import NodeQueue
from kohakuaccel.driver.node.queue import layout as L
from kohakuaccel.driver.transport.rebase import UnitGlobal
from kohakuaccel.driver.transport.verilator import VerilatorTransport
from kohakutpu.compiler import _rs
from kohakutpu.driver.clock.card import load_board
from kohakutpu.driver.host import board_map
from kohakutpu.language import machine as MF

ROOT = pathlib.Path(__file__).resolve().parents[3]
SRC, DST = 0x0060_0000, 0x0070_0000
WORDS = 32  # 32-byte words moved
#: Seconds of modeled time a firmware wait may take (the boot block's bound).
WAIT = 0.02


def packages(src, dst, machine_json, words=WORDS, slot=7):
    wait = _rs.package_build(
        json.dumps(
            {"credit": json.loads(machine_json)["machine"]["unit"]["inst_depth"]}
        ),
        json.dumps([["wait_bell", {"mesh": 0, "count": 1}], ["signal", 1, 0xB]]),
        machine_json,
    )
    walk = [(words, 32)]
    writes = mover.copy(
        mover.Walker(src, dims=walk), mover.Walker(dst, dims=walk), ewidth=mover.W32
    )
    push = _rs.package_build(
        json.dumps(
            {"credit": json.loads(machine_json)["machine"]["unit"]["inst_depth"]}
        ),
        json.dumps([["mover", writes], ["ring", {"mesh": 1, "slot": slot}]]),
        machine_json,
    )
    return wait, push


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", default=str(ROOT / "build/sw/vlt_card_v8t8_2n"))
    ap.add_argument("--fw", default=str(ROOT / "build/fw/kohakutpu_node.elf"))
    a = ap.parse_args()
    board = load_board("multimesh_v8t8")
    m = board_map(board)
    t0 = time.monotonic()
    t = VerilatorTransport(build_dir=a.build)
    mem = UnitGlobal(t, m["dram"], m["mem"], m["size"], staging=True)
    fails = 0

    machine_json = (MF.MACHINES / "multimesh_v9t8.json").read_text(encoding="utf-8")
    layout = L.Layout.from_machine(machine_json)
    queues = []
    for i in (0, 1):
        # Each node's queue in its own staging store (bit 39, mesh in [37:36]).
        q = NodeQueue(mem, 1 << 39 | i << 36, idle=lambda: t.run(600), layout=layout)
        q.init()
        NodeBoot(t, m["ctrl"][i], i, sim=t).load(
            a.fw,
            BootArgs(
                queue=q.base,
                mesh=i,
                timeout=idle_bound(board, WAIT),
                regions=[(q.base, q.size, REGION_QUEUE)],
            ),
            "burn",
        )
        queues.append(q)
    for q in queues:
        q.wait_ready()
    print(f"  both nodes ready ({time.monotonic() - t0:.1f}s)")

    blob = bytes((k * 37 + 11) & 0xFF for k in range(WORDS * 32))
    mem.write_block(m["dram"][0] + SRC, blob)
    mem.write_block(m["dram"][1] + DST, bytes(len(blob)))

    wait, push = packages(m["dram"][0] + SRC, m["dram"][1] + DST, machine_json)

    t1 = time.monotonic()
    waiting = queues[1].submit(wait)
    queues[1].poll()
    early = queues[1].progress[:]
    pushed = queues[0].run(push)
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
