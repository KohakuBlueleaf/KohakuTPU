"""Eager attention chooses the flash motif; an explicit precise request keeps the dense path."""

import numpy as np
from native_mlp_reference import (
    fast_projection_attention_graph,
    projection_attention_graph,
    statements,
)
from native_schedule import graph_cycles
from test_forms import tensor as tiny_tensor

from kohakutpu.application.api.attention import attention
from kohakutpu.application.api.tensor import tensor
from kohakutpu.application.api.tinygrad_attention import attention as tiny_attention
from kohakutpu.application.kernels import transpose as T
from kohakutpu.application.kernels.native import layout as Y
from kohakutpu.application.kernels.template import GraphKernel
from kohakutpu.application.runtime import device as card
from kohakutpu.application.runtime import generated
from kohakutpu.application.runtime.call import call


def reference(q, k, v):
    scores = q.astype(np.float32) @ k.astype(np.float32).swapaxes(-1, -2) / 8.0
    weights = np.exp(scores - scores.max(-1, keepdims=True))
    return (weights / weights.sum(-1, keepdims=True)) @ v.astype(np.float32)


def statistics(label, got, want):
    absolute = np.abs(got.astype(np.float32) - want)
    relative = absolute / np.maximum(np.abs(want), np.finfo(np.float32).eps)
    message = (
        f"{label} vs f32: abs max={absolute.max():.6g} "
        f"mean={absolute.mean():.6g} median={np.median(absolute):.6g}; "
        f"rel max={relative.max():.6g} mean={relative.mean():.6g} "
        f"median={np.median(relative):.6g}"
    )
    print(message)
    assert np.isfinite(got).all(), message


def test_eager_attention_takes_the_flash_motif(dev):
    rng = np.random.default_rng(31)
    q = (rng.standard_normal((2, 256, 64)) / 4).astype(np.float16)
    k = (rng.standard_normal((2, 1024, 64)) / 4).astype(np.float16)
    v = rng.standard_normal((2, 1024, 64)).astype(np.float16)
    inputs = tuple(tiny_tensor(x) for x in (q, k, v))
    start = len(dev.runs)
    got = tiny_attention(*inputs).numpy()
    runs = dev.runs[start:]
    flash = [r for r in runs if r.info.get("attention") == "attention.flash"]
    assert len(flash) == 1
    compiled = flash[0].info["compiled"]
    assert "flash_p" in compiled.l1.source
    assert set(compiled.packages) == set(dev.card.nodes)
    statistics("flash", got, reference(q, k, v))


def test_ineligible_eager_attention_reports_the_key_domain(dev):
    rng = np.random.default_rng(32)
    q = (rng.standard_normal((2, 32, 64)) / 4).astype(np.float16)
    k = (rng.standard_normal((2, 100, 64)) / 4).astype(np.float16)
    v = rng.standard_normal((2, 100, 64)).astype(np.float16)
    start = len(dev.runs)
    got = attention(tensor(q), tensor(k), tensor(v.swapaxes(-1, -2))).numpy()
    calls = [r for r in dev.runs[start:] if "attention" in r.info]
    assert len(calls) == 1
    assert calls[0].info["attention"] == "flash requires 1024 keys"
    assert calls[0].info["compiled"].name == "attention_partial"
    statistics("100 keys", got, reference(q, k, v))
    inputs = tuple(tiny_tensor(x) for x in (q, k, v))
    start = len(dev.runs)
    precise = tiny_attention(*inputs, precise_scores=True).numpy()
    assert any(
        r.info.get("attention") == "precise scores requested" for r in dev.runs[start:]
    )
    statistics("precise scores", precise, reference(q, k, v))


def projection_attention_schedule_cycles(dev, *, batch=2, tokens=193, heads=12, dim=64):
    """Uncollected full chunked/head-blocked price helper; no simulator launch."""

    assert len(dev.card.nodes) == 1, "the comparison needs an actual one-node machine"
    cores = dev.machine.count("VC")
    before = projection_attention_graph(
        batch, tokens, heads, dim, cores, dev.machine, query_blocked=True
    )
    typed = Y.attention_kernel(
        batch,
        tokens,
        heads,
        dim,
        cores,
        machine=dev.machine,
        space="stage",
        query_blocked=True,
        fast_attn=False,
    )
    assert isinstance(typed, GraphKernel)
    after = typed.generated
    costs = []
    for source in (before, after):
        costs.append(graph_cycles(source, dev.machine))
    return {"before": costs[0], "after": costs[1]}


def fast_projection_attention_schedule_cycles(
    dev, *, batch=2, heads=2, mover_overlap=True
):
    """Uncollected complete fast schedule prices, including every chunk and mask."""

    assert len(dev.card.nodes) == 1
    tokens, dim = 193, 64
    cores = dev.machine.count("VC")
    before = fast_projection_attention_graph(
        tokens, heads, dim, cores, dev.machine, batch=batch, mover_overlap=mover_overlap
    )
    typed = Y.attention_kernel(
        batch,
        tokens,
        heads,
        dim,
        cores,
        machine=dev.machine,
        space="stage",
        fast_attn=True,
        mover_overlap=mover_overlap,
    )
    assert isinstance(typed, GraphKernel)
    after = typed.generated
    costs = []
    for source in (before, after):
        costs.append(graph_cycles(source, dev.machine))
    assert costs[1] <= costs[0], costs
    return {"before": costs[0], "after": costs[1]}


def test_native_fast_projection_attention_keeps_materialized_stage_bytes(
    monkeypatch, record_property
):
    batch, tokens, heads, dim = 2, 193, 2, 64
    rng = np.random.default_rng(33)
    x = (rng.standard_normal((batch * tokens, 3 * heads * dim)) / 4).astype(np.float16)
    arrays = {
        "x": x,
        "mask": T.masks(),
        "tail": (np.arange(16) < tokens % 16).astype(np.float16),
    }
    dev = card.Device(kind="double", board="multimesh_v9t8", nodes=4)
    try:
        cores = dev.machine.count("VC")
        source = fast_projection_attention_graph(
            tokens, heads, dim, cores, dev.machine, batch=batch
        )
        kernel = Y.attention_kernel(
            batch,
            tokens,
            heads,
            dim,
            cores,
            machine=dev.machine,
            space="stage",
            fast_attn=True,
            mover_overlap=True,
            quant_backend="mover_quant",
        )
        assert isinstance(kernel, GraphKernel)
        descriptor = kernel.template_call
        assert descriptor is not None
        with dev.on_node(0):
            expected, _ = call(dev, GraphKernel(source), arrays)
            local, local_run = call(dev, kernel, arrays)
        monkeypatch.setenv("KTPU_SPLIT_FUSED_KERNEL_OPTION", "groups")
        actual, run = call(dev, kernel, arrays)
        assert (
            actual["out"].tobytes()
            == local["out"].tobytes()
            == expected["out"].tobytes()
        )
        assert tuple(run.info["nodes"]) == tuple(dev.card.nodes)
        assert run.info["option_id"] == "groups"
        assert run.info["active_nodes"] == 4
        record_property(
            "native_fast_attention_selected_owners", run.info["active_nodes"]
        )
        mask_walk = {"walk": [[1, 0], [1, 0], [8, 32], [4, 0]]}
        for emitted, owners in ((local_run, 1), (run, 4)):
            initializations = [
                stmt
                for stmt in statements(emitted.info["compiled"].traced.graph["body"])
                if stmt.get("kind") == "mover"
                and stmt.get("op") == "gt4"
                and dict(stmt.get("kw", ())).get("walk") == mask_walk
            ]
            assert len(initializations) == owners
        with monkeypatch.context() as patch:
            patch.setattr(
                generated.rs._rs,
                "generate_split",
                lambda *args: (_ for _ in ()).throw(AssertionError("cache miss")),
            )
            _, cached = generated.invocation(
                dev, descriptor.generator, descriptor.arguments
            )
            assert cached["active_nodes"] == run.info["active_nodes"]
    finally:
        dev.close()
