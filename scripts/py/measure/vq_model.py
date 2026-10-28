"""Compile on the Linux host, execute immutable packages on the read-only model.

One execute invocation is one model session. No compiler is called by execute.
"""

import argparse
import json
import os
import pickle
import re
import time
from pathlib import Path

import numpy as np

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import matmul as MM
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.kernels.classes.policy import ShapeClass
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import generated
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.application.runtime.packing import NonfinitePackingError
from kohakutpu.language.numerics import mxfp7
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs

ROOT = Path(__file__).resolve().parents[3]


def packed_reference(x, side, groups, nk):
    """Independent byte slots using the numerical MX7 oracle, not a roundtrip."""
    q, es, m8 = mxfp7.quantise_fp16(x)
    rows, cols = x.shape
    fields = ((es + 20) << 3) | (m8 - 8)
    out = bytearray()
    for tile in range(rows // (4 * groups)):
        for chunk in range(cols // (32 * nk)):
            for group in range(groups):
                for block in range(nk):
                    r = tile * 4 * groups + group * 4
                    k = chunk * 32 * nk + block * 32
                    scale = sum(
                        int(fields[r + lane, k // 32]) << (24 - 8 * lane)
                        for lane in range(4)
                    )
                    for word in range(4):
                        bits = scale
                        for lane in range(4):
                            for kk in range(8):
                                slot = lane * 8 + kk if side == "a" else kk * 4 + lane
                                bits |= (int(q[r + lane, k + word * 8 + kk]) & 127) << (
                                    249 - 7 * slot
                                )
                        out.extend(bits.to_bytes(32, "little"))
    return bytes(out)


def quant_cases():
    for label, rows, cols, rp, cp, pitch, offset in (
        ("aligned", 64, 32, 64, 32, 32, 0),
        ("padded", 131, 16, 132, 32, 48, 16),
    ):
        live = (
            ((np.arange(rows * cols) % 97 - 48) / 32).astype("<f2").reshape(rows, cols)
        )
        x = np.full((rows + bool(offset), pitch), np.nan, dtype="<f2")
        x[:rows, offset : offset + cols] = live
        padded = np.zeros((rp, cp), dtype="<f2")
        padded[:rows, :cols] = live
        side = "a" if label == "aligned" else "b"
        for finite, batch, overlap, pipeline in (
            (True, 1, 0, 0),
            (True, 4, 0, 0),
            (False, 4, 0, 0),
            (False, 4, 1, 0),
            (True, 4, 0, 1),
            (False, 4, 0, 1),
        ):
            os.environ["KTPU_QUANT_PIPELINE"] = str(pipeline)
            os.environ["KTPU_PIPE_FINITE_BATCH"] = str(batch)
            os.environ["KTPU_QUANT_CLASSIFIER_OVERLAP"] = str(overlap)
            os.environ["KTPU_PIPE_OVERLAP"] = "1"
            kernel, given = MM.quant_kernel(
                V.Operand("full", pitch=pitch, offset=offset),
                rows,
                cols,
                rp,
                cp,
                side,
                rp // 4,
                cp // 32,
                finite=finite,
            )
            yield dict(
                label=f"quant-{label}-finite{int(finite)}-batch{batch}-overlap{overlap}-pipe{pipeline}",
                kernel=kernel,
                given=given,
                inputs={"x": x.tobytes()},
                output="q",
                expected=packed_reference(padded, side, rp // 4, cp // 32),
                read_bytes=live.nbytes,
                write_bytes=rp * cp,
                math_lane_ops=None,
                shape=[rows, cols],
                kind="quant",
            )


def dsl_cases():
    for label, width, rows, expr, kinds in (
        ("relu", 128, 32, X.op("max", X.Load(0), 0.0), ("full",)),
        ("add", 128, 32, X.op("add", X.Load(0), X.Load(1)), ("full", "full")),
        ("scale", 128, 64, X.op("mul", X.Load(0), X.Load(1)), ("full", "scalar")),
        ("row-bias", 128, 32, X.op("add", X.Load(0), X.Load(1)), ("full", "row")),
        ("reduce", 128, 32, X.Reduce("sum", X.Load(0)), ("full",)),
        ("wide", 512, 16, X.op("max", X.Load(0), 0.0), ("full",)),
    ):
        operands = tuple(
            V.Operand(kind, pitch=width if kind == "full" else 0) for kind in kinds
        )
        out = V.Operand("row") if label == "reduce" else V.Operand("full", pitch=width)
        x = ((np.arange(rows * width) % 16 - 8) / 16).astype("<f2").reshape(rows, width)
        inputs = {"in0": x.tobytes()}
        if len(kinds) == 2:
            other = np.full(
                (rows, width) if kinds[1] == "full" else (max(16, rows),),
                0.5,
                dtype="<f2",
            )
            inputs["in1"] = other.tobytes()
        expected = {
            "relu": np.maximum(x, 0),
            "wide": np.maximum(x, 0),
            "add": x + np.float16(0.5),
            "row-bias": x + np.float16(0.5),
            "scale": x * np.float16(0.5),
            "reduce": x.astype(np.float32).sum(-1).astype("<f2"),
        }[label]
        variants = (
            (
                (False, 32, 0),
                (True, 32, 0),
                (True, 128, 0),
                (True, 256, 0),
                (True, 256, 1),
            )
            if label == "wide"
            else ((False, 32, 0), (True, 32, 0))
        )
        for classed, class_width, direct in variants:
            os.environ["KTPU_CLASS_WIDE_WIDTH"] = str(class_width)
            os.environ["KTPU_CLASS_WIDE_DIRECT"] = str(direct)
            kernel, given, _ = V.rows_kernel(
                expr,
                operands,
                rows,
                width,
                out,
                1,
                shape_class=ShapeClass(32, 256) if classed else None,
            )
            yield dict(
                label=f"dsl-{label}-class{int(classed)}-w{class_width}-direct{direct}",
                kernel=kernel,
                given=given,
                inputs=inputs,
                output="out",
                expected=expected.tobytes(),
                read_bytes=sum(len(b) for b in inputs.values()),
                write_bytes=expected.nbytes,
                math_lane_ops=X.count(expr) * rows * width,
                shape=[rows, width],
                kind="dsl",
            )


def compile_cases(args):
    machine = machine_of("vlt_v9t6s_1n", packing_backend="vector_mx7")
    cases = quant_cases() if args.group == "quant" else dsl_cases()
    compiled = []
    for case in cases:
        if args.select and args.select not in case["label"]:
            continue
        kernel, given = case.pop("kernel"), case.pop("given")
        trace = kernel.trace(given, machine=K.dsl_machine(machine))
        output = rs._rs.compile(
            json.dumps(trace.graph), rs.machine_json(machine), cache_dir="", fetch=True
        )
        assert not output["verdict"]["refused"], output["verdict"]
        assert output["bound"] is None
        case.update(graph=trace.graph, compiled=output)
        compiled.append(case)
        print(
            json.dumps(
                dict(
                    label=case["label"],
                    package_bytes=sum(
                        len(p["bytes"]) for p in output["packages"].values()
                    ),
                )
            ),
            flush=True,
        )
    args.artifact.parent.mkdir(parents=True, exist_ok=True)
    args.artifact.write_bytes(
        pickle.dumps(dict(machine=machine.machine_json, cases=compiled))
    )


def model_counters(log):
    counters = {}
    for line in log.read_text(encoding="utf-8").splitlines():
        match = re.match(
            r"(VRES|V2MATH|V2UNPACK|V2PACK|V2DMA|V2DISPATCH) (\S+) (.*)", line
        )
        if match:
            fields = match[3].split()
            counters.setdefault(match[2], {})[match[1]] = {
                fields[i]: int(fields[i + 1]) for i in range(0, len(fields) - 1, 2)
            }
    return counters


def execute(args):
    bundle = pickle.loads(args.artifact.read_bytes())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    log = args.output.with_suffix(".log")
    started = time.monotonic()
    dev = D.Device(
        kind="sim",
        board="vlt_v9t6s_1n",
        nodes=1,
        build=ROOT / "build/v9t6s/vlt_card_v9t6s_1n",
        log=log,
        classes=False,
    )
    dev.machine = machine_of("vlt_v9t6s_1n", packing_backend="vector_mx7")
    assert dev.machine.machine_json == bundle["machine"]
    records = []
    try:
        for case in bundle["cases"]:
            if args.select and args.select not in case["label"]:
                continue
            comp = generated.from_output(dev, case["graph"], case["compiled"])
            binds = {}
            for name, data in case["inputs"].items():
                binds[name] = dev.alloc(len(data))
                dev.write(binds[name], data)
            guard = dev.alloc(len(case["expected"]) + 256)
            binds[case["output"]] = guard + 128
            try:
                dev.write(guard, bytes([0xD3]) * (len(case["expected"]) + 256))
                cycles = []
                for _ in range(args.repeat):
                    run = dev.run(comp, binds, kind=case["kind"])
                    cycles.append(run.cycles)
                actual = dev.read(guard, len(case["expected"]) + 256)
                assert actual[:128] == actual[-128:] == bytes([0xD3]) * 128, case[
                    "label"
                ]
                got = actual[128:-128]
                assert got == case["expected"], (
                    case["label"],
                    got[:64].hex(),
                    case["expected"][:64].hex(),
                )
                for name, data in case["inputs"].items():
                    assert dev.read(binds[name], len(data)) == data, (
                        case["label"],
                        name,
                    )
                record = {
                    k: case[k]
                    for k in (
                        "label",
                        "kind",
                        "shape",
                        "read_bytes",
                        "write_bytes",
                        "math_lane_ops",
                    )
                }
                record.update(
                    cycles=cycles,
                    parity="exact",
                    package_bytes=sum(len(p["bytes"]) for p in comp.packages.values()),
                )
                records.append(record)
                print(json.dumps(record), flush=True)
                args.output.write_text(
                    json.dumps(dict(records=records), indent=2), encoding="utf-8"
                )
            finally:
                for name in case["inputs"]:
                    dev.free(binds[name])
                dev.free(guard)
        if args.nonfinite_artifact:
            negative = pickle.loads(args.nonfinite_artifact.read_bytes())
            case = next(
                c
                for c in negative["cases"]
                if c["label"] == "quant-padded-finite0-batch4-overlap0-pipe1"
            )
            comp = generated.from_output(dev, case["graph"], case["compiled"])
            raw = bytearray(case["inputs"]["x"])
            raw[(130 * 48 + 31) * 2 : (130 * 48 + 32) * 2] = bytes.fromhex("007c")
            x, q = dev.alloc(len(raw)), dev.alloc(len(case["expected"]))
            dev.write(x, raw)
            try:
                dev.run(comp, {"x": x, "q": q}, kind="quant")
            except NonfinitePackingError as error:
                records.append(
                    dict(label="pipeline-live-infinity", rejected=str(error))
                )
            else:
                raise AssertionError("pipeline accepted live infinity")
            finally:
                dev.free(x)
                dev.free(q)
    finally:
        try:
            dev.close()
        except NonfinitePackingError:
            assert args.nonfinite_artifact
        args.output.write_text(
            json.dumps(
                dict(
                    records=records,
                    counters=model_counters(log),
                    wall_seconds=time.monotonic() - started,
                ),
                indent=2,
            ),
            encoding="utf-8",
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("compile", "execute", "counters"))
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument("--group", choices=("quant", "dsl"), default="quant")
    parser.add_argument("--select")
    parser.add_argument("--nonfinite-artifact", type=Path)
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.mode == "counters":
        report = json.loads(args.output.read_text(encoding="utf-8"))
        report["counters"] = model_counters(args.output.with_suffix(".log"))
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    else:
        (compile_cases if args.mode == "compile" else execute)(args)


if __name__ == "__main__":
    main()
