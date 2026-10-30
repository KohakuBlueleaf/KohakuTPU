"""`forms.PREPARED` on against off on the Verilated v9t6s model: the
TG.RUN.015 read (`T(A40).sum(axis=1)`, A40 [8, 40]), one LeNet inference
step and one ViT-tiny block step (batch 1), each at both knob values.

    python scripts/py/measure/relayout/prepared_model.py one --dir D --workload W --knob K
    python scripts/py/measure/relayout/prepared_model.py execute --dir D --workload W --knob K
    python scripts/py/measure/relayout/prepared_model.py summary --dir D

`one`: the variant's graphs and kernels compiled offline (run with
`KTPU_PREPARED` the variant's: it is read at import). `execute`: the
variant on the model, `forms.PREPARED` its value, `KTPU_PLAN_CACHE` off so
no plan crosses variants; writes its dispatch_measure report
`D/W-K.json` (`--repeat` passes, all but the first timed). `summary`: each workload's on
against off from the reports, into `D/summary.json`; a workload whose two
variants compile to the same graphs and kernels is reported identical and
needs no model run. One variant a process, so each call is one piece.
"""

import argparse
import json
import os
import pickle
from pathlib import Path
from unittest.mock import patch

import numpy as np
from examples.tinygrad import dispatch_measure
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import forms as FM
from kohakutpu.application.runtime import generated
from kohakutpu.stack import rs
from tinygrad import Tensor

ROOT = Path(__file__).resolve().parents[4]
BOARD = "vlt_v9t6s_1n"
#: workload -> (dispatch_measure model, blocks, input shape or None)
WORKLOADS = {
    "tg015": ("tg015", 1, (8, 40)),
    "lenet": ("lenet", 1, None),
    "vit_block": ("vit", 1, None),
}
KNOBS = {"on": True, "off": False}


class RowSum:
    """TG.RUN.015's read: each row of [8, 40] summed."""

    def __call__(self, x):
        return x.sum(axis=1)


def workload(name, blocks, image_size, width, features, hidden, batch=1):
    model, blocks_, shape = WORKLOADS[name]
    if model == "tg015":
        Tensor.manual_seed(17)
        return RowSum(), shape
    return KEEP(model, blocks_, image_size, width, features, hidden, batch)


KEEP = dispatch_measure.workload


def make_args(name, device, output, repeat):
    return argparse.Namespace(
        model=name,
        device=device,
        transport=None,
        board=BOARD,
        depth=1,
        replay=True,
        warmup=0,
        repeat=repeat,
        batch=1,
        blocks=WORKLOADS[name][1],
        image_size=224,
        width=192,
        features=784,
        hidden=128,
        library="host_bundle",
        library_only=False,
        output=str(output) if output else None,
        quiet=True,
        knobs=False,
        depth_sweep=False,
        compare=False,
    )


def compile_one(name, knob, artifact):
    graphs, compiled = {}, {}
    original = generated.graph

    def rec_graph(gen, gargs, mach, mutant):
        out = original(gen, gargs, mach, mutant)
        graphs[(gen, gargs)] = out
        return out

    def rec_compile(self, source):
        if source not in compiled:
            with rs.compiler_owner(self):
                compiled[source] = rs.compile(
                    source, json.loads(source)["name"], self.machine
                )
        return compiled[source]

    with (
        patch.object(dispatch_measure, "workload", workload),
        patch.object(generated, "graph", rec_graph),
        patch.object(D.Device, "compile_graph", rec_compile),
    ):
        dispatch_measure.measure(make_args(name, "offline", None, 2))
    artifact.write_bytes(pickle.dumps({"graphs": graphs, "compiled": compiled}))
    print(f"{name}-{knob}: {len(graphs)} graphs, {len(compiled)} kernels", flush=True)


def execute_one(name, knob, artifact, output, repeat):
    bundle = pickle.loads(artifact.read_bytes())

    def host_graph(gen, gargs, mach, mutant):
        return bundle["graphs"][(gen, gargs)]

    def host_compile(self, source):
        self.counters["exact_hits"] += 1
        self.counters["library_hits"] += 1
        return bundle["compiled"][source]

    FM.PREPARED = KNOBS[knob]
    with (
        patch.object(dispatch_measure, "workload", workload),
        patch.object(generated, "graph", host_graph),
        patch.object(D.Device, "compile_graph", host_compile),
    ):
        return dispatch_measure.measure(make_args(name, "sim", output, repeat))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mode", choices=("one", "execute", "summary"))
    ap.add_argument("--dir", type=Path, required=True)
    ap.add_argument("--workload", choices=sorted(WORKLOADS))
    ap.add_argument("--knob", choices=sorted(KNOBS))
    ap.add_argument(
        "--repeat",
        type=int,
        default=2,
        help="passes on the model; all but the first timed",
    )
    args = ap.parse_args()
    args.dir.mkdir(parents=True, exist_ok=True)
    variant = args.dir / f"{args.workload}-{args.knob}"
    if args.mode == "one":
        assert FM.PREPARED == KNOBS[args.knob], "KTPU_PREPARED is the variant's"
        assert os.environ.get("KTPU_PLAN_CACHE") == "0", "no plan crosses variants"
        compile_one(args.workload, args.knob, variant.with_suffix(".pkl"))
        return
    if args.mode == "execute":
        os.environ["KTPU_PLAN_CACHE"] = "0"
        os.environ.setdefault(
            "KOHAKUTPU_SIM_BUILD", str(ROOT / "build/v9t6s/vlt_card_v9t6s_1n")
        )
        execute_one(
            args.workload,
            args.knob,
            variant.with_suffix(".pkl"),
            variant.with_suffix(".json"),
            args.repeat,
        )
        return
    summary = {}
    for name in WORKLOADS:
        on, off = (args.dir / f"{name}-{k}.pkl" for k in ("on", "off"))
        if on.read_bytes() == off.read_bytes():
            summary[name] = {"identical_graphs_and_kernels": True}
            continue
        reports = {
            (name, knob): json.loads((args.dir / f"{name}-{knob}.json").read_text())
            for knob in KNOBS
        }
        on, off = reports[name, "on"], reports[name, "off"]
        summary[name] = {
            knob: {
                "busy_cycles": reports[name, knob]["summary"]["busy_cycles"],
                "idle_cycles": reports[name, knob]["summary"]["idle_cycles"],
                "launches": reports[name, knob]["summary"]["launches"],
            }
            for knob in KNOBS
        }
        summary[name]["on_minus_off_busy"] = (
            on["summary"]["busy_cycles"] - off["summary"]["busy_cycles"]
        )
        summary[name]["max_abs_output_difference"] = float(
            np.max(np.abs(np.asarray(on["output"]) - np.asarray(off["output"])))
        )
    (args.dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    main()
