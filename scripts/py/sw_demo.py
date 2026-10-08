"""The software stack, end to end, on the Verilator card model.

    python firmware/build.py kohakutpu_node
    python scripts/py/sw_demo.py [--node 0] [--load burn|axi] [--build build/sw/vlt_card_v8t8_2n]

Each step timed and checked: the model up and mesh `--node` enumerated; the
dispatcher firmware booted on its RV64 (`burn` pokes the arrays, `axi` streams
the load window as the card does) and a NOP and an empty package round-tripped;
kohakutpu.lang kernels (matmul 32x64 @ 64x32, a 1,024-element add, the matmul
again on operands at new addresses, which must REUSE the package) and tinygrad
through ktpugrad (add, matmul, relu(x @ w.T + b)) run with the NODE
dispatching. DSL results must match numpy and the unit models running the same
packages (the vector core exactly, the cluster within 4 fp16 ulps at the
result's scale); tinygrad results must match numpy. Prints PASS or FAIL (n).
"""

import argparse
import pathlib
import subprocess
import sys
import time

import ktpugrad
import numpy as np
from kohakuaccel.node.boot import BootArgs, NodeBoot
from kohakuaccel.node.queue import NodeQueue
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
from kohakutpu.rt import Device
from tinygrad import Tensor

from kohakutpu import api, ops
from kohakutpu import layout as LO

ROOT = pathlib.Path(__file__).resolve().parents[2]
BOARD = "multimesh_v8t8"
#: The model's DRAM is 16 MB flat and aliases above it: every region below.
ARENA_AT = 0x0040_0000  # + node * 4 MB
#: Offset of the queue region in the node's 2 MB staging store.
QUEUE_STAGE = 0
ARENA_BYTES = 0x0040_0000
#: Cycles the model advances between two host polls.
POLL_CYCLES = 600


class Demo:
    def __init__(self) -> None:
        self.fails = 0
        self.rows: list[tuple[str, float, str]] = []

    def step(self, name: str, seconds: float, ok: bool, note: str = "") -> None:
        self.fails += not ok
        self.rows.append((name, seconds, ("ok  " if ok else "FAIL") + "  " + note))
        print(
            f"  {'ok  ' if ok else 'FAIL'} {name:34s} {seconds:7.1f}s  {note}",
            flush=True,
        )


def reference(units: tuple) -> SimDevice:
    """The unit models, node-dispatched through the reference interpreter."""
    mg = tuple(c for k, c in units if k == "MG")
    vc = tuple(c for k, c in units if k == "VC")
    ref = SimDevice(mg=mg, vc=vc, agent=(0, 1))
    ref.node = LocalNode(
        SimMailbox(ref.card),
        machine_units(ref.machine),
        mover=lambda wr: run_move(wr, ref.card.mem),
    )
    ref.fields = FIELDS
    return ref


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--node", type=int, default=0)
    ap.add_argument("--load", choices=("burn", "axi"), default="burn")
    ap.add_argument("--build", default=str(ROOT / "build/sw/vlt_card_v8t8_2n"))
    ap.add_argument("--fw", default=str(ROOT / "build/fw/kohakutpu_node.elf"))
    ap.add_argument("--no-tinygrad", action="store_true")
    # Both halves at once run past the 5-minute bound on a loaded host.
    ap.add_argument("--no-dsl", action="store_true")
    a = ap.parse_args()
    i = a.node
    d = Demo()
    t_all = time.monotonic()

    fw = pathlib.Path(a.fw)
    if not fw.exists():
        t0 = time.monotonic()
        subprocess.run(
            [sys.executable, str(ROOT / "firmware/build.py"), fw.stem], check=True
        )
        d.step("firmware build", time.monotonic() - t0, fw.exists(), fw.name)

    # 1 -------------------------------------------------------- model + card
    m = board_map(load_board(BOARD))
    t0 = time.monotonic()
    t = VerilatorTransport(build_dir=a.build)
    d.step("model up", time.monotonic() - t0, t.ready.startswith("READY"), t.ready)
    t0 = time.monotonic()
    card = Card.from_board(
        BOARD, transport=t, which=[i], verify=False, reply_at_agent=True
    )
    mesh = card.mesh
    units = tuple((k, c) for k in ("MG", "VC") for c in mesh.coords(k))
    d.step("enumerate mesh", time.monotonic() - t0, bool(units), f"mesh {i}: {units}")

    # 2 ---------------------------------------------------- firmware + queue
    # The queue, its rings and the package heap live in the node's STAGING,
    # which the host reaches through its memory window with the address whole.
    mem = UnitGlobal(t, m["dram"], m["mem"], m["size"], staging=True)
    q = NodeQueue(
        mem, card.mesh.stage_addr(QUEUE_STAGE), idle=lambda: t.run(POLL_CYCLES)
    )
    q.init()
    # Uploads are FP16; the node's mover quantises every matmul operand to MXFP7.
    dev = Device(card, base=ARENA_AT + i * ARENA_BYTES, size=ARENA_BYTES, node=q)
    dev.keep_packages = True
    boot = NodeBoot(t, m["ctrl"][i], i, sim=t)
    # No unit table: the node enumerates its own mesh, edge ring included.
    args = BootArgs(queue=q.base, mesh=i, scan=mesh.caps.grid_hi + 1)
    t0 = time.monotonic()
    info = boot.load(fw, args, a.load)
    st = q.wait_ready()
    d.step(
        f"boot firmware ({a.load})",
        time.monotonic() - t0,
        st["state"] == "ready",
        f"text {info['text']} B, spad {info['spad']} B, load {info['load_seconds']:.1f}s",
    )
    found = sorted(q.units())
    host = sorted((k, i, c[0], c[1]) for k, c in units)
    d.step("node enumerated its own units", 0.0, found == host, f"{found}")
    t0 = time.monotonic()
    c = q.nop()
    d.step("NOP round trip", time.monotonic() - t0, c.ok, "")
    t0 = time.monotonic()
    c = q.run(Package(signature=0).to_bytes())
    empty_cycles = c.cycles
    d.step(
        "empty package round trip",
        time.monotonic() - t0,
        c.ok,
        f"{c.cycles} node cycles",
    )

    ref = reference(units)
    rng = np.random.default_rng(7)
    #: Every operand stays alive, so a later call's land at NEW addresses and a
    #: reused package is only right if its relocations are.
    alive: list = []

    def ulps(x, y) -> float:
        """Largest |x - y| in fp16 ulps at the result's scale (y's peak).

        An element near zero has tiny ulps of its own.
        """
        peak = float(np.abs(y).max())
        if not peak:
            return float(np.abs(x - y).max())
        return float(np.abs(x - y).max()) / 2.0 ** (np.floor(np.log2(peak)) - 10)

    def run(name, fn, want, tol, *operands, model_ulps=0):
        """`fn(device, *tensors)` on the card and on the models; compare both.

        `model_ulps` is how far the card may sit from the unit models: 0 for the
        vector core; the cluster's drain rounding differs from kohakutpu.model's
        by a few fp16 ulps on some elements (measured: <= 2 at K=64).
        """
        before = len(dev.packages)
        c0 = dict(q.counters)
        t1 = time.monotonic()
        held = [dev.tensor(o) for o in operands]
        alive.extend(held)
        got = np.asarray(fn(dev, *held), np.float32)
        secs = time.monotonic() - t1
        mod = np.asarray(fn(ref, *[ref.tensor(o) for o in operands]), np.float32)
        err = float(np.abs(got - want).max() / max(1e-6, float(np.abs(want).max())))
        far = ulps(got, mod)
        pk = [Package.from_bytes(p) for p in dev.packages[before:]]
        flits = sum(len(p.payloads) for p in pk)
        moves = sum(1 for p in pk for s in p.steps if s.op.name == "MOVER")
        up = q.counters["uploads"] - c0["uploads"]
        hits = q.counters["cache_hits"] - c0["cache_hits"]
        d.step(
            name,
            secs,
            err <= tol and far <= model_ulps,
            f"rel err vs numpy {err:.2e} (tol {tol:g}), vs models {far:.1f} ulp "
            f"(<= {model_ulps}, {int(np.sum(got != mod))} of {got.size} differ); "
            f"{len(pk)} pkg, {flits} words, {moves} MOVER, {up} upload(s), "
            f"{hits} reused; last package {dev.last_completion.cycles} node cycles",
        )
        return got

    # 3 ------------------------------------------------------- DSL kernels
    def dsl():
        A = (rng.standard_normal((32, 64)) * 0.5).astype(np.float16)
        W = (rng.standard_normal((32, 64)) * 0.5).astype(np.float16)
        run(
            "DSL matmul 32x64 @ 64x32",
            lambda dv, x, w: ops.matmul(x, w).numpy(),
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
            lambda dv, x, y: ops.residual(x, y).numpy(),
            (X + Y).astype(np.float32),
            0.0,
            X,
            Y,
        )
        A2 = (rng.standard_normal((32, 64)) * 0.5).astype(np.float16)
        W2 = (rng.standard_normal((32, 64)) * 0.5).astype(np.float16)
        run(
            "DSL matmul again, new operands",
            lambda dv, x, w: ops.matmul(x, w).numpy(),
            A2.astype(np.float32) @ W2.astype(np.float32).T,
            0.05,
            A2,
            W2,
            model_ulps=4,
        )
        # The converting move alone, per operand: a package holding only its
        # MOVER, less the empty package's cycles.
        for side, held in (("A", alive[-2]), ("B", alive[-1])):
            lay = next(b.layout for b in held.buffers.values() if LO.mx_inner(b.layout))
            x = dev.tensor((rng.standard_normal(held.shape) * 0.5).astype(np.float16))
            x.prepare(lay)
            dev.flush()
            t0 = time.monotonic()
            x.address(lay)
            dev.flush()
            c = dev.last_completion
            count, _, entries = LO.mx_runs(lay, held.shape)
            net = c.cycles - empty_cycles
            d.step(
                f"converting move, side {side}",
                time.monotonic() - t0,
                c.ok,
                f"{count * entries} entries ({lay.key}): {c.cycles} node cycles, "
                f"{net} over an empty package, {net / (count * entries):.0f} per entry",
            )

    if not a.no_dsl:
        dsl()

    # 4 ------------------------------------------------------------ tinygrad
    if not a.no_tinygrad:
        api.attach(dev)
        ktpugrad.install()

        def tg(name, build, want, tol):
            before = len(dev.packages)
            t1 = time.monotonic()
            got = np.asarray(build(), np.float32)
            secs = time.monotonic() - t1
            err = float(np.abs(got - want).max() / max(1e-6, float(np.abs(want).max())))
            pk = [Package.from_bytes(p) for p in dev.packages[before:]]
            d.step(
                name,
                secs,
                err <= tol,
                f"rel err vs numpy {err:.2e}; {len(pk)} pkg, "
                f"{sum(len(p.payloads) for p in pk)} words",
            )

        x = (rng.standard_normal((32, 64)) * 0.5).astype(np.float16)
        w = (rng.standard_normal((32, 64)) * 0.5).astype(np.float16)
        b = (rng.standard_normal(32) * 0.5).astype(np.float16)
        tg(
            "tinygrad x + x",
            lambda: (Tensor(x, device="KTPU") + Tensor(x, device="KTPU")).numpy(),
            (x + x).astype(np.float32),
            0.0,
        )
        tg(
            "tinygrad x @ w.T",
            lambda: (Tensor(x, device="KTPU") @ Tensor(w, device="KTPU").T).numpy(),
            x.astype(np.float32) @ w.astype(np.float32).T,
            0.05,
        )
        tg(
            "tinygrad relu(x @ w.T + b)",
            lambda: (
                (Tensor(x, device="KTPU") @ Tensor(w, device="KTPU").T)
                + Tensor(b, device="KTPU")
            )
            .relu()
            .numpy(),
            np.maximum(x.astype(np.float32) @ w.astype(np.float32).T + b, 0),
            0.05,
        )

    # 5 ---------------------------------------------------------- wind down
    out = q.read_stdout()
    if out.strip():
        print("  firmware stdout:")
        for line in out.strip().splitlines():
            print(f"    {line}")
    t0 = time.monotonic()
    c = q.stop()
    st = boot.status()
    d.step(
        "firmware stop",
        time.monotonic() - t0,
        c.ok and st.get("exited", False),
        f"exit {st.get('exit', 0):#x}",
    )
    s = t.status()
    d.step(
        "station DECERR",
        0.0,
        s["decerr"] == 0,
        f"{s['decerr']:#x}; {s['sys']} sys cycles, {t.calls} host calls",
    )
    t.close()
    total = time.monotonic() - t_all
    print(f"  queue: {q.counters}")
    print(f"  total {total:.1f}s")
    print("PASS" if not d.fails else f"FAIL ({d.fails})")
    return 1 if d.fails else 0


if __name__ == "__main__":
    sys.exit(main())
