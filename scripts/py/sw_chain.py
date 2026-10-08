"""The converting move on the card model, and a kernel chain that needs it.

    python scripts/py/sw_chain.py [--build build/sw/vlt_card_v8t8_2n]

A matmul FILL reads PRE-QUANTISED operands; one a previous kernel produced is
FP16 in whatever order that kernel left it. Checked, on node 0's dispatcher:
  1. the transform bank: an FP16 operand in `Entry` order, converted by a
     package's MOVER step (mover mode 5, slot 1), must be BYTE-IDENTICAL to the
     host's MXFP7 packing of the same operand -- A side and B side;
  2. the chain: y = a + b on the vector core, then y @ w.T on the cluster. y
     has no host copy, so the runtime walks it into FP16 entries on the vector
     core and converts it with the mover; w was uploaded FP16 and is converted
     the same way -- two MOVER steps, all inside the node's package. Checked
     against numpy and against the unit models running the same.
"""

import argparse
import pathlib
import sys
import time

import numpy as np
from kohakuaccel.node.boot import BootArgs, NodeBoot
from kohakuaccel.node.queue import NodeQueue
from kohakuaccel.package import mover as PM
from kohakuaccel.package.build import PackageBuilder
from kohakuaccel.package.format import Package
from kohakuaccel.package.interp import LocalNode
from kohakuaccel.package.lower import machine_units
from kohakuaccel.sim.mailbox import SimMailbox
from kohakuaccel.transport.rebase import UnitGlobal
from kohakuaccel.transport.verilator import VerilatorTransport
from kohakutpu.clock.card import load_board
from kohakutpu.host import Card, board_map
from kohakutpu.isa.fields import FIELDS
from kohakutpu.model import SimDevice, run_move
from kohakutpu.rt import XFORM_MXFP7, Device

from kohakutpu import layout as LO
from kohakutpu import ops

ROOT = pathlib.Path(__file__).resolve().parents[2]
BOARD = "multimesh_v8t8"
SRC, DST = 0x0080_0000, 0x0090_0000  # the bank witness, inside the model's 16 MB
ARENA_AT, ARENA_BYTES = 0x0040_0000, 0x0040_0000


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", default=str(ROOT / "build/sw/vlt_card_v8t8_2n"))
    ap.add_argument("--fw", default=str(ROOT / "build/fw/kohakutpu_node.elf"))
    a = ap.parse_args()
    m = board_map(load_board(BOARD))
    t_all = time.monotonic()
    t = VerilatorTransport(build_dir=a.build)
    card = Card.from_board(
        BOARD, transport=t, which=[0], verify=False, reply_at_agent=True
    )
    mem = UnitGlobal(t, m["dram"], m["mem"], m["size"], staging=True)
    q = NodeQueue(mem, card.mesh.stage_addr(0), idle=lambda: t.run(600))
    q.init()
    dev = Device(card, base=ARENA_AT, size=ARENA_BYTES, node=q)
    dev.keep_packages = True
    NodeBoot(t, m["ctrl"][0], 0, sim=t).load(
        a.fw, BootArgs(queue=q.base, scan=card.mesh.caps.grid_hi + 1), "burn"
    )
    q.wait_ready()
    print(f"  node ready ({time.monotonic() - t_all:.1f}s)")
    fails = 0
    rng = np.random.default_rng(11)

    # 1 ------------------------------------------- the bank against the packer
    x = (rng.standard_normal((32, 64)) * 2).astype(np.float16)
    for side in (0, 1):
        fp, mx = LO.Entry(8, 2), LO.MxEntry(8, 2, side)
        mem.write_block(SRC, fp.pack(x))
        mem.write_block(DST, bytes(mx.nbytes(x.shape)))
        b = PackageBuilder()
        entries = fp.nbytes(x.shape) // 256
        b.mover(PM.convert(SRC, DST, entries, XFORM_MXFP7, side))
        t0 = time.monotonic()
        c = q.run(b.build().to_bytes())
        got = mem.read_block(DST, mx.nbytes(x.shape))
        want = mx.pack(x)
        same = got == want
        bad = sum(got[i : i + 32] != want[i : i + 32] for i in range(0, len(want), 32))
        fails += not same
        print(
            f"  {'ok  ' if same else 'FAIL'} converting move, side {'AB'[side]}: "
            f"{entries} entries, {c.cycles} node cycles, "
            f"{'byte-identical to the host packer' if same else f'{bad} of {len(want) // 32} words differ'} "
            f"({time.monotonic() - t0:.1f}s)",
            flush=True,
        )

    # 2 ------------------------------------------------------ the kernel chain
    ref = SimDevice(mg=((1, 1),), vc=((1, 0),), agent=(0, 1))
    ref.node = LocalNode(
        SimMailbox(ref.card),
        machine_units(ref.machine),
        mover=lambda wr: run_move(wr, ref.card.mem),
    )
    ref.fields = FIELDS
    av = (rng.standard_normal((32, 64)) * 0.5).astype(np.float16)
    bv = (rng.standard_normal((32, 64)) * 0.5).astype(np.float16)
    w = (rng.standard_normal((32, 64)) * 0.5).astype(np.float16)
    before = len(dev.packages)
    t0 = time.monotonic()
    got = ops.matmul(
        ops.residual(dev.tensor(av), dev.tensor(bv)), dev.tensor(w)
    ).numpy()
    secs = time.monotonic() - t0
    mod = ops.matmul(
        ops.residual(ref.tensor(av), ref.tensor(bv)), ref.tensor(w)
    ).numpy()
    want = (av + bv).astype(np.float32) @ w.astype(np.float32).T
    err = float(np.abs(got - want).max() / np.abs(want).max())
    # In fp16 ulps AT THE RESULT'S SCALE: an element near zero has tiny ulps of
    # its own, and one drain-rounding step there reads as hundreds of them.
    scale = 2.0 ** (np.floor(np.log2(float(np.abs(mod).max()))) - 10)
    ulp = float(np.abs(got - mod).max() / scale)
    print(
        f"    {int(np.sum(got != mod))} of {got.size} elements differ from the "
        f"models, max |card - models| {float(np.abs(got - mod).max()):.3g}"
    )
    pk = [Package.from_bytes(p) for p in dev.packages[before:]]
    steps = sum(len(p.steps) for p in pk)
    movers = sum(1 for p in pk for s in p.steps if s.op.name == "MOVER")
    # Two conversions: y (produced) and w (uploaded FP16) -- every operand.
    ok = err <= 0.05 and ulp <= 4 and movers == 2
    fails += not ok
    print(
        f"  {'ok  ' if ok else 'FAIL'} chain (a + b) @ w.T: rel err vs numpy {err:.2e}, "
        f"{ulp:.1f} scale-ulp vs models; {len(pk)} packages, {steps} steps, {movers} MOVER "
        f"({secs:.1f}s)",
        flush=True,
    )
    s = t.status()
    fails += s["decerr"] != 0
    q.stop()
    t.close()
    print(
        f"  total {time.monotonic() - t_all:.1f}s, {s['sys']} sys cycles, DECERR {s['decerr']:#x}"
    )
    print("PASS" if not fails else f"FAIL ({fails})")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
