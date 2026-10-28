"""Measure both host count-binding paths on one verified activation package."""

import argparse
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import ktpu_pytest_lock
import numpy as np

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import forms
from kohakutpu.application.runtime.packages import Prebuilt
from kohakutpu.stack import rs

ROOT = Path(__file__).resolve().parents[3]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    ktpu_pytest_lock.pytest_configure(SimpleNamespace())
    dev = None
    try:
        dev = D.attach(
            D.Device(
                kind="sim",
                board="vlt_v9t6s_1n",
                build=ROOT / "build/v9t6s/vlt_card_v9t6s_1n",
                queued=False,
            )
        )
        dev.recording = True
        x, y = dev.alloc(256), dev.alloc(256)
        data = np.arange(128, dtype=np.float16)
        dev.write(x, data.tobytes())
        captured = []
        submit = dev.card.submit

        def capture(program, *pos, **kw):
            captured.append(
                next(iter(program.values())) if isinstance(program, dict) else program
            )
            return submit(program, *pos, **kw)

        dev.card.submit = capture
        kernel, given = V.flat_kernel(
            X.op("add", X.Load(0), 1.0), ("f16",), 128, "f16", 1
        )
        forms.launch(dev, kernel, given, {"in0": x, "out": y})
        dev.card.submit = submit
        p = captured[0]
        raw = p.bound(False)
        machine = rs.machine_json(dev.machine)
        package = rs._rs.package_read(raw, machine)
        index, step = next(
            (i, step) for i, step in enumerate(package["steps"]) if step[0] == 1
        )
        count = step[3]
        meta = json.dumps(
            {
                "parameters": {"count": {"lo": count, "hi": count}},
                "sites": [
                    {
                        "name": "dispatch_count",
                        "step": index,
                        "bit": 32,
                        "width": 32,
                        "coefficients": {"count": 1},
                    }
                ],
            }
        )
        result = {
            "command": "python " + " ".join(sys.argv),
            "board": "vlt_v9t6s_1n",
            "elements": 128,
            "dispatch_count": count,
            "paths": {},
        }
        for path in ("step", "register"):
            start = time.perf_counter()
            bound = rs._rs.bind(
                raw,
                [],
                machine,
                params={"count": count},
                template_json=meta,
                count_path=path,
            )
            bind_seconds = time.perf_counter() - start
            dev.write(y, bytes(256))
            run = dev.card.run({0: Prebuilt(bound, (), machine, 0, (), False, p.fetch)})
            np.testing.assert_array_equal(
                np.frombuffer(dev.read(y, 256), np.float16), data + np.float16(1)
            )
            result["paths"][path] = {
                "cycles": run["cycles"],
                "bytes": len(bound),
                "steps": len(rs._rs.package_read(bound, machine)["steps"]),
                "bind_seconds": bind_seconds,
            }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result), flush=True)
    finally:
        if dev is not None:
            dev.close()
        ktpu_pytest_lock.pytest_unconfigure(SimpleNamespace())


if __name__ == "__main__":
    main()
