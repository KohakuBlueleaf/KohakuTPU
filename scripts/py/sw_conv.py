"""Convolutions on the card model: the mover's im2col, then a matmul.

    python scripts/py/sw_conv.py [--build build/sw/vlt_card_v8t8_2n]

Node 0's dispatcher runs each convolution in two packages so the costs part:
the im2col (converting MOVER steps that gather every 3x3 window and quantise on
the way) and the matmul over it (with the weights' own converting move). Each
result is checked against a float64 convolution and the unit models running
the same moves; the im2col operand is read back and compared BYTE FOR BYTE with
the patch matrix built in numpy and packed by `MxEntry.pack`.
"""

import argparse
import pathlib
import sys
import time

import numpy as np
from kohakuaccel.node.boot import BootArgs, NodeBoot
from kohakuaccel.node.queue import NodeQueue
from kohakuaccel.package.format import Package
from kohakuaccel.transport.rebase import UnitGlobal
from kohakuaccel.transport.verilator import VerilatorTransport
from kohakutpu.clock.card import load_board
from kohakutpu.host import Card, board_map
from kohakutpu.ops.conv2d import Geometry, im2col_moves
from kohakutpu.ops.matmul import matmul
from kohakutpu.rt import Device
from sw_demo import ARENA_AT, ARENA_BYTES, BOARD, Demo, reference

from kohakutpu import layout as LO
from kohakutpu import ops as O

ROOT = pathlib.Path(__file__).resolve().parents[2]

#: (H, W, C_in, C_out, stride): a multiple of 32, a ragged C_in, stride 2.
CASES = [(8, 8, 32, 32, 1), (8, 8, 20, 32, 1), (12, 12, 32, 32, 2)]


def ref_conv(x, k, stride):
    h, w, _ = x.shape
    xp = np.pad(np.float64(x), ((1, 1), (1, 1), (0, 0)))
    oh, ow = (h - 1) // stride + 1, (w - 1) // stride + 1
    out = np.zeros((oh, ow, k.shape[0]))
    for dy in range(3):
        for dx in range(3):
            win = xp[dy : dy + stride * oh : stride, dx : dx + stride * ow : stride]
            out += win @ np.float64(k[:, :, dy, dx]).T
    return out


def patches(x, g: Geometry):
    """The im2col in numpy, as `ops.conv2d` lays it out: the independent reference."""
    img = g.image()
    xp = np.zeros((img.hp, img.wp, g.cp), np.float16)
    xp[1 : 1 + g.h, 1 : 1 + g.w, : g.c] = x
    out = np.zeros((g.rows, g.k), np.float16)
    for oy in range(g.oh):
        for ox in range(g.ow4):
            for t in range(9):
                dy, dx = divmod(t, 3)
                pix = xp[oy * g.stride + dy, ox * g.stride + dx]
                out[oy * g.ow4 + ox, t * g.cp : (t + 1) * g.cp] = pix
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", default=str(ROOT / "build/sw/vlt_card_v8t8_2n"))
    ap.add_argument("--fw", default=str(ROOT / "build/fw/kohakutpu_node.elf"))
    # One case is ~100 s of model time; run them one invocation each.
    ap.add_argument("--cases", default=",".join(str(i) for i in range(len(CASES))))
    a = ap.parse_args()
    d = Demo()
    t_all = time.monotonic()
    m = board_map(load_board(BOARD))
    t = VerilatorTransport(build_dir=a.build)
    card = Card.from_board(
        BOARD, transport=t, which=[0], verify=False, reply_at_agent=True
    )
    mesh = card.mesh
    units = tuple((k, c) for k in ("MG", "VC") for c in mesh.coords(k))
    mem = UnitGlobal(t, m["dram"], m["mem"], m["size"], staging=True)
    q = NodeQueue(mem, mesh.stage_addr(0), idle=lambda: t.run(600))
    q.init()
    dev = Device(card, base=ARENA_AT, size=ARENA_BYTES, node=q)
    NodeBoot(t, m["ctrl"][0], 0, sim=t).load(
        a.fw, BootArgs(queue=q.base, mesh=0, scan=mesh.caps.grid_hi + 1), "burn"
    )
    q.wait_ready()
    empty = q.run(Package(signature=0).to_bytes()).cycles
    print(
        f"  ready; empty package {empty} node cycles ({time.monotonic() - t_all:.1f}s)"
    )

    for h, w, cin, cout, s in [CASES[int(i)] for i in a.cases.split(",")]:
        rng = np.random.default_rng(h * 100 + cin)
        x = (rng.standard_normal((h, w, cin)) * 0.5).astype(np.float16)
        k = (rng.standard_normal((cout, cin, 3, 3)) * 0.2).astype(np.float16)
        g = Geometry(h, w, cin, stride=s)
        gt = g.groups(16)
        t0 = time.monotonic()
        ta, tb = dev.tensor(x), dev.tensor(O.weights_for_k(k, cin))
        src = ta.address(g.image())
        dev.flush()
        lay = LO.MxEntry(gt, 1, 0)
        col = dev.empty((g.rows, g.k), lay)
        dst = col.buffers[lay.key].addr
        moves = im2col_moves(g, gt, src, dst)
        for writes in moves:
            dev.move(writes, "im2col")
        dev.flush()
        c_im = dev.last_completion
        same = mem.read_block(dst, lay.nbytes((g.rows, g.k))) == lay.pack(patches(x, g))
        out = matmul(col, tb, gm=gt, gn=min(32, -(-cout // 4)), nk=1)
        held = out.numpy()
        c_mm = dev.last_completion
        secs = time.monotonic() - t0
        oh, ow = g.oh, g.ow
        got = np.zeros((oh, ow, cout))
        for row, y, xx in O.positions(h, w, s):
            got[y, xx] = held[row, :cout]
        want = ref_conv(x, k, s)
        err = float(np.abs(got - want).max() / np.abs(want).max())
        ref = reference(units)
        fn = O.conv2d if s == 1 else O.conv2d_stride2
        mod = fn(ref.tensor(x), ref.tensor(O.weights_for_k(k, cin)), gm=16, gn=32)
        mheld = np.asarray(mod.numpy())[: g.rows, :cout]
        peak = float(np.abs(mheld).max())
        far = float(np.abs(held[: g.rows, :cout] - mheld).max()) / 2.0 ** (
            np.floor(np.log2(peak)) - 10
        )
        entries = g.rows * g.k // (4 * 32)
        d.step(
            f"conv {h}x{w}x{cin}->{cout} s{s}",
            secs,
            same and err <= 0.05 and far <= 4,
            f"im2col {len(moves)} MOVER, {entries} entries: {c_im.cycles} node cycles "
            f"({c_im.cycles - empty} over empty, {(c_im.cycles - empty) / entries:.0f}"
            f"/entry), byte-identical to numpy im2col: {same}; matmul pkg "
            f"{c_mm.cycles} cycles; rel err vs numpy {err:.2e}, "
            f"{far:.1f} scale-ulp vs models",
        )
    q.stop()
    st = t.status()
    print(
        f"  DECERR {st['decerr']:#x}; {st['sys']} sys cycles; {time.monotonic() - t_all:.1f}s"
    )
    d.fails += st["decerr"] != 0
    t.close()
    print("PASS" if not d.fails else f"FAIL ({d.fails})")
    return 1 if d.fails else 0


if __name__ == "__main__":
    sys.exit(main())
