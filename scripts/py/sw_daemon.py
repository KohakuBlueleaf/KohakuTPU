"""The host stack through the card daemon, against the Verilator card model.

    python scripts/py/sw_daemon.py [--build build/sw/vlt_card_v8t8_2n]

Starts `python -m kohakuaccel.daemon --backend verilator` on the model, then as
an ordinary client: enumerates mesh 0 over a DaemonTransport, attaches node 0's
queue in the daemon (RemoteNodeQueue), loads the dispatcher firmware daemon-side,
and runs the DSL matmul and vector add with the node dispatching -- each
package ONE round trip, the daemon polling the completion ring and advancing
the model. Results must match numpy and the unit models; node heap ops through
the daemon must land where kohakuaccel.node.heap predicts.
"""

import argparse
import pathlib
import re
import subprocess
import sys
import time

import numpy as np
from kohakuaccel.daemon.client import DaemonClient, DaemonTransport, RemoteNodeQueue
from kohakuaccel.node.boot import BootArgs
from kohakuaccel.node.heap import Heap
from kohakuaccel.node.queue import layout as L
from kohakuaccel.package.format import Package
from kohakutpu.host import Card
from kohakutpu.rt import Device
from sw_demo import ARENA_AT, ARENA_BYTES, BOARD, Demo, reference

from kohakutpu import ops

ROOT = pathlib.Path(__file__).resolve().parents[2]
#: Node 0's DRAM heap, clear of the arena (ARENA_AT .. + 4 MB).
HEAP_AT, HEAP_BYTES = 0x00C0_0000, 1 << 20


def start_daemon(build: str) -> tuple[subprocess.Popen, int]:
    cmd = [
        sys.executable,
        "-m",
        "kohakuaccel.daemon",
        "--board",
        BOARD,
        "--backend",
        "verilator",
        "--build",
        build,
        "--port",
        "0",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
    line = proc.stdout.readline()
    m = re.search(r":(\d+)\s*$", line)
    if not m:
        proc.kill()
        raise SystemExit(f"daemon did not start: {line!r}")
    return proc, int(m.group(1))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", default=str(ROOT / "build/sw/vlt_card_v8t8_2n"))
    ap.add_argument("--fw", default=str(ROOT / "build/fw/kohakutpu_node.elf"))
    a = ap.parse_args()
    d = Demo()
    t_all = time.monotonic()

    t0 = time.monotonic()
    proc, port = start_daemon(a.build)
    client = DaemonClient(port=port, timeout=600)
    t = DaemonTransport(client=client)
    d.step("daemon up (verilator)", time.monotonic() - t0, True, f"{client.hello}")

    t0 = time.monotonic()
    card = Card.from_board(
        BOARD, transport=t, which=[0], verify=False, reply_at_agent=True
    )
    mesh = card.mesh
    units = tuple((k, c) for k in ("MG", "VC") for c in mesh.coords(k))
    d.step("enumerate over the daemon", time.monotonic() - t0, bool(units), f"{units}")

    t0 = time.monotonic()
    q = RemoteNodeQueue(client, node=0, base=mesh.stage_addr(0))
    info = q.load(a.fw, BootArgs(queue=q.base, mesh=0, scan=mesh.caps.grid_hi + 1))
    st = q.wait_ready()
    d.step(
        "firmware loaded + ready, daemon-side",
        time.monotonic() - t0,
        st["state"] == "ready",
        f"text {info.get('text')} B, queue {q.base:#x}",
    )

    dev = Device(card, base=ARENA_AT, size=ARENA_BYTES, node=q)
    dev.keep_packages = True
    ref = reference(units)
    rng = np.random.default_rng(7)
    alive: list = []

    def run(name, fn, want, tol, *operands, model_ulps=0):
        before = len(dev.packages)
        calls0 = client._id
        t1 = time.monotonic()
        held = [dev.tensor(o) for o in operands]
        alive.extend(held)
        got = np.asarray(fn(*held), np.float32)
        secs = time.monotonic() - t1
        mod = np.asarray(fn(*[ref.tensor(o) for o in operands]), np.float32)
        err = float(np.abs(got - want).max() / max(1e-6, float(np.abs(want).max())))
        peak = float(np.abs(mod).max())
        far = float(np.abs(got - mod).max()) / 2.0 ** (np.floor(np.log2(peak)) - 10)
        pk = [Package.from_bytes(p) for p in dev.packages[before:]]
        d.step(
            name,
            secs,
            err <= tol and far <= model_ulps,
            f"rel err vs numpy {err:.2e} (tol {tol:g}), vs models {far:.1f} ulp; "
            f"{len(pk)} pkg; {client._id - calls0} daemon calls in all "
            f"(operand writes, one node_run, result reads)",
        )

    A = (rng.standard_normal((32, 64)) * 0.5).astype(np.float16)
    W = (rng.standard_normal((32, 64)) * 0.5).astype(np.float16)
    run(
        "DSL matmul 32x64 @ 64x32",
        lambda x, w: ops.matmul(x, w).numpy(),
        A.astype(np.float32) @ W.astype(np.float32).T,
        0.05,
        A,
        W,
        model_ulps=4,
    )
    X = rng.standard_normal(1024).astype(np.float16)
    Y = rng.standard_normal(1024).astype(np.float16)
    run(
        "DSL vector add 1024",
        lambda x, y: ops.residual(x, y).numpy(),
        (X + Y).astype(np.float32),
        0.0,
        X,
        Y,
    )

    # One package, one round trip: the empty package through run().
    calls0 = client._id
    t0 = time.monotonic()
    c = q.run(Package(signature=0).to_bytes())
    d.step(
        "empty package: daemon round trips",
        time.monotonic() - t0,
        c.ok and client._id - calls0 == 1,
        f"{client._id - calls0} call(s), {c.cycles} node cycles",
    )

    t0 = time.monotonic()
    dram = HEAP_AT
    q.heap(L.MEM_DRAM, dram, HEAP_BYTES)
    want = Heap(dram, HEAP_BYTES, 64, 64)
    got = [
        q.alloc(L.MEM_DRAM, n, align=al)
        for n, al in ((4096, 0), (100, 4096), (70000, 256))
    ]
    exp = [want.alloc(n, al)[1] for n, al in ((4096, 0), (100, 4096), (70000, 256))]
    for addr in got:
        q.free(L.MEM_DRAM, addr)
    s = q.heap_stats(L.MEM_DRAM)
    d.step(
        "node heap through the daemon",
        time.monotonic() - t0,
        got == exp and s["live"] == 0 and s["blocks"] == 1,
        f"{[hex(x) for x in got]} (policy {[hex(x) for x in exp]}), after free {s}",
    )

    out = q.read_stdout().strip()
    if out:
        print("  firmware stdout:")
        for line in out.splitlines():
            print(f"    {line}")
    t0 = time.monotonic()
    c = q.stop()
    d.step("firmware stop", time.monotonic() - t0, c.ok, "")
    s = t.status()
    d.step(
        "station DECERR",
        0.0,
        s["decerr"] == 0,
        f"{s['decerr']:#x}; {s['sys']} sys cycles",
    )
    print(f"  queue (daemon-side): {q.counters}; client calls {client._id}")
    client.shutdown()
    client.close()
    proc.wait(timeout=60)
    print(f"  total {time.monotonic() - t_all:.1f}s")
    print("PASS" if not d.fails else f"FAIL ({d.fails})")
    return 1 if d.fails else 0


if __name__ == "__main__":
    sys.exit(main())
