"""Certify a fused tinygrad elementwise AST over a finite two-shape domain.

Uses the shared rows class generator, Rust template proof and ClassLibrary;
the selector certifies only these members, not arbitrary dynamic shapes.
"""

import argparse
import json
import time
from pathlib import Path
from unittest.mock import patch

import numpy as np
from examples.tinygrad import fusion_measure, fusion_replay
from tinygrad import Tensor, tensor

from kohakutpu.application.kernels import vector as V
from kohakutpu.application.kernels.classes.policy import ShapeClass
from kohakutpu.application.ktpugrad import lower
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime.class_library import ClassLibrary
from kohakutpu.application.runtime.template_store import TemplateStore
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs

SHAPES = (2048, 4096)


def sources_for(dev):
    sources, expressions = [], []
    with fusion_measure.configured("bounded"), D.attached(dev):
        for n in SHAPES:
            kernels = []

            def collect(linear, *args, kernels=kernels, **kwargs):
                kernels.extend(fusion_measure.asts(linear))

            x = Tensor.empty(n, device="KTPU")
            with patch.object(tensor, "run_linear", collect):
                ((x + 0.125) * 0.5).relu().realize()
            assert len(kernels) == 1
            plan = lower.plan(kernels[0])
            assert plan.kind == "vector" and not plan.reductions
            kernel, given = V.flat_kernel(
                plan.expr, ("f16",), n, "f16", 1, (), shape_class=ShapeClass(32, 256)
            )
            sources.append(
                json.dumps(
                    kernel.trace(given, machine=K.dsl_machine(dev.machine)).graph
                )
            )
            expressions.append(repr(plan.expr))
    return sources, expressions


def admit(root, machine, sources, outputs):
    points = [{"member": i} for i in range(2)]
    derived = rs._rs.template_derive(
        [o["packages"][0]["bytes"] for o in outputs],
        json.dumps(points),
        json.dumps({"member": {"lo": 0, "hi": 1}}),
        machine,
    )
    raw, table = derived["bytes"], derived["template_json"]
    bindings = [
        0x10000000 + i * 0x1000000
        for i in range(len(outputs[0]["packages"][0]["params"]))
    ]
    identity = TemplateStore(root, machine).admit(sources[0], raw, table, bindings)
    for source, output, point in zip(sources, outputs, points, strict=True):
        ClassLibrary(root, machine).admit(
            source,
            output,
            {0: {"source": sources[0], "table": table, "params": point}},
            {0: bindings},
        )
    return identity


def measure(root, record, *, library_only=False):
    dev = D.Device(
        "offline", board="vlt_v9t6s_1n", library=str(root), library_only=True
    )
    try:
        sources, expressions = sources_for(dev)
        started = time.perf_counter()
        identity = None
        if not library_only:
            outputs = rs._rs.template_compile(
                sources[0], sources, dev.machine.machine_json
            )
            identity = admit(root, dev.machine.machine_json, sources, outputs)
        seconds = time.perf_counter() - started
        expected, addresses = [], []
        dev.record()
        with (
            patch.object(
                rs, "compile", side_effect=AssertionError("compile on class hit")
            ),
            patch.object(
                rs._rs,
                "compile",
                side_effect=AssertionError("native compile on class hit"),
            ),
            patch.object(
                rs._rs,
                "library_prebuild",
                side_effect=AssertionError("prebuild on class hit"),
            ),
            patch.object(
                rs._rs,
                "template_compile",
                side_effect=AssertionError("template compile on class hit"),
            ),
        ):
            for n, source in zip(SHAPES, sources, strict=True):
                compiled = dev.kernel(source)
                for _ in range(2):
                    x, y = dev.alloc(n * 2), dev.alloc(n * 2)
                    values = ((np.arange(n) % 99) / 32 - 1.5).astype(np.float16)
                    dev.write(x, values.tobytes())
                    dev.write(y, bytes([0xA5]) * (n * 2))
                    dev.run(compiled, {"in0": x, "out": y})
                    dev.read(y, n * 2)
                    expected.append(
                        np.maximum((values.astype(np.float32) + 0.125) * 0.5, 0)
                        .astype(np.float16)
                        .tolist()
                    )
                    addresses.append([x, y])
        limits = [
            max((at + size for at, size in a.live.items()), default=a.lo)
            for a in dev.memory.arenas()
        ]
        log = dev.card.recorded()
        fusion_replay.save(
            record,
            log,
            [((n,), "<f2") for n in SHAPES for _ in range(2)],
            "vlt_v9t6s_1n",
            limits,
        )
        assert dev.counters["class_hits"] == 2 and dev.counters["compile_calls"] == 0
        return dict(
            template_id=identity,
            certified_templates=1,
            admitted_elements=list(SHAPES),
            addresses=addresses,
            class_hits=dev.counters["class_hits"],
            compile_calls=dev.counters["compile_calls"],
            certification_seconds=None if library_only else seconds,
            expressions=expressions,
            expected=expected,
            record=str(record),
            semantics="one fused scheduler AST, shared finite-domain class and native relocation; no shape extrapolation",
        )
    finally:
        dev.close()


def verify(expected, measured):
    """Check saved model values, including FP16 sign bits and output lengths."""
    assert len(expected) == len(measured) == 4
    for wanted, got in zip(expected, measured, strict=True):
        a, b = np.asarray(wanted, dtype=np.float16), np.asarray(got, dtype=np.float16)
        assert np.isfinite(b).all()
        np.testing.assert_array_equal(a.view(np.uint16), b.view(np.uint16))
    return dict(outputs=4, elements=sum(map(len, measured)), fp16_bit_identical=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path)
    parser.add_argument("--library-only", action="store_true")
    parser.add_argument("--record", type=Path)
    parser.add_argument(
        "--check-result", type=Path, help="verify existing model replay; opens no model"
    )
    parser.add_argument("--expected", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.check_result:
        result = verify(
            json.loads(args.expected.read_text(encoding="utf-8"))["expected"],
            json.loads(args.check_result.read_text(encoding="utf-8"))["values"],
        )
    else:
        if args.library is None or args.record is None:
            parser.error("prebuild/hit requires --library and --record")
        result = measure(args.library, args.record, library_only=args.library_only)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "expected"}), flush=True)


if __name__ == "__main__":
    main()
