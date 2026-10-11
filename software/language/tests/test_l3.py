"""L3: every kernel's `fn NAME.l3` reads, verifies and prints to a fixed point;
the reference interpreter computes what numpy computes on the hardware's
numerics (MXFP7 operands, fp16 results); the verifier refuses each kind of
wrong body at its line."""

import numpy as np
import pytest
from kohakutpu.language import kernels
from kohakutpu.language.l3 import reader, reference
from kohakutpu.language.numerics.mxfp7 import value_fp16
from kohakutpu.language.text import module
from kohakutpu.language.text.syntax import TextError

L3 = sorted(
    (name, path)
    for path in kernels.KERNELS.rglob("*.ktpu")
    for name, level in kernels.load(path).fns
    if level == "l3"
)
LOG2E = np.log2(np.e)


def f16(x):
    return np.asarray(x, np.float16).astype(np.float64)


def rel(got, want) -> float:
    return float(np.abs(got - want).max() / np.abs(want).max())


def sigmoid(x):
    return 1 / (1 + np.exp(-x))


def run(name, *inputs, exact=False):
    return reference.run(kernels.load(kernels.kernel(name)), name, list(inputs), exact)


@pytest.mark.parametrize("name, path", L3, ids=[n for n, _ in L3])
def test_a_kernel_prints_to_a_fixed_point(name, path):
    fn = reader.module(kernels.load(path)).fns[name]
    text = reader.fn_text(fn)
    back = reader.module(module.read(text, "<printed>")).fns[name]
    assert back == fn
    assert reader.fn_text(back) == text


def test_every_kernel_has_an_l3_body():
    assert {n for n, _ in L3} == set(kernels.names())


def rng_f16(seed, *shape, scale=1.0, shift=0.0):
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(shape) * scale + shift).astype(np.float16)


def test_add_is_the_rounded_sum():
    x, y = rng_f16(0, 4096), rng_f16(1, 4096)
    assert np.array_equal(run("add", x, y), f16(f16(x) + f16(y)))


def test_silu():
    x = rng_f16(2, 4096, scale=3.0)
    v = f16(x)
    assert rel(run("silu", x), v * sigmoid(v)) < 2e-3


def test_softmax():
    x = rng_f16(3, 64, 128, scale=2.0, shift=0.5)
    v = f16(x)
    e = np.exp(v - v.max(1, keepdims=True))
    assert rel(run("softmax", x), e / e.sum(1, keepdims=True)) < 2e-3


def test_layernorm():
    x = rng_f16(4, 64, 128, scale=2.0, shift=0.5)
    g = rng_f16(5, 128, scale=0.5, shift=1.0)
    b = rng_f16(6, 128, scale=0.5)
    v = f16(x)
    want = (v - v.mean(1, keepdims=True)) / np.sqrt(v.var(1, keepdims=True) + 1e-5)
    want = want * f16(g) + f16(b)
    assert rel(run("layernorm", x, g, b), want) < 4e-3


def test_mlp_is_the_quantised_products():
    x = rng_f16(7, 64, 128, scale=0.5)
    w1 = rng_f16(8, 96, 128, scale=0.5)
    w2 = rng_f16(9, 64, 96, scale=0.5)
    h = f16(value_fp16(x) @ value_fp16(w1).T)
    s = f16(h * sigmoid(h))
    want = f16(value_fp16(s) @ value_fp16(w2).T)
    assert rel(run("mlp", x, w1, w2), want) < 2e-3


def test_swiglu_is_the_quantised_products():
    x = rng_f16(10, 64, 128, scale=0.5)
    wg = rng_f16(11, 96, 128, scale=0.5)
    wu = rng_f16(12, 96, 128, scale=0.5)
    wd = rng_f16(13, 64, 96, scale=0.5)
    g = f16(value_fp16(x) @ value_fp16(wg).T)
    up = f16(value_fp16(x) @ value_fp16(wu).T)
    a = f16(g * sigmoid(g) * up)
    want = f16(value_fp16(a) @ value_fp16(wd).T)
    assert rel(run("swiglu", x, wg, wu, wd), want) < 2e-3


def attention(q, k, v):
    """softmax2(q k^T) v in float64, on the MXFP7 operands the clusters see:
    q and k, then P and v^T."""
    s = f16(value_fp16(q) @ value_fp16(k).T)
    p = f16(np.exp2(s - s.max(1, keepdims=True)))
    o = f16(value_fp16(p) @ value_fp16(f16(v).T).T)
    return o / p.sum(1, keepdims=True)


def test_attention():
    q = (rng_f16(14, 128, 64) * LOG2E / 8).astype(np.float16)
    k, v = rng_f16(15, 256, 64), rng_f16(16, 256, 64)
    assert rel(run("attention", q, k, v), attention(q, k, v)) < 2e-3


def test_flash_is_attention_per_head():
    q = (rng_f16(17, 2048, 64) * LOG2E / 8).astype(np.float16)
    k, v = rng_f16(18, 2048, 64), rng_f16(19, 2048, 64)
    got = run("flash", q, k, v)
    for h in range(2):
        rows = slice(1024 * h, 1024 * (h + 1))
        assert np.array_equal(got[rows], run("attention", q[rows], k[rows], v[rows]))


HEAD = "fn p.l3(x: f16[N, 64], y: f16[N, 64]) -> f16[N, 64]\n"


def body(*lines) -> str:
    return HEAD + "".join(f"    {ln}\n" for ln in lines)


@pytest.mark.parametrize(
    "text,where,what",
    [
        (body("a = add x, z : f16[N, 64]", "return a"), 2, "'z' is not bound"),
        (body("a = add x, y : f16[N, 32]", "return a"), 2, "add gives [N, 64]"),
        (body("a = frob x : f16[N, 64]", "return a"), 2, "no op 'frob'"),
        (body("a = add x : f16[N, 64]", "return a"), 2, "takes 2"),
        (body("a = add x, y", "return a"), 2, "names its type"),
        (body("a = quantise x : f16[N, 64]", "return a"), 2, "gives mx7"),
        (
            body("a = add x, y : f16[N, 64]", "a = sub x, y : f16[N, 64]", "return a"),
            3,
            "already bound",
        ),
        (body("store x = y", "return y"), 2, "not an output"),
        (body("m = carry 0.0 : f32[N]", "return x"), 2, "no scan after it"),
        (body("next x = y", "return x"), 2, "not a carry"),
        (
            body(
                "o = output : f16[N, 64]",
                "tile b = 16",
                "map i in tiles(64, b)",
                "    a = copy x[i, :] : f16[b, 64]",
                "return o",
            ),
            5,
            "tiles 64, the dimension is N",
        ),
        (
            body(
                "m = carry 0.0 : f32[64]",
                "scan j in 0..N",
                "    a = copy x[j] : f32[64]",
                "return x",
            ),
            3,
            "updated 0 times",
        ),
        (
            "fn f.l3(x: f32[4]) -> f32[4]\n    y = call f(x)\n    return y\n",
            1,
            "calls itself",
        ),
        (
            "fn f.l3(x: f32[4]) -> f32[4]\n    return x\n    y = copy x : f32[4]\n",
            2,
            "return ends",
        ),
        (body("a = mmt x, y : f16[N, N]", "return x"), 2, "mmt reads mx7"),
        (
            body("a = quantise x[:, 0 : 48] : mx7[N, 48]", "return x"),
            2,
            "48 is not whole",
        ),
        (
            body(
                "a = quantise x : mx7[N, 64]", "b = add a, x : f16[N, 64]", "return b"
            ),
            3,
            "is mx7",
        ),
    ],
)
def test_a_wrong_body_is_refused_at_its_line(text, where, what):
    with pytest.raises(TextError) as e:
        reader.module(module.read(text, "<test>"))
    assert (e.value.line, what in e.value.message) == (where, True), e.value.message


def test_syntax_errors_carry_their_position():
    with pytest.raises(TextError) as e:
        module.read(body("a = add x,, x : f16[N, 64]", "return a"), "<test>")
    assert e.value.line == 2
