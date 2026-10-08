"""Vector-core relayouts on the card model against the unit models.

    python scripts/py/sw_relayout_probe.py [--build build/sw/vlt_card_v8t8_2n]

Node-dispatched on node 0, each relayout is checked by its RESULT -- the tensor
read back in the new order against the same tensor read in the old:
  1. Tile -> Flat of a matmul result (`RL` plan with transpose: the lane-groups
     mask table and VSHUF);
  2. Flat -> Entry of a vector result (a word permute, no transpose).
The unit models run the same package; a case the models get right and the
card does not is a model/RTL divergence in the relayout program's vector code.
"""

import argparse
import pathlib
import sys
import time

import numpy as np
from kohakuaccel.node.boot import BootArgs, NodeBoot
from kohakuaccel.node.queue import NodeQueue
from kohakuaccel.package.interp import LocalNode
from kohakuaccel.package.lower import machine_units
from kohakuaccel.sim.mailbox import SimMailbox
from kohakuaccel.transport.rebase import UnitGlobal
from kohakuaccel.transport.verilator import VerilatorTransport
from kohakutpu.clock.card import load_board
from kohakutpu.host import Card, board_map
from kohakutpu.isa.fields import FIELDS
from kohakutpu.model import SimDevice, run_move
from kohakutpu.rt import Device

from kohakutpu import layout as LO
from kohakutpu import ops

ROOT = pathlib.Path(__file__).resolve().parents[2]
BOARD = "multimesh_v8t8"


def models() -> SimDevice:
    d = SimDevice(mg=((1, 1),), vc=((1, 0),), agent=(0, 1))
    d.node = LocalNode(
        SimMailbox(d.card),
        machine_units(d.machine),
        mover=lambda wr: run_move(wr, d.card.mem),
    )
    d.fields = FIELDS
    return d


def check(dev, tensor, after) -> str:
    """Elements that differ between `tensor` read in its order and in `after`."""
    before = tensor.numpy()
    tensor.host = None
    tensor.address(after)
    again = dev.get(tensor.buffers[after.key])
    return f"{int(np.sum(before != again))} of {before.size}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", default=str(ROOT / "build/sw/vlt_card_v8t8_2n"))
    ap.add_argument("--fw", default=str(ROOT / "build/fw/kohakutpu_node.elf"))
    a = ap.parse_args()
    m = board_map(load_board(BOARD))
    t = VerilatorTransport(build_dir=a.build)
    card = Card.from_board(
        BOARD, transport=t, which=[0], verify=False, reply_at_agent=True
    )
    mem = UnitGlobal(t, m["dram"], m["mem"], m["size"], staging=True)
    q = NodeQueue(mem, card.mesh.stage_addr(0), idle=lambda: t.run(600))
    q.init()
    dev = Device(card, base=0x40_0000, size=0x40_0000, node=q)
    NodeBoot(t, m["ctrl"][0], 0, sim=t).load(
        a.fw, BootArgs(queue=q.base, scan=2), "burn"
    )
    q.wait_ready()
    ref = models()
    rng = np.random.default_rng(13)
    x = (rng.standard_normal((32, 64)) * 0.5).astype(np.float16)
    w = (rng.standard_normal((32, 64)) * 0.5).astype(np.float16)
    cases = [
        (
            "Tile -> Flat (transpose)",
            lambda d: ops.matmul(d.tensor(x), d.tensor(w)),
            LO.Flat(),
        ),
        (
            "Flat -> Entry(8, 2)",
            lambda d: ops.residual(d.tensor(x), d.tensor(w)),
            LO.Entry(8, 2),
        ),
    ]
    fails = 0
    for name, make, after in cases:
        t0 = time.monotonic()
        card_bad = check(dev, make(dev), after)
        model_bad = check(ref, make(ref), after)
        ok = card_bad.startswith("0 ") and model_bad.startswith("0 ")
        fails += not ok
        print(
            f"  {'ok  ' if ok else 'FAIL'} {name:26s} card: {card_bad} elements "
            f"moved wrong, models: {model_bad} ({time.monotonic() - t0:.1f}s)",
            flush=True,
        )
    q.stop()
    t.close()
    print("PASS" if not fails else f"FAIL ({fails})")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
