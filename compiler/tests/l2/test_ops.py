"""KohakuTPU's hand-written L2 schedules, compiled by the L2 -> L1 compiler and
run on the unit models: every L1 reference kernel's arithmetic, graded against
the same references as compiler/tests/l1."""

import pathlib
import sys

import numpy as np
import pytest
from kohakuaccel.ir.l2 import Schedule
from kohakuaccel.sim import MEM_BASE
from kohakutpu.hw.mxfp7 import value_fp16
from kohakutpu.ir import l2
from kohakutpu.ir.l1.kernels import attention as AT
from kohakutpu.ir.l1.kernels import conv2d as CV
from kohakutpu.ir.l1.kernels import matmul as MM
from kohakutpu.ir.l1.model import L1Model
from kohakutpu.ir.l2.layouts import BandLane, ConvB, Flat, MxA, MxB, Rows, Tiles

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "l1"))
from test_kernels import flash_reference

TOL = 2e-3


def rel(got, want) -> float:
    return float(np.abs(got - want).max() / np.abs(want).max())


class Rig:
    """A model, a schedule over its memory, and the run."""

    def __init__(self) -> None:
        self.mdl = L1Model()
        self.s = Schedule()
        self.mgs = sorted(self.mdl.machine.units["MG"])
        self.vcs = sorted(self.mdl.machine.units["VC"])

    def buf(self, name, layout, data=None, nbytes=None):
        n = nbytes if nbytes is not None else layout.nbytes
        b = self.s.buffer(name, n, layout, base=self.mdl.alloc(n))
        if data is not None:
            raw = data if isinstance(data, (bytes, bytearray)) else layout.pack(data)
            self.mdl.mem.write(b.base - MEM_BASE, bytes(raw))
        return b

    def run(self) -> None:
        for prog in l2.compile(self.s, self.mdl.machine):
            self.mdl.run(prog)


@pytest.mark.parametrize("bias", [False, True])
@pytest.mark.parametrize("late", [True, False])
def test_matmul(bias, late):
    m, k, n, gm, gn, nk = 128, 128, 128, 8, 8, 2
    rng = np.random.default_rng(3)
    x = (rng.standard_normal((m, k)) * 0.5).astype(np.float16)
    w = (rng.standard_normal((n, k)) * 0.5).astype(np.float16)
    bv = (rng.standard_normal(n) * 2).astype(np.float16)
    r = Rig()
    a = r.buf("a", MxA(m, k, gm, nk), x)
    b = r.buf("b", MxB(n, k, gn, nk), w)
    c = r.buf("c", Tiles(m, n, gm, gn))
    extra = {}
    if bias:
        extra = {
            "ones": r.buf("ones", None, MM.ones_block(gm), nbytes=gm * MM.ENTRY),
            "bias": r.buf(
                "bias", None, MM.bias_block(bv, gn), nbytes=n // 4 * MM.ENTRY
            ),
        }
    l2.ops.matmul(r.s, r.mgs, a, b, c, late=late, **extra)
    r.run()
    want = value_fp16(x) @ value_fp16(w).T
    if bias:
        want = (
            want + value_fp16(np.pad(bv[:, None], ((0, 0), (0, MM.KBLOCK - 1))))[:, 0]
        )
    assert rel(c.layout.unpack(r.mdl.get, c.base), want) < TOL


def test_conv2d():
    h, w, cin, cout, gm, gn, cbc = 16, 16, 64, 32, 12, 8, 2
    rng = np.random.default_rng(h * w)
    x = (rng.standard_normal((h, w, cin)) * 0.5).astype(np.float16)
    wt = (rng.standard_normal((cout, cin, 3, 3)) * 0.2).astype(np.float16)
    r = Rig()
    xb = r.buf("x", BandLane(h, w, cin, gm, cbc), x)
    wb = r.buf("w", ConvB(cout, cin, gn, cbc), wt)
    positions = CV.geometry(h, w, gm)[2]
    cb = r.buf("c", Tiles(positions * CV.LANES, cout, gm, gn))
    l2.ops.conv2d(r.s, r.mgs, xb, wb, cb)
    r.run()
    xq = np.zeros((h + 2, w + 2, cin))
    xq[1:-1, 1:-1] = value_fp16(x)
    wk = np.ascontiguousarray(wt.transpose(0, 2, 3, 1)).reshape(cout, 9 * cin)
    wq = value_fp16(wk).reshape(cout, 3, 3, cin)
    want = sum(xq[dy : dy + h, dx : dx + w] @ wq[:, dy, dx, :].T for dy, dx in CV.TAPS)
    where = [
        (i * gm, gm, j, cb.base + cb.layout.tile(i, j)[0])
        for i in range(positions // gm)
        for j in range(cout // (4 * gn))
    ]
    got = CV.unpack(r.mdl.get, where, h, w, cout, gm, gn)
    assert rel(got, want) < TOL


def test_silu_and_binary():
    n = 16384
    rng = np.random.default_rng(1)
    x = (rng.standard_normal(n) * 2).astype(np.float16)
    z = (rng.standard_normal(n) * 2).astype(np.float16)
    r = Rig()
    xb, zb = r.buf("x", Flat(n), x), r.buf("z", Flat(n), z)
    yb, sb = r.buf("y", Flat(n)), r.buf("sum", Flat(n))
    l2.ops.silu(r.s, r.vcs, xb, yb, n)
    l2.ops.binary(r.s, r.vcs, "add", yb, zb, sb, n)
    r.run()
    v = x.astype(np.float64)
    silu = v / (1 + np.exp(-v))
    got = np.frombuffer(r.mdl.get(sb.base, n * 2), np.float16).astype(np.float64)
    assert rel(got, silu + z.astype(np.float64)) < 4e-3


def test_softmax_then_layernorm():
    rows, cols = 32, 128
    rng = np.random.default_rng(cols)
    x = (rng.standard_normal((rows, cols)) * 2).astype(np.float16)
    g = (rng.standard_normal(cols) * 0.5 + 1).astype(np.float16)
    bt = (rng.standard_normal(cols) * 0.5).astype(np.float16)
    r = Rig()
    xb = r.buf("x", Rows(rows * cols, cols), x)
    sm = r.buf("sm", Rows(rows * cols, cols))
    ln = r.buf("ln", Rows(rows * cols, cols))
    gb = r.buf("gb", Flat(2 * cols), np.concatenate([g, bt]))
    l2.ops.softmax(r.s, r.vcs, xb, sm, rows, cols)
    l2.ops.layernorm(r.s, r.vcs, sm, ln, gb, rows, cols)
    r.run()
    v = x.astype(np.float64)
    e = np.exp(v - v.max(1, keepdims=True))
    p = np.asarray(e / e.sum(1, keepdims=True), np.float16).astype(np.float64)
    want = (p - p.mean(1, keepdims=True)) / np.sqrt(p.var(1, keepdims=True) + 1e-5)
    want = want * g.astype(np.float64) + bt.astype(np.float64)
    got = np.frombuffer(r.mdl.get(ln.base, x.nbytes), np.float16).astype(np.float64)
    assert rel(got.reshape(rows, cols), want) < 2e-2


@pytest.mark.parametrize("late", [False, True])
def test_matmul_silu(late):
    m, k, n, gm, gn, nk = 256, 128, 256, 16, 16, 2
    rng = np.random.default_rng(5)
    x = (rng.standard_normal((m, k)) * 0.3).astype(np.float16)
    w = (rng.standard_normal((n, k)) * 0.3).astype(np.float16)
    r = Rig()
    a, b = r.buf("a", MxA(m, k, gm, nk), x), r.buf("b", MxB(n, k, gn, nk), w)
    c = r.buf("c", Tiles(m, n, gm, gn))
    sinks = [r.buf(f"sink{i}", Flat(128 * 16)) for i in range(2)]
    l2.ops.matmul_silu(r.s, r.mgs, r.vcs, a, b, c, sinks, late=late, words=128)
    r.run()
    h = value_fp16(x) @ value_fp16(w).T
    assert rel(c.layout.unpack(r.mdl.get, c.base), h / (1 + np.exp(-h))) < TOL


@pytest.mark.parametrize("gm,blocks", [(8, 2), (4, 3)])
def test_attention(gm, blocks):
    rng = np.random.default_rng(gm * 10 + blocks)
    rows, keys = 4 * gm, 64 * blocks
    q = (rng.standard_normal((rows, 64)) * np.log2(np.e) / 8).astype(np.float16)
    k = rng.standard_normal((keys, 64)).astype(np.float16)
    v = rng.standard_normal((keys, 64)).astype(np.float16)
    r = Rig()
    qb = r.buf("q", MxA(rows, 64, gm, 2), q)
    kraw = b"".join(MM.pack_b(k[j * 64 : (j + 1) * 64], 16, 2) for j in range(blocks))
    vraw = b"".join(
        MM.pack_b(np.ascontiguousarray(v[j * 64 : (j + 1) * 64].T), 16, 2)
        for j in range(blocks)
    )
    kb = r.buf("k", None, kraw, nbytes=len(kraw))
    vb = r.buf("v", None, vraw, nbytes=len(vraw))
    ob = r.buf("o", Tiles(rows, 64, gm, 16))
    scratch = r.buf("scratch", None, nbytes=4 * gm * 16 * 32)
    idx = r.buf("idx", Flat(32), AT.index_words())
    l2.ops.attention(r.s, r.mgs[0], r.vcs[0], qb, kb, vb, ob, scratch, idx, gm, blocks)
    r.run()
    got = MM.unpack(r.mdl.get, [(0, gm, 0, ob.base)], rows, 64, gm, 16)
    assert rel(got, flash_reference(q, k, v, blocks)) < 1e-2
