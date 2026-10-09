"""The hand-written L1 kernels, lowered and run on the unit models.

Graded element by element against the reference computed on what the hardware
computes on (MXFP7-quantised operands for the clusters); a correct result
differs by fp16 / accumulator rounding only.
"""

import numpy as np
import pytest
from kohakutpu.hw.mxfp7 import value_fp16
from kohakutpu.ir.l1 import Program
from kohakutpu.ir.l1.kernels import conv2d as CV
from kohakutpu.ir.l1.kernels import matmul as MM
from kohakutpu.ir.l1.kernels import silu as SI
from kohakutpu.ir.l1.kernels import softmax as SM
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
        want = want + value_fp16(np.pad(b[:, None], ((0, 0), (0, MM.KBLOCK - 1))))[:, 0]
    assert rel(MM.unpack(mdl.get, where, m, n, gm, gn), want) < TOL


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


def test_a_softmax_whose_pipeline_overflows_imem_is_refused():
    """Two parity variants of a 16-row, 256-wide body are 752 words of 512."""
    mdl = L1Model()
    with pytest.raises(ValueError, match="IMEM"):
        SM.softmax(Program(mdl.machine), 0, 0, 32, 256, 16)


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
