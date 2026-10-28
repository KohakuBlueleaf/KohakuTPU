"""Measure bound class-body linking against separate launches on v9t6s."""

import argparse
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import ktpu_pytest_lock
import numpy as np

from kohakutpu.application.kernels import lanes as L
from kohakutpu.application.kernels.classes import lanes as C
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
        dev.card.alarm = 500
        dev.recording = True
        source = np.arange(64, dtype=np.float16)
        plan = L.plan(64, ((3, 17), (7, 1)), 0, ((3, 9), (7, 1)), 0, 32, True)
        x, y = dev.alloc(128), dev.alloc(64)
        dev.write(x, source.tobytes())
        dev.write(y, np.zeros(32, np.float16).tobytes())
        raw, prototype = [], []
        submit = dev.card.submit

        def capture(program, *pos, **kw):
            p = next(iter(program.values())) if isinstance(program, dict) else program
            prototype.append(p)
            raw.append(p.bound(False))
            return submit(program, *pos, **kw)

        dev.card.submit = capture
        started = time.perf_counter()
        for cls, index, masks in C.tables(plan):
            kernel, given = C.kernel(
                cls, plan.src_words, plan.dst_words, dev.machine.count("VC")
            )
            forms.launch(
                dev,
                kernel,
                given,
                {
                    "src": x,
                    "dst": y,
                    "index": forms.table(dev, index, np.float32),
                    "mask": forms.table(dev, masks),
                },
            )
        launch_seconds = time.perf_counter() - started
        dev.card.submit = submit
        expected = np.zeros(32, np.float16)
        for i in range(3):
            expected[i * 9 : i * 9 + 7] = source[i * 17 : i * 17 + 7]
        np.testing.assert_array_equal(
            np.frombuffer(dev.read(y, 64), np.float16), expected
        )
        cycles = dev.counters["cycles"]
        machine = rs.machine_json(dev.machine)
        started = time.perf_counter()
        linked = rs._rs.package_link(raw, machine)
        link_seconds = time.perf_counter() - started
        dev.write(y, np.zeros(32, np.float16).tobytes())
        p = prototype[0]
        started = time.perf_counter()
        result = dev.card.run({0: Prebuilt(linked, (), machine, 0, (), False, p.fetch)})
        linked_seconds = time.perf_counter() - started
        np.testing.assert_array_equal(
            np.frombuffer(dev.read(y, 64), np.float16), expected
        )
        report = {
            "command": "python " + " ".join(sys.argv),
            "board": "vlt_v9t6s_1n",
            "class_instances": len(raw),
            "launch_cycles": cycles,
            "linked_cycles": result["cycles"],
            "launch_seconds_including_compile": launch_seconds,
            "link_seconds_including_verifier": link_seconds,
            "linked_run_seconds": linked_seconds,
            "launch_bytes": sum(map(len, raw)),
            "linked_bytes": len(linked),
            "values_equal": True,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report), flush=True)
    finally:
        if dev is not None:
            dev.close()
        ktpu_pytest_lock.pytest_unconfigure(SimpleNamespace())


if __name__ == "__main__":
    main()
