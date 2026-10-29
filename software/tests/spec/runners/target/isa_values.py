"""Seeded ISA target-example inputs and host numerical expectations."""

import numpy as np

from kohakutpu.language.numerics import fp16
from kohakutpu.language.numerics.mxfp7 import value_fp16
from kohakutpu.stack.buffer import Buffer


def _f16(seed, *shape, scale=1.0):
    return (np.random.default_rng(seed).standard_normal(shape) * scale).astype(
        np.float16
    )


def _rel(got, want):
    got, want = np.asarray(got, np.float64), np.asarray(want, np.float64)
    return float(np.abs(got - want).max() / np.abs(want).max())


def _mxmm(x, w):
    return value_fp16(x) @ value_fp16(w).T


def _silu(x):
    v = np.asarray(x, np.float64)
    return v / (1 + np.exp(-v))


def _operands(seed, m, n, k):
    rng = np.random.default_rng(seed)
    return rng.standard_normal((m, k)).astype(np.float16), (
        rng.standard_normal((n, k)) / np.sqrt(k)
    ).astype(np.float16)


def _softmax(v):
    e = np.exp(v - v.max(1, keepdims=True))
    return e / e.sum(1, keepdims=True)


def _ex_add():
    rng = np.random.default_rng(13)
    x, y = rng.standard_normal(65536).astype(np.float16), rng.standard_normal(
        65536
    ).astype(np.float16)
    return {"x": x, "y": y}, lambda o: _rel(o["out"], x.astype(np.float64) + y) < 1e-3


def _ex_push():
    x = _f16(7, 1024)
    return {"x": x}, lambda o: bool(
        np.array_equal(o["y"], x) and np.array_equal(o["z"][16:], x)
    )


def _ex_transpose():
    x = _f16(1, 8, 256)
    want = x.reshape(8, 16, 16).transpose(1, 0, 2).reshape(16, 128)
    return {"x": x}, lambda o: bool(np.array_equal(o["y"], want))


def _ex_pad():
    x = _f16(2, 256)
    return {"x": x}, lambda o: bool(
        np.all(o["y"][:16] == 1.0) and np.array_equal(o["y"][16:], x)
    )


def _ex_mask():
    x = _f16(3, 256)
    y0 = np.full(256, 7.0, np.float16)
    want = y0.copy()
    want[64:192] = x[64:192]
    return {"x": x, "y": y0}, lambda o: bool(np.array_equal(o["y"], want))


def _ex_fill():
    def ok(o):
        v = 0x12345678
        for name, width in (("y8", 1), ("y16", 2), ("y32", 4)):
            unit = (v & ((1 << (8 * width)) - 1)).to_bytes(width, "little")
            if o[name + ".raw"][:64] != unit * (64 // width):
                return False
        return True

    return {}, ok


def _ex_generate():
    return {}, lambda o: bool(
        np.array_equal(o["y"], o["z"]) and len(np.unique(o["y"])) > 200
    )


def _ex_gather():
    rng = np.random.default_rng(4)
    t = rng.standard_normal((64, 64)).astype(np.float16)
    idx = np.zeros(16, np.uint32)
    idx[:12] = rng.choice(64, 12, replace=True)
    return {"t": t, "idx": idx, "idxf": idx.astype(np.float32)}, lambda o: bool(
        np.array_equal(o["y"], t[idx[:12]]) and np.array_equal(o["z"], t[idx[:12]])
    )


def _ex_scatter():
    rng = np.random.default_rng(5)
    s = rng.standard_normal((12, 64)).astype(np.float16)
    idx = np.zeros(16, np.uint32)
    idx[:12] = rng.choice(64, 12, replace=False)
    t0 = np.full((64, 64), -3.0, np.float16)
    want = t0.copy()
    want[idx[:12]] = s
    return {"s": s, "idx": idx, "t": t0}, lambda o: bool(np.array_equal(o["t"], want))


def _ex_memops():
    def ok(o):
        v = [int(i) for i in o["out"].astype(np.uint64)]
        return (
            v[0] == 0x1234
            and v[1] == 0x1235
            and v[2] == 4 << 16 | 2048
            and (v[8] >> 32) & 1 == 0
            and (v[8] >> 28) & 0xF == 0
        )

    return {}, ok


def _ex_matmul(seed, m, n, k):
    def make():
        x, w = _operands(seed, m, n, k)
        return {"x": x, "w": w}, lambda o: _rel(o["y"], _mxmm(x, w)) < 1e-2

    return make


def _ex_tiles():
    x, w = _operands(9, 1024, 256, 256)
    cnt = np.array([3, 0, 0, 0], np.uint64)
    return {"cnt": cnt, "x": x, "w": w}, lambda o: bool(
        _rel(o["y"][:768], _mxmm(x[:768], w)) < 1e-2 and not o["y"][768:].any()
    )


def _ex_tonode():
    x, w = _operands(11, 8, 16, 64)
    want = _mxmm(x, w).reshape(2, 4, 4, 4).transpose(0, 2, 1, 3).reshape(8, 16)
    return {"x": x, "w": w}, lambda o: _rel(o["z"][16:].reshape(8, 16), want) < 1e-2


def _ex_softmax():
    x = _f16(14, 1024, 128)
    return {"x": x}, lambda o: _rel(o["y"], _softmax(x.astype(np.float64))) < 2e-3


def _ex_layernorm():
    rng = np.random.default_rng(15)
    x = rng.standard_normal((1024, 128)).astype(np.float16)
    g = (rng.standard_normal(128) * 0.5 + 1).astype(np.float16)
    b = (rng.standard_normal(128) * 0.5).astype(np.float16)
    v = x.astype(np.float64)
    want = (v - v.mean(1, keepdims=True)) / np.sqrt(v.var(1, keepdims=True) + 1e-5)
    return {"x": x, "g": g, "b": b}, lambda o: _rel(o["y"], want * g + b) < 4e-3


def _ex_leaky():
    x = _f16(42, 512, 128)
    v = x.astype(np.float64)
    return {"x": x}, lambda o: _rel(o["y"], np.where(v > 0, v, 0.1 * v)) < 1e-3


def _ex_lazysoftmax(scale):
    def make():
        x = _f16(43, 1024, 128, scale=scale)
        return {"x": x}, lambda o: _rel(o["y"], _softmax(x.astype(np.float64))) < 2e-3

    return make


def _ex_mlp():
    rng = np.random.default_rng(31)
    x = rng.standard_normal((256, 256)).astype(np.float16)
    w1, w2 = (
        (rng.standard_normal((256, 256)) / 16).astype(np.float16) for _ in range(2)
    )
    a = _silu(_mxmm(x, w1).astype(np.float16)).astype(np.float16)
    return {"x": x, "w1": w1, "w2": w2}, lambda o: _rel(o["y"], _mxmm(a, w2)) < 2e-2


def _ex_tilesilu():
    rng = np.random.default_rng(40)
    x = rng.standard_normal((256, 256)).astype(np.float16)
    w = (rng.standard_normal((256, 256)) / 16).astype(np.float16)
    want = _silu(_mxmm(x, w).astype(np.float16))
    return {"x": x, "w": w}, lambda o: _rel(o["y"], want) < 2e-3


def _ex_mlpdirect():
    rng = np.random.default_rng(41)
    x = rng.standard_normal((64, 64)).astype(np.float16)
    w1 = (rng.standard_normal((128, 64)) / 8).astype(np.float16)
    w2 = (rng.standard_normal((64, 128)) / 11).astype(np.float16)
    a = _silu(_mxmm(x, w1).astype(np.float16)).astype(np.float16)
    return {"x": x, "w1": w1, "w2": w2}, lambda o: _rel(o["y"], _mxmm(a, w2)) < 2e-2


def _ex_mmsilu():
    x, w = _operands(30, 256, 256, 512)
    return {"x": x, "w": w}, lambda o: _rel(o["y"], _silu(_mxmm(x, w))) < 1e-2


def _ex_mmsoftmax():
    rng = np.random.default_rng(44)
    x = rng.standard_normal((256, 128)).astype(np.float16)
    w = (rng.standard_normal((128, 128)) / 4).astype(np.float16)
    s = _mxmm(x, w).astype(np.float16).astype(np.float64)
    return {"x": x, "w": w}, lambda o: _rel(o["y"], _softmax(s)) < 4e-3


def _ex_convert(machine_json):
    x = _f16(3, 64, 128)
    a = Buffer.of("mx7a[64,128] @dram(gm=8,nk=2)", machine_json).pack(x)
    b = Buffer.of("mx7b[64,128] @dram(gn=16,nk=4)", machine_json).pack(x)

    def ok(o):
        return bool(
            o["a.raw"] == a
            and o["b.raw"] == b
            and np.array_equal(
                o["t"], fp16.values(x.view(np.uint16), consumer="cluster")
            )
        )

    return {"x": x}, ok


EXAMPLES = {
    "add": ("add", _ex_add),
    "push": ("push", _ex_push),
    "transpose": ("transpose", _ex_transpose),
    "pad": ("pad", _ex_pad),
    "mask": ("mask", _ex_mask),
    "fill": ("fill", _ex_fill),
    "generate": ("generate", _ex_generate),
    "gather": ("gather", _ex_gather),
    "scatter": ("scatter", _ex_scatter),
    "memops": ("memops", _ex_memops),
    "matmul": ("matmul", _ex_matmul(8, 1024, 1024, 1024)),
    "longk": ("longk", _ex_matmul(21, 1024, 1024, 2048)),
    "splitk": ("splitk", _ex_matmul(10, 256, 256, 1024)),
    "tonode": ("tonode", _ex_tonode),
    "tiles": ("tiles", _ex_tiles),
    "softmax": ("softmax", _ex_softmax),
    "layernorm": ("layernorm", _ex_layernorm),
    "leaky.l2": ("leaky", _ex_leaky),
    "lazysoftmax1.l2": ("lazysoftmax", _ex_lazysoftmax(1.0)),
    "lazysoftmax8.l2": ("lazysoftmax", _ex_lazysoftmax(8.0)),
    "splitk.l2": ("splitk", _ex_matmul(28, 256, 256, 1024)),
    "mmsilu.l2": ("mmsilu", _ex_mmsilu),
    "mlp.l2": ("mlp", _ex_mlp),
    "tilesilu.l2": ("tilesilu", _ex_tilesilu),
    "mmsoftmax.l2": ("mmsoftmax", _ex_mmsoftmax),
    "mlpdirect.l2": ("mlpdirect", _ex_mlpdirect),
    "convert.l2": ("convert", _ex_convert),
    "gather.l2": ("gather", _ex_gather),
    "transpose.l2": ("transpose", _ex_transpose),
}
