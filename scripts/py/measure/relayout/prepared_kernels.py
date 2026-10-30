"""`forms.PREPARED` on against off for one LeNet inference step, kernel by
kernel, on the Verilated v9t6s model: each kernel only one variant launches
run alone on the model, from the bytes its ports held when the offline step
launched it.

    python scripts/py/measure/relayout/prepared_kernels.py capture --dir D --knob K
    python scripts/py/measure/relayout/prepared_kernels.py plan --dir D
    python scripts/py/measure/relayout/prepared_kernels.py measure --dir D --pick N
    python scripts/py/measure/relayout/prepared_kernels.py summary --dir D

`capture` (offline; `KTPU_PREPARED` the knob's, `KTPU_PLAN_CACHE=0`): the
step's second pass, every launch in order -- its graph, its compiled
output, its binds, each port's bytes at launch -- into `D/K.pkl`. `plan`:
the kernels (by graph text) only `on` launches and only `off` launches,
with their launch counts, ordered heaviest first by their ports' bytes,
the two variants interleaved; into `D/plan.json`. `measure`: the plan's
kernel N alone on the model (`KOHAKUTPU_SIM_BUILD`), twice, the second
run's cycles (the first warms nothing the model keeps, so the two agree;
both are written), into `D/kernel-N.json`; one kernel a process.
`summary`: each variant's kernels measured, the rest derived from a fit of
cycles to port bytes over the measured ones of that variant (the fit and
its worst relative residual written), each kernel times its launches, plus
`--launch-gap` cycles a launch for the launch counts of the whole step;
into `D/summary.json`.
"""

import argparse
import itertools
import json
import os
import pickle
import time
from pathlib import Path
from unittest.mock import patch

import numpy as np
from examples.tinygrad import dispatch_measure
from kohakutpu.application.ktpugrad import Inference
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import forms as FM
from kohakutpu.application.runtime import generated
from kohakutpu.language.dsl import TensorSpec

ROOT = Path(__file__).resolve().parents[4]
BOARD = "vlt_v9t6s_1n"
KNOBS = {"on": True, "off": False}


def args_of(device):
    return argparse.Namespace(
        model="lenet",
        device=device,
        transport=None,
        board=BOARD,
        depth=1,
        replay=True,
        warmup=0,
        repeat=2,
        batch=1,
        blocks=1,
        image_size=224,
        width=192,
        features=784,
        hidden=128,
        library="host_bundle",
        library_only=False,
        output=None,
        quiet=True,
        knobs=False,
        depth_sweep=False,
        compare=False,
    )


def port_bytes(graph) -> dict:
    return {
        p["name"]: TensorSpec(
            tuple(p["spec"]["shape"]), p["spec"]["dtype"], (), "dram"
        ).nbytes()
        for p in graph["ports"]
    }


#: The captured launches, and where each pass's start.
LAUNCHES: list = []
MARKS: list = []


class Marked(Inference):
    """`Inference` marking where each pass's launches start."""

    def __call__(self, *a, **kw):
        MARKS.append(len(LAUNCHES))
        return super().__call__(*a, **kw)


def capture(knob, artifact):
    sources, outputs, launches = {}, {}, LAUNCHES
    kernel, compile_graph, submit = (
        D.Device.kernel,
        D.Device.compile_graph,
        D.Device.submit,
    )

    def keep_kernel(self, source, graph=None, traced=None):
        got = kernel(self, source, graph, traced)
        sources[id(got)] = source
        return got

    def keep_compile(self, source):
        out = outputs[source] = compile_graph(self, source)
        return out

    def keep_submit(self, compiled, binds, kind="dsl", info=None):
        source = sources[id(compiled)]
        sizes = port_bytes(json.loads(source))
        data = {p: self.read(int(a), sizes[p]) for p, a in binds.items() if p in sizes}
        launches.append({"source": source, "kind": kind, "data": data})
        return submit(self, compiled, binds, kind, info)

    with (
        patch.object(dispatch_measure, "Inference", Marked),
        patch.object(D.Device, "kernel", keep_kernel),
        patch.object(D.Device, "compile_graph", keep_compile),
        patch.object(D.Device, "submit", keep_submit),
    ):
        dispatch_measure.measure(args_of("offline"))
    # The last pass: the first makes the forms and uploads it reads.
    marks = [*MARKS, len(launches)]
    step = launches[marks[-2] :]
    artifact.write_bytes(
        pickle.dumps(
            {
                "knob": knob,
                "launches": step,
                "passes": [b - a for a, b in itertools.pairwise(marks)],
                "outputs": {s: outputs[s] for s in {x["source"] for x in step}},
            }
        )
    )
    kernels = len({x["source"] for x in step})
    print(f"{knob}: {len(step)} launches, {kernels} kernels", flush=True)


def plan(d):
    runs = {k: pickle.loads((d / f"{k}.pkl").read_bytes())["launches"] for k in KNOBS}
    kernels = {k: {} for k in KNOBS}
    for k, ls in runs.items():
        for i, x in enumerate(ls):
            got = kernels[k].setdefault(
                x["source"],
                {
                    "first": i,
                    "launches": 0,
                    "name": json.loads(x["source"])["name"],
                    "bytes": sum(len(b) for b in x["data"].values()),
                },
            )
            got["launches"] += 1
    only = {
        k: sorted(
            (
                {"knob": k, **v}
                for s, v in kernels[k].items()
                if s not in kernels["off" if k == "on" else "on"]
            ),
            key=lambda v: -v["bytes"],
        )
        for k in KNOBS
    }
    order = []
    for i in range(max(len(v) for v in only.values())):
        order += [only[k][i] for k in KNOBS if i < len(only[k])]
    shared = sum(1 for s in kernels["on"] if s in kernels["off"])
    out = {
        "launches": {k: len(v) for k, v in runs.items()},
        "kernels": {k: len(v) for k, v in kernels.items()},
        "shared_kernels": shared,
        "shared_launches": {
            k: sum(v["launches"] for s, v in kernels[k].items() if s in kernels[o])
            for k, o in (("on", "off"), ("off", "on"))
        },
        "only": {k: len(v) for k, v in only.items()},
        "order": order,
    }
    (d / "plan.json").write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps({k: v for k, v in out.items() if k != "order"}), flush=True)


def measure(d, pick):
    item = json.loads((d / "plan.json").read_text())["order"][pick]
    cap = pickle.loads((d / f"{item['knob']}.pkl").read_bytes())
    x = cap["launches"][item["first"]]
    output = cap["outputs"][x["source"]]
    graph = json.loads(x["source"])
    sizes = port_bytes(graph)
    os.environ.setdefault(
        "KOHAKUTPU_SIM_BUILD", str(ROOT / "build/v9t6s/vlt_card_v9t6s_1n")
    )
    wall = time.perf_counter()
    dev = D.Device("sim", board=BOARD, nodes=1)
    records = []
    original = dev.card._result

    def result(*a, **kw):
        got = original(*a, **kw)
        records.append(dict(got["phases"], cycles=got["cycles"]))
        return got

    with D.attached(dev), patch.object(dev.card, "_result", result):
        compiled = generated.from_output(dev, graph, output)
        binds = {}
        for p, data in x["data"].items():
            addr = dev.alloc(max(sizes[p], 64))
            dev.write(addr, data)
            binds[p] = addr
        for _ in range(2):
            dev.run(compiled, binds, kind=x["kind"])
        dev.drain()
    out = {
        **item,
        "pick": pick,
        "runs": records,
        "cycles": records[-1]["cycles"],
        "wall_seconds": time.perf_counter() - wall,
    }
    (d / f"kernel-{pick}.json").write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps({k: v for k, v in out.items() if k != "runs"}), flush=True)


def summary(d, gap):
    p = json.loads((d / "plan.json").read_text())
    measured = {}
    for f in d.glob("kernel-*.json"):
        m = json.loads(f.read_text())
        measured[m["pick"]] = m
    out = {"launch_gap_cycles": gap, "variants": {}}
    for k in KNOBS:
        items = [(i, v) for i, v in enumerate(p["order"]) if v["knob"] == k]
        got = [(v["bytes"], measured[i]["cycles"]) for i, v in items if i in measured]
        b, c = (np.array(z, float) for z in zip(*got)) if got else ([], [])
        a1, a0 = np.polyfit(b, c, 1) if len(got) > 1 else (0.0, float(np.mean(c)))
        worst = max((abs(a1 * x + a0 - y) / y for x, y in got), default=0.0)
        kernels = []
        for i, v in items:
            m = measured.get(i)
            cycles = m["cycles"] if m else max(a1 * v["bytes"] + a0, 0.0)
            kernels.append(
                {
                    "name": v["name"],
                    "launches": v["launches"],
                    "bytes": v["bytes"],
                    "cycles": cycles,
                    "how": "measured" if m else "derived",
                }
            )
        busy = sum(x["cycles"] * x["launches"] for x in kernels)
        out["variants"][k] = {
            "kernels": kernels,
            "measured": sum(x["how"] == "measured" for x in kernels),
            "derived": sum(x["how"] == "derived" for x in kernels),
            "fit": {"cycles_per_byte": a1, "cycles": a0, "worst_relative": worst},
            "only_busy_cycles": busy,
            "launches": p["launches"][k],
            "launch_gap_total": gap * p["launches"][k],
            "model_seconds": sum(
                measured[i]["wall_seconds"] for i, _ in items if i in measured
            ),
        }
    on, off = out["variants"]["on"], out["variants"]["off"]
    out["on_minus_off_cycles"] = (
        on["only_busy_cycles"] + on["launch_gap_total"]
        - off["only_busy_cycles"] - off["launch_gap_total"]
    )  # fmt: skip
    (d / "summary.json").write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps({k: v for k, v in out.items() if k != "variants"}), flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mode", choices=("capture", "plan", "measure", "summary"))
    ap.add_argument("--dir", type=Path, required=True)
    ap.add_argument("--knob", choices=sorted(KNOBS))
    ap.add_argument("--pick", type=int)
    ap.add_argument("--launch-gap", type=float, default=0.0)
    args = ap.parse_args()
    args.dir.mkdir(parents=True, exist_ok=True)
    if args.mode == "capture":
        assert FM.PREPARED == KNOBS[args.knob], "KTPU_PREPARED is the knob's"
        assert os.environ.get("KTPU_PLAN_CACHE") == "0", "no plan crosses variants"
        capture(args.knob, args.dir / f"{args.knob}.pkl")
    elif args.mode == "plan":
        plan(args.dir)
    elif args.mode == "measure":
        measure(args.dir, args.pick)
    else:
        summary(args.dir, args.launch_gap)


if __name__ == "__main__":
    main()
