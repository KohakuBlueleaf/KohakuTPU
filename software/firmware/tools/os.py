"""The node OS basics on the simulated card: tasks, the run queue, region heaps.

    python software/firmware/tools/os.py [--build build/sw/vlt_card_v8t8_2n]

1. osprobe on nodes 0 and 1 at once: it checks its own run queue (round
   robin, sleep, waits and timeouts, spawn from a task, switch cost) and its
   heaps over DRAM and staging (churn with each block's ends re-read, every
   refusal), and exits 0 only when every check passed.
2. The dispatcher on both nodes: reserved memory-management opcodes refuse
   with BAD_OP; its task exits through STOP.
"""

import argparse
import pathlib
import struct
import sys
import time

from kohakuaccel.driver.node.boot import BootArgs, NodeBoot
from kohakuaccel.driver.node.boot.args import REGION_QUEUE, idle_bound
from kohakuaccel.driver.node.queue import NodeError, NodeQueue
from kohakuaccel.driver.transport.rebase import UnitGlobal
from kohakuaccel.driver.transport.verilator import VerilatorTransport
from kohakutpu.driver.clock.card import load_board
from kohakutpu.driver.host import board_map

ROOT = pathlib.Path(__file__).resolve().parents[3]
#: The model's DRAM is 16 MB flat SHARED by every mesh (an offset aliases across
#: meshes), so each node's spans sit at their own offset. osprobe: results at
#: the span, its DRAM heap at +64 KB for 1 MB.
PROBE = (0x0030_0000, 0x0050_0000)
#: Seconds of modeled time a firmware wait may take (the boot block's bound).
WAIT = 0.02
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


def osprobe(t, m, mem, wait: int) -> int:
    nodes = []
    for i in (0, 1):
        n = NodeBoot(t, m["ctrl"][i], i, sim=t)
        n.load(
            ROOT / "build/fw/osprobe.elf",
            BootArgs(queue=m["dram"][i] + PROBE[i], mesh=i, timeout=wait),
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


def dispatcher(t, m, mem, wait: int) -> int:
    queues = []
    for i in (0, 1):
        q = NodeQueue(mem, 1 << 39 | i << 36, idle=lambda: t.run(600))
        q.init()
        NodeBoot(t, m["ctrl"][i], i, sim=t).load(
            ROOT / "build/fw/kohakutpu_node.elf",
            BootArgs(
                queue=q.base,
                mesh=i,
                timeout=wait,
                regions=[(q.base, q.size, REGION_QUEUE)],
            ),
            "burn",
        )
        queues.append(q)
    for q in queues:
        q.wait_ready()
    fails = 0
    for i, q in enumerate(queues):
        for op in (3, 4, 5):
            tag = q.submit(op=op, words=[0, 0x400000, 64, 32])
            try:
                q.wait(tag)
                fails += 1
                print(f"  node {i}: reserved opcode {op} accepted")
            except NodeError as e:
                fails += e.completion.name != "BAD_OP"
                print(f"  node {i}: reserved opcode {op}: {e.completion.name}")
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
    board = load_board("multimesh_v8t8")
    m = board_map(board)
    t0 = time.monotonic()
    t = VerilatorTransport(build_dir=a.build)
    mem = UnitGlobal(t, m["dram"], m["mem"], m["size"], staging=True)
    print(f"model up {time.monotonic() - t0:.1f}s")
    fails = 0
    for name, part in (("osprobe", osprobe), ("dispatcher", dispatcher)):
        if name in a.parts.split(","):
            t1 = time.monotonic()
            fails += part(t, m, mem, idle_bound(board, WAIT))
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
