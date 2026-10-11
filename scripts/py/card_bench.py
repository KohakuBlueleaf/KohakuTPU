"""Every software tier on a card model, measured through the full stack.

    python scripts/py/card_bench.py --build build/v9prof/vlt_card_v9_1n --out build/bench.json
    python scripts/py/card_bench.py --build ... --case silu --case matmul_1024

The model: `scripts/py/card_model.py card_v9_1n -d VC_STATE_PROF` (without the
define the lane column is empty). Each case runs in its own model process,
because a model dumps its vector counters (`VRES`) once, at exit: boot node 0
with the queue firmware, one warm-up call, one measured call, numpy reference.

Per case:
  call      the measured package's own cycle count, dispatch included
  mac_eff   multiply-accumulates / (1,024 per cluster-cycle x clusters x call)
  lane_eff  vector ALU issue beats of the measured call / (call x vector cores);
            the counters cover both calls, which do the same work, so half
  err       max |got - want| / max |want| against a float64 numpy reference
"""

import argparse
import functools
import hashlib
import json
import pathlib
import re
import subprocess
import sys
import time

import kohakuaccel.runtime as R
import numpy as np
from kohakuaccel.node.boot import BootArgs, NodeBoot
from kohakuaccel.node.queue import NodeQueue
from kohakuaccel.package.format import Package
from kohakuaccel.transport.rebase import UnitGlobal
from kohakuaccel.transport.simdram import SimDram
from kohakuaccel.transport.verilator import VerilatorTransport
from kohakutpu.clock.card import load_board
from kohakutpu.host import Card, board_map
from kohakutpu.kernels import (
    LOG2E,
    flash_attention,
    linear_add,
    linear_bias,
    linear_silu,
    mlp,
    swiglu,
)
from kohakutpu.rt import Device

from kohakutpu import ops

ROOT = pathlib.Path(__file__).resolve().parents[2]
#: Node 0's device span, below the card model's 16 MB (it wraps), and its queue
#: in mesh 0's on-chip staging store (docs/spec/node-queue.md s1).
DEV_BASE, DEV_SIZE = 0x0010_0000, 0x00D0_0000
STAGING = 1 << 39
Q_AT, Q_SIZE = STAGING, 0x0010_0000


def _sig(v):
    return 1.0 / (1.0 + np.exp(-v))


def _softmax(v):
    e = np.exp(v - v.max(-1, keepdims=True))
    return e / e.sum(-1, keepdims=True)


def _rms(v):
    return v / np.sqrt((v * v).mean(-1, keepdims=True) + 1e-5)


def _ln(v):
    d = v - v.mean(-1, keepdims=True)
    return d / np.sqrt((d * d).mean(-1, keepdims=True) + 1e-5)


def cases() -> dict:
    """name -> (kind, build(dev, rng) -> (call, want, macs)). `call()` returns
    the result tensor; `want` is float64."""

    def f16(rng, *s, scale=1.0):
        return (rng.standard_normal(s) * scale).astype(np.float16)

    def f64(*xs):
        return [x.astype(np.float64) for x in xs]

    def matmul(m, k, n):
        def build(dev, rng):
            a, b = f16(rng, m, k, scale=0.5), f16(rng, n, k, scale=0.5)
            ta, tb = dev.tensor(a), dev.tensor(b)
            fa, fb = f64(a, b)
            return (lambda **kw: ops.matmul(ta, tb, **kw)), fa @ fb.T, m * k * n

        return build

    def lin_silu(m, k, n):
        def build(dev, rng):
            x, w = f16(rng, m, k, scale=0.3), f16(rng, n, k, scale=0.3)
            tx, tw = dev.tensor(x), dev.tensor(w)
            h = np.float64(x) @ np.float64(w).T
            return (lambda **kw: linear_silu(tx, tw, **kw)), h * _sig(h), m * k * n

        return build

    def lin_bias(m, k, n):
        def build(dev, rng):
            x, w, b = f16(rng, m, k, scale=0.3), f16(rng, n, k, scale=0.3), f16(rng, n)
            tx, tw, tb = dev.tensor(x), dev.tensor(w), dev.tensor(b)
            want = np.float64(x) @ np.float64(w).T + np.float64(b)
            return (lambda **kw: linear_bias(tx, tw, tb, **kw)), want, m * k * n

        return build

    def lin_add(m, k, n):
        def build(dev, rng):
            x, w, r = (
                f16(rng, m, k, scale=0.3),
                f16(rng, n, k, scale=0.3),
                f16(rng, m, n),
            )
            tx, tw, tr = dev.tensor(x), dev.tensor(w), dev.tensor(r)
            want = np.float64(x) @ np.float64(w).T + np.float64(r)
            return (lambda **kw: linear_add(tx, tw, tr, **kw)), want, m * k * n

        return build

    def mlp_case(m, k, f):
        def build(dev, rng):
            x, up = f16(rng, m, k, scale=0.3), f16(rng, f, k, scale=0.3)
            dn = f16(rng, k, f, scale=0.3)
            tx, tu, td = dev.tensor(x), dev.tensor(up), dev.tensor(dn)
            h = np.float64(x) @ np.float64(up).T
            want = (h * _sig(h)) @ np.float64(dn).T
            return (lambda **kw: mlp(tx, tu, td, **kw)), want, m * k * f + m * f * k

        return build

    def swiglu_case(m, k, n):
        def build(dev, rng):
            x, wg = f16(rng, m, k, scale=0.3), f16(rng, n, k, scale=0.3)
            wu = f16(rng, n, k, scale=0.3)
            tx, tg, tu = dev.tensor(x), dev.tensor(wg), dev.tensor(wu)
            g, u = np.float64(x) @ np.float64(wg).T, np.float64(x) @ np.float64(wu).T
            return (
                (lambda **kw: swiglu(tx, tg, tu, **kw)),
                g * _sig(g) * u,
                2 * m * k * n,
            )

        return build

    def attention(lq, lkv, d):
        def build(dev, rng):
            q, k, v = f16(rng, lq, d), f16(rng, lkv, d), f16(rng, lkv, d)
            scale = d**-0.5
            tq = dev.tensor((q * scale * LOG2E).astype(np.float16))
            tk, tv = dev.tensor(k), dev.tensor(np.ascontiguousarray(v.T))
            s = np.float64(q) @ np.float64(k).T * scale
            want = _softmax(s) @ np.float64(v)
            return (
                (lambda **kw: flash_attention(tq, tk, tv, **kw)),
                want,
                2 * lq * lkv * d,
            )

        return build

    def unary(op, ref, n, scale=2.0):
        def build(dev, rng):
            x = f16(rng, n, scale=scale)
            tx = dev.tensor(x)
            return (lambda **kw: op(tx, **kw)), ref(np.float64(x)), 0

        return build

    def binary(op, ref, n):
        def build(dev, rng):
            a, b = f16(rng, n), f16(rng, n)
            ta, tb = dev.tensor(a), dev.tensor(b)
            return (lambda **kw: op(ta, tb, **kw)), ref(np.float64(a), np.float64(b)), 0

        return build

    def rows(op, ref, m, n, scale=1.0):
        def build(dev, rng):
            x = f16(rng, m, n, scale=scale)
            tx = dev.tensor(x)
            return (lambda **kw: op(tx, **kw)), ref(np.float64(x)), 0

        return build

    return {
        "matmul_s": ("mm", matmul(128, 64, 128)),
        "matmul_s2": ("mm", matmul(128, 128, 128)),
        "matmul_s4": ("mm", matmul(128, 512, 128)),
        "matmul_512": ("mm", matmul(512, 512, 512)),
        "matmul_1024": ("mm", matmul(1024, 512, 1024)),
        "matmul_k2048": ("mm", matmul(512, 2048, 512)),
        "linear_silu": ("mm", lin_silu(1024, 512, 1024)),
        # Small forms: every path the full size takes, at a few tiles per core.
        "linear_silu_s": ("mm", lin_silu(128, 512, 128)),
        "linear_bias_s": ("mm", lin_bias(128, 512, 128)),
        "linear_bias": ("mm", lin_bias(1024, 512, 1024)),
        "linear_add": ("mm", lin_add(1024, 512, 1024)),
        "mlp": ("mm", mlp_case(512, 512, 1024)),
        "swiglu": ("mm", swiglu_case(512, 512, 1024)),
        "attention": ("mm", attention(512, 512, 64)),
        "attention_s": ("mm", attention(128, 128, 64)),
        "silu": ("vec", unary(ops.silu, lambda v: v * _sig(v), 65536)),
        "gelu": ("vec", unary(ops.gelu, lambda v: v * _sig(1.702 * v), 65536)),
        "residual": ("vec", binary(ops.residual, lambda a, b: a + b, 65536)),
        "mul": ("vec", binary(ops.mul, lambda a, b: a * b, 65536)),
        "softmax": ("vec", rows(ops.softmax, _softmax, 256, 256, scale=2.0)),
        "rmsnorm": ("vec", rows(ops.rmsnorm, _rms, 256, 256)),
        "layernorm": ("vec", rows(ops.layernorm, _ln, 256, 256)),
    }


def vector_counters(log: pathlib.Path) -> dict:
    """From a VC_STATE_PROF model's log: issue beats per core (exit dump) and
    each core's program runs (start/halt stamps), the measured call's half."""
    beats, runs = {}, {}
    for ln in log.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"VRES (\S+) busy \d+ beats (\d+)", ln)
        if m:
            beats[m[1]] = int(m[2])
        m = re.match(r"VRUN (\S+) (start|halt) (\d+)", ln)
        if m:
            runs.setdefault(m[1], []).append((m[2], int(m[3])))
    busy, first, last, nruns = 0, None, 0, 0
    for ev in runs.values():
        starts = [t for k, t in ev if k == "start"]
        halts = [t for k, t in ev if k == "halt"]
        s, h = starts[len(starts) // 2 :], halts[len(halts) // 2 :]
        nruns += len(s)
        busy += sum(b - a for a, b in zip(s, h, strict=False))
        if s:
            first = s[0] if first is None else min(first, s[0])
            last = max(last, h[-1] if h else s[-1])
    return {
        "beats": sum(beats.values()),
        "cores": len(beats),
        "runs": nruns,
        "run_cycles": busy,
        "span": (last - first) if first is not None else 0,
    }


def one(a) -> dict:
    """Boot, warm up, measure one case; the model is closed before its log is read."""
    R.execute = functools.partial(R.execute, timeout=3600.0)
    # `name:gm=8,gn=16` passes the knobs to the kernel call.
    name, _, spec = a.case.partition(":")
    knobs = {k: int(v) for k, v in (kv.split("=") for kv in spec.split(",") if kv)}
    kind, build_case = cases()[name]
    build = pathlib.Path(a.build)
    mp = board_map(load_board(a.board))
    t0 = time.monotonic()
    t = VerilatorTransport(build_dir=build)
    host = t if a.axi else SimDram(t, load_board(a.board), mp["mem"], mp["size"])
    card = Card.from_board(
        a.board, transport=host, which=[0], verify=False, reply_at_agent=True
    )
    mem = UnitGlobal(host, mp["dram"], mp["mem"], mp["size"], staging=True)
    q = NodeQueue(mem, Q_AT, size=Q_SIZE, idle=lambda: t.run(600))
    q.init()
    dev = Device(card, base=DEV_BASE, size=DEV_SIZE, node=q)
    dev.engine_packages = a.engine
    NodeBoot(t, mp["ctrl"][0], 0, sim=t).load(
        pathlib.Path(a.fw),
        BootArgs(queue=q.base, mesh=0, scan=card.mesh.caps.grid_hi + 1, flags=a.flags),
        "burn",
    )
    q.wait_ready()
    wall = {"boot": time.monotonic() - t0}

    def lap(name, t):
        wall[name] = round(time.monotonic() - t, 2)

    t1 = time.monotonic()
    call, want, macs = build_case(dev, np.random.default_rng(7))
    lap("upload", t1)
    t1 = time.monotonic()
    y = call(**knobs)
    dev.flush()
    lap("trace_and_warm", t1)
    t1 = time.monotonic()
    warm = y.numpy()
    lap("warm_read", t1)
    warm_cycles = dev.last_completion.cycles
    t1 = time.monotonic()
    dev.keep_packages = True
    y = call(**knobs)
    dev.flush()
    lap("measured", t1)
    # The measured call's last package, step by step, to name the trace's rows.
    listed = Package.from_bytes(dev.packages[-1]).steps if dev.packages else []
    t1 = time.monotonic()
    raw = np.ascontiguousarray(y.numpy())
    got = np.asarray(raw, np.float64)
    lap("read", t1)
    cyc = dev.last_completion.cycles
    err = float(np.abs(got - want).max() / np.abs(want).max())
    said = q.read_stdout().splitlines()
    pkgt = [ln for ln in said if ln.startswith("PKGT")]
    # KA_BOOT_F_STEPS: `PKGS first last cycle` per step group; a run restarts at
    # step 0, and the measured call's package is the last run.
    trace: list = []
    for ln in said:
        if ln.startswith("PKGS "):
            first, last, at = (int(v) for v in ln.split()[1:4])
            trace = [] if first == 0 else trace
            what = [
                f"{st.op.name} u{st.unit} n{st.count}"
                for st in listed[first : last + 1]
            ]
            trace.append([first, last, at, ", ".join(what)])
    clusters = dev.machine.count("MG")
    vcores = dev.machine.count("VC")
    q.stop()
    t.close()
    vc = vector_counters(build / "model.log")
    beats, cores = vc["beats"], vc["cores"]
    return {
        "case": a.case,
        "kind": kind,
        "err": round(err, 5),
        "out_sha1": hashlib.sha1(raw.tobytes()).hexdigest(),
        "warm_identical": bool(
            np.array_equal(np.asarray(warm), got.astype(warm.dtype))
        ),
        "call": cyc,
        "warm_call": warm_cycles,
        "macs": macs,
        "clusters": clusters,
        "mac_eff": round(macs / (1024 * clusters * cyc), 4) if macs else None,
        "vcores": vcores,
        "lane_eff": round(beats / 2 / (cyc * cores), 4) if cores else None,
        # Of the measured call: each core's program runs, summed over cores and
        # over the call, and the first start to the last halt, in vector cycles.
        "vec_runs": vc["runs"],
        "vec_run_frac": round(vc["run_cycles"] / (cyc * cores), 4) if cores else None,
        "vec_span": vc["span"],
        "pkgt": pkgt[-1] if pkgt else None,
        "steps": trace,
        "wall_s": round(time.monotonic() - t0, 1),
        "wall": {k: round(v, 2) for k, v in wall.items()},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", required=True, help="a card model directory")
    ap.add_argument("--board", default="multimesh_v9")
    ap.add_argument("--fw", default=str(ROOT / "build/fw/kohakutpu_node.elf"))
    ap.add_argument("--flags", type=lambda s: int(s, 0), default=2, help="KA_BOOT_F_*")
    ap.add_argument("--case", action="append", default=[])
    ap.add_argument("--out", default=None, help="JSON report, one row per case")
    ap.add_argument("--timeout", type=float, default=600.0, help="seconds per case")
    ap.add_argument(
        "--axi", action="store_true", help="host bytes over the modelled AXI"
    )
    ap.add_argument(
        "--engine", action="store_true", help="packages lowered for the dispatch engine"
    )
    ap.add_argument("--one", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args()

    if a.one:
        print(
            "ROW "
            + json.dumps(one(argparse.Namespace(**{**vars(a), "case": a.case[0]})))
        )
        return 0

    names = a.case or list(cases())
    rows = []
    for name in names:
        cmd = [
            sys.executable,
            __file__,
            "--one",
            "--build",
            a.build,
            "--board",
            a.board,
            "--fw",
            a.fw,
            "--flags",
            str(a.flags),
            "--case",
            name,
        ]
        cmd += ["--axi"] if a.axi else []
        cmd += ["--engine"] if a.engine else []
        try:
            p = subprocess.run(
                cmd, capture_output=True, text=True, timeout=a.timeout, check=False
            )
            got = [ln[4:] for ln in p.stdout.splitlines() if ln.startswith("ROW ")]
            row = (
                json.loads(got[0])
                if got
                else {
                    "case": name,
                    "error": (p.stderr or p.stdout).strip().splitlines()[-1:],
                }
            )
        except subprocess.TimeoutExpired:
            row = {"case": name, "error": f"timeout after {a.timeout:.0f}s"}
        rows.append(row)
        print(json.dumps(row), flush=True)
    if a.out:
        pathlib.Path(a.out).write_text(json.dumps(rows, indent=1), encoding="utf-8")
    return 0 if all("error" not in r for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
