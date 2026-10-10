"""The hand-written L1 kernels, lowered and run on the unit models.

Graded element by element against the reference computed on what the hardware
computes on (MXFP7-quantised operands for the clusters); a correct result
differs by fp16 / accumulator rounding only.
"""

import numpy as np
import pytest
from kohakutpu.hw.mxfp7 import value_fp16
from kohakutpu.ir.l1 import Program
from kohakutpu.ir.l1.kernels import attention as AT
from kohakutpu.ir.l1.kernels import binary as BI
from kohakutpu.ir.l1.kernels import conv2d as CV
from kohakutpu.ir.l1.kernels import fused as FU
from kohakutpu.ir.l1.kernels import layernorm as LN
from kohakutpu.ir.l1.kernels import matmul as MM
from kohakutpu.ir.l1.kernels import silu as SI
from kohakutpu.ir.l1.kernels import softmax as SM
from kohakutpu.ir.l1.kernels import stream as ST
from kohakutpu.ir.l1.model import L1Model

TOL = 2e-3


def rel(got, want) -> float:
    return float(np.abs(got - want).max() / np.abs(want).max())


@pytest.mark.parametrize(
    "shape,tile,stagger",
    [
        ((128, 128, 128), (8, 8, 2), False),
        ((64, 256, 96), (4, 8, 4), False),
        ((128, 128, 128), (8, 8, 2), True),
        ((384, 64, 128), (12, 8, 2), True),
    ],
)
@pytest.mark.parametrize("bias", [False, True])
def test_matmul_and_linear_bias(shape, tile, stagger, bias):
    """Staggered bands cut row tiles across packed A tiles (two FILL runs)."""
    m, k, n = shape
    gm, gn, nk = tile
    rng = np.random.default_rng(m + n)
    x = (rng.standard_normal((m, k)) * 0.5).astype(np.float16)
    w = (rng.standard_normal((n, k)) * 0.5).astype(np.float16)
    b = (rng.standard_normal(n) * 2).astype(np.float16)
    mdl = L1Model()
    a_at, b_at, c_at = (
        mdl.put(MM.pack_a(x, gm, nk)),
        mdl.put(MM.pack_b(w, gn, nk)),
        mdl.alloc(m * n * 2),
    )
    extra = (
        {
            "ones_at": mdl.put(MM.ones_block(gm)),
            "bias_at": mdl.put(MM.bias_block(b, gn)),
        }
        if bias
        else {}
    )
    prog = Program(mdl.machine)
    where = MM.matmul(
        prog, a_at, b_at, c_at, m, n, k, gm, gn, nk, stagger=stagger, **extra
    )
    mdl.run(prog)
    want = value_fp16(x) @ value_fp16(w).T
    if bias:
        want = want + b.astype(np.float64)
    assert rel(MM.unpack(mdl.get, where, m, n, gm, gn), want) < TOL


def test_the_bias_block_carries_the_bias_past_fp16():
    """Linear against the product plus the EXACT bias: an fp16 store's rel_l2.
    One MXFP7 column (the bias at int7, the ones at 1.00195) measured 3.5e-3."""
    m, k, n, gm, gn, nk = 64, 64, 128, 8, 8, 2
    rng = np.random.default_rng(5)
    x = (rng.standard_normal((m, k)) * 0.5).astype(np.float16)
    w = (rng.standard_normal((n, k)) * 0.5).astype(np.float16)
    b = (rng.standard_normal(n) * 4).astype(np.float16)
    mdl = L1Model()
    a_at, b_at = mdl.put(MM.pack_a(x, gm, nk)), mdl.put(MM.pack_b(w, gn, nk))
    kw = {
        "ones_at": mdl.put(MM.ones_block(gm)),
        "bias_at": mdl.put(MM.bias_block(b, gn)),
    }
    prog = Program(mdl.machine)
    c_at = mdl.alloc(m * n * 2)
    where = MM.matmul(prog, a_at, b_at, c_at, m, n, k, gm, gn, nk, **kw)
    mdl.run(prog)
    got = MM.unpack(mdl.get, where, m, n, gm, gn)
    want = value_fp16(x) @ value_fp16(w).T + b.astype(np.float64)
    assert np.linalg.norm(got - want) / np.linalg.norm(want) < 6e-4


@pytest.mark.parametrize("late", [False, True])
def test_matmul_silu_overlapped_in_one_package(late):
    """Tiles epilogued from memory behind marks; the matmul part is graded on
    the quantised operands, silu in fp64 on that result."""
    m, k, n, gm, gn, nk = 256, 128, 256, 16, 16, 2
    rng = np.random.default_rng(5)
    x = (rng.standard_normal((m, k)) * 0.3).astype(np.float16)
    w = (rng.standard_normal((n, k)) * 0.3).astype(np.float16)
    mdl = L1Model()
    a_at, b_at = mdl.put(MM.pack_a(x, gm, nk)), mdl.put(MM.pack_b(w, gn, nk))
    c_at = mdl.alloc(m * n * 2)
    sinks = [mdl.alloc(128 * 32) for _ in range(2)]
    prog = Program(mdl.machine)
    # 128-word RUNs: two a tile, so RUN 1's input is still in memory when RUN 0
    # drains -- where a stale drain into the tile would land.
    where = FU.matmul_silu(
        prog, a_at, b_at, c_at, m, n, k, gm, gn, nk, late=late, words=128, sinks=sinks
    )
    mdl.run(prog)
    h = value_fp16(x) @ value_fp16(w).T
    assert rel(MM.unpack(mdl.get, where, m, n, gm, gn), h / (1 + np.exp(-h))) < TOL


def flash_reference(q, k, v, blocks: int):
    """The kernel's own arithmetic in numpy: MXFP7 operands, fp16 stores, the
    online softmax in base 2 (q pre-scaled), P quantised before P @ v."""
    f16 = lambda x: np.asarray(x, np.float16).astype(np.float64)
    qq = value_fp16(q)
    m = np.full((q.shape[0], 1), AT.NEG)
    l = np.zeros((q.shape[0], 1))
    o = np.zeros((q.shape[0], AT.D))
    for j in range(blocks):
        kj, vj = k[j * 64 : (j + 1) * 64], v[j * 64 : (j + 1) * 64]
        s = f16(qq @ value_fp16(kj).T)
        mn = np.maximum(m, s.max(1, keepdims=True))
        corr = np.exp2(m - mn)
        p = f16(np.exp2(s - mn))
        l = l * corr + p.sum(1, keepdims=True)
        pv = f16(value_fp16(p) @ value_fp16(np.ascontiguousarray(vj.T)).T)
        o = f16(o * corr + pv)
        m = mn
    return o / l


@pytest.mark.parametrize("gm,blocks", [(8, 2), (4, 1), (8, 3)])
def test_flash_attention_key_blocks(gm, blocks):
    rng = np.random.default_rng(gm * 10 + blocks)
    rows, keys = 4 * gm, 64 * blocks
    q = (rng.standard_normal((rows, 64)) * np.log2(np.e) / 8).astype(np.float16)
    k = rng.standard_normal((keys, 64)).astype(np.float16)
    v = rng.standard_normal((keys, 64)).astype(np.float16)
    mdl = L1Model()
    q_at = mdl.put(MM.pack_a(q, gm, 2))
    k_at = mdl.put(
        b"".join(MM.pack_b(k[j * 64 : (j + 1) * 64], 16, 2) for j in range(blocks))
    )
    v_at = mdl.put(
        b"".join(
            MM.pack_b(np.ascontiguousarray(v[j * 64 : (j + 1) * 64].T), 16, 2)
            for j in range(blocks)
        )
    )
    o_at = mdl.alloc(gm * 16 * 32)
    scratch = mdl.alloc(4 * gm * 16 * 32)
    idx_at = mdl.put(AT.index_words())
    prog = Program(mdl.machine)
    AT.attention(prog, q_at, k_at, v_at, o_at, scratch, idx_at, gm, blocks)
    mdl.run(prog)
    got = MM.unpack(mdl.get, [(0, gm, 0, o_at)], rows, 64, gm, 16)
    assert rel(got, flash_reference(q, k, v, blocks)) < 1e-2


def test_an_in_place_stream_without_a_sink_is_refused():
    mdl = L1Model()
    prog = Program(mdl.machine)
    progs = ST.programs([], lambda slots: [], 128, ((1, 8),))
    with pytest.raises(ValueError, match="sink"):
        ST.send(
            prog, prog.units("VC")[0], progs, ((1, 8),), 128, 0x1000, 0x1000, 2, 4096
        )


@pytest.mark.parametrize(
    "shape,tile,cbc",
    [
        ((16, 16, 64, 32), (12, 8), 2),
        ((8, 12, 32, 16), (5, 4), 1),
        ((8, 8, 128, 16), (6, 4), 2),
    ],
)
def test_conv2d_band_lanes(shape, tile, cbc):
    """`cbc` below Cin/32 splits the channels into chunks inside each tap."""
    h, w, cin, cout = shape
    gm, gn = tile
    rng = np.random.default_rng(h * w)
    x = (rng.standard_normal((h, w, cin)) * 0.5).astype(np.float16)
    wt = (rng.standard_normal((cout, cin, 3, 3)) * 0.2).astype(np.float16)
    mdl = L1Model()
    a_at, b_at = mdl.put(CV.pack_input(x, gm, cbc)), mdl.put(
        CV.pack_weights(wt, gn, cbc)
    )
    c_at = mdl.alloc(CV.geometry(h, w, gm)[2] * CV.LANES * cout * 2)
    prog = Program(mdl.machine)
    where = CV.conv(prog, a_at, b_at, c_at, h, w, cin, cout, gm, gn, cbc)
    mdl.run(prog)
    xq = np.zeros((h + 2, w + 2, cin))
    xq[1:-1, 1:-1] = value_fp16(x)
    wk = np.ascontiguousarray(wt.transpose(0, 2, 3, 1)).reshape(cout, 9 * cin)
    wq = value_fp16(wk).reshape(cout, 3, 3, cin)
    want = sum(xq[dy : dy + h, dx : dx + w] @ wq[:, dy, dx, :].T for dy, dx in CV.TAPS)
    assert rel(CV.unpack(mdl.get, where, h, w, cout, gm, gn), want) < TOL


@pytest.mark.parametrize("n,words", [(8192, 128), (16384, 256)])
def test_silu(n, words):
    x = (np.random.default_rng(n).standard_normal(n) * 2).astype(np.float16)
    mdl = L1Model()
    src, dst = mdl.put(x), mdl.alloc(n * 2)
    prog = Program(mdl.machine)
    SI.silu(prog, src, dst, n, words)
    mdl.run(prog)
    v = x.astype(np.float64)
    got = np.frombuffer(mdl.get(dst, n * 2), np.float16).astype(np.float64)
    assert rel(got, v / (1 + np.exp(-v))) < TOL


@pytest.mark.parametrize("op", ["add", "sub", "mul"])
@pytest.mark.parametrize("n", [4096, 16384])
def test_binary_elementwise(op, n):
    """Two input streams; n 4096 is one RUN a core (the epilogue drains RUN 0)."""
    rng = np.random.default_rng(n)
    a = (rng.standard_normal(n) * 2).astype(np.float16)
    b = (rng.standard_normal(n) * 2).astype(np.float16)
    mdl = L1Model()
    pa, pb, dst = mdl.put(a), mdl.put(b), mdl.alloc(n * 2)
    prog = Program(mdl.machine)
    BI.binary(prog, op, pa, pb, dst, n)
    mdl.run(prog)
    va, vb = a.astype(np.float64), b.astype(np.float64)
    want = {"add": va + vb, "sub": va - vb, "mul": va * vb}[op]
    got = np.frombuffer(mdl.get(dst, n * 2), np.float16).astype(np.float64)
    assert rel(got, want) < TOL


@pytest.mark.parametrize(
    "rows,cols,per_run", [(32, 320, 8), (64, 128, 16), (16, 64, 8)]
)
def test_layernorm_rows_across_chunks(rows, cols, per_run):
    """gamma/beta through the stride-0 walk; 320 = SDXL's narrowest width."""
    rng = np.random.default_rng(cols)
    x = (rng.standard_normal((rows, cols)) * 2 + 0.5).astype(np.float16)
    g = (rng.standard_normal(cols) * 0.5 + 1).astype(np.float16)
    b = (rng.standard_normal(cols) * 0.5).astype(np.float16)
    mdl = L1Model()
    src, gb, dst = mdl.put(x), mdl.put(np.concatenate([g, b])), mdl.alloc(x.nbytes)
    prog = Program(mdl.machine)
    LN.layernorm(prog, src, dst, gb, rows, cols, per_run)
    mdl.run(prog)
    v = x.astype(np.float64)
    mean, var = v.mean(1, keepdims=True), v.var(1, keepdims=True)
    want = (v - mean) / np.sqrt(var + 1e-5) * g.astype(np.float64) + b.astype(
        np.float64
    )
    got = np.frombuffer(mdl.get(dst, x.nbytes), np.float16).astype(np.float64)
    assert rel(got.reshape(rows, cols), want) < 4e-3


def test_a_softmax_whose_pipeline_overflows_imem_is_refused():
    """Two parity variants of a 16-row, 256-wide body are 752 words of 512."""
    mdl = L1Model()
    with pytest.raises(ValueError, match="IMEM"):
        SM.softmax(Program(mdl.machine), 0, 1 << 20, 32, 256, 16)


@pytest.mark.parametrize("rows,cols,per_run", [(64, 96, 8), (32, 256, 8), (16, 16, 8)])
def test_softmax_rows_across_chunks(rows, cols, per_run):
    x = (np.random.default_rng(cols).standard_normal((rows, cols)) * 2).astype(
        np.float16
    )
    mdl = L1Model()
    src, dst = mdl.put(x), mdl.alloc(x.nbytes)
    prog = Program(mdl.machine)
    SM.softmax(prog, src, dst, rows, cols, per_run)
    mdl.run(prog)
    v = x.astype(np.float64)
    e = np.exp(v - v.max(1, keepdims=True))
    got = (
        np.frombuffer(mdl.get(dst, x.nbytes), np.float16)
        .astype(np.float64)
        .reshape(rows, cols)
    )
    assert rel(got, e / e.sum(1, keepdims=True)) < TOL
