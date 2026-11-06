"""A batch-one encoder stays kernel-parallel through eager and TinyJit execution."""

import json

import numpy as np
from tinygrad.engine.jit import TinyJit
from tinygrad.tensor import Tensor

from kohakutpu.application import ktpugrad
from kohakutpu.application.checks.accuracy import error_stats
from kohakutpu.application.ktpugrad import run as R
from kohakutpu.application.ktpugrad.device import KTPUGraph
from kohakutpu.application.runtime import device as card


def _parameters():
    """One 32-wide attention head and a four-times-wide GELU feed-forward."""
    rng = np.random.default_rng(17)
    weights = {}
    for name, rows, cols in (
        ("qkv", 96, 32),
        ("proj", 32, 32),
        ("fc1", 128, 32),
        ("fc2", 32, 128),
    ):
        weights[f"{name}.weight"] = (rng.standard_normal((rows, cols)) * 0.02).astype(
            np.float32
        )
        weights[f"{name}.bias"] = (rng.standard_normal(rows) * 0.02).astype(np.float32)
    for name in ("norm1", "norm2"):
        weights[f"{name}.weight"] = np.ones(32, np.float32)
        weights[f"{name}.bias"] = np.zeros(32, np.float32)
    return weights


class _Encoder:
    def __init__(self, weights):
        self.weights = {
            name: Tensor(value.astype(np.float16), device="KTPU").realize()
            for name, value in weights.items()
        }

    def linear(self, x, name):
        return x @ self.weights[f"{name}.weight"].T + self.weights[f"{name}.bias"]

    def norm(self, x, name):
        return (
            x.layernorm(eps=1e-6) * self.weights[f"{name}.weight"]
            + self.weights[f"{name}.bias"]
        )

    def __call__(self, x):
        qkv = self.linear(self.norm(x, "norm1"), "qkv").reshape(1, 32, 3, 1, 32)
        q, k, v = (qkv[:, :, i].transpose(1, 2) for i in range(3))
        scores = (q @ k.transpose(-1, -2)) * (32**-0.5)
        attention = (scores.softmax(-1) @ v).transpose(1, 2).reshape(1, 32, 32)
        x = x + self.linear(attention, "proj")
        hidden = self.linear(self.norm(x, "norm2"), "fc1").gelu()
        return (x + self.linear(hidden, "fc2")).realize()


def _reference(value, weights):
    """NumPy f32 encoder, with the same input and pre-placement f32 weights."""

    def linear(x, name):
        return x @ weights[f"{name}.weight"].T + weights[f"{name}.bias"]

    def norm(x, name):
        centered = x - x.mean(axis=-1, keepdims=True)
        normalized = centered / np.sqrt(
            (centered * centered).mean(axis=-1, keepdims=True) + 1e-6
        )
        return normalized * weights[f"{name}.weight"] + weights[f"{name}.bias"]

    x = value.astype(np.float32)
    q, k, v = np.split(linear(norm(x, "norm1"), "qkv"), 3, axis=-1)
    scores = (q @ k.swapaxes(-1, -2)) * (32**-0.5)
    probabilities = np.exp(scores - scores.max(axis=-1, keepdims=True))
    probabilities /= probabilities.sum(axis=-1, keepdims=True)
    x = x + linear(probabilities @ v, "proj")
    hidden = linear(norm(x, "norm2"), "fc1")
    gelu_scale = np.float32(np.sqrt(2 / np.pi))
    hidden = 0.5 * hidden * (1 + np.tanh(gelu_scale * (hidden + 0.044715 * hidden**3)))
    return x + linear(hidden, "fc2")


def _statistics(got, want):
    stats = error_stats(got, want)
    finite = np.isfinite(got) & np.isfinite(want)
    absolute = np.abs(got[finite].astype(np.float64) - want[finite])
    stats.update(
        nonfinite_got=int(np.count_nonzero(~np.isfinite(got))),
        nonfinite_reference=int(np.count_nonzero(~np.isfinite(want))),
        rms=float(np.sqrt(np.mean(absolute**2))) if absolute.size else None,
        absolute_quantiles=(
            np.quantile(absolute, [0.5, 0.9, 0.99, 1.0]).tolist()
            if absolute.size
            else []
        ),
    )
    return json.dumps(stats)


def _selections(runs, nodes):
    assert runs
    for run in runs:
        assert run.info["nodes"] == tuple(nodes)
        assert run.info["option_id"]
    return [
        dict(
            kind=run.kind,
            option=run.info["option_id"],
            active_nodes=run.info["active_nodes"],
        )
        for run in runs
    ]


def test_batch_one_encoder_eager_and_split_tinyjit(monkeypatch, record_property):
    # Whole 32-value rows retain the pack, relayout and attention paths without tails.
    value = np.random.default_rng(71).standard_normal((1, 32, 32)).astype(np.float16)
    weights = _parameters()
    reference = _reference(value, weights)
    graphs, row_regions = [], []
    init, units = KTPUGraph.__init__, R.units

    def captured(self, *args, **kwargs):
        init(self, *args, **kwargs)
        graphs.append(self)

    def composed(items, held):
        result = units(items, held)
        row_regions.extend(
            [items[i][0].name for i in members]
            for members, plan, _ in result
            if len(members) > 1 and plan.name.endswith("_region")
        )
        return result

    monkeypatch.setattr(KTPUGraph, "__init__", captured)
    monkeypatch.setattr(R, "units", composed)
    results, options = {}, {}
    replay = None
    for count in (1, 4):
        dev = card.Device(kind="double", board="multimesh_v9t8", nodes=count)
        published = []
        submit = dev.card.submit_launch

        def publish(*args, submit=submit, published=published):
            pending = submit(*args)
            published.append(pending)
            return pending

        monkeypatch.setattr(dev.card, "submit_launch", publish)
        try:
            with card.attached(dev):
                ktpugrad.install()
                block = _Encoder(weights)
                step = TinyJit(block) if count == 4 else block
                begin = len(dev.runs)
                results[count] = step(Tensor(value, device="KTPU")).numpy().copy()
                options[count] = _selections(dev.runs[begin:], dev.card.nodes)
                # The products and the row kernels have useful work on every node.
                assert {"dsl", "matmul"} <= {
                    item["kind"]
                    for item in options[count]
                    if item["active_nodes"] == count
                }
                if count == 1:
                    continue

                recorded = None
                first_logs = None
                for _ in range(1, 8):
                    got = step(Tensor(value, device="KTPU")).numpy().copy()
                    if not graphs or not all(
                        g.history and g.history[-1] == "replayed" for g in graphs
                    ):
                        recorded = got
                        continue
                    # SplitRunner preserves the captured program, including its rounding.
                    assert recorded is not None
                    assert got.tobytes() == recorded.tobytes()
                    logs = tuple(g.log[1] for g in graphs)
                    if replay is not None:
                        assert first_logs is not None
                        assert all(
                            a is b for a, b in zip(logs, first_logs, strict=True)
                        )
                        assert got.tobytes() == replay.tobytes()
                        break
                    replay, first_logs = got, logs
                else:
                    raise AssertionError([g.history for g in graphs])

                # Row regions inline half-output expressions before their f16 stores.
                assert row_regions
                assert all(g.counts["replayed"] >= 2 for g in graphs)
                assert all(g.log[1].split is not None for g in graphs)
                prepared_kernels = {
                    id(region.compiled)
                    for graph in graphs
                    for kind, region in graph.log[1].split.segments
                    if kind == "run"
                }
                replays = [
                    pending
                    for pending in published
                    if id(pending.compiled) in prepared_kernels
                ]
                assert replays
                for pending in replays:
                    assert pending.order == [
                        *(("publish", node) for node in dev.card.nodes),
                        *(("collect", node) for node in dev.card.nodes),
                    ]
                for graph in graphs:
                    for _, _, info, *_ in graph.log[1].runs:
                        assert info["nodes"] == tuple(dev.card.nodes)
                        assert info["option_id"]
                    events = [
                        event
                        for kind, region in graph.log[1].split.segments
                        if kind == "run"
                        for event in region.events
                    ]
                    assert any(event["slot"] is not None for event in events)
                    assert all(
                        event["slot"] is None or 9 <= event["slot"] < 16
                        for event in events
                    )
                record_property(
                    "capture_history", json.dumps([g.history for g in graphs])
                )
        finally:
            for graph in graphs:
                graph._release_logs()
            dev.close()

    record_property("split_options", json.dumps(options))
    record_property("row_region_members", json.dumps(row_regions))
    if any(item["option"].startswith("k-") for item in options[4]):
        record_property("k_split_error", _statistics(results[4], results[1]))
    else:
        assert results[4].tobytes() == results[1].tobytes()
    assert replay is not None
    record_property("p1_eager_vs_f32", _statistics(results[1], reference))
    record_property("p4_eager_vs_f32", _statistics(results[4], reference))
    record_property("p4_replay_vs_f32", _statistics(replay, reference))
    record_property("p4_replay_vs_eager", _statistics(replay, results[4]))
