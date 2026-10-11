"""Compiled kernels over every sysnode of a card, through `kohakutpu.nodes.Nodes`.

    python scripts/py/card_nodes.py --build build/v9m/vlt_card_v9_2n --nodes 0,1
    python scripts/py/card_nodes.py --build ... --nodes 0,1,2,3 --case matmul_1024

Each case runs on the node group and on node 0 alone, same card, same
operands: a warm-up call, the measured call, numpy reference. Reported: error,
each node's package cycles, the slowest node against the clusters used
(`mac_eff`), the speed-up over node 0 alone, and the barriers the group
inserted. Host bytes go through `SimDram` unless `--axi`.
"""

import argparse
import functools
import json
import pathlib
import sys
import time

import kohakuaccel.runtime as R
import numpy as np
from kohakuaccel.node.boot import BootArgs, NodeBoot
from kohakuaccel.node.queue import NodeQueue
from kohakuaccel.transport.rebase import UnitGlobal
from kohakuaccel.transport.simdram import SimDram
from kohakuaccel.transport.verilator import VerilatorTransport
from kohakutpu.clock.card import load_board
from kohakutpu.host import Card, board_map
from kohakutpu.nodes import Nodes
from kohakutpu.rt import Device

from kohakutpu import ops

ROOT = pathlib.Path(__file__).resolve().parents[2]
#: The group's arena, node 0 alone's (two runtimes, two allocators: they must
#: not share bytes), then each node's own span, all below the card model's
#: 16 MB (it wraps). Node i's queue is in mesh i's on-chip staging store
#: (docs/spec/node-queue.md s1).
ARENA = (0x0000_0000, 0x0058_0000)
SOLO = (0x0058_0000, 0x0058_0000)
OWN, OWN_SIZE = 0x00B0_0000, 0x0004_0000
STAGING, MESH_SHIFT = 1 << 39, 36
Q_SIZE = 0x0010_0000


def cases() -> dict:
    """name -> build(rt, rng) -> (call, want, macs)."""

    def f16(rng, *s, scale=1.0):
        return (rng.standard_normal(s) * scale).astype(np.float16)

    def matmul(m, k, n):
        def build(rt, rng):
            a, b = f16(rng, m, k, scale=0.5), f16(rng, n, k, scale=0.5)
            ta, tb = rt.tensor(a), rt.tensor(b)
            want = np.float64(a) @ np.float64(b).T
            return (lambda: ops.matmul(ta, tb)), want, m * k * n

        return build

    def silu(n):
        def build(rt, rng):
            x = f16(rng, n, scale=2.0)
            tx = rt.tensor(x)
            v = np.float64(x)
            return (lambda: ops.silu(tx)), v / (1 + np.exp(-v)), 0

        return build

    return {
        "matmul_512": matmul(512, 512, 512),
        "matmul_1024": matmul(1024, 512, 1024),
        "silu": silu(65536),
    }


def boot(a):
    board = load_board(a.board)
    mp = board_map(board)
    ix = [int(x) for x in a.nodes.split(",")]
    t = VerilatorTransport(build_dir=a.build)
    host = t if a.axi else SimDram(t, board, mp["mem"], mp["size"])
    card = Card.from_board(
        a.board, transport=host, which=ix, verify=False, reply_at_agent=True
    )
    mem = UnitGlobal(host, mp["dram"], mp["mem"], mp["size"], staging=True)
    devs = []
    for i in ix:
        q = NodeQueue(
            mem, STAGING | i << MESH_SHIFT, size=Q_SIZE, idle=lambda: t.run(600)
        )
        q.init()
        mesh = card.select(i)
        devs.append(Device(card, base=OWN + i * OWN_SIZE, size=OWN_SIZE, node=q))
        NodeBoot(t, mp["ctrl"][i], i, sim=t).load(
            pathlib.Path(a.fw),
            BootArgs(queue=q.base, mesh=i, scan=mesh.caps.grid_hi + 1, flags=a.flags),
            "burn",
        )
    for d in devs:
        d.node.wait_ready()
    return t, devs


def measure(rt, build_case) -> dict:
    call, want, macs = build_case(rt, np.random.default_rng(7))
    call().numpy()
    for d in rt.devices:
        d.last_completion = None
    got = np.asarray(call().numpy(), np.float64)
    cyc = {
        d.machine.default: d.last_completion.cycles
        for d in rt.devices
        if d.last_completion
    }
    clusters = rt.machine.count("MG") * len(rt.devices)
    return {
        "err": round(float(np.abs(got - want).max() / np.abs(want).max()), 5),
        "node_cycles": cyc,
        "mac_eff": (
            round(macs / 1024 / clusters / max(cyc.values()), 4) if macs else None
        ),
        "barriers": rt.counters.get("barriers", 0),
        "slowest": max(cyc.values()),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", required=True)
    ap.add_argument("--board", default="multimesh_v9")
    ap.add_argument("--nodes", default="0,1")
    ap.add_argument("--fw", default=str(ROOT / "build/fw/kohakutpu_node.elf"))
    ap.add_argument("--flags", type=lambda s: int(s, 0), default=2)
    ap.add_argument("--case", action="append", default=[])
    ap.add_argument("--axi", action="store_true")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    R.execute = functools.partial(R.execute, timeout=3600.0)
    t0 = time.monotonic()
    t, devs = boot(a)
    print(f"booted {len(devs)} nodes in {time.monotonic() - t0:.1f}s", flush=True)
    group = Nodes(devs, base=ARENA[0], size=ARENA[1])
    solo = Nodes(devs[:1], base=SOLO[0], size=SOLO[1])
    rows = []
    for name in a.case or list(cases()):
        w0 = time.monotonic()
        try:
            g = measure(group, cases()[name])
            s = measure(solo, cases()[name])
        except RuntimeError:
            code = t.proc.wait(timeout=30)
            print(f"{name}: the model exited with {code & 0xFFFFFFFF:#x}", flush=True)
            raise
        row = {
            "case": name,
            "nodes": len(devs),
            "group": g,
            "solo": s,
            "speedup": round(s["slowest"] / g["slowest"], 3),
            "wall_s": round(time.monotonic() - w0, 1),
        }
        rows.append(row)
        print(json.dumps(row), flush=True)
    for d in devs:
        d.node.stop()
    t.close()
    if a.out:
        pathlib.Path(a.out).write_text(json.dumps(rows, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
