"""Bounded v9t6s prefix and class-policy activation cycle measurements."""

import argparse
import json
import runpy
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import ktpu_pytest_lock
import numpy as np

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.kernels.classes.policy import ShapeClass
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import forms

ROOT = Path(__file__).resolve().parents[3]


class PrefixDone(Exception):
    """The requested number of launches has completed."""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workload", choices=("activation", "prefix"))
    parser.add_argument("output", type=Path)
    parser.add_argument("--policy", choices=("exact", "pow2", "coarse"), default="pow2")
    parser.add_argument("--cores", choices=("all", "min"), default="all")
    parser.add_argument("--limit", type=int, default=4)
    args = parser.parse_args()
    ktpu_pytest_lock.pytest_configure(SimpleNamespace())
    dev = None
    started = time.perf_counter()
    result = {
        "command": "python " + " ".join(sys.argv),
        "workload": args.workload,
        "policy": args.policy,
        "cores": args.cores,
        "board": "vlt_v9t6s_1n",
        "batch": 1,
    }
    try:
        dev = D.attach(
            D.Device(
                kind="sim",
                board="vlt_v9t6s_1n",
                build=ROOT / "build/v9t6s/vlt_card_v9t6s_1n",
                queued=False,
            )
        )
        dev.card.alarm = 500
        if args.workload == "activation":
            x, y = dev.alloc(192), dev.alloc(192)
            data = np.linspace(-1, 1, 96, dtype=np.float16)
            dev.write(x, data.tobytes())
            kernel, given, _ = V.rows_kernel(
                X.op("max", X.Load(0), 0.0),
                (V.Operand("full", pitch=96, rows=1),),
                1,
                96,
                V.Operand("full", pitch=96),
                dev.machine.count("VC"),
                shape_class=ShapeClass(32, 256, policy=args.policy, cores=args.cores),
            )
            result["class_info"] = kernel.class_info
            for _ in range(args.limit):
                forms.launch(dev, kernel, given, {"in0": x, "out": y})
                x, y = y, x
            actual = np.frombuffer(dev.read(x, 192), np.float16)
            np.testing.assert_array_equal(actual, np.maximum(data, 0))
            dev.free(x)
            dev.free(y)
        else:
            harness = runpy.run_path(str(ROOT / "scripts/py/templates/prebuild.py"))
            original = dev.run

            def run(*pos, **kw):
                if dev.counters["runs"] >= args.limit:
                    raise PrefixDone
                return original(*pos, **kw)

            dev.run = run
            try:
                harness["lenet"](1, False)
            except PrefixDone:
                pass
        dev.drain()
        result["counters"] = dict(dev.counters)
        result["seconds"] = time.perf_counter() - started
        assert result["seconds"] < 600
    finally:
        if dev is not None:
            dev.close()
        ktpu_pytest_lock.pytest_unconfigure(SimpleNamespace())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
