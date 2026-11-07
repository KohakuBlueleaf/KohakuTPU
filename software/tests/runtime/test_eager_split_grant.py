"""Eager row programs use the same grant and cache as contractions."""

from dataclasses import asdict
from importlib.util import find_spec

import numpy as np
from native_mlp_reference import (
    apply_quant_backend,
    materialized_chunks,
    renamed,
    trim_out_tail,
)
from native_schedule import graph_cycles, prices

from kohakutpu.application.api import native
from kohakutpu.application.api.custom import call as eager_call
from kohakutpu.application.api.tensor import tensor
from kohakutpu.application.kernels.native import generated as G
from kohakutpu.application.kernels.native import linear as L
from kohakutpu.application.kernels.template import GraphKernel
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.plan.emit import expr
from kohakutpu.application.runtime import device as card
from kohakutpu.application.runtime import generated
from kohakutpu.application.runtime.call import call
from kohakutpu.language import dsl as kt
from kohakutpu.stack.kernel import dsl_machine


def full_mlp_schedule_cycles(machine):
    """Complete P1 model prices; no device allocation or simulation."""
    reference = materialized_chunks(128, 64, 2048, 4096, 2048, machine)
    typed = G.mlp(128, 2048, 4096, 2048, machine).generated
    prices = {}
    for name, source in (("before", reference), ("after", typed)):
        prices[name] = graph_cycles(source, machine)
    return prices


def test_native_mlp_keeps_rounded_bytes_and_splits(monkeypatch, record_property):
    assert find_spec("kohakutpu.application.kernels.native.composition") is None
    assert GraphKernel.__module__ == "kohakutpu.application.kernels.template"
    rows, chunk_rows, width, hidden, columns = 32, 16, 32, 32, 32
    rng = np.random.default_rng(84)
    arrays = {
        name: (rng.standard_normal(shape) / 4).astype(np.float16)
        for name, shape in (
            ("p_a", (rows, width)),
            ("p_b", (hidden, width)),
            ("p_in1", (hidden,)),
            ("c_b", (columns, hidden)),
            ("c_in1", (columns,)),
        )
    }
    dev = card.Device(kind="double", board="multimesh_v9t8", nodes=4)
    try:

        reference = GraphKernel(
            materialized_chunks(rows, chunk_rows, width, hidden, columns, dev.machine)
        )
        with dev.on_node(0):
            expected, baseline = call(dev, reference, arrays)
        descriptor = G.mlp_chunks_call(
            rows, chunk_rows, width, hidden, columns, dev.machine, "gelu", 2
        )
        kernel = GraphKernel(
            G.graph(descriptor.generator, descriptor.arguments, dev.machine),
            template_call=descriptor,
        )
        with card.attached(dev):
            inputs = [tensor(value) for value in arrays.values()]
            with dev.on_node(0):
                local = eager_call(kernel, *inputs)[0].numpy().tobytes()
                local_run = dev.runs[-1]
                assert local_run.info["active_nodes"] == 1
            with monkeypatch.context() as patch:
                patch.setenv("KTPU_SPLIT_KERNEL_SEQUENCE_OPTION", "rows")
                distributed = eager_call(kernel, *inputs)[0].numpy().tobytes()
                distributed_run = dev.runs[-1]
                assert distributed_run.info["option_id"] == "rows"
                assert distributed_run.info["active_nodes"] == 4
                assert distributed_run.info["nodes"] == tuple(dev.card.nodes)
                patch.setattr(
                    generated.rs._rs,
                    "generate_split",
                    lambda *args: (_ for _ in ()).throw(AssertionError("cache miss")),
                )
                _, cached = generated.invocation(
                    dev, descriptor.generator, descriptor.arguments
                )
                assert cached["active_nodes"] == 4
        assert local == distributed == expected["c_out"].tobytes()
        costs = prices(baseline, local_run, machine=dev.machine)
        record_property("native_mlp_p1_cycles", {"before": costs[0], "after": costs[1]})
        assert costs[1] <= costs[0], costs
    finally:
        dev.close()


def test_native_convolution_records_its_single_and_multi_node_plan():
    rng = np.random.default_rng(19)
    arrays = [
        (rng.standard_normal(shape) / 8).astype(np.float16)
        for shape in ((1, 4, 4, 32), (128, 32), (128,))
    ]
    outputs = []
    for count in (1, 4):
        dev = card.Device(kind="double", board="multimesh_v9t8", nodes=count)
        try:
            with card.attached(dev):
                x, weight, bias = (tensor(value) for value in arrays)
                outputs.append(native.conv2d(x, weight, bias).numpy().tobytes())
                (run,) = [run for run in dev.runs if run.kind == "custom"]
                assert run.info["nodes"] == tuple(dev.card.nodes)
                assert run.info["option_id"]
                assert run.info["active_nodes"] == count
        finally:
            dev.close()
    assert outputs[1] == outputs[0]


def test_native_product_keeps_materialized_bytes_and_splits(
    monkeypatch, record_property
):
    rows, capacity, width, columns = 13, 16, 32, 64
    rng = np.random.default_rng(83)
    arrays = {
        name: (rng.standard_normal(shape) / 8).astype(np.float16)
        for name, shape in (
            ("a", (rows, width)),
            ("b", (columns, width)),
            ("in1", (columns,)),
            ("in2", (rows, columns)),
        )
    }
    dev = card.Device(kind="double", board="multimesh_v9t8", nodes=4)
    try:
        full = Operand("full", pitch=columns, rows=capacity)
        source = G.graph(
            "matmul.mm_kernel",
            {
                "activation_any": True,
                "mm": {
                    "made_by": {
                        "gen": "matmul.plan",
                        "args": {
                            "bt": 1,
                            "m": capacity,
                            "n": columns,
                            "k": width,
                            "pad_m": False,
                            "dense_n": True,
                            "n_tiles": 4,
                        },
                    }
                },
                "e": expr(L.epilogue("gelu", True)),
                "expr_cse": True,
                "extras": [asdict(Operand("col")), asdict(full)],
                "out": asdict(full),
                "ring_depth": 1,
                "quant_ring": 0,
            },
            dev.machine,
        )
        source = trim_out_tail(source, rows, capacity, "out", dev.machine)
        before = []
        for port, pitch in (("a", width), ("in2", columns)):
            private = "capacity_" + port
            source["body"] = renamed(source["body"], {port: private})
            source["scratch"].append(
                {
                    "name": private,
                    "spec": {
                        "shape": [capacity, pitch],
                        "dtype": "f16",
                        "layout": [],
                        "space": "stage",
                    },
                }
            )
            for declaration in source["ports"]:
                if declaration["name"] == port:
                    declaration["spec"]["shape"] = [rows, pitch]

            def boundary(x, out, *, pitch=pitch):
                kt.fill_value(out[rows:capacity, :], 0)
                kt.copy(
                    out,
                    x,
                    walk=((rows * pitch // 16, 32),),
                    dwalk=((rows * pitch // 16, 32),),
                )

            copy = (
                kt.Kernel.build(
                    "logical_input",
                    boundary,
                    {"x": kt.In(rows, pitch), "out": kt.Out(capacity, pitch)},
                    {"pitch": pitch},
                )
                .trace({"x": (rows, pitch)}, machine=dsl_machine(dev.machine))
                .graph
            )
            before.extend(renamed(copy["body"], {"x": port, "out": private}))
        source["body"] = before + source["body"]
        reference = GraphKernel(apply_quant_backend(source, "vector_mx7", dev.machine))
        with dev.on_node(0):
            expected, baseline = call(dev, reference, arrays)
        with card.attached(dev):
            x, weight, bias, residual = (
                tensor(arrays[name]) for name in ("a", "b", "in1", "in2")
            )
            options = {
                "activation": "gelu",
                "residual": residual,
                "backend": "generated",
                "ring_depth": 2,
                "quant_backend": "vector_mx7",
            }
            with dev.on_node(0):
                local = native.linear(x, weight, bias, **options).numpy().tobytes()
                local_run = dev.runs[-1]
                assert local_run.info["active_nodes"] == 1
            with monkeypatch.context() as patch:
                patch.setenv("KTPU_SPLIT_KERNEL_CHAIN_OPTION", "rows")
                distributed = (
                    native.linear(x, weight, bias, **options).numpy().tobytes()
                )
                distributed_run = dev.runs[-1]
                assert distributed_run.info["option_id"] == "rows"
                assert distributed_run.info["active_nodes"] == 4
                assert distributed_run.info["nodes"] == tuple(dev.card.nodes)
        assert local == distributed == expected["out"].tobytes()
        costs = prices(baseline, local_run, machine=dev.machine)
        record_property(
            "native_product_p1_cycles", {"before": costs[0], "after": costs[1]}
        )
        assert costs[1] <= costs[0], costs
    finally:
        dev.close()
