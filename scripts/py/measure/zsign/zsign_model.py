"""zero_sign on the Verilated v9t6 card model (L2.VC.026): the ViT block's
logits and N LeNet train steps under one value, or their CPU f32 reference;
`compare` reads every run of a directory and writes the report.

    zsign_model.py vit|lenet --zero-sign native|preserve|cpu --output FILE.json
    zsign_model.py compare --dir DIR

A run writes FILE.json (counters, cycles, compile seconds, per-step loss,
every launch in collection order: name, whether its graph reduces (rsum or
rmax), its outputs' digest and -0 count), FILE.npz (every array as raw bits:
logits; per step loss, logits, each parameter's gradient and value) and
FILE.launches.npz (each reducing launch's output bytes). The card runs one
launch at a time (queue_depth 1), so a launch's outputs are read when it is
collected, before the next is submitted.
"""

import argparse
import hashlib
import json
import re
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import numpy as np
from examples.tinygrad.mnist_mlp import LeNet, train_step
from examples.tinygrad.vit_tiny import seeded_model
from kohakutpu.application import ktpugrad
from kohakutpu.application.ktpugrad.nn import Adam16, place
from kohakutpu.application.runtime import device as D
from kohakutpu.stack import rs
from tinygrad import Tensor
from tinygrad.helpers import Context
from tinygrad.nn.optim import Adam
from tinygrad.nn.state import get_parameters

ROOT = Path(__file__).resolve().parents[4]
BATCH = 16
REDUCES = re.compile(r"\b(rsum|rmax)\b")
ITEM = {"f16": 2, "f32": 4, "mx7a": 1, "mx7b": 1}
LANE = {1: np.uint8, 2: np.uint16, 4: np.uint32}


def batch(step, dtype):
    rng = np.random.default_rng(1000 + step)
    x = rng.standard_normal((BATCH, 784)).astype(dtype)
    y = np.eye(10, dtype=dtype)[rng.integers(0, 10, BATCH)]
    return x, y


class Launches:
    """Every launch the device collects, in order, with its outputs read
    straight from card memory (no host-read counters or pins). A replayed
    launch (not submitted through `Device.submit`) is a row with no ports."""

    def __init__(self):
        self.pending, self.rows, self.outputs = {}, [], {}

    def install(self, stack):
        submit, collect = D.Device.submit, D.Device._collect_one

        def submitted(dev, compiled, binds, *a, **kw):
            s = submit(dev, compiled, binds, *a, **kw)
            self.pending[id(s)] = (compiled, dict(binds))
            return s

        def collected(dev, s):
            fresh = s.run is None
            run = collect(dev, s)
            if fresh and id(s) in self.pending:
                self.record(dev, *self.pending.pop(id(s)))
            elif fresh:
                self.rows.append({"name": s.name, "reduces": None, "ports": None})
            return run

        stack.enter_context(patch.object(D.Device, "submit", submitted))
        stack.enter_context(patch.object(D.Device, "_collect_one", collected))

    def record(self, dev, compiled, binds):
        """A launch traced from no graph has no port roles: its row says
        so (`ports` None) and `compare` counts it apart."""
        index = len(self.rows)
        reduces = bool(REDUCES.search(compiled.l2_text))
        row = {"name": compiled.name, "reduces": reduces, "ports": None}
        self.rows.append(row)
        graph = compiled.traced.graph if compiled.traced is not None else {}
        if not graph:
            return
        digest, negative, ports = hashlib.sha256(), 0, {}
        for p in graph["ports"]:
            if p["role"] == "in":
                continue
            item = ITEM[p["spec"]["dtype"]]
            raw = card_bytes(dev, binds[p["name"]], np.prod(p["spec"]["shape"]) * item)
            digest.update(raw)
            lanes = np.frombuffer(raw[: len(raw) // item * item], LANE[item])
            if item > 1:
                negative += int(np.sum(lanes == (1 << (8 * item - 1))))
            ports[p["name"]] = item
            if reduces:
                self.outputs[f"launch{index}/{p['name']}"] = np.frombuffer(
                    raw, np.uint8
                )
        row.update(digest=digest.hexdigest(), negative_zeros=negative, ports=ports)


def card_bytes(dev, addr, nbytes):
    entry, physical = dev.mmu.resolve(addr)
    room = entry.arena.live[entry.physical] - (physical - entry.physical)
    nbytes = min(int(nbytes), room)
    line = -(-nbytes // dev.card.line) * dev.card.line
    return bytes(dev.card.get(physical, min(line, room)))[:nbytes]


def device(args):
    return D.attached(
        D.Device("sim", board=args.board, build=args.build, queue_depth=1)
    )


def counters(dev):
    return {k: dev.counters[k] for k in sorted(dev.counters)}


def vit(args):
    rng = np.random.default_rng(18)
    x_np = rng.standard_normal((1, 3, 16, 16)).astype(np.float32)
    Tensor.manual_seed(0)
    model = seeded_model(1, 17, width=64, image_size=16)
    if args.zero_sign == "cpu":
        with Context(TRAINING=0, DEV="CPU"):
            out = model(Tensor(x_np, device="CPU")).numpy()
        return {}, {"logits": out}
    ktpugrad.install()
    with device(args) as dev:
        place(model)
        x = Tensor(x_np.astype(np.float16), device="KTPU").realize()
        with Context(TRAINING=0):
            out = model(x).realize().numpy()
        dev.drain()
        report = counters(dev)
    return report, {"logits": out}


def lenet(args):
    Tensor.manual_seed(0)
    with Context(DEV="CPU"):
        model = LeNet()
    params = get_parameters(model)
    arrays, steps = {}, []
    if args.zero_sign == "cpu":
        opt = Adam(params, lr=2e-3)
        for step in range(args.steps):
            x, y = batch(step, np.float32)
            with Context(TRAINING=1, DEV="CPU"):
                loss, logits = train_step(
                    model, opt, Tensor(x, device="CPU"), Tensor(y, device="CPU")
                )
            record(arrays, step, loss, logits, params)
            steps.append({"step": step, "loss": float(arrays[f"s{step}/loss"])})
        return {"steps": steps}, arrays
    ktpugrad.install()
    with device(args) as dev:
        place(model)
        opt = Adam16(params, lr=2e-3)
        for step in range(args.steps):
            x, y = batch(step, np.float16)
            before = dev.counters["cycles"], dev.counters["runs"]
            xb = Tensor(x, device="KTPU").realize()
            yb = Tensor(y, device="KTPU").realize()
            with Context(TRAINING=1):
                loss, logits = train_step(model, opt, xb, yb)
            dev.drain()
            cycles = dev.counters["cycles"] - before[0]
            runs = dev.counters["runs"] - before[1]
            record(arrays, step, loss, logits, params)
            steps.append(
                {
                    "step": step,
                    "loss": float(arrays[f"s{step}/loss"]),
                    "cycles": cycles,
                    "runs": runs,
                }
            )
        report = counters(dev)
    report["steps"] = steps
    return report, arrays


def record(arrays, step, loss, logits, params):
    arrays[f"s{step}/loss"] = loss.numpy()
    arrays[f"s{step}/logits"] = logits.numpy()
    for i, p in enumerate(params):
        arrays[f"s{step}/param{i}"] = p.numpy()
        if p.grad is not None:
            arrays[f"s{step}/grad{i}"] = p.grad.numpy()


def run(args):
    t0 = time.monotonic()
    launches = Launches()
    with ExitStack() as stack:
        if args.zero_sign != "cpu":
            launches.install(stack)
        with rs.zero_sign(None if args.zero_sign == "cpu" else args.zero_sign):
            report, arrays = (vit if args.case == "vit" else lenet)(args)
    report.update(
        case=args.case,
        zero_sign=args.zero_sign,
        wall_seconds=time.monotonic() - t0,
        negative_zeros={k: negative_zeros(v) for k, v in arrays.items()},
        launches=launches.rows,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    np.savez(args.output.with_suffix(".npz"), **arrays)
    np.savez(args.output.with_suffix(".launches.npz"), **launches.outputs)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {k: v for k, v in report.items() if k not in ("negative_zeros", "launches")}
        )
    )


def negative_zeros(a):
    a = np.asarray(a)
    return int(np.sum((a == 0) & np.signbit(a)))


def bits(a):
    a = np.ascontiguousarray(a)
    return a.view(np.uint16 if a.dtype.itemsize == 2 else np.uint32).reshape(-1)


def lanes(raw, item):
    return raw[: len(raw) // item * item].view(LANE[item])


def launch_compare(native, preserve, outputs_n, outputs_p):
    """Launch i of one value against launch i of the other: which outputs'
    bits differ, and for each reducing launch that differs, how many lanes
    of each output port."""
    pairs = list(zip(native, preserve))
    seen = [i for i, (a, b) in enumerate(pairs) if a["ports"] and b["ports"]]
    differ = [i for i in seen if pairs[i][0]["digest"] != pairs[i][1]["digest"]]
    reducing = [i for i in seen if pairs[i][0]["reduces"]]
    detail = [
        {
            "index": i,
            "name": pairs[i][0]["name"],
            "lanes_differing": {
                port: int(
                    np.sum(
                        lanes(outputs_n[f"launch{i}/{port}"], item)
                        != lanes(outputs_p[f"launch{i}/{port}"], item)
                    )
                )
                for port, item in pairs[i][0]["ports"].items()
            },
            "negative_zeros": {
                "native": pairs[i][0]["negative_zeros"],
                "preserve": pairs[i][1]["negative_zeros"],
            },
        }
        for i in differ
        if pairs[i][0]["reduces"]
    ]
    first = differ[0] if differ else None
    return {
        "launches": {"native": len(native), "preserve": len(preserve)},
        "same_sequence": [r["name"] for r in native] == [r["name"] for r in preserve],
        "unclassified": len(pairs) - len(seen),
        "differing": len(differ),
        "first_differing": (
            None
            if first is None
            else {
                "index": first,
                **{k: pairs[first][0][k] for k in ("name", "reduces")},
            }
        ),
        "reductions": len(reducing),
        "reductions_differing": len(detail),
        "reductions_differing_detail": detail,
        "negative_zeros": {
            "native": sum(r.get("negative_zeros", 0) for r in native),
            "preserve": sum(r.get("negative_zeros", 0) for r in preserve),
        },
    }


def compare(args):
    d = args.dir
    out = {}
    for case in ("vit", "lenet"):
        if not (d / f"{case}-native.json").exists():
            continue
        runs = {
            z: (
                json.loads((d / f"{case}-{z}.json").read_text(encoding="utf-8")),
                np.load(d / f"{case}-{z}.npz"),
            )
            for z in ("native", "preserve", "cpu")
        }
        (rn, an), (rp, ap), (_, ac) = runs["native"], runs["preserve"], runs["cpu"]
        names = sorted(an.files)
        differ = {k: int(np.sum(bits(an[k]) != bits(ap[k]))) for k in names}
        steps = sorted({k.split("/")[0] for k in names if "/" in k})
        per_step = {
            s: {k: v for k, v in differ.items() if k.startswith(s + "/") and v}
            for s in steps
        }
        first = next((s for s in steps if per_step[s]), None)
        error = {}
        for z, arrays in (("native", an), ("preserve", ap)):
            error[z] = {
                k: {
                    "max_abs": float(
                        np.max(np.abs(arrays[k].astype(np.float64) - ac[k]))
                    ),
                    "mean_abs": float(
                        np.mean(np.abs(arrays[k].astype(np.float64) - ac[k]))
                    ),
                }
                for k in names
                if k in ac.files and ac[k].shape == arrays[k].shape
            }
        out[case] = {
            "cycles": {"native": rn.get("cycles"), "preserve": rp.get("cycles")},
            "runs": {"native": rn.get("runs"), "preserve": rp.get("runs")},
            "compile_seconds": {
                "native": rn.get("compile_seconds"),
                "preserve": rp.get("compile_seconds"),
            },
            "differing_elements": differ,
            "differing_by_step": per_step,
            "first_differing_step": first,
            "elements": {k: int(an[k].size) for k in names},
            "negative_zeros": {
                "native": rn["negative_zeros"],
                "preserve": rp["negative_zeros"],
            },
            "vs_cpu_f32": error,
            "steps": {"native": rn.get("steps"), "preserve": rp.get("steps")},
            "launches": launch_compare(
                rn["launches"],
                rp["launches"],
                np.load(d / f"{case}-native.launches.npz"),
                np.load(d / f"{case}-preserve.launches.npz"),
            ),
        }
    (d / "compare.json").write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    for case, r in out.items():
        print(
            json.dumps(
                {
                    "case": case,
                    "cycles": r["cycles"],
                    "first_differing_step": r["first_differing_step"],
                    "differing_total": sum(r["differing_elements"].values()),
                    **{
                        k: r["launches"][k]
                        for k in (
                            "launches",
                            "same_sequence",
                            "unclassified",
                            "differing",
                            "reductions",
                            "reductions_differing",
                        )
                    },
                }
            )
        )


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("case", choices=("vit", "lenet", "compare"))
    ap.add_argument("--zero-sign", choices=("native", "preserve", "cpu"))
    ap.add_argument("--steps", type=int, default=20)
    ap.add_argument("--board", default="vlt_v9t6s_1n")
    ap.add_argument(
        "--build", type=Path, default=ROOT / "build/v9t6s/vlt_card_v9t6s_1n"
    )
    ap.add_argument("--output", type=Path)
    ap.add_argument("--dir", type=Path)
    args = ap.parse_args()
    if args.case == "compare":
        compare(args)
    else:
        run(args)


if __name__ == "__main__":
    main()
