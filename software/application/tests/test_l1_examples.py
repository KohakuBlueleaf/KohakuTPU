"""The hand-written L1 examples, and the L2 examples compiled to L1, on a
Verilated card, each against numpy.

    KTPU_CARD1=build/v9g/vlt_card_v9_1n_v2 KTPU_CARD2=build/v9g/vlt_card_v9_2n_v2 pytest ...
    KTPU_BOARD=multimesh_v9t4 KTPU_CARD1=build/v9t4m/vlt_card_v9t4_1n pytest ...

A test whose card model is not named is skipped. One card is booted per
module and node count; each example runs twice, the second timed.
"""

import os

import numpy as np
import pytest

from kohakutpu.application.runtime.card import VerilatorCard
from kohakutpu.application.tools.l1 import run, run_compiled
from kohakutpu.language import examples
from kohakutpu.language.numerics import fp16
from kohakutpu.language.numerics.mxfp7 import value_fp16
from kohakutpu.stack import kernel
from kohakutpu.stack.buffer import Buffer

CARD1 = os.environ.get("KTPU_CARD1")
CARD2 = os.environ.get("KTPU_CARD2")
#: The board the models were generated from (boards/<name>.json).
BOARD = os.environ.get("KTPU_BOARD", "multimesh_v9")


@pytest.fixture(scope="module")
def card1():
    if not CARD1:
        pytest.skip("KTPU_CARD1 names no card model")
    card = VerilatorCard(CARD1, board=BOARD)
    yield card
    card.close()


@pytest.fixture(scope="module")
def card2():
    if not CARD2:
        pytest.skip("KTPU_CARD2 names no two-node card model")
    card = VerilatorCard(CARD2, board=BOARD, nodes=(0, 1))
    yield card
    card.close()


def f16(rng, *shape):
    return rng.standard_normal(shape).astype(np.float16)


# ------------------------------------------------------------------- mover
def test_transpose(card1):
    x = f16(np.random.default_rng(1), 8, 256)
    y = run(card1, "transpose", {"x": x})["out"]["y"]
    want = x.reshape(8, 16, 16).transpose(1, 0, 2).reshape(16, 128)
    assert np.array_equal(y, want)


def test_pad(card1):
    x = f16(np.random.default_rng(2), 256)
    y = run(card1, "pad", {"x": x})["out"]["y"]
    assert np.all(y[:16] == 1.0) and np.array_equal(y[16:], x)


def test_mask(card1):
    x = f16(np.random.default_rng(3), 256)
    y0 = np.full(256, 7.0, np.float16)
    y = run(card1, "mask", {"x": x, "y": y0})["out"]["y"]
    want = y0.copy()
    want[64:192] = x[64:192]
    assert np.array_equal(y, want)


def test_fill(card1):
    out = run(card1, "fill", {})["out"]
    v = 0x12345678
    for name, width in (("y8", 1), ("y16", 2), ("y32", 4)):
        unit = (v & ((1 << (8 * width)) - 1)).to_bytes(width, "little")
        got = out[name].astype(np.uint8).tobytes()
        assert got == unit * (64 // width), (name, got[:8].hex())


def test_generate(card1):
    out = run(card1, "generate", {})["out"]
    assert np.array_equal(out["y"], out["z"])
    assert len(np.unique(out["y"])) > 200


def test_gather(card1):
    rng = np.random.default_rng(4)
    t = f16(rng, 64, 64)
    idx = np.zeros(16, np.uint32)
    idx[:12] = rng.choice(64, 12, replace=True)
    out = run(card1, "gather", {"t": t, "idx": idx, "idxf": idx.astype(np.float32)})[
        "out"
    ]
    assert np.array_equal(out["y"], t[idx[:12]])
    assert np.array_equal(out["z"], t[idx[:12]])


@pytest.mark.parametrize("n", [3, 16])
def test_loop_runs_a_count_from_memory(card1, n):
    x = f16(np.random.default_rng(6), 16, 256)
    cnt = np.array([n, 0, 0, 0], np.uint64)
    out = run(card1, "loop", {"cnt": cnt, "x": x})["out"]
    assert np.array_equal(out["y"][:n], x[:n]) and not out["y"][n:].any()
    assert out["cnt"][1] == n


def test_memops(card1):
    module = examples.load(examples.example("memops"))
    compiled = kernel.compile(module, "memops", card1.machine, level="l1")
    # This package must work by binding, not by the legacy concrete-build fallback.
    # Numeric output alone cannot distinguish a dropped relocation from a rebuild.
    assert compiled.bound is None, compiled.bound
    o = run_compiled(card1, compiled, {})["out"]["out"].astype(np.uint64)
    assert o[0] == 0x1234 and o[1] == 0x1235
    assert o[2] == 4 << 16 | 2048  # the quantiser's {out words, in bits}
    caps = (int(o[6]) >> 48 | int(o[7]) << 16) & ((1 << 64) - 1)
    assert caps >> 48 == 0x4D47  # 'MG'
    assert (int(o[8]) >> 32) & 1 == 0 and (int(o[8]) >> 28) & 0xF == 0


def test_push_and_recv(card1):
    x = f16(np.random.default_rng(7), 1024)
    out = run(card1, "push", {"x": x})["out"]
    assert np.array_equal(out["y"], x)
    assert np.array_equal(out["z"][16:], x)


# ------------------------------------------------------------------ matmul
def mxmm(x, w):
    """x w^T on the MXFP7 values the clusters read, in float64."""
    return value_fp16(x) @ value_fp16(w).T


def rel(got, want) -> float:
    return float(np.abs(got - want).max() / np.abs(want).max())


def operands(seed, m, n, k):
    rng = np.random.default_rng(seed)
    x = rng.standard_normal((m, k)).astype(np.float16)
    w = (rng.standard_normal((n, k)) / np.sqrt(k)).astype(np.float16)
    return x, w


def test_matmul_four_clusters(card1):
    x, w = operands(8, 1024, 1024, 1024)
    res = run(card1, "matmul", {"x": x, "w": w}, reps=1)
    assert rel(res["out"]["y"], mxmm(x, w)) < 1e-2
    mfu = 1024**3 / (1024 * 4 * res["cycles"])
    assert mfu > 0.6, (res["cycles"], mfu)


def test_matmul_long_k(card1):
    x, w = operands(21, 1024, 1024, 2048)
    res = run(card1, "longk", {"x": x, "w": w}, reps=1)
    assert rel(res["out"]["y"], mxmm(x, w)) < 1e-2


@pytest.mark.parametrize("n", [1, 3])
def test_tiles_from_a_count_in_memory(card1, n):
    x, w = operands(9, 1024, 256, 256)
    cnt = np.array([n, 0, 0, 0], np.uint64)
    y = run(card1, "tiles", {"cnt": cnt, "x": x, "w": w}, reps=1)["out"]["y"]
    rows = 256 * n
    assert rel(y[:rows], mxmm(x[:rows], w)) < 1e-2
    assert not y[rows:].any()


def test_split_k_through_the_peer_accumulator(card1):
    x, w = operands(10, 256, 256, 1024)
    y = run(card1, "splitk", {"x": x, "w": w}, reps=1)["out"]["y"]
    assert rel(y, mxmm(x, w)) < 1e-2


def test_a_drain_into_the_node(card1):
    x, w = operands(11, 8, 16, 64)
    z = run(card1, "tonode", {"x": x, "w": w}, reps=1)["out"]["z"]
    want = mxmm(x, w).reshape(2, 4, 4, 4).transpose(0, 2, 1, 3).reshape(8, 16)
    got = z[16:].reshape(8, 16)  # sub-tile t = (t // 4, t % 4), row-major 4 x 4
    assert rel(got, want) < 1e-2


# ------------------------------------------------------------------ vector
def silu(x):
    v = np.asarray(x, np.float64)
    return v / (1 + np.exp(-v))


def test_silu(card1):
    x = f16(np.random.default_rng(12), 65536)
    assert rel(run(card1, "silu", {"x": x})["out"]["y"], silu(x)) < 2e-3


def test_add(card1):
    rng = np.random.default_rng(13)
    x, y = f16(rng, 65536), f16(rng, 65536)
    got = run(card1, "add", {"x": x, "y": y})["out"]["out"]
    assert rel(got, x.astype(np.float64) + y) < 1e-3


def test_softmax(card1):
    x = f16(np.random.default_rng(14), 1024, 128)
    v = x.astype(np.float64)
    e = np.exp(v - v.max(1, keepdims=True))
    got = run(card1, "softmax", {"x": x})["out"]["y"]
    assert rel(got, e / e.sum(1, keepdims=True)) < 2e-3


def test_layernorm(card1):
    rng = np.random.default_rng(15)
    x = f16(rng, 1024, 128)
    g = (rng.standard_normal(128) * 0.5 + 1).astype(np.float16)
    b = (rng.standard_normal(128) * 0.5).astype(np.float16)
    v = x.astype(np.float64)
    want = (v - v.mean(1, keepdims=True)) / np.sqrt(v.var(1, keepdims=True) + 1e-5)
    got = run(card1, "layernorm", {"x": x, "g": g, "b": b})["out"]["y"]
    assert rel(got, want * g + b) < 4e-3


# --------------------------------------------------------------- two nodes
def test_silu_on_two_nodes(card2):
    x = f16(np.random.default_rng(16), 131072)
    res = run(card2, "silu2", {"x": x})
    assert rel(res["out"]["y"], silu(x)) < 2e-3
    assert set(res["nodes"]) == {0, 1}


def test_ring_slot_and_bell(card2):
    x = f16(np.random.default_rng(18), 2048)
    out = run(card2, "ring", {"x": x})["out"]
    assert np.array_equal(out["y"], x) and np.array_equal(out["z"], x)


def test_remote_store(card2):
    run(card2, "remote", {}, reps=1)
    got = np.frombuffer(card2.get(0x90_0018_0000, 16), np.uint64)
    assert list(got) == [0xABCD, 0xABCE]


def test_split_k_across_meshes(card2):
    x, w = operands(19, 256, 256, 1024)
    y = run(card2, "xsplitk", {"x": x, "w": w}, reps=1)["out"]["y"]
    assert rel(y, mxmm(x, w)) < 1e-2


def test_vector_peer_drain_across_meshes(card2):
    x = f16(np.random.default_rng(20), 1024)
    y = run(card2, "xpeer", {"x": x}, reps=1)["out"]["y"]
    assert np.array_equal(y, x)


# ------------------------------------------------------- compiled from L2
def l2(name: str, tmp_path, card) -> str:
    """The L2 example `name` through the compile pipeline (passes, lowering,
    image scheduling) for `card`'s machine, as an L1 file; its path."""
    m = examples.load(examples.example(name, "l2"))
    l1 = kernel.compile(m, name, card.machine).l1
    p = tmp_path / f"{name}.ktpu"
    p.write_text(l1.source, encoding="utf-8")
    return str(p)


def test_l2_silu(card1, tmp_path):
    x = f16(np.random.default_rng(22), 512, 128)
    y = run(card1, l2("silu", tmp_path, card1), {"x": x})["out"]["y"]
    assert rel(y, silu(x)) < 2e-3


def test_l2_add(card1, tmp_path):
    rng = np.random.default_rng(23)
    x, y = f16(rng, 512, 128), f16(rng, 512, 128)
    got = run(card1, l2("add", tmp_path, card1), {"x": x, "y": y})["out"]["out"]
    assert rel(got, x.astype(np.float64) + y) < 1e-3


def test_l2_softmax(card1, tmp_path):
    x = f16(np.random.default_rng(24), 1024, 128)
    v = x.astype(np.float64)
    e = np.exp(v - v.max(1, keepdims=True))
    got = run(card1, l2("softmax", tmp_path, card1), {"x": x})["out"]["y"]
    assert rel(got, e / e.sum(1, keepdims=True)) < 2e-3


def test_l2_layernorm(card1, tmp_path):
    rng = np.random.default_rng(25)
    x = f16(rng, 1024, 128)
    g = (rng.standard_normal(128) * 0.5 + 1).astype(np.float16)
    b = (rng.standard_normal(128) * 0.5).astype(np.float16)
    v = x.astype(np.float64)
    want = (v - v.mean(1, keepdims=True)) / np.sqrt(v.var(1, keepdims=True) + 1e-5)
    got = run(card1, l2("layernorm", tmp_path, card1), {"x": x, "g": g, "b": b})["out"][
        "y"
    ]
    assert rel(got, want * g + b) < 4e-3


def test_l2_compare_and_select(card1, tmp_path):
    x = f16(np.random.default_rng(42), 512, 128)
    y = run(card1, l2("leaky", tmp_path, card1), {"x": x})["out"]["y"]
    v = x.astype(np.float64)
    assert rel(y, np.where(v > 0, v, 0.1 * v)) < 1e-3


@pytest.mark.parametrize("scale", [1.0, 8.0])
def test_l2_sticky_softmax_on_both_paths(card1, tmp_path, scale):
    # x N(0, 1) never reaches 2^8 (the max pass is skipped); x 8 N(0, 1) does.
    x = (np.random.default_rng(43).standard_normal((1024, 128)) * scale).astype(
        np.float16
    )
    got = run(card1, l2("lazysoftmax", tmp_path, card1), {"x": x})["out"]["y"]
    v = x.astype(np.float64)
    p = np.exp(v - v.max(1, keepdims=True))
    assert rel(got, p / p.sum(1, keepdims=True)) < 2e-3


def test_l2_split_k(card1, tmp_path):
    x, w = operands(28, 256, 256, 1024)
    y = run(card1, l2("splitk", tmp_path, card1), {"x": x, "w": w}, reps=1)["out"]["y"]
    assert rel(y, mxmm(x, w)) < 1e-2


def test_l2_matmul_then_silu_through_gt4(card1, tmp_path):
    x, w = operands(30, 256, 256, 512)
    y = run(card1, l2("mmsilu", tmp_path, card1), {"x": x, "w": w}, reps=1)["out"]["y"]
    assert rel(y, silu(mxmm(x, w))) < 1e-2


def test_l2_mlp_quantises_its_activation_on_the_core(card1, tmp_path):
    rng = np.random.default_rng(31)
    x = rng.standard_normal((256, 256)).astype(np.float16)
    w1, w2 = (
        (rng.standard_normal((256, 256)) / 16).astype(np.float16) for _ in range(2)
    )
    y = run(card1, l2("mlp", tmp_path, card1), {"x": x, "w1": w1, "w2": w2}, reps=1)[
        "out"
    ]["y"]
    h = mxmm(x, w1).astype(np.float16)
    a = silu(h).astype(np.float16)
    assert rel(y, mxmm(a, w2)) < 2e-2


def test_l2_tiles_drain_into_the_cores_and_overlap(card1, tmp_path):
    rng = np.random.default_rng(40)
    x = rng.standard_normal((256, 256)).astype(np.float16)
    w = (rng.standard_normal((256, 256)) / 16).astype(np.float16)
    y = run(card1, l2("tilesilu", tmp_path, card1), {"x": x, "w": w}, reps=1)["out"][
        "y"
    ]
    assert rel(y, silu(mxmm(x, w).astype(np.float16))) < 2e-3


def test_l2_softmax_of_scores_never_in_memory(card1, tmp_path):
    rng = np.random.default_rng(44)
    x = rng.standard_normal((256, 128)).astype(np.float16)
    w = (rng.standard_normal((128, 128)) / 4).astype(np.float16)
    y = run(card1, l2("mmsoftmax", tmp_path, card1), {"x": x, "w": w}, reps=1)["out"][
        "y"
    ]
    s = mxmm(x, w).astype(np.float16).astype(np.float64)
    p = np.exp(s - s.max(1, keepdims=True))
    assert rel(y, p / p.sum(1, keepdims=True)) < 4e-3


def test_l2_mlp_with_nothing_in_memory_between_layers(card1, tmp_path):
    rng = np.random.default_rng(41)
    x = rng.standard_normal((64, 64)).astype(np.float16)
    w1 = (rng.standard_normal((128, 64)) / 8).astype(np.float16)
    w2 = (rng.standard_normal((64, 128)) / 11).astype(np.float16)
    ins = {"x": x, "w1": w1, "w2": w2}
    y = run(card1, l2("mlpdirect", tmp_path, card1), ins, reps=1)["out"]["y"]
    a = silu(mxmm(x, w1).astype(np.float16)).astype(np.float16)
    assert rel(y, mxmm(a, w2)) < 2e-2


def test_l2_mover_quantises_and_tiles(card1, tmp_path):
    x = f16(np.random.default_rng(3), 64, 128)
    out = run(card1, l2("convert", tmp_path, card1), {"x": x}, reps=1)["out"]
    # Witnessed against the host packing, byte for byte.
    assert bytes(out["a"]) == Buffer.of(
        "mx7a[64,128] @dram(gm=8,nk=2)", card1.machine_json
    ).pack(x)
    assert bytes(out["b"]) == Buffer.of(
        "mx7b[64,128] @dram(gn=16,nk=4)", card1.machine_json
    ).pack(x)
    # A tile= buffer reads back as the accumulator does (no subnormals).
    assert np.array_equal(out["t"], fp16.values(x.view(np.uint16), consumer="cluster"))


def test_l2_mover_gather_and_transpose(card1, tmp_path):
    rng = np.random.default_rng(29)
    t = f16(rng, 64, 64)
    idx = np.zeros(16, np.uint32)
    idx[:12] = rng.choice(64, 12, replace=True)
    ins = {"t": t, "idx": idx, "idxf": idx.astype(np.float32)}
    out = run(card1, l2("gather", tmp_path, card1), ins)["out"]
    assert np.array_equal(out["y"], t[idx[:12]])
    assert np.array_equal(out["z"], t[idx[:12]])
    x = f16(rng, 8, 256)
    y = run(card1, l2("transpose", tmp_path, card1), {"x": x})["out"]["y"]
    assert np.array_equal(y, x.reshape(8, 16, 16).transpose(1, 0, 2).reshape(16, 128))


def test_l2_shared_fills_on_two_nodes(card2, tmp_path):
    x, w = operands(26, 256, 2048, 512)
    y = run(card2, l2("share2", tmp_path, card2), {"x": x, "w": w}, reps=1)["out"]["y"]
    assert rel(y, mxmm(x, w)) < 1e-2


def test_l2_silu_on_two_nodes(card2, tmp_path):
    x = f16(np.random.default_rng(27), 1024, 128)
    y = run(card2, l2("silu2", tmp_path, card2), {"x": x})["out"]["y"]
    assert rel(y, silu(x)) < 2e-3


def test_scatter(card1):
    rng = np.random.default_rng(5)
    s = f16(rng, 12, 64)
    idx = np.zeros(16, np.uint32)
    idx[:12] = rng.choice(64, 12, replace=False)
    t0 = np.full((64, 64), -3.0, np.float16)
    t = run(card1, "scatter", {"s": s, "idx": idx, "t": t0})["out"]["t"]
    want = t0.copy()
    want[idx[:12]] = s
    assert np.array_equal(t, want)
