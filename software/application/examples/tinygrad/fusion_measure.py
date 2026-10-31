"""Offline scheduler census and shared-library prebuild, one workload per process.

Census never executes arrays. Compile never reports values or device cycles.
Use --phase reference for small plan-semantics probes (not silicon numerics).
"""

import argparse
import faulthandler
import json
import os
import shutil
import sys
import time
from collections import Counter
from contextlib import ExitStack, contextmanager, nullcontext
from pathlib import Path
from unittest.mock import patch

import numpy as np
from examples.tinygrad import fusion_replay
from examples.tinygrad.boundaries import boundary
from examples.tinygrad.mnist_models import LeNet
from examples.tinygrad.vit_tiny import Block, ViTTiny
from tinygrad import Tensor, tensor
from tinygrad.helpers import Context
from tinygrad.nn.state import get_parameters, get_state_dict
from tinygrad.uop import Ops

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels.vector import expr_key
from kohakutpu.application.ktpugrad import fusion, lower, reference, run, seam
from kohakutpu.application.ktpugrad.nn import Adam16
from kohakutpu.application.runtime import device as D
from kohakutpu.stack import rs


def workloads(args):
    """Return a prepared callable; initialization is outside measured schedules."""
    if args.workload == "probe":
        rng = np.random.default_rng(17)
        x = Tensor((rng.standard_normal((4, 32)) / 8).astype(np.float16), device="KTPU")
        w = Tensor(
            (rng.standard_normal((32, 32)) / 8).astype(np.float16), device="KTPU"
        )
        x.realize(w)

        def probe():
            a = boundary(x * 0.5, args.realize_ops)
            b = boundary(a + 0.25, args.realize_ops)
            return (b.relu(), (x.sum(-1) * 0.5 + 1), (x @ w + 0.25).relu())

        return probe
    with Context(DEV="CPU"):
        if args.workload == "lenet":
            model = LeNet(realize_ops=args.realize_ops)
            shape = (args.batch, 784)
        elif args.workload == "block":
            model = Block(args.width, args.realize_ops)
            shape = (args.batch, args.tokens, args.width)
        else:
            model = ViTTiny(args.blocks, args.width, 224, args.realize_ops)
            shape = (args.batch, 3, 224, 224)
    if args.initialized:
        rng = np.random.default_rng(71)
        for name, t in get_state_dict(model).items():
            values = (
                np.ones(t.shape)
                if "norm" in name and name.endswith("weight")
                else rng.standard_normal(t.shape) * 0.02
            )
            t.replace(Tensor(values.astype(np.float16), device="KTPU"))
        x = Tensor(
            rng.standard_normal(shape).astype(np.float16), device="KTPU"
        ).realize()
    else:
        for t in get_state_dict(model).values():
            t.replace(Tensor.empty(*t.shape, device="KTPU", dtype=t.dtype))
        x = Tensor.empty(*shape, device="KTPU")
    if args.workload != "lenet":
        return lambda: (model(x),)
    optimizer = Adam16(get_parameters(model))
    labels = Tensor.empty(args.batch, 10, device="KTPU")

    def train():
        optimizer.zero_grad()
        logits = model(x)
        loss = -(logits.log_softmax() * labels).sum(-1).mean()
        optimizer.scale(loss).backward()
        return (loss, logits, *optimizer.schedule_step())

    return train


def asts(linear):
    return [
        c.src[0]
        for c in linear.toposort()
        if c.op is Ops.CALL and c.src[0].op is Ops.SINK
    ]


def describe(kernels):
    rows, classes = [], set()
    for ast in kernels:
        try:
            p = lower.plan(ast)
        except lower.Refused as error:
            rows.append({"kind": "refused", "reason": str(error), "ast": str(ast)})
            continue
        classes.add((p.kind, expr_key(p.expr) if p.expr is not None else ()))
        rows.append(
            dict(
                kind=p.kind,
                shape=p.out_shape,
                reads=len(p.accesses),
                reduction_sets=len(set(p.reductions.values())),
                epilogue=p.kind == "matmul" and not isinstance(p.expr, X.Load),
                expression_alu=sum(u.op in lower.ALU for u in ast.toposort()),
                output_bytes=p.out_n * p.out_dtype.itemsize,
                generator_fp16_scratch=(
                    "matmul scratch; placement is determined by generated graph, not AST"
                    if p.kind == "matmul"
                    else (
                        "nested reduction materialisation"
                        if len(set(p.reductions.values())) > 1
                        else None
                    )
                ),
            )
        )
    return dict(
        scheduler_kernels=len(kernels),
        kinds=dict(Counter(r["kind"] for r in rows)),
        expression_classes=len(classes),
        expression_class_semantics="kind + expression DAG only; NOT certified template classes",
        kernels=rows,
        fusion_cuts=dict(fusion.DECISIONS),
    )


@contextmanager
def configured(level):
    """Scope scheduler settings without changing a caller's installed seam."""
    installed = seam.installed()
    try:
        with patch.dict(
            os.environ, {seam.FUSE_ENV: level, "KTPU_HORIZONTAL_FUSION": "0"}
        ):
            seam.install()
            try:
                yield
            finally:
                seam.uninstall()
    finally:
        if installed:
            seam.install()


def measure(args):
    with configured(args.level):
        return _measure(args)


def _measure(args):
    kernels = []
    dev = D.Device(
        "sim" if args.phase == "sim" else "offline",
        board=args.board,
        nodes=1,
        library=args.library,
        library_only=args.library_only or args.phase == "sim",
    )

    def record(linear, *unused, **kwargs):
        kernels.extend(asts(linear))

    original_execute = run.execute

    def execute(rt, plan, buffers):
        kernels.append(plan.ast)
        return original_execute(rt, plan, buffers)

    sources = set()
    build_seconds = 0.0
    prebuild_compiler_calls = 0

    def prebuild(source):
        nonlocal build_seconds, prebuild_compiler_calls
        started = time.perf_counter()
        machine = rs.machine_json(dev.machine)
        graph = json.loads(source)
        if args.graphs:
            args.graphs.mkdir(parents=True, exist_ok=True)
            (args.graphs / f"{graph['name']}.json").write_text(source, encoding="utf-8")
        print(f"prebuild start {graph['name']}", file=sys.stderr, flush=True)
        built = rs._rs.library_prebuild(args.library, source, machine)
        print(
            f"prebuild done {graph['name']} {time.perf_counter() - started:.6f}s",
            file=sys.stderr,
            flush=True,
        )
        build_seconds += time.perf_counter() - started
        prebuild_compiler_calls += not built["hit"]
        sources.add(built["key"])
        return rs._rs.library_get(args.library, source, machine)

    recording = (
        patch.object(tensor, "run_linear", record)
        if args.phase == "census"
        else patch.object(run, "execute", execute)
    )
    building = (
        patch.object(dev, "compile_graph", prebuild)
        if args.phase == "compile" and args.library and not args.library_only
        else nullcontext()
    )
    try:
        with (
            D.attached(dev),
            Context(TRAINING=int(args.workload == "lenet")),
            recording,
            building,
            ExitStack() as stack,
        ):
            if args.phase == "sim":
                for owner, name in (
                    (rs, "compile"),
                    (rs._rs, "compile"),
                    (rs._rs, "library_prebuild"),
                ):
                    stack.enter_context(
                        patch.object(
                            owner,
                            name,
                            side_effect=AssertionError("compiler in model run"),
                        )
                    )
            if args.record:
                dev.record()
            operation = workloads(args)
            kernels.clear()
            fusion.DECISIONS.clear()
            before = len(dev.runs)
            started = time.perf_counter()
            outputs = operation()
            outputs[0].realize(*outputs[1:])
            values = (
                [t.numpy() for t in outputs]
                if (args.workload == "probe" or args.initialized)
                and args.phase != "census"
                else []
            )
            dev.drain()
            if args.record:
                fusion_replay.save(
                    args.record,
                    dev.card.recorded(),
                    [(v.shape, v.dtype.str) for v in values],
                    args.board,
                    [
                        max(
                            (at + size for at, size in arena.live.items()),
                            default=arena.lo,
                        )
                        for arena in dev.memory.arenas()
                    ],
                )
            seconds = time.perf_counter() - started
            result = describe(kernels)
            result.update(
                phase=args.phase,
                workload=args.workload,
                level=args.level,
                realize_ops=args.realize_ops,
                batch=args.batch,
                width=args.width,
                tokens=args.tokens,
                initialized=args.initialized,
                local_scratch=os.environ.get("KTPU_LOCAL_SCRATCH", "0"),
                matmul_ring_depth=int(os.environ.get("KTPU_MATMUL_RING_DEPTH", "0")),
                blocks=args.blocks,
                seconds=seconds,
                device_launches=(
                    None if args.phase == "census" else len(dev.runs) - before
                ),
                device_cycles=(
                    sum(r.cycles for r in dev.runs[before:])
                    if args.phase == "sim"
                    else None
                ),
                launch_records=[
                    dict(name=r.name, kind=r.kind) for r in dev.runs[before:]
                ],
                mfu=None,
                numerics="model outputs" if args.phase == "sim" else "not executed",
                values=[v.tolist() for v in values] if args.phase == "sim" else None,
                library_entries=(
                    len(sources)
                    if args.library
                    and not args.library_only
                    and args.phase == "compile"
                    else None
                ),
                prebuild_seconds=build_seconds if args.library else None,
                certified_class_count=None,
                counters=dict(dev.counters),
                prebuild_compiler_calls=prebuild_compiler_calls,
                total_compiler_calls=dev.counters["compile_calls"]
                + prebuild_compiler_calls,
                library_semantics="distinct exact entries encountered by prebuild wrapper (null without prebuild); not certified shape classes",
            )
            return result
    finally:
        dev.close()


def reference_probe(args):
    """Same seeded inputs, plan meaning with/without model realization boundaries."""
    values = []
    for eager in (True, False):
        args.realize_ops = eager
        dev = D.Device("offline", board=args.board, nodes=1)
        try:
            with (
                configured(args.level),
                D.attached(dev),
                patch.object(run, "execute", reference.execute),
                patch.object(tensor, "run_linear", seam._RUN_LINEAR),
            ):
                outputs = workloads(args)()
                values.append([t.numpy() for t in outputs])
        finally:
            dev.close()
    return dict(
        phase="reference",
        level=args.level,
        semantics="numpy plan oracle with fp16 boundary writes; no MXFP7/hardware execution",
        comparisons=[
            dict(
                bit_identical=bool(np.array_equal(a, b)),
                max_absolute=float(np.max(np.abs(a.astype(np.float64) - b))),
                max_relative=float(
                    np.max(np.abs(a.astype(np.float64) - b))
                    / max(float(np.max(np.abs(a))), 1e-9)
                ),
            )
            for a, b in zip(*values)
        ],
    )


def cpu_block(args):
    """Independent CPU execution of the seeded block, not KTPU plan semantics."""
    rng = np.random.default_rng(71)
    with Context(DEV="CPU", TRAINING=0):
        model = Block(args.width, args.realize_ops)
        for name, t in get_state_dict(model).items():
            values = (
                np.ones(t.shape)
                if "norm" in name and name.endswith("weight")
                else rng.standard_normal(t.shape) * 0.02
            )
            t.replace(Tensor(values.astype(np.float16), device="CPU"))
        x = Tensor(
            rng.standard_normal((args.batch, args.tokens, args.width)).astype(
                np.float16
            ),
            device="CPU",
        )
        values = model(x).numpy()
    return dict(
        phase="cpu",
        values=[values.tolist()],
        semantics="tinygrad CPU block, same seeded fp16 weights/input; no MXFP7 quantization",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--phase",
        choices=("census", "compile", "reference", "sim", "cpu"),
        default="census",
    )
    parser.add_argument(
        "--workload", choices=("probe", "block", "vit", "lenet"), default="probe"
    )
    parser.add_argument("--level", choices=seam.FUSE_MODES, default="elementwise")
    parser.add_argument("--realize-ops", action="store_true")
    parser.add_argument("--batch", type=int, default=1)
    parser.add_argument("--width", type=int, default=192)
    parser.add_argument("--tokens", type=int, default=197)
    parser.add_argument(
        "--initialized",
        action="store_true",
        help="seed block weights/inputs and read its actual output",
    )
    parser.add_argument("--blocks", type=int, default=12)
    parser.add_argument("--board", default="multimesh_v9t6")
    parser.add_argument("--library")
    parser.add_argument("--library-only", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--graphs", type=Path, help="export each typed graph before native prebuild"
    )
    parser.add_argument(
        "--record",
        type=Path,
        help="compile probe: export fixed-address package recording",
    )
    parser.add_argument(
        "--trace-after",
        type=float,
        default=0,
        help="dump active Python stack after this many seconds",
    )
    parser.add_argument(
        "--archive",
        type=Path,
        help="compile: zip the prebuilt library after completion",
    )
    args = parser.parse_args()
    if args.phase in ("reference", "sim") and args.workload != "probe":
        parser.error("reference/sim phases are bounded to the small probe")
    if args.phase == "cpu" and args.workload != "block":
        parser.error("cpu phase requires --workload block")
    if args.initialized and args.workload != "block":
        parser.error("--initialized currently measures a block")
    if args.record and (
        args.phase != "compile" or not (args.workload == "probe" or args.initialized)
    ):
        parser.error("--record requires compile probe or initialized block")
    if args.phase == "sim" and not args.library:
        parser.error("sim requires a prebuilt --library")
    if args.archive and (args.phase != "compile" or not args.library):
        parser.error("--archive requires compile and --library")
    if args.trace_after:
        faulthandler.dump_traceback_later(args.trace_after)
    result = (
        cpu_block(args)
        if args.phase == "cpu"
        else reference_probe(args) if args.phase == "reference" else measure(args)
    )
    if args.trace_after:
        faulthandler.cancel_dump_traceback_later()
    text = json.dumps(result, sort_keys=True, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    if args.archive:
        shutil.make_archive(str(args.archive.with_suffix("")), "zip", args.library)
    print(text)


if __name__ == "__main__":
    main()
