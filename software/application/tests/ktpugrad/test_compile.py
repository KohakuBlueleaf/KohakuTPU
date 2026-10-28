"""Every tinygrad function the models use, across shapes, on an offline
device (`runtime.device` kind ``offline``):

- `test_compiles`: each case's kernels planned, generated, traced, lowered
  L2 -> L1 -> L0, scheduled and checked by the static verifier (nothing is
  executed: the toolchain takes the program);
- `test_plans_compute`: each case with every plan computed by its numpy
  meaning (`ktpugrad.reference.execute`, no kernel) against tinygrad on the
  CPU: the tensor -> plan level's values.

A case's inputs come from a generator seeded by its name, on the device it
is built for (`build`)."""

import zlib

import numpy as np
import pytest
from tinygrad import nn
from tinygrad.dtype import dtypes
from tinygrad.tensor import Tensor

import kohakutpu.application.ktpugrad  # noqa: F401
from kohakutpu.application.ktpugrad import reference as REF
from kohakutpu.application.ktpugrad import run as R
from kohakutpu.application.ktpugrad.nn import place
from kohakutpu.application.runtime import device as D

#: The case being built: its input generator and device.
NOW: dict = {"rng": np.random.default_rng(3), "dev": "KTPU"}


def T(*shape):
    a = (NOW["rng"].standard_normal(shape) / 4).astype(np.float16)
    return Tensor(a, device=NOW["dev"])


def L(classes: int, n: int):
    """`n` one-hot float labels over `classes`: a loss reads labels as values."""
    lab = NOW["rng"].integers(0, classes, n)
    return Tensor(np.eye(classes, dtype=np.float16)[lab], device=NOW["dev"])


def F32(*shape):
    return Tensor(
        NOW["rng"].standard_normal(shape).astype(np.float32), device=NOW["dev"]
    )


def P(module):
    """`module`'s tensors on the case's device (f16 floats)."""
    return place(module, NOW["dev"])


# ---------------------------------------------------------------- elementwise
ELEMENT_SHAPES = [
    (1,),
    (7,),
    (33,),
    (16, 16),
    (3, 5, 7),
    (2, 3, 17, 19),
    (128, 1000),
    (1, 20480),
    (4096, 64),
]
UNARY = {
    "neg": lambda x: -x,
    "abs": lambda x: x.abs(),
    "exp": lambda x: x.exp(),
    "log": lambda x: (x.abs() + 1).log(),
    "sqrt": lambda x: x.abs().sqrt(),
    "rsqrt": lambda x: (x.abs() + 1).rsqrt(),
    "sin": lambda x: x.sin(),
    "cos": lambda x: x.cos(),
    "tanh": lambda x: x.tanh(),
    "sigmoid": lambda x: x.sigmoid(),
    "silu": lambda x: x.silu(),
    "gelu": lambda x: x.gelu(),
    "relu": lambda x: x.relu(),
    "leaky_relu": lambda x: x.leaky_relu(0.1),
    "square": lambda x: x.square(),
    "reciprocal": lambda x: (x.abs() + 1).reciprocal(),
    "exp2": lambda x: x.exp2(),
    "log2": lambda x: (x.abs() + 1).log2(),
    "clip": lambda x: x.clip(-0.5, 0.5),
    "floor": lambda x: x.floor(),
    "ceil": lambda x: x.ceil(),
    "sign": lambda x: x.sign(),
    "erf": lambda x: x.erf(),
}
BINARY = {
    "add": lambda a, b: a + b,
    "sub": lambda a, b: a - b,
    "mul": lambda a, b: a * b,
    "div": lambda a, b: a / (b.abs() + 1),
    "maximum": lambda a, b: a.maximum(b),
    "minimum": lambda a, b: a.minimum(b),
    "where": lambda a, b: (a > b).where(a, b),
    "lt": lambda a, b: (a < b).where(1.0, 0.0),
    "eq": lambda a, b: (a == b).where(1.0, 0.0),
    "fma": lambda a, b: a * b + a,
}
BROADCASTS = [
    ((4, 1, 8), (1, 5, 8)),
    ((16, 64), (64,)),
    ((2, 3, 4, 5), (3, 1, 1)),
    ((7, 1), (1, 9)),
    ((1,), (33, 17)),
]

# ----------------------------------------------------------------- reductions
REDUCE_SHAPES = [
    (7,),
    (5, 9),
    (3, 17, 33),
    (2, 6, 28, 28),
    (4, 20480),
    (128, 1000),
    (64, 6, 14, 14),
]
REDUCE = {
    "sum": lambda x, ax: x.sum(ax),
    "max": lambda x, ax: x.max(ax),
    "min": lambda x, ax: x.min(ax),
    "mean": lambda x, ax: x.mean(ax),
    "var": lambda x, ax: x.var(ax),
    "std": lambda x, ax: x.std(ax),
    "sum_keep": lambda x, ax: x.sum(ax, keepdim=True),
}

# ----------------------------------------------------------------- the rest
CASES: dict = {}


def case(name):
    def reg(f):
        CASES[name] = f
        return f

    return reg


for shape in ELEMENT_SHAPES:
    for op, f in UNARY.items():
        CASES[f"unary.{op}{shape}"] = lambda f=f, s=shape: f(T(*s))
    for op, f in BINARY.items():
        CASES[f"binary.{op}{shape}"] = lambda f=f, s=shape: f(T(*s), T(*s))
for a, b in BROADCASTS:
    for op in ("add", "mul", "maximum", "where"):
        CASES[f"broadcast.{op}{a}x{b}"] = lambda f=BINARY[op], a=a, b=b: f(T(*a), T(*b))
for shape in REDUCE_SHAPES:
    axes = [None, *range(len(shape))]
    if len(shape) > 2:
        axes += [(0, 2), tuple(range(1, len(shape)))]
    for op, f in REDUCE.items():
        for ax in axes:
            CASES[f"reduce.{op}{shape}@{ax}"] = lambda f=f, s=shape, ax=ax: f(T(*s), ax)


def rmsnorm(x):
    return x * (x.square().mean(-1, keepdim=True) + 1e-6).rsqrt()


def batchnorm_stats(x):
    mean, var = x.mean((0, 2, 3), keepdim=True), x.var((0, 2, 3), keepdim=True)
    return (x - mean) / (var + 1e-5).sqrt()


NORM_SHAPES = [(16, 128), (7, 33), (2, 4, 96), (8, 2048), (64, 120), (1, 4096)]
for s in NORM_SHAPES:
    CASES[f"norm.softmax{s}"] = lambda s=s: T(*s).softmax()
    CASES[f"norm.log_softmax{s}"] = lambda s=s: T(*s).log_softmax()
    CASES[f"norm.layernorm{s}"] = lambda s=s: T(*s).layernorm()
    CASES[f"norm.rmsnorm{s}"] = lambda s=s: rmsnorm(T(*s))
    CASES[f"norm.LayerNorm{s}"] = lambda s=s: P(nn.LayerNorm(s[-1]))(T(*s))
for n, c, h, w, g in [
    (2, 6, 28, 28, 2),
    (8, 16, 10, 10, 4),
    (1, 32, 7, 7, 8),
    (4, 12, 5, 5, 3),
    (64, 6, 28, 28, 2),
]:
    CASES[f"norm.GroupNorm({n},{c},{h},{w})g{g}"] = lambda n=n, c=c, h=h, w=w, g=g: P(
        nn.GroupNorm(g, c)
    )(T(n, c, h, w))
    CASES[f"norm.batchnorm_stats({n},{c},{h},{w})"] = (
        lambda n=n, c=c, h=h, w=w: batchnorm_stats(T(n, c, h, w))
    )

MOVE = {
    "reshape": lambda: T(6, 20).reshape(4, 30),
    "permute3": lambda: T(3, 5, 7).permute(2, 0, 1),
    "permute4": lambda: T(2, 3, 16, 32).permute(0, 2, 3, 1),
    "nchw_nhwc": lambda: T(4, 6, 14, 14).permute(0, 2, 3, 1),
    "nhwc_nchw": lambda: T(4, 14, 14, 6).permute(0, 3, 1, 2),
    "transpose": lambda: T(33, 17).T,
    "transpose_big": lambda: T(784, 128).T,
    "expand": lambda: T(1, 64).expand(32, 64),
    "expand_mid": lambda: T(4, 1, 16).expand(4, 8, 16),
    "pad": lambda: T(3, 8, 8).pad(((0, 0), (1, 1), (2, 2))),
    "pad_value": lambda: T(5, 7).pad(((1, 2), (3, 0)), value=1.0),
    "shrink": lambda: T(16, 64)[2:13, 5:50],
    "slice_step": lambda: T(32, 64)[::2, 1::3],
    "flip": lambda: T(5, 33).flip(1),
    "flip0": lambda: T(5, 33).flip(0),
    "cat0": lambda: T(3, 32).cat(T(5, 32)),
    "cat1": lambda: T(4, 7).cat(T(4, 9), dim=1),
    "stack": lambda: T(4, 16).stack(T(4, 16), T(4, 16)),
    "repeat": lambda: T(3, 4).repeat(2, 3),
    "upsample2x": lambda: T(2, 4, 7, 7).repeat_interleave(2, 2).repeat_interleave(2, 3),
    "contiguous_view": lambda: (T(16, 32)[:, ::2] + 0),
    # Rows overlapping their neighbours (pitch 16 < width 32), and rows off
    # a 32-byte word (a column offset of 8).
    "unfold_overlap": lambda: T(4, 80).unfold(1, 32, 16) * 2,
    "offset_cols": lambda: T(32, 128)[:, 8:72] * 2,
    "offset_cols_sum": lambda: T(32, 128)[:, 8:72].sum(0),
    "offset_cols_sum_narrow": lambda: T(32, 128)[:, 8:58].sum(0),
}
for name, f in MOVE.items():
    CASES[f"move.{name}"] = f

for m, k, n in [
    (1, 7, 3),
    (5, 33, 17),
    (32, 64, 96),
    (128, 256, 64),
    (64, 784, 128),
    (100, 84, 10),
    (64, 120, 84),
    (1, 512, 512),
    (256, 64, 1000),
    (64, 400, 120),
    (4, 64, 16384),  # an LM head: a 4-row band of the output 128 KB a trip
]:
    CASES[f"matmul.xw({m},{k},{n})"] = lambda m=m, k=k, n=n: T(m, k) @ T(k, n)
    CASES[f"matmul.xwT({m},{k},{n})"] = lambda m=m, k=k, n=n: T(m, k) @ T(n, k).T
    CASES[f"matmul.xTy({m},{k},{n})"] = lambda m=m, k=k, n=n: T(k, m).T @ T(k, n)
    CASES[f"matmul.linear({m},{k},{n})"] = lambda m=m, k=k, n=n: P(nn.Linear(k, n))(
        T(m, k)
    )
for b, h, m, k, n in [
    (2, 4, 8, 32, 8),
    (3, 1, 7, 16, 5),
    (2, 2, 64, 64, 64),
    (1, 8, 16, 64, 16),
]:
    CASES[f"matmul.batched({b},{h},{m},{k},{n})"] = lambda b=b, h=h, m=m, k=k, n=n: T(
        b, h, m, k
    ) @ T(b, h, k, n)

# Epilogue operands off a 32-byte word: a bias and a residual at column 8.
CASES["matmul.bias_offset"] = lambda: T(16, 32) @ T(32, 40) + T(64)[8:48]
CASES["matmul.residual_offset"] = lambda: T(16, 32) @ T(32, 64) + T(16, 80)[:, 8:72]

CONVS = [
    # (N, C, H, W), (O, K), stride, padding, dilation, groups
    ((2, 1, 28, 28), (6, 5), 1, 2, 1, 1),
    ((2, 6, 14, 14), (16, 5), 1, 0, 1, 1),
    ((2, 3, 16, 16), (8, 3), 2, 1, 1, 1),
    ((1, 16, 9, 9), (16, 3), 1, 1, 1, 16),
    ((2, 8, 12, 12), (8, 3), 1, 2, 2, 1),
    ((1, 4, 8, 8), (12, 1), 1, 0, 1, 1),
    ((1, 32, 16, 16), (32, 3), 1, 1, 1, 4),
    ((8, 3, 32, 32), (16, 3), 1, 1, 1, 1),
]
for (n, c, h, w), (o, k), st, pad, dil, g in CONVS:
    key = f"conv.({n},{c},{h},{w})o{o}k{k}s{st}p{pad}d{dil}g{g}"
    CASES[key] = lambda n=n, c=c, h=h, w=w, o=o, k=k, st=st, pad=pad, dil=dil, g=g: T(
        n, c, h, w
    ).conv2d(T(o, c // g, k, k), groups=g, stride=st, padding=pad, dilation=dil)
CASES["conv.transpose_s2"] = lambda: T(1, 8, 8, 8).conv_transpose2d(
    T(8, 4, 4, 4), stride=2, padding=1
)
CASES["conv.bias"] = lambda: P(nn.Conv2d(3, 8, 3, padding=1))(T(2, 3, 10, 10))

for s, (k, st) in [
    ((2, 6, 28, 28), (2, None)),
    ((2, 16, 10, 10), (2, None)),
    ((1, 4, 9, 9), (3, 2)),
    ((64, 6, 28, 28), (2, None)),
]:
    CASES[f"pool.avg{s}k{k}s{st}"] = lambda s=s, k=k, st=st: T(*s).avg_pool2d(
        (k, k), stride=st
    )
    CASES[f"pool.max{s}k{k}s{st}"] = lambda s=s, k=k, st=st: T(*s).max_pool2d(
        (k, k), stride=st
    )

for b, h, t, d in [(1, 2, 16, 32), (2, 4, 64, 64), (1, 1, 7, 16)]:
    CASES[f"attn.sdpa({b},{h},{t},{d})"] = lambda b=b, h=h, t=t, d=d: T(
        b, h, t, d
    ).scaled_dot_product_attention(T(b, h, t, d), T(b, h, t, d))
    CASES[f"attn.causal({b},{h},{t},{d})"] = lambda b=b, h=h, t=t, d=d: T(
        b, h, t, d
    ).scaled_dot_product_attention(T(b, h, t, d), T(b, h, t, d), is_causal=True)


def cross_entropy(logits, labels):
    return -(logits.log_softmax() * labels).sum(-1).mean()


CASES["misc.cross_entropy"] = lambda: cross_entropy(T(32, 10), L(10, 32))
CASES["misc.cumsum"] = lambda: T(8, 16).cumsum(1)
CASES["misc.arange"] = lambda: Tensor.arange(100, dtype=dtypes.half).to(NOW["dev"]) * 2
CASES["misc.f32_sum"] = lambda: F32(64, 64).sum(1)
CASES["misc.f32_matmul"] = lambda: F32(16, 32) @ F32(32, 8)
CASES["misc.cast_half_float"] = lambda: T(16, 32).cast(dtypes.float32).sum()
CASES["misc.mean_all_large"] = lambda: T(256, 1024).mean()
# Four reads down the rows: twice over, past a core's eight views.
CASES["misc.four_reads_down"] = lambda: (
    T(64, 64) * T(64, 64) + T(64, 64) * T(64, 64)
).sum(0)
CASES["misc.four_reads_down_odd"] = lambda: (
    T(37, 64) * T(37, 64) + T(37, 64) * T(37, 64)
).sum(0)
# A reduction read along an axis it does not vary over.
CASES["misc.sum_expand"] = lambda: T(8, 16).sum(1, keepdim=True).expand(8, 64) + 0
CASES["misc.max_expand_mid"] = (
    lambda: T(4, 8, 32).max(1, keepdim=True).expand(4, 6, 32) * 2
)

# Past the families above: permuted reductions, size-1 axes, bool masks,
# indexing, masks, scans, broadcast batches.
EXTRA = {
    "sum_of_transpose": lambda: T(48, 40).T.sum(0),
    "max_of_permute": lambda: T(3, 20, 24).permute(2, 0, 1).max(1),
    "mean_odd_middle": lambda: T(5, 1, 7).mean(2),
    "sum_size1_axes": lambda: T(1, 33, 1, 65).sum((1, 3)),
    "softmax_axis0": lambda: T(40, 24).softmax(0),
    "logsumexp": lambda: T(12, 100).logsumexp(1),
    "layernorm_wide": lambda: T(3, 1000).layernorm(),
    "cumsum0": lambda: T(16, 8).cumsum(0),
    "cumsum_long": lambda: T(4, 300).cumsum(1),
    "tril": lambda: T(24, 24).tril(),
    "triu_masked_fill": lambda: (
        Tensor.ones(16, 16, device=NOW["dev"]).triu(1).cast(dtypes.bool)
    ).where(-float("inf"), T(16, 16)),
    "bool_logic": lambda: ((T(8, 40) > 0) & (T(8, 40) < 0.1)).where(1.0, 0.0),
    "vecmat": lambda: T(64) @ T(64, 96),
    "matvec": lambda: T(96, 64) @ T(64),
    "matmul_odd": lambda: T(3, 1000) @ T(1000, 7),
    "matmul_bcast_batch": lambda: T(2, 1, 8, 32) @ T(1, 3, 32, 16),
    "einsum_bij_bjk": lambda: Tensor.einsum("bij,bjk->bik", T(3, 5, 9), T(3, 9, 4)),
    "outer": lambda: T(20, 1) * T(1, 36),
    "pad_reflect_free": lambda: T(2, 3, 9, 9).pad(((0, 0), (0, 0), (2, 1), (0, 3))),
    "maxpool_s1": lambda: T(2, 4, 9, 9).max_pool2d((3, 3), stride=1),
    "avgpool_pad": lambda: T(2, 4, 10, 10).avg_pool2d((3, 3), stride=2, padding=1),
    "interp_nearest": lambda: T(1, 2, 5, 7).interpolate((10, 14), mode="nearest"),
    "flatten_linear": lambda: P(nn.Linear(6 * 5 * 5, 10))(T(4, 6, 5, 5).flatten(1)),
    "stack_odd": lambda: T(3, 7).stack(T(3, 7), dim=1),
    "clamp_grad_like": lambda: (T(32, 32).clip(-0.1, 0.1) * T(32, 32)).sum(),
}
for name, f in EXTRA.items():
    CASES[f"extra.{name}"] = f


def _grad(build, shapes):
    params = [T(*s) for s in shapes]
    return list(build(*params).gradient(*params))


GRADS = {
    "linear_relu_ce": (
        lambda x, w, b: cross_entropy((x @ w.T + b).relu(), L(10, 16)),
        [(16, 32), (10, 32), (10,)],
    ),
    "conv": (lambda x, w: x.conv2d(w, padding=1).sum(), [(2, 3, 8, 8), (4, 3, 3, 3)]),
    "conv_p2": (
        lambda x, w: x.conv2d(w, padding=2).silu().sum(),
        [(2, 1, 12, 12), (6, 1, 5, 5)],
    ),
    "conv_s2": (
        lambda x, w: x.conv2d(w, stride=2, padding=1).square().sum(),
        [(2, 3, 16, 16), (8, 3, 3, 3)],
    ),
    "avg_pool": (lambda x: x.avg_pool2d((2, 2)).square().sum(), [(2, 4, 8, 8)]),
    "max_pool": (lambda x: x.max_pool2d((2, 2)).square().sum(), [(2, 4, 8, 8)]),
    # Weighted, not squared: sum(norm(x)^2) is constant up to eps, its
    # gradient eps-sized (no f16 holds it).
    "layernorm": (lambda x: (x.layernorm() * T(8, 64)).sum(), [(8, 64)]),
    "groupnorm": (
        lambda x: (
            x.reshape(4, 2, 3 * 36).layernorm().reshape(4, 6, 6, 6) * T(4, 6, 6, 6)
        ).sum(),
        [(4, 6, 6, 6)],
    ),
    "softmax": (lambda x: x.softmax().square().sum(), [(8, 32)]),
    "silu_matmul": (lambda x, w: (x @ w).silu().sum(), [(16, 64), (64, 32)]),
    "attention": (
        lambda q, k, v: q.scaled_dot_product_attention(k, v).sum(),
        [(1, 2, 16, 32)] * 3,
    ),
    "embedding_like": (
        lambda w: (L(50, 8) @ w).sum(),
        [(50, 16)],
    ),
    "lenet_tail": (
        lambda x, w1, w2: (x.reshape(4, -1) @ w1).layernorm().silu().matmul(w2).sum(),
        [(4, 16, 5, 5), (400, 120), (120, 84)],
    ),
}
for name, (build, shapes) in GRADS.items():
    CASES[f"grad.{name}"] = lambda build=build, shapes=shapes: _grad(build, shapes)


def build(name: str, dev: str = "KTPU") -> list:
    """The tensors case `name` builds on `dev`, its inputs (and any layer's
    initial weights) the same on every device."""
    NOW["rng"] = np.random.default_rng(zlib.crc32(name.encode()))
    NOW["dev"] = dev
    Tensor.manual_seed(zlib.crc32(name.encode()))
    out = CASES[name]()
    return out if isinstance(out, list) else [out]


def run_case(name: str) -> None:
    """Realise every tensor case `name` builds."""
    Tensor.realize(*build(name))


@pytest.fixture(scope="module", autouse=True)
def offline():
    with D.attached(D.Device(kind="offline", board="multimesh_v9t8")):
        yield


@pytest.mark.parametrize("name", sorted(CASES))
def test_compiles(name):
    run_case(name)


#: Relative to the reference's largest magnitude: f16 data and results.
TOLERANCE = 3e-2


def agree(got, want) -> None:
    """Within `TOLERANCE` of the reference's largest magnitude."""
    assert got.shape == want.shape
    w = want.astype(np.float64)
    scale = float(np.abs(w[np.isfinite(w)]).max(initial=0.0)) or 1.0
    np.testing.assert_allclose(
        got.astype(np.float64), w, rtol=TOLERANCE, atol=TOLERANCE * scale
    )


@pytest.mark.parametrize("name", sorted(CASES))
def test_plans_compute(name, monkeypatch):
    monkeypatch.setattr(R, "execute", REF.execute)
    got = [t.numpy() for t in build(name)]
    want = [t.numpy() for t in build(name, "CPU")]
    for g, w in zip(got, want, strict=True):
        agree(g, w)
