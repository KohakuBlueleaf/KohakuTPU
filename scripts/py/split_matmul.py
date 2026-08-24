"""One matmul on one mesh, then the same matmul column-parallel across four.

    python scripts/py/split_matmul.py
    python scripts/py/split_matmul.py --meshes 02 --m 64

`b` is split along N, so mesh m holds `a` copied and `b_m`, computes its own
output columns, and NOTHING is exchanged. Concatenating the four is the answer.

Measured 2026-08-13 at 128x256x256: compute scales **3.98x** with total cycles
conserved to 0.05%, and wall time gets **1.80x WORSE**. The wall loss is
transport -- `a` is copied so upload doubles, four dispatch streams share one
13 ms-per-access wire, and the meshes never overlap because `Program.kick` polls
`PROG_STAT` before each kick. CYCLES is the silicon; WALL is today's cost.
"""

import argparse
import time
from dataclasses import replace

import numpy as np
from kohakuaccel.device import control_read
from kohakuaccel.device.registers import CU_COUNTERS
from kohakutpu.host import Card
from kohakutpu.meshes import MeshGroup, Sharded
from kohakutpu.ops import matmul

MHZ = 100.09
WATCH = ("rounds", "sent", "fetched")


def busy(mesh) -> int:
    """Total busy cycles over every cluster on this mesh."""
    total = 0
    for coord in mesh.coords("MG"):
        w = control_read(mesh.ctrl, coord, CU_COUNTERS)
        if w is not None:
            total += w & 0xFFFF_FFFF
    return total


def timed(dev, label: str, work) -> tuple:
    """Call `work`, reporting its wall time, cycles and traffic.

    Returns `(result, wall, cycles)`. The counters are deltas: one device serves
    every call here, so `stats()` is cumulative.
    """
    was = {k: dev.stats()[k] for k in WATCH}
    c0 = busy(dev.mesh)
    t0 = time.perf_counter()
    got = work()
    wall = time.perf_counter() - t0
    cyc = (busy(dev.mesh) - c0) % (1 << 32)
    s = {k: dev.stats()[k] - was[k] for k in WATCH}
    print(
        f"  {label}: {wall:5.2f} s wall  |  {cyc:,} cycles = {cyc / MHZ:,.0f} us"
        f"  |  {s['rounds']} rounds, {s['sent']:,} B up, {s['fetched']:,} B down"
    )
    return got, wall, cyc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--board", help="boards/<name>.json; omit for the legacy scan")
    ap.add_argument("--port", type=int, help="reach the card through this daemon")
    ap.add_argument("--meshes", default="0123", help="which meshes, e.g. 02")
    ap.add_argument("--m", type=int, default=128)
    ap.add_argument("--k", type=int, default=256)
    ap.add_argument("--n", type=int, default=256)
    ap.add_argument("--gm", type=int, default=8)
    ap.add_argument("--gn", type=int, default=8)
    ap.add_argument("--nk", type=int, default=2)
    args = ap.parse_args()

    meshes = [int(c) for c in args.meshes]
    tiling = {"gm": args.gm, "gn": args.gn, "nk": args.nk}
    rng = np.random.default_rng(11)
    a = rng.normal(0, 0.02, (args.m, args.k)).astype(np.float16)
    b = rng.normal(0, 1.00, (args.n, args.k)).astype(np.float16)
    want = a.astype(np.float32) @ b.astype(np.float32).T
    peak = np.abs(want).max()

    if args.board:
        transport = None
        if args.port is not None:
            from kohakuaccel.daemon.client import DaemonTransport

            transport = DaemonTransport(port=args.port)
        card = Card.from_board(args.board, transport=transport, which=meshes)
    else:
        card = Card(which=meshes)
    group = MeshGroup.open(card, meshes)
    print(f"{card}\n{group}\n{args.m}x{args.k}x{args.n}, tiling {tiling}\n")

    print(f"SINGLE MESH (mesh_{meshes[0]})")
    head = group[0]
    y_one, w_one, c_one = timed(
        head, "whole", lambda: matmul(head.tensor(a), head.tensor(b), **tiling).numpy()
    )

    print(f"\nCOLUMN-PARALLEL over {len(meshes)} meshes (b split along N)")
    x, w = group.copy(a), group.split(b, 0)
    plan = group.plan_matmul(x, w, **tiling)
    per = args.n // len(meshes)
    parts, walls, cycs = [], [], []
    for step in plan.steps:
        dev = group[step.rank]
        got, wall, cyc = timed(
            dev,
            f"mesh_{dev.machine.default} cols {step.rank * per:4}+",
            lambda one=replace(plan, steps=(step,)): group.run(one)[0],
        )
        parts.append(got)
        walls.append(wall)
        cycs.append(cyc)

    y_split = Sharded(group, plan.spec, parts, plan.shape).numpy()
    # p50/p99, never max: ~0.3% of elements are blown and ~0.6% flicker, and a
    # max reports those rather than the split. See nondeterminism.py.
    for label, got in (("single", y_one), ("split ", y_split)):
        e = np.abs(got - want)
        print(
            f"  {label} vs fp32: p50 {np.median(e):.4e}  "
            f"p99 {np.percentile(e, 99):.4e}  (peak {peak:.4f})"
        )
    d = np.abs(y_split - y_one)
    print(
        f"  split vs single: identical {int((d == 0).sum()):,}/{d.size:,}  "
        f"p99 {np.percentile(d, 99):.4e}"
    )

    print(f"\nCOST at {args.m}x{args.k}x{args.n}")
    print(f"  single mesh : {w_one:6.2f} s wall, {c_one:,} cycles")
    print(f"  split, sum  : {sum(walls):6.2f} s wall, {sum(cycs):,} cycles")
    print(f"  slowest mesh: {max(cycs):,} cycles = {max(cycs) / MHZ:,.0f} us")
    print(
        f"  -> wall {sum(walls) / w_one:.2f}x, compute {c_one / max(cycs):.2f}x "
        f"if the meshes ran at once (they do not; kicks serialise)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
