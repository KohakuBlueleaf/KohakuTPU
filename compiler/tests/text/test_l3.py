"""L3: the reference kernels read, verify, print to a fixed point, and compute
what numpy computes on the hardware's numerics (MXFP7 operands, fp16 stores);
the verifier refuses each kind of wrong program at its line."""

import pathlib
import sys

import numpy as np
import pytest
from kohakuaccel.ir.l3 import text as L3T
from kohakuaccel.text.syntax import TextError
from kohakutpu.hw.mxfp7 import value_fp16
from kohakutpu.ir import l3

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "l1"))
from test_kernels import flash_reference


def f16(x):
    return np.asarray(x, np.float16).astype(np.float64)


def rel(got, want) -> float:
    return float(np.abs(got - want).max() / np.abs(want).max())


@pytest.mark.parametrize("name", ["attention", "linear", "rows", "conv"])
def test_a_kernel_prints_to_a_fixed_point(name):
    m = l3.kernel(name)
    text = l3.write(m)
    back = l3.read(text)
    assert back == m
    assert l3.write(back) == text


@pytest.mark.parametrize("blocks", [2, 3])
def test_attention_is_the_flash_reference(blocks):
    rng = np.random.default_rng(blocks)
    q = (rng.standard_normal((32, 64)) * np.log2(np.e) / 8).astype(np.float16)
    k = rng.standard_normal((64 * blocks, 64)).astype(np.float16)
    v = rng.standard_normal((64 * blocks, 64)).astype(np.float16)
    inputs = {"q": q[None], "k": k[None], "vt": v.T[None]}
    out = l3.run(l3.kernel("attention"), "attention", inputs)
    assert rel(out["o"][0], flash_reference(q, k, v, blocks)) < 2e-3


def test_linear_is_the_quantised_product_plus_bias_exactly():
    rng = np.random.default_rng(0)
    x = (rng.standard_normal((64, 128)) * 0.5).astype(np.float16)
    w = (rng.standard_normal((96, 128)) * 0.5).astype(np.float16)
    b = (rng.standard_normal(96) * 2).astype(np.float16)
    out = l3.run(l3.kernel("linear"), "linear", {"x": x, "w": w, "bias": b})
    want = f16(f16(value_fp16(x) @ value_fp16(w).T) + b)
    assert np.array_equal(out["y"], want)


def test_linear_silu_inlined_and_silu_called():
    rng = np.random.default_rng(1)
    x = (rng.standard_normal((128, 64)) * 0.5).astype(np.float16)
    w = (rng.standard_normal((128, 64)) * 0.5).astype(np.float16)
    m = l3.kernel("linear")
    h = f16(value_fp16(x) @ value_fp16(w).T)
    got = l3.run(m, "linear_silu", {"x": x, "w": w})["y"]
    assert rel(got, h / (1 + np.exp(-h))) < 2e-3
    v = x.astype(np.float64)
    got = l3.run(m, "silu_call", {"x": x})["y"]
    assert rel(got, v / (1 + np.exp(-v))) < 2e-3


def test_softmax_and_layernorm():
    rng = np.random.default_rng(2)
    x = (rng.standard_normal((32, 320)) * 2 + 0.5).astype(np.float16)
    g = (rng.standard_normal(320) * 0.5 + 1).astype(np.float16)
    b = (rng.standard_normal(320) * 0.5).astype(np.float16)
    m = l3.kernel("rows")
    v = x.astype(np.float64)
    e = np.exp(v - v.max(1, keepdims=True))
    assert rel(l3.run(m, "softmax", {"x": x})["y"], e / e.sum(1, keepdims=True)) < 2e-3
    want = (v - v.mean(1, keepdims=True)) / np.sqrt(v.var(1, keepdims=True) + 1e-5)
    want = want * g.astype(np.float64) + b.astype(np.float64)
    assert rel(l3.run(m, "layernorm", {"x": x, "g": g, "b": b})["y"], want) < 4e-3


def test_conv_then_add():
    rng = np.random.default_rng(3)
    h, w, cin, cout = 8, 12, 64, 32
    x = (rng.standard_normal((h, w, cin)) * 0.5).astype(np.float16)
    wt = (rng.standard_normal((cout, cin, 3, 3)) * 0.2).astype(np.float16)
    x2 = rng.standard_normal((h, w, cout)).astype(np.float16)
    taps = np.ascontiguousarray(wt.transpose(0, 2, 3, 1))
    got = l3.run(l3.kernel("conv"), "conv", {"x": x, "w": taps, "x2": x2})["y"]
    xq = np.zeros((h + 2, w + 2, cin))
    xq[1:-1, 1:-1] = value_fp16(x)
    wk = np.ascontiguousarray(wt.transpose(0, 2, 3, 1)).reshape(cout, 9 * cin)
    wq = value_fp16(wk).reshape(cout, 3, 3, cin)
    taps = [(dy, dx) for dy in range(3) for dx in range(3)]
    conv = sum(xq[dy : dy + h, dx : dx + w] @ wq[:, dy, dx, :].T for dy, dx in taps)
    assert rel(got, f16(f16(conv) + x2)) < 1e-3


def body(*lines) -> str:
    return "level l3\nprogram p(x: f16[N, 64], y: f16[N, 64])\n" + "".join(
        f"    {ln}\n" for ln in lines
    )


@pytest.mark.parametrize(
    "text,where,what",
    [
        (body("a = add x, z : f16[N, 64]"), 3, "'z' is not bound"),
        (body("a = add x, y : f16[N, 32]"), 3, "add gives [N, 64]"),
        (body("a = frob x : f16[N, 64]"), 3, "no op 'frob'"),
        (body("a = add x : f16[N, 64]"), 3, "takes 2"),
        (body("a = add x, y"), 3, "names its type"),
        (body("a = quantise x : f16[N, 64]"), 3, "gives mx7"),
        (
            body("a = add x, y : f16[N, 64]", "a = sub x, y : f16[N, 64]"),
            4,
            "already bound",
        ),
        (body("store x = y"), 3, "not an output"),
        (body("m = carry 0.0 : f32[N]"), 3, "no scan after it"),
        (body("next x = y"), 3, "not a carry"),
        (
            body(
                "o = output : f16[N, 64]",
                "tile b = 16",
                "map i in tiles(64, b)",
                "    a = copy x[i, :] : f16[b, 64]",
            ),
            6,
            "tiles 64, the dimension is N",
        ),
        (
            body(
                "m = carry 0.0 : f32[64]",
                "scan j in 0..N",
                "    a = copy x[j] : f32[64]",
            ),
            4,
            "updated 0 times",
        ),
        (
            "level l3\nfn f(x: f32[4]) -> f32[4]\n    y = call f(x)\n    return y\n",
            2,
            "calls itself",
        ),
        (
            "level l3\nfn f(x: f32[4]) -> f32[4]\n    return x\n    y = copy x : f32[4]\n",
            3,
            "return ends",
        ),
        (body("a = mmt x, y : f16[N, N]"), 3, "mmt reads mx7; an operand is f16"),
        (body("a = quantise x[:, 0 : 48] : mx7[N, 48]"), 3, "48 is not whole"),
        (body("a = quantise x : mx7[N, 64]", "b = add a, x : f16[N, 64]"), 4, "is mx7"),
    ],
)
def test_a_wrong_program_is_refused_at_its_line(text, where, what):
    with pytest.raises(TextError) as e:
        l3.read(text)
    assert e.value.line == where
    assert what in e.value.message


def test_syntax_errors_carry_their_position():
    with pytest.raises(TextError) as e:
        L3T.read("level l3\nprogram p(x: f16[4])\n    a = add x,, x : f16[4]\n")
    assert e.value.line == 3
