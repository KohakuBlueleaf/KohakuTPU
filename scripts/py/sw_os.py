"""The node OS basics on the simulated card: tasks, the run queue, region heaps.

    python scripts/py/sw_os.py [--build build/sw/vlt_card_v8t8_2n]

1. osprobe on nodes 0 and 1 at once: it checks its own run queue (round
   robin, sleep, waits and timeouts, spawn from a task, switch cost) and its
   heaps over DRAM and staging (churn with each block's ends re-read, every
   refusal), and exits 0 only when every check passed.
2. The dispatcher (its queue service is a task) on both nodes: the host sets a
   DRAM and a staging heap through the queue, checks each allocated address
   against kohakuaccel.node.heap, runs a MOVER package between two allocated
   blocks, checks the bytes, frees all and reads the heap lines back.
"""

import argparse
import pathlib
import struct
import sys
import time

from kohakuaccel.device import mover
from kohakuaccel.node.boot import BootArgs, NodeBoot
from kohakuaccel.node.heap import Heap
from kohakuaccel.node.queue import NodeError, NodeQueue
from kohakuaccel.node.queue import layout as L
from kohakuaccel.package.build import PackageBuilder
from kohakuaccel.transport.rebase import UnitGlobal
from kohakuaccel.transport.verilator import VerilatorTransport
from kohakutpu.clock.card import load_board
from kohakutpu.host import board_map

ROOT = pathlib.Path(__file__).resolve().parents[2]
#: The model's DRAM is 16 MB flat SHARED by every mesh (an offset aliases across
#: meshes), so each node's spans sit at their own offset. osprobe: results at
#: the span, its DRAM heap at +64 KB for 1 MB.
PROBE = (0x0030_0000, 0x0050_0000)
#: The dispatcher's heaps: DRAM past every other script's spans, staging past the queue.
HEAP_DRAM, HEAP_DRAM_BYTES = (0x0080_0000, 0x00A0_0000), 1 << 20
HEAP_STAGE, HEAP_STAGE_BYTES = 0x0010_0000, 512 << 10
RESULTS = (
    "pass",
    "fail",
    "cycles/switch",
    "deepest stack B",
    "heap ops",
    "checks",
    "spawned",
    "switches",
    "idle passes",
    "end cycle",
)


def osprobe(t, m, mem) -> int:
    nodes = []
    for i in (0, 1):
        n = NodeBoot(t, m["ctrl"][i], i, sim=t)
        n.load(
            ROOT / "build/fw/osprobe.elf",
            BootArgs(queue=m["dram"][i] + PROBE[i], mesh=i),
            "burn",
        )
        nodes.append(n)
    text, st, t0 = ["", ""], [{}, {}], time.monotonic()
    while time.monotonic() - t0 < 240:
        for i, n in enumerate(nodes):
            text[i] += n.console()
            st[i] = n.status()
        if all(s.get("exited") or s.get("halted") for s in st):
            break
        t.run(20000)
    fails = 0
    for i, n in enumerate(nodes):
        text[i] += n.console()
        raw = mem.read_block(m["dram"][i] + PROBE[i], 8 * len(RESULTS))
        res = dict(zip(RESULTS, struct.unpack(f"<{len(RESULTS)}Q", raw), strict=True))
        print(
            f"  node {i}: exit {st[i].get('exit', 'none')} after "
            f"{time.monotonic() - t0:.1f}s; "
            + ", ".join(
                f"{k} {v:#x}" if k in ("pass", "fail") else f"{k} {v}"
                for k, v in res.items()
            )
        )
        for ln in text[i].strip().splitlines():
            print(f"    console: {ln}")
        base = m["dram"][i] + PROBE[i]
        start, done = mem.read64(base + 0x70), mem.read64(base + 0x78)
        stamps = struct.unpack(
            f"<{done}Q", mem.read_block(base + 0x80, 8 * done) if done else b""
        )
        print(
            f"    {done} checks reached; cycles since start at each: "
            + " ".join(str(s - start) for s in stamps)
            + f"; now {t.status()['sys']} sys cycles"
        )
        ok = (
            st[i].get("exited")
            and st[i].get("exit") == 0
            and res["fail"] == 0
            and res["checks"] >= 20
            and res["pass"] == (1 << res["checks"]) - 1
        )
        fails += not ok
    return fails


def dispatcher(t, m, mem) -> int:
    queues = []
    for i in (0, 1):
        q = NodeQueue(mem, 1 << 39 | i << 36, idle=lambda: t.run(600))
        q.init()
        NodeBoot(t, m["ctrl"][i], i, sim=t).load(
            ROOT / "build/fw/kohakutpu_node.elf", BootArgs(queue=q.base, mesh=i), "burn"
        )
        queues.append(q)
    for q in queues:
        q.wait_ready()
    fails = 0
    for i, q in enumerate(queues):
        t0 = time.monotonic()
        dram = m["dram"][i] + HEAP_DRAM[i]
        stage = 1 << 39 | i << 36 | HEAP_STAGE
        q.heap(L.MEM_DRAM, dram, HEAP_DRAM_BYTES)
        q.heap(L.MEM_STAGING, stage, HEAP_STAGE_BYTES, granule=32)
        ref = {
            L.MEM_DRAM: Heap(dram, HEAP_DRAM_BYTES, 64, 64),
            L.MEM_STAGING: Heap(stage, HEAP_STAGE_BYTES, 32, 64),
        }
        reqs = [
            (L.MEM_DRAM, 4096, 0),
            (L.MEM_DRAM, 100, 4096),
            (L.MEM_STAGING, 1000, 0),
            (L.MEM_DRAM, 70000, 256),
            (L.MEM_STAGING, 33, 1024),
        ]
        got, match = [], True
        for r, n, a in reqs:
            addr = q.alloc(r, n, align=a, tag=len(got))
            want = ref[r].alloc(n, a, len(got))[1]
            match &= addr == want
            got.append((r, addr, n))
        print(
            f"  node {i}: 5 allocs {'match' if match else 'DIFFER from'} the Python policy: "
            + ", ".join(f"{a:#x}" for _, a, _ in got)
        )
        fails += not match

        # A package moves 1 KB from a DRAM block into a staging block, both node-allocated.
        src, dst = got[0][1], got[2][1]
        blob = bytes((k * 29 + 3 * i + 1) & 0xFF for k in range(1024))
        mem.write_block(src, blob)
        mem.write_block(dst, bytes(1024))
        pb = PackageBuilder()
        walk = [(32, 32)]
        pb.mover(
            mover.copy(
                mover.Walker(src, dims=walk),
                mover.Walker(dst, dims=walk),
                ewidth=mover.W32,
            )
        )
        done = q.run(pb.build().to_bytes())
        moved = mem.read_block(dst, 1024) == blob
        print(
            f"  node {i}: MOVER DRAM block -> staging block: {done.describe()}; "
            f"bytes {'ok' if moved else 'WRONG'}"
        )
        fails += not (done.ok and moved)

        errs = []
        for name, call, args in (
            ("BAD_FREE", q.free, (L.MEM_DRAM, src + 64)),
            ("NO_MEMORY", q.alloc, (L.MEM_STAGING, 1 << 20)),
            ("HEAP_BUSY", q.heap, (L.MEM_DRAM, dram, 4096)),
        ):
            try:
                call(*args)
                errs.append(f"{name}: accepted")
            except NodeError as e:
                if e.completion.name != name:
                    errs.append(f"{name}: got {e.completion.name}")
        for r, addr, _ in got:
            q.free(r, addr)
        st = [q.heap_stats(r) for r in (L.MEM_DRAM, L.MEM_STAGING)]
        whole = (
            st[0]["free"] == st[0]["largest"] == HEAP_DRAM_BYTES
            and st[0]["blocks"] == 1
            and st[1]["free"] == st[1]["largest"] == HEAP_STAGE_BYTES
            and st[1]["blocks"] == 1
            and st[0]["live"] == st[1]["live"] == 0
            and st[1]["fails"] == 1
        )
        print(
            f"  node {i}: refusals {'ok' if not errs else errs}; after freeing all: "
            f"DRAM {st[0]}, staging {st[1]} ({time.monotonic() - t0:.1f}s)"
        )
        fails += bool(errs) + (not whole)
    for i, q in enumerate(queues):
        q.stop()
        out = q.read_stdout().strip().splitlines()
        stop = [ln for ln in out if "stop:" in ln]
        print(f"  node {i}: {stop[-1] if stop else 'no stop line'}")
    return fails


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", default=str(ROOT / "build/sw/vlt_card_v8t8_2n"))
    ap.add_argument("--parts", default="osprobe,dispatcher")
    a = ap.parse_args()
    m = board_map(load_board("multimesh_v8t8"))
    t0 = time.monotonic()
    t = VerilatorTransport(build_dir=a.build)
    mem = UnitGlobal(t, m["dram"], m["mem"], m["size"], staging=True)
    print(f"model up {time.monotonic() - t0:.1f}s")
    fails = 0
    for name, part in (("osprobe", osprobe), ("dispatcher", dispatcher)):
        if name in a.parts.split(","):
            t1 = time.monotonic()
            fails += part(t, m, mem)
            print(f"{name} {time.monotonic() - t1:.1f}s")
    s = t.status()
    print(
        f"DECERR {s['decerr']:#x}; {s['sys']} sys cycles; {time.monotonic() - t0:.1f}s total"
    )
    fails += s["decerr"] != 0
    t.close()
    print("PASS" if not fails else f"FAIL ({fails})")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
