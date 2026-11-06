"""Eager attention chooses the flash motif; an explicit precise request keeps the dense path."""

import numpy as np
from test_forms import close
from test_forms import tensor as tiny_tensor

from kohakutpu.application.api.attention import attention
from kohakutpu.application.api.tensor import tensor
from kohakutpu.application.api.tinygrad_attention import attention as tiny_attention


def reference(q, k, v):
    scores = q.astype(np.float32) @ k.astype(np.float32).swapaxes(-1, -2) / 8.0
    weights = np.exp(scores - scores.max(-1, keepdims=True))
    return (weights / weights.sum(-1, keepdims=True)) @ v.astype(np.float32)


def statistics(label, got, want):
    absolute = np.abs(got.astype(np.float32) - want)
    relative = absolute / np.maximum(np.abs(want), np.finfo(np.float32).eps)
    message = (
        f"{label} vs f32: abs max={absolute.max():.6g} "
        f"mean={absolute.mean():.6g}; rel max={relative.max():.6g} "
        f"mean={relative.mean():.6g}"
    )
    print(message)
    assert np.isfinite(got).all(), message
    close(got, want)


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
