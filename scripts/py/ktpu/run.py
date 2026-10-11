"""Run a `.ktpu` kernel's L1 body on a card model and check it against its L3
body: the runner loads the file, binds buffers, runs; the kernel is the text.

    python scripts/py/ktpu/run.py --build build/v9v2c/vlt_card_v9_1n_v2 silu
        [--fn NAME] [--init x=normal:3] [--seed 2] [--out DIR]

The L1 body's parameters are the L3 body's inputs, then its result, then
scratch; each is laid out as its type says (`kohakutpu.ktpu.buffers`). Inputs
are seeded random fp16 (`--init NAME=normal:SCALE|uniform:LO:HI`). Each run
goes twice; the second (image and descriptors resident) is the one timed.
Lane use is lane beats over 16 lanes x busy cores x node cycles; work use is
the L3 body's lane ops over the same; MFU is its multiply-adds over 1024 a
cluster a cycle. The error is against the L3 body run as written (fp16 and
MXFP7 rounding where its types say) and against the same math with no
rounding at all. Everything lands in `--out` as JSON.
"""

import argparse
import json
import pathlib
import sys

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "l1"))
from card import L1Card
from kohakuaccel.text.module import read as read_module
from kohakutpu.ktpu.buffers import Buffer
from kohakutpu.ktpu.compile import compile_l1, compile_l3
from kohakutpu.ktpu.l1 import node
from kohakutpu.ktpu.l3 import reference

from kohakutpu import ktpu

#: A cluster's MXFP7 multiply-adds a cycle, the MFU denominator per cluster.
MACS_PER_CYCLE = 1024


def make(rng, spec: str, dtype, shape):
    kind, *a = spec.split(":")
    if kind == "normal":
        x = rng.standard_normal(shape) * float(a[0] if a else 1.0)
    elif kind == "uniform":
        x = rng.uniform(float(a[0]), float(a[1]), shape)
    else:
        raise ValueError(f"--init {spec}: normal:SCALE or uniform:LO:HI")
    return x.astype(dtype)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", required=True)
    ap.add_argument("kernel", help="a kernel name under ktpu/kernels, or a .ktpu path")
    ap.add_argument("--fn", default=None)
    ap.add_argument("--init", action="append", default=[], help="NAME=normal:3")
    ap.add_argument("--seed", type=int, default=2)
    ap.add_argument("--out", default=None, help="directory for the JSON report")
    ap.add_argument(
        "--level",
        default="l1",
        choices=("l1", "l2", "l3"),
        help="the body compiled from",
    )
    ap.add_argument(
        "--l1-passes",
        default="schedule",
        help="L1 -> L1 passes of a compile, comma-separated",
    )
    ap.add_argument(
        "--l2-passes",
        default="retile=auto",
        help="L2 -> L2 passes of a compile (retile=8|auto), comma-separated",
    )
    ap.add_argument("--tag", default="", help="a suffix naming this run's reports")
    ap.add_argument(
        "--lower", action="append", default=[], help="a lowering option KEY=INT (lag=1)"
    )
    a = ap.parse_args()
    path = pathlib.Path(a.kernel)
    path = path if path.suffix == ".ktpu" else ktpu.kernel(a.kernel)
    m = ktpu.load(path)
    name = a.fn or m.names()[0]
    l3 = m.body(name, "l3")
    hand = m
    tag = f"{name}.{a.level}" + (f".{a.tag}" if a.tag else "")
    if a.level != "l1":
        l1p = [p for p in a.l1_passes.split(",") if p]
        l2p = [p for p in a.l2_passes.split(",") if p]
        opts = {
            k: int(v) if v.lstrip("-").isdigit() else v
            for k, v in (kv.split("=", 1) for kv in a.lower)
        }
        text = compile_l1(m, name, a.level, l1p, l2p, opts)
        if a.out:
            out = pathlib.Path(a.out)
            out.mkdir(parents=True, exist_ok=True)
            (out / f"{tag}.l1.ktpu").write_text(text, encoding="utf-8")
            if a.level == "l3":
                planned = compile_l3(m, name, opts)
                (out / f"{tag}.l2.ktpu").write_text(planned, encoding="utf-8")
        m = read_module(text, f"<{name}.{a.level} compiled>")
    l1 = m.body(name, "l1")
    n_in = len(l3.params)
    inits = dict(kv.split("=", 1) for kv in a.init)
    rng = np.random.default_rng(a.seed)
    bufs = [(p, Buffer.of(t)) for p, t in l1.params]
    inputs = [
        make(rng, inits.get(p, "normal:1"), b.host_dtype, b.host_shape)
        for p, b in bufs[:n_in]
    ]
    want = reference.run(hand, name, inputs)
    exact = reference.run(hand, name, inputs, exact=True)
    work = reference.work(hand, name, [x.shape for x in inputs])

    log = None
    if a.out:
        pathlib.Path(a.out).mkdir(parents=True, exist_ok=True)
        log = pathlib.Path(a.out) / f"{tag}.model.log"
    card = L1Card(a.build, log=log)
    binds = {}
    for (p, b), x in zip(bufs[:n_in], inputs, strict=True):
        binds[p] = card.put(np.frombuffer(b.pack(x), np.uint8))
    # The output, then scratch: zeroed, so a stale run cannot pass.
    for p, b in bufs[n_in:]:
        binds[p] = card.put(np.zeros(b.nbytes, np.uint8), b.space)
    out_name, out_buf = bufs[n_in]
    prog = node.program(m, name, binds, card.machine)
    cold = card.run(prog)["cycles"]
    got = card.run(prog)
    warm = got["cycles"]
    y = out_buf.unpack(card.get(binds[out_name], out_buf.nbytes))
    errs, errs_exact = {}, {}
    for into, ref in ((errs, want), (errs_exact, exact)):
        w = np.asarray(ref, np.float64).reshape(y.shape)
        into[out_name] = float(np.abs(y - w).max() / max(np.abs(w).max(), 1e-30))
    stats = card.close()
    busy = {c: s["vres"].get("busy", 0) // 2 for c, s in sorted(stats.items())}
    beats = {c: s["vres"].get("lanes", 0) // 2 for c, s in sorted(stats.items())}
    cores = sum(1 for v in busy.values() if v)
    clusters = len(prog.units("MG"))
    lane = sum(beats.values()) / (16 * max(cores, 1) * warm)
    use = work["lane_ops"] / (16 * max(cores, 1) * warm)
    mfu = work["macs"] / (MACS_PER_CYCLE * clusters * warm)
    report = {
        "kernel": str(path),
        "fn": f"{name}.l1",
        "level": a.level,
        "l1_passes": a.l1_passes if a.level != "l1" else "",
        "l2_passes": a.l2_passes if a.level != "l1" else "",
        "lower": a.lower if a.level != "l1" else [],
        "build": a.build,
        "cycles": warm,
        "cold": cold,
        "cores": cores,
        "clusters": clusters,
        "lane_use": lane,
        "work_use": use,
        "mfu": mfu,
        "work": work,
        "err": errs,
        "err_exact": errs_exact,
        "busy": busy,
        "lane_beats": beats,
        "vruns": {c: s["runs"] for c, s in sorted(stats.items())},
        "phases": got["phases"],
        "steps": len(got["pkg"].steps),
    }
    print(
        f"{name}.l1: {warm} cycles (cold {cold})  lane {lane:.1%}  work {use:.1%}  "
        f"MFU {mfu:.1%}  cores {cores}  err {errs} (exact math {errs_exact})"
    )
    if a.out:
        out = pathlib.Path(a.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{tag}.json").write_text(json.dumps(report, indent=1))
        np.savez(
            out / f"{tag}.npz",
            got=y,
            want=np.asarray(want, np.float64).reshape(y.shape),
            exact=np.asarray(exact, np.float64).reshape(y.shape),
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
