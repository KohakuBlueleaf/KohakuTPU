"""Bounded observation of scheduled KTPU kernels, including TinyJit graph calls."""

import re
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import numpy as np
from tinygrad.dtype import dtypes
from tinygrad.uop import Ops

from kohakutpu.application.checks.kernels import (
    CPUKernel,
    compare_output,
    f16_storage,
    op_summary,
    policy,
    tolerance_class,
)
from kohakutpu.application.checks.path import ArithmeticPath
from kohakutpu.application.ktpugrad import device as KD
from kohakutpu.application.ktpugrad import fold, lower, run
from kohakutpu.application.runtime import forms as FM


class StepChecker:
    """One step only. A filtered-out kernel still runs normally.

    TinyJit still replays its captured graph and input substitutions. The check
    replays that graph's cached runtimes individually, rather than the opaque
    packet log: per-kernel readbacks cannot be inserted into a packet batch.
    All patches are scoped to this diagnostic context, never normal training.
    """

    def __init__(self, device, kernel_filter=None, corrupt=None, failure_dir=None):
        self.device = device
        self.failure_dir = Path(failure_dir) if failure_dir is not None else None
        self.filter = re.compile(kernel_filter) if kernel_filter else None
        self.corrupt_auto = corrupt == "auto"
        self.corrupt = (
            re.compile(corrupt) if corrupt and not self.corrupt_auto else None
        )
        self.active = False
        self.rows = []
        self.seen = 0
        self.graph_replays = 0
        self.corrupted = None
        self.asts = {}
        self.stack = ExitStack()

    def __enter__(self):
        original_plan, original_execute = lower.plan, run.execute
        original_graph = KD.KTPUGraph.__call__

        def plan(ast):
            p = original_plan(ast)
            self.asts[id(p)] = ast
            # Cached tinygrad programs can outlive this diagnostic context.
            p._check_original_ast = ast
            return p

        def execute(device, p, buffers):
            if not self.active or device is not self.device:
                return original_execute(device, p, buffers)
            index = self.seen
            self.seen += 1
            if self.filter and not self.filter.search(f"{index}:{p.kind}:{p.name}"):
                return original_execute(device, p, buffers)
            ast = self.asts.get(id(p), getattr(p, "_check_original_ast", None))
            if ast is None:
                raise RuntimeError(f"original schedule AST missing for {p.name}")
            category = tolerance_class(ast, p.kind)
            cpu = CPUKernel(ast)
            if device.kind == "offline":
                start = len(device.runs)
                with ArithmeticPath(device) as trace:
                    launches = original_execute(device, p, buffers)
                physical = [
                    dict(name=r.name, kind=r.kind, cycles=r.cycles)
                    for r in device.runs[start:]
                ]
                path = trace.report(physical)
                self.rows.append(
                    dict(
                        index=index,
                        name=p.name,
                        kind=p.kind,
                        ops=op_summary(ast),
                        elements=p.out_n,
                        tolerance=policy(category, path),
                        diagnosis=path,
                        passed=None,
                        status="shape_only_not_executed",
                        launches=[
                            dict(name=r.name, kind=r.kind, cycles=r.cycles)
                            for r in device.runs[start:]
                        ],
                    )
                )
                print(
                    f"{index:4d} {p.kind:8s} {p.name:30.30s} {p.out_n:8d} "
                    "           -            - SHAPE ONLY",
                    flush=True,
                )
                return launches
            arrays = {}
            nodes = list(ast.toposort())
            loaded_slots = {
                source.src[0].arg.slot
                for node in nodes
                if node.op is not Ops.STORE
                for source in node.src
                if source.op is Ops.INDEX and source.src[0].op is Ops.PARAM
            }
            device.drain()
            for slot, param in cpu.params.items():
                if not dtypes.is_float(param.dtype):
                    raise ValueError(f"{p.name}: nonfloat data buffer {slot}")
                # The CPU AST uses f32, but snapshots use each original byte type.
                orig = next(
                    u
                    for u in ast.toposort()
                    if u.op is Ops.PARAM and u.arg.slot == slot
                )
                dtype = np.dtype(fold.np_dtype(orig.dtype))
                view = buffers[slot]
                n = view.nbytes // dtype.itemsize
                # Outputs not read by this AST have no input value.
                if slot in cpu.outputs and slot not in loaded_slots:
                    arrays[slot] = np.zeros(n, np.float32)
                else:
                    raw = FM.read(device, view, dtype, n)
                    arrays[slot] = f16_storage(np.frombuffer(raw, dtype)).astype(
                        np.float32
                    )
            quant_slots = [
                a.slot for a in (p.a, p.b) if a is not None and a.slot is not None
            ]
            start = len(device.runs)
            with ArithmeticPath(device) as trace:
                launches = original_execute(device, p, buffers)
            device.drain()
            physical = [
                dict(name=r.name, kind=r.kind, cycles=r.cycles)
                for r in device.runs[start:]
            ]
            path = trace.report(physical)
            wants, bounds = cpu.reference(arrays, category, quant_slots, path=path)
            for slot in cpu.outputs:
                if len(wants[slot]) != p.out_n:
                    raise ValueError(
                        f"{p.name}: non-dense/multi-output AST needs explicit output coverage"
                    )
                view = buffers[slot]
                dt = np.dtype(fold.np_dtype(p.out_dtype))
                got = np.frombuffer(FM.read(device, view, dt, p.out_n), dt).copy()
                want = f16_storage(wants[slot])
                natural = compare_output(got, want, bounds[slot], category)
                natural_got = got.copy()
                target = (
                    self.corrupt_auto and category == "exact" and natural["passed"]
                ) or (
                    self.corrupt is not None
                    and self.corrupt.search(f"{index}:{p.kind}:{p.name}")
                )
                if target and self.corrupted is None:
                    # Require an independently passing element, even if this kernel
                    # has other natural failures. Never corrupt the oracle/device.
                    for element in range(len(got)):
                        before = compare_output(
                            got[element : element + 1],
                            want[element : element + 1],
                            bounds[slot][element : element + 1],
                            category,
                        )
                        if before["passed"]:
                            got[element] = np.nan
                            after = compare_output(
                                got[element : element + 1],
                                want[element : element + 1],
                                bounds[slot][element : element + 1],
                                category,
                            )
                            self.corrupted = dict(
                                index=index,
                                name=p.name,
                                output_slot=slot,
                                element=element,
                                natural_element_passed=True,
                                corrupted_element_passed=after["passed"],
                                detected=not after["passed"],
                                natural_kernel_passed=natural["passed"],
                            )
                            break
                row = dict(
                    index=index,
                    name=p.name,
                    kind=p.kind,
                    output_slot=slot,
                    ops=op_summary(ast),
                    tolerance=policy(category, path),
                    diagnosis=path,
                    launches=physical,
                    natural_comparison=natural,
                    corruption=(
                        self.corrupted
                        if self.corrupted is not None
                        and self.corrupted["index"] == index
                        and self.corrupted["output_slot"] == slot
                        else None
                    ),
                    **compare_output(got, want, bounds[slot], category),
                )
                if not row["passed"] and self.failure_dir is not None:
                    self.failure_dir.mkdir(parents=True, exist_ok=True)
                    target = self.failure_dir / f"kernel-{index:03d}-out-{slot}.npz"
                    np.savez_compressed(
                        target,
                        got=got,
                        uncorrupted_got=natural_got,
                        want=want,
                        bound=bounds[slot],
                        **{f"input_{key}": value for key, value in arrays.items()},
                    )
                    row["failure_arrays"] = str(target)
                    row["schedule_ast"] = repr(ast)
                self.rows.append(row)
                print(
                    f"{index:4d} {p.kind:8s} {p.name:30.30s} {row['elements']:8d} "
                    f"{row['max_absolute_error']!s:>12s} "
                    f"{row['max_relative_error']!s:>12s} "
                    f"{'PASS' if row['passed'] else 'FAIL'}",
                    flush=True,
                )
            return launches

        def graph(graph, inputs, var_vals, wait=False):
            if not self.active or graph.dev.rt is not self.device:
                return original_graph(graph, inputs, var_vals, wait)
            self.graph_replays += 1
            # A diagnostic changes forms/read events: no stale packet log survives.
            graph._release_logs()
            graph.prev = graph.log = None
            # Captured TinyJit graph, same runtimes/buffers and input replacement.
            graph._plan(inputs, slots=False)
            if wait:
                self.device.drain()
            return None

        self.stack.enter_context(patch.object(lower, "plan", plan))
        self.stack.enter_context(patch.object(run, "execute", execute))
        self.stack.enter_context(patch.object(KD.KTPUGraph, "__call__", graph))
        return self

    def __exit__(self, *exc):
        return self.stack.__exit__(*exc)

    def report(self):
        return dict(
            seen_kernels=self.seen,
            checked_outputs=len(self.rows),
            checked_kernels=len({r["index"] for r in self.rows}),
            filtered=self.filter is not None,
            full_step_coverage=bool(self.rows)
            and len({r["index"] for r in self.rows}) == self.seen,
            boundary="one original tinygrad scheduled AST per kernel; constituent runtime packages are listed under launches",
            graph_replays=self.graph_replays,
            replay_mode="TinyJit captured runtimes; packet-log batching split for readbacks",
            corrupted=self.corrupted,
            natural_failures=sum(
                not r["natural_comparison"]["passed"]
                for r in self.rows
                if "natural_comparison" in r
            ),
            injected_failures=int(
                self.corrupted is not None and self.corrupted["detected"]
            ),
            rows=self.rows,
            passed=(
                (bool(self.rows) and all(r["passed"] for r in self.rows))
                if self.device.kind != "offline"
                else None
            ),
        )
