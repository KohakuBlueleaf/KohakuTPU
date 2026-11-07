"""Projected Q/K/V compositions retain FP16 boundaries, layouts and grants."""

import numpy as np
from native_mlp_reference import norm_linear_graph, qkv_graph, wide_norm_graph
from native_schedule import graph_cycles, prices

from kohakutpu.application.api import native
from kohakutpu.application.api.tensor import tensor
from kohakutpu.application.kernels import transpose as T
from kohakutpu.application.kernels.native import generated as G
from kohakutpu.application.kernels.template import GraphKernel
from kohakutpu.application.runtime import device as card
from kohakutpu.application.runtime import generated
from kohakutpu.application.runtime.call import call


def component_prices(dev, name, before, after, record_property):
    costs = [graph_cycles(graph, dev.machine) for graph in (before, after)]
    record_property(name, {"before": costs[0], "after": costs[1]})
    assert costs[1] <= costs[0], (name, costs)


def norm_component_schedule_cycles(dev, record_property, *, width=384, tokens=17):
    """Complete component prices without additional simulator launches."""
    eps = 1e-5
    component_prices(
        dev,
        "native_norm_p1_cycles",
        wide_norm_graph(32, width, dev.machine, eps),
        G.rows_kernel("layernorm", 32, width, dev.machine, eps).generated,
        record_property,
    )
    component_prices(
        dev,
        "native_norm_linear_p1_cycles",
        norm_linear_graph(tokens, width, 3 * width, dev.machine, eps),
        G.norm_linear(tokens, width, 3 * width, dev.machine, eps).generated,
        record_property,
    )


def projection_case(monkeypatch, record_property, eps):
    batch, tokens = 1, 1
    heads, dim = (4, 16) if eps is None else (6, 48)
    width = heads * dim
    rng = np.random.default_rng(85)
    x = (rng.standard_normal((batch, tokens, width)) / 4).astype(np.float16)
    weight = (rng.standard_normal((3 * width, width)) / 8).astype(np.float16)
    bias = (rng.standard_normal(3 * width) / 4).astype(np.float16)
    arrays = {"p_a": x.reshape(-1, width), "p_b": weight, "p_in1": bias}
    if eps is not None:
        scale = rng.standard_normal(width).astype(np.float16)
        shift = rng.standard_normal(width).astype(np.float16)
        arrays = {
            "p_p_in0": x.reshape(-1, width),
            "p_p_in1": scale,
            "p_p_in2": shift,
            "p_c_b": weight,
            "p_c_in1": bias,
        }
    arrays["c_mask"] = T.masks()
    dev = card.Device(kind="double", board="multimesh_v9t8", nodes=4)
    try:
        reference = qkv_graph(
            batch, tokens, width, dev.machine, eps=eps, heads=heads, dim=dim
        )
        with dev.on_node(0):
            expected, baseline = call(dev, GraphKernel(reference), arrays)
        with card.attached(dev):
            inputs = [tensor(value) for value in arrays.values()]
            inputs[0] = inputs[0].reshape(batch, tokens, width)
            operation = native.qkv_projection if eps is None else native.norm_qkv
            options = {"heads": heads, "dim": dim}
            if eps is not None:
                options["eps"] = eps
            emitted = []
            emit = G.graph
            with monkeypatch.context() as patch, dev.on_node(0):
                patch.setattr(
                    G, "graph", lambda *args: emitted.append(args) or emit(*args)
                )
                local = [
                    value.numpy().tobytes() for value in operation(*inputs, **options)
                ]
                local_run = dev.runs[-1]
                assert local_run.info["active_nodes"] == 1
            (outer_call,) = emitted
            generator, arguments, _ = outer_call
            monkeypatch.setenv("KTPU_SPLIT_KERNEL_CHAIN_OPTION", "rows")
            distributed = [
                value.numpy().tobytes() for value in operation(*inputs, **options)
            ]
            distributed_run = dev.runs[-1]
            assert distributed_run.info["option_id"] == "rows"
            assert distributed_run.info["active_nodes"] == 4
            assert distributed_run.info["nodes"] == tuple(dev.card.nodes)
            with monkeypatch.context() as patch:
                patch.setattr(
                    generated.rs._rs,
                    "generate_split",
                    lambda *args: (_ for _ in ()).throw(AssertionError("cache miss")),
                )
                _, cached = generated.invocation(dev, generator, arguments)
                assert cached["active_nodes"] == 4
        assert (
            local
            == distributed
            == [expected[name].tobytes() for name in ("c_q", "c_k", "c_vt")]
        )
        before, after = prices(baseline, local_run, machine=dev.machine)
        label = "native_qkv_p1_cycles" if eps is None else "native_norm_qkv_p1_cycles"
        record_property(label, {"before": before, "after": after})
        assert after <= before, (before, after)
    finally:
        dev.close()


def test_generated_norm_qkv_keeps_materialized_bytes_and_splits(
    monkeypatch, record_property
):
    projection_case(monkeypatch, record_property, 1e-5)
