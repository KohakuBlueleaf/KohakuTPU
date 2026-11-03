"""Two products as one kernel (`KTPU_MM_CHAIN`, `runtime.chain`): an eager
Linear -> ReLU -> Linear runs as one `kernel.chain` launch with the hidden
activation in a scratch, and computes the bytes the two launches compute."""

import numpy as np
import pytest
from tinygrad.tensor import Tensor

from kohakutpu.application import ktpugrad
from kohakutpu.application.kernels import matmul as MM
from kohakutpu.application.runtime import chain, generated
from kohakutpu.application.runtime import device as card

RNG = np.random.default_rng(9)
X = RNG.standard_normal((40, 64)).astype(np.float16)
W1 = (RNG.standard_normal((128, 64)) / 8).astype(np.float16)
B1 = RNG.standard_normal(128).astype(np.float16)
W2 = (RNG.standard_normal((96, 128)) / 8).astype(np.float16)
B2 = RNG.standard_normal(96).astype(np.float16)


@pytest.fixture(autouse=True)
def plain_knobs(monkeypatch):
    for env, _ in MM.KNOBS.values():
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("KTPU_MX7_PAIRING", "vector")
    monkeypatch.setenv("KTPU_MM_ACTIVATION_FP16", "1")


def T(a) -> Tensor:
    return Tensor(a, device="KTPU")


def mlp(kind, library, monkeypatch) -> tuple:
    """``(bytes or None, generators launched)`` of the MLP on a fresh device
    of `kind` (the values on the L2 simulator; offline every pass and
    lowering), realized at once."""
    seen = []
    now = generated.launch_now

    def counted(d, generator, arguments, *a, **k):
        seen.append((generator, arguments.get("keep")))
        return now(d, generator, arguments, *a, **k)

    monkeypatch.setattr(generated, "launch_now", counted)
    ktpugrad.install()
    dev = card.Device(kind=kind, board="multimesh_v9t6", nodes=1, library=library)
    with card.attached(dev):
        x, w1, b1, w2, b2 = map(T, (X, W1, B1, W2, B2))
        y = x.matmul(w1.T).add(b1).relu().matmul(w2.T).add(b2).realize()
        got = y.numpy().tobytes() if kind == "double" else None
    dev.close()
    monkeypatch.setattr(generated, "launch_now", now)
    return got, seen


@pytest.mark.parametrize("kind", ["double", "offline"])
def test_two_products_run_as_one_kernel(kind, tmp_path, monkeypatch):
    want, apart = mlp(kind, str(tmp_path), monkeypatch)
    assert [g for g, _ in apart].count("matmul.mm_kernel") == 2, apart
    monkeypatch.setenv("KTPU_MM_CHAIN", "1")
    got, chained = mlp(kind, str(tmp_path), monkeypatch)
    # One launch for both, the hidden activation read by no later call kept out of DRAM.
    assert ("kernel.chain", False) in chained, chained
    assert [g for g, _ in chained].count("kernel.chain") == 1, chained
    assert "matmul.mm_kernel" not in [g for g, _ in chained], chained
    assert got == want


#: (rows, K, hidden, N): whole K-block pairs, K 784 of single blocks, rows
#: not a whole quantum, a hidden width of three blocks.
SWEEP = [(40, 64, 128, 96), (8, 784, 128, 32), (33, 96, 64, 64), (197, 192, 96, 192)]


@pytest.mark.parametrize("board", ["multimesh_v9t6", "multimesh_v9t8"])
@pytest.mark.parametrize("shape", SWEEP, ids=lambda s: "x".join(map(str, s)))
def test_a_linkable_pair_always_lowers(board, shape, tmp_path, monkeypatch):
    """Every pair `chain.linkable` admits is one `kernel.chain` that lowers
    offline (a refusal raises: there is no second try)."""
    m, k, h, n = shape
    seen = []
    linkable = chain.linkable

    def judged(first, second):
        seen.append(linkable(first, second))
        return seen[-1]

    monkeypatch.setattr(chain, "linkable", judged)
    monkeypatch.setenv("KTPU_MM_CHAIN", "1")
    ktpugrad.install()
    dev = card.Device(kind="offline", board=board, nodes=1, library=str(tmp_path))
    with card.attached(dev):
        x = T(RNG.standard_normal((m, k)).astype(np.float16))
        w1 = T((RNG.standard_normal((h, k)) / 8).astype(np.float16))
        w2 = T((RNG.standard_normal((n, h)) / 8).astype(np.float16))
        x.matmul(w1.T).relu().matmul(w2.T).realize()
        dev.drain()
    dev.close()
    # A K of three blocks on 4+2 quantises its A apart: no pair to judge.
    assert seen == ([] if (h, board) == (96, "multimesh_v9t8") else [True]), seen


def test_a_pair_the_generator_would_refuse_is_not_linked():
    def spec(rows, cols, dtype="f16", layout=()):
        return {"dtype": dtype, "shape": [rows, cols], "layout": list(layout)}

    out = {"out": ("out", 0, spec(48, 128))}
    assert chain.linkable(out, {"a": ("in", 0, spec(40, 128))})
    assert not chain.linkable(out, {"a": ("in", 0, spec(56, 128))})
    assert not chain.linkable(out, {"a": ("in", 0, spec(40, 96))})
    assert not chain.linkable(out, {"a": ("in", 0, spec(40, 128, "mx7a"))})
    assert not chain.linkable(out, {"a": ("in", 0, spec(40, 128, layout=[["t", 1]]))})
    assert not chain.linkable(out, {"a": ("out", 0, spec(40, 128))})


def blocks(library, monkeypatch) -> tuple:
    """``(bytes, chain launches)`` of two pre-norm MLP blocks (LayerNorm, FC1 +
    GELU, FC2 + residual) on a fresh double device: the second block's plans
    are the first's, so recipes recorded on the first replay on the second."""
    seen = []
    now = generated.launch_now

    def counted(d, generator, *a, **k):
        seen.append(generator)
        return now(d, generator, *a, **k)

    monkeypatch.setattr(generated, "launch_now", counted)
    ktpugrad.install()
    dev = card.Device(kind="double", board="multimesh_v9t6", nodes=1, library=library)
    with card.attached(dev):
        x = T(X)
        for k in range(2):
            w1, b1 = T(W1 / (k + 1)), T(B1)
            w2, b2 = T(np.ascontiguousarray(W2[:64]) / (k + 2)), T(B2[:64].copy())
            h = x.layernorm().matmul(w1.T).add(b1).gelu()
            x = h.matmul(w2.T).add(b2).add(x)
        got = x.realize().numpy().tobytes()
    dev.close()
    monkeypatch.setattr(generated, "launch_now", now)
    return got, seen.count("kernel.chain")


def test_a_replayed_block_chains_as_its_first_did(tmp_path, monkeypatch):
    want, apart = blocks(str(tmp_path), monkeypatch)
    assert apart == 0
    monkeypatch.setenv("KTPU_MM_CHAIN", "1")
    got, chained = blocks(str(tmp_path), monkeypatch)
    assert chained == 2
    assert got == want
