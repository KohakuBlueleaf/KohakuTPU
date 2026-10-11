"""Every hand L2 body, every body planned from L3 (at several sizes and unit
counts) and every body after an L2 pass, run against its L3 body.

The two interpreters are independent: L3 rounds each op once to its
statement's dtype, L2 rounds as the units do. The error is max |L2 - L3| over
max |L3|; a wrong tile, offset or unit makes it O(1). By form:

- elementwise, rows, gemm chains: the L3 ops in the same dtypes, exactly;
- attention (`exact`): y is stored through the lane format to f16 where L3
  rounds once to f16, so within one f16 ulp of the peak (`ULP`);
- the online form: P is quantised at its lazy offset, not the row max, so its
  MXFP7 rounding is L3's only in distribution; its distance from unrounded
  math is within `ONLINE` times L3's own.
"""

import dataclasses

import numpy as np
import pytest
from kohakutpu.compiler.target import TARGET
from kohakutpu.language import kernels
from kohakutpu.language.l2 import interp, reader
from kohakutpu.language.l3 import reader as l3_reader
from kohakutpu.language.l3 import reference
from kohakutpu.language.lower.l3 import PlanError, problem
from kohakutpu.language.lower.l3.common import transposed
from kohakutpu.language.pipeline import compile_l3, l2_text
from kohakutpu.language.text import module

ONE_ROUNDING = {"silu", "add", "softmax", "layernorm", "mlp", "swiglu"}
#: An f16 ulp is at most 2^-10 of a value in its binade.
ULP = 2.0**-10
#: The online form's distance from unrounded math over L3's.
ONLINE = 1.5


def load(name):
    return kernels.load(kernels.kernel(name))


def units(vc: int, mg: int):
    return dataclasses.replace(TARGET, units={"VC": vc, "MG": mg})


def outputs(m, name: str, body, shapes: dict, seed: int = 2) -> tuple:
    """``(L2, L3, unrounded L3)`` of `body` on seeded inputs at `shapes`."""
    fn = l3_reader.module(m).fns[name]
    params = [p for p, _ in fn.params]
    rng = np.random.default_rng(seed)
    xs = kernels.inputs(name, [(p, shapes[p]) for p in params], rng)
    xs = [np.asarray(x, np.float16).astype(np.float64) for x in xs]
    want = np.asarray(reference.run(m, name, xs), np.float64)
    exact = np.asarray(reference.run(m, name, xs, exact=True), np.float64)
    flip = transposed(fn)
    feed = {p: (x.T if p in flip else x) for p, x in zip(params, xs, strict=True)}
    (out,) = [p for p, _ in body.params if p not in params]
    got = interp.run(body, feed)[out]
    assert got.shape == want.shape
    return got, want, exact


def rel(a, b) -> float:
    return float(np.abs(a - b).max() / np.abs(b).max())


def planned(m, name: str, target, shapes=None):
    return reader.body(module.read(compile_l3(m, name, target, None, shapes)), name)


def check(m, name: str, body, shapes: dict, seed: int = 2) -> None:
    got, want, exact = outputs(m, name, body, shapes, seed)
    if name in ONE_ROUNDING:
        assert rel(got, want) == 0.0
    elif name == "attention":
        assert rel(got, want) <= ULP
    else:
        assert rel(got, exact) <= ONLINE * rel(want, exact)


# ------------------------------------------------------------ hand bodies
@pytest.mark.parametrize("seed", [1, 2, 3])
@pytest.mark.parametrize("name", kernels.names())
def test_hand_l2_computes_its_l3(name, seed):
    m = load(name)
    check(m, name, reader.body(m, name), problem(m, name), seed)


@pytest.mark.parametrize(
    "name, old, new",
    [
        ("silu", "c*32768 + t*2048", "c*32768 + (t/2)*2048"),
        ("softmax", "k*512 + t*16", "k*512 + (t/2)*16"),
        ("mlp", "+256, u*256 : +256] <- drain", "+256, (u/2)*256 : +256] <- drain"),
        ("attention", "<- mul ot, r[:, *]", "<- mul ot, 1.0"),
        ("flash", "v[:, (g/8)*1024 + kh*512 ", "v[:, (g/8)*1024 "),
        ("flash", "fma ob, wc[:, *], ya", "mul ob, wc[:, *]"),
    ],
)
def test_the_check_sees_a_broken_plan(name, old, new):
    m = load(name)
    text = compile_l3(m, name, TARGET)
    assert old in text
    body = reader.body(module.read(text.replace(old, new)), name)
    with pytest.raises(AssertionError):
        check(m, name, body, problem(m, name))


# --------------------------------------------------------------- planned
#: (kernel, L3 shapes or None for the hand size, (cores, clusters)).
CASES = [
    *[(n, None, (2, 4)) for n in kernels.names()],
    ("silu", {"x": (2048,)}, (2, 4)),
    ("silu", {"x": (6144,)}, (2, 4)),
    ("silu", {"x": (8192,)}, (4, 8)),
    ("add", {"x": (4096,), "y": (4096,)}, (1, 2)),
    ("softmax", {"x": (64, 128)}, (2, 4)),
    ("softmax", {"x": (96, 128)}, (2, 4)),
    ("softmax", {"x": (64, 128)}, (4, 8)),
    ("layernorm", {"x": (96, 128), "g": (128,), "b": (128,)}, (1, 2)),
    ("mlp", {"x": (256, 256), "w1": (512, 256), "w2": (256, 512)}, (2, 4)),
    ("mlp", {"x": (512, 256), "w1": (768, 256), "w2": (256, 768)}, (1, 2)),
    (
        "swiglu",
        {"x": (256, 256), "wg": (512, 256), "wu": (512, 256), "wd": (256, 512)},
        (2, 4),
    ),
    ("attention", {"q": (256, 64), "k": (512, 64), "v": (512, 64)}, (2, 4)),
    ("attention", {"q": (512, 32), "k": (256, 32), "v": (256, 32)}, (1, 1)),
    ("flash", {"q": (2048, 64), "k": (2048, 64), "v": (2048, 64)}, (2, 4)),
]


@pytest.mark.parametrize(
    "name, shapes, at",
    CASES,
    ids=[
        f"{n}-{'hand' if s is None else 'x'.join(map(str, next(iter(s.values()))))}"
        f"-{c}vc{g}mg"
        for n, s, (c, g) in CASES
    ],
)
def test_planned_l2_computes_its_l3(name, shapes, at):
    m = load(name)
    check(m, name, planned(m, name, units(*at), shapes), shapes or problem(m, name))


def test_a_plan_places_on_the_targets_units():
    m = load("softmax")
    text = compile_l3(m, "softmax", units(4, 8), None, {"x": (64, 128)})
    assert "par k in 0..4 on vc[k]" in text


@pytest.mark.parametrize(
    "name, shapes, at, message",
    [
        ("silu", {"x": (1000,)}, (2, 4), "pairs"),
        ("silu", {"x": (2049,)}, (2, 4), "split over 2 cores"),
        ("softmax", {"x": (40, 128)}, (2, 4), "tile pairs"),
        ("mlp", {"x": (100, 256), "w1": (512, 256), "w2": (256, 512)}, (2, 4), "256"),
        ("flash", {"q": (2048, 64), "k": (2048, 64), "v": (2048, 64)}, (1, 2), "4 clu"),
        ("silu", {"x": (2048,)}, (0, 4), "no vector core"),
    ],
)
def test_a_plan_refuses_with_its_reason(name, shapes, at, message):
    with pytest.raises(PlanError, match=message):
        compile_l3(load(name), name, units(*at), None, shapes)


def test_a_kernel_with_no_hand_l2_takes_its_size_from_the_caller():
    m = load("silu")
    m.fns = {k: v for k, v in m.fns.items() if k[1] != "l2"}
    with pytest.raises(PlanError, match="pass shapes"):
        compile_l3(m, "silu", TARGET)
    check(m, "silu", planned(m, "silu", TARGET, {"x": (4096,)}), {"x": (4096,)})


# ---------------------------------------------------------------- passes
@pytest.mark.parametrize("name", ["softmax", "layernorm"])
@pytest.mark.parametrize("passes", [("retile=8",), ("retile=auto",)])
def test_an_l2_pass_keeps_every_value(name, passes):
    m = load(name)
    shapes = problem(m, name)
    before = reader.body(m, name)
    after = reader.body(module.read(l2_text(m, name, TARGET, passes)), name)
    fn = l3_reader.module(m).fns[name]
    rng = np.random.default_rng(5)
    xs = kernels.inputs(name, [(p, shapes[p]) for p, _ in fn.params], rng)
    feed = {
        p: np.asarray(x, np.float16) for (p, _), x in zip(fn.params, xs, strict=True)
    }
    (out,) = [p for p, _ in before.params if p not in feed]
    got, want = interp.run(after, feed)[out], interp.run(before, feed)[out]
    assert np.array_equal(got, want)


# ------------------------------------------------------- the interpreter
def test_the_interpreter_carries_a_value_through_a_loop_and_rounds_stores():
    text = """fn k.l2(x: f16[128, 64] @dram, y: f16[32, 64] @dram)
    par u in 0..1 on vc[0]
        xt = load x[0 : +32, :]                     : f16[32, 64] @l1
        m = copy xt                                 : f32[32, 64]
        for i in 1..4
            xi = load x[i*32 : +32, :]              : f16[32, 64] @l1
            m = max m, xi                           : f32[32, 64]
        store y[0 : +32, :] <- mul m, 3.0
"""
    body = reader.body(module.read(text), "k")
    x = np.random.default_rng(1).standard_normal((128, 64))
    got = interp.run(body, {"x": x})["y"]
    x16 = x.astype(np.float16).astype(np.float64)
    want = (x16.reshape(4, 32, 64).max(0) * 3.0).astype(np.float32)
    assert np.array_equal(got, want.astype(np.float16).astype(np.float64))
