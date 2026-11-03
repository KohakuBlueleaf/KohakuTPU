"""A product's tiling on 2+2: the N tile is the legal divisor the plan's wave
cost prices lowest (`KTPU_MM_N_TILE=auto`, `matmul::wave_cost`), and batches
whose own tiles underfill the clusters run their tiles together
(`matmul::batches_tiles`)."""

import json

import numpy as np
import pytest
from tinygrad.tensor import Tensor

from kohakutpu.application.kernels import matmul as MM
from kohakutpu.application.runtime import generated
from kohakutpu.stack import rs

MACHINE = rs.machine_json("multimesh_v9t6")


@pytest.fixture(autouse=True)
def plain_knobs(monkeypatch):
    for env, _ in MM.KNOBS.values():
        monkeypatch.delenv(env, raising=False)


def tn(n: int, k: int, tile: str = "auto") -> int:
    return MM.plan(1, 197, n, k, machine_json=MACHINE, n_tile=tile).tn


@pytest.mark.parametrize(("n", "k"), [(192, 768), (192, 192), (576, 192)])
def test_auto_prices_every_legal_n_tile(n, k):
    """ViT's down projection, residual projection and QKV: 96 wide, two
    tiles over the two clusters in one wave, neither the plain tile nor the
    dense one."""
    assert tn(n, k) == 96
    assert 96 not in (tn(n, k, "plain"), tn(n, k, "dense"))
    # Auto is the default.
    assert MM.plan(1, 197, n, k, machine_json=MACHINE).tn == 96


def test_heads_of_one_tile_each_share_the_clusters(dev, monkeypatch):
    """12 heads of [200, 224] x [224, 64]: one output tile a head, so batch
    by batch leaves a cluster idle; the kernel runs every head's tile at once,
    with what each head computes unchanged."""
    calls = []
    launch = generated.launch

    def spied(dev, gen, arguments, binds, kind="dsl", info=None):
        if gen == "matmul.mm_kernel":
            calls.append(arguments)
        return launch(dev, gen, arguments, binds, kind=kind, info=info)

    monkeypatch.setattr(generated, "launch", spied)
    rng = np.random.default_rng(5)
    a = rng.normal(size=(12, 200, 224)) / 16
    b = rng.normal(size=(12, 224, 64)) / 16
    ta = Tensor(a.astype(np.float16), device="KTPU")
    tb = Tensor(b.astype(np.float16), device="KTPU")
    got = (ta @ tb).numpy().astype(np.float64)
    want = a @ b
    assert np.abs(got - want).max() <= 0.05 * np.abs(want).max()
    (args,) = calls
    knobs = MM.resolved(args, MACHINE)["knobs"]
    assert knobs["batch_tiles"] and not knobs["ring_depth"], (knobs, json.dumps(args))
