"""Independent uniform-attention oracle and a poisoned tail-mask model mutation."""

import argparse
import json
import shutil
from pathlib import Path
from unittest.mock import patch

import numpy as np
from kohakutpu.application import api as kp
from kohakutpu.application.api.custom import call
from kohakutpu.application.kernels import transpose as T
from kohakutpu.application.kernels.native import layout as Y
from kohakutpu.application.runtime import device as D
from kohakutpu.language.numerics import e8m15, mxfp7
from kohakutpu.stack import rs
from native_values import decode_bytes, encode_bytes


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=("prebuild", "sim"))
    p.add_argument("--library", type=Path, required=True)
    p.add_argument("--archive", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    if a.mode == "sim":
        shutil.unpack_archive(str(a.archive), str(a.library), "zip")
    dev = D.Device(
        "offline" if a.mode == "prebuild" else "sim",
        board="vlt_v9t6s_1n",
        library=a.library,
        library_only=True,
    )
    records = (
        {}
        if a.mode == "prebuild"
        else json.loads(
            (a.library / "recorded.json").read_text(), object_hook=decode_bytes
        )
    )
    original = dev.compile_graph

    def lookup(source):
        machine = rs.machine_json(dev.machine)
        key = json.loads(rs._rs.library_key(source, machine))["class"]
        if a.mode == "prebuild":
            rs._rs.library_prebuild(str(a.library), source, machine)
            records[key] = original(source)
        output = records[key]
        output["packages"] = {int(k): v for k, v in output["packages"].items()}
        return output

    def forbidden(*a, **kw):
        raise AssertionError("runtime compiler reached")

    report = {"mode": a.mode, "variants": []}
    x = np.zeros((197, 3, 1, 64), np.float16)
    x[:, 2, :, :] = 0.5
    p_ref = np.zeros((1, 256), np.float16)
    p_ref[:, :197] = 1.0
    v_ref = np.zeros((64, 256), np.float16)
    v_ref[:, :197] = 0.5
    prod_ref = mxfp7.model_matmul(p_ref, v_ref).astype(np.float16)
    oracle = float(
        np.float16(e8m15.value(prod_ref.astype(np.float64) * e8m15.value(1.0 / 197.0)))[
            0, 0
        ]
    )
    with (
        D.attached(dev),
        patch.object(dev, "compile_graph", lookup),
        patch.object(
            rs._rs, "compile", rs._rs.compile if a.mode == "prebuild" else forbidden
        ),
    ):
        inp, mask = kp.tensor(x.reshape(197, 192)), kp.tensor(T.masks())
        for blocked, poison in ((False, False), (True, False), (True, True)):
            tail = kp.tensor(
                (np.ones(16) if poison else np.arange(16) < 5).astype(np.float16)
            )
            mark = len(dev.runs)
            result = call(
                Y.attention_kernel(1, 197, 1, 64, 1, query_blocked=blocked),
                inp,
                mask,
                tail,
            )[0]
            dev.drain()
            row = {
                "query_blocked": blocked,
                "poison_tail": poison,
                "cycles": sum(r.cycles for r in dev.runs[mark:]),
            }
            if a.mode == "sim":
                value = result.numpy().astype(np.float32)
                row.update(
                    minimum=float(value.min()),
                    maximum=float(value.max()),
                    oracle=oracle,
                    oracle_max_absolute=float(np.abs(value - oracle).max()),
                    fp64_gap=float(np.abs(value - 0.5).max()),
                )
            report["variants"].append(row)
            a.output.write_text(json.dumps(report, indent=2))
            if a.mode == "sim":
                if not poison:
                    np.testing.assert_array_equal(value, np.float32(oracle))
                else:
                    assert np.abs(value - oracle).max() > 0.01
    a.output.write_text(json.dumps(report, indent=2))
    if a.mode == "prebuild":
        (a.library / "recorded.json").write_text(
            json.dumps(records, default=encode_bytes)
        )
        shutil.make_archive(str(a.archive.with_suffix("")), "zip", a.library)
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
