"""S0 cases: one ViT-B operation at a full or shard shape as a small tinygrad graph,
its seeded network-init arrays, and the exact witnesses its outputs must meet.

A case's graph is `one(x, p)` for one repetition; a point runs R of them in one
TinyJit, each repetition with its own parameters (no two share a kernel input
tinygrad could merge), the input shared (a linear) or one each (a row operation).
"""

import dataclasses
import zlib
from collections.abc import Callable

import numpy as np
from tinygrad.tensor import Tensor

from kohakutpu.application.checks.accuracy import operand
from kohakutpu.language.numerics import e8m15

TOKENS = 197
WIDTH = 768
HEAD = 64
FFN = 4 * WIDTH
#: Rows given one seeded row: the first and both sides of each 50-row tile cut.
CUTS = (0, 49, 50, 99, 100, 149, 150)
#: Rows of a linear's input that are one-hot: 1 at an odd K index j (`row *
#: ONEHOT_STRIDE % K`), PEAK at j - 1 (a zero weight column), so the MXFP7 block
#: scale makes the 1 exact and the row's sum one exact weight.
ONEHOT = (1, 51, 101, 151, 195)
ONEHOT_STRIDE = 37
PEAK = 63 / 32
#: Rows of 50: the tile rows coverage statistics group by.
TILE = 50

Arrays = dict[str, np.ndarray]
Tensors = dict[str, Tensor]
Witness = list[tuple[str, np.ndarray, np.ndarray]]


@dataclasses.dataclass(frozen=True)
class Rep:
    """One repetition's f16 arrays: its input and its parameters."""

    x: np.ndarray
    p: Arrays


@dataclasses.dataclass(frozen=True)
class Case:
    """`shape` is (M, K, N) of a linear, (heads, rows, cols) of a row operation.
    `body` names the launch kinds f counts (empty: every launch); `paired` runs
    the S0.2 producer-packing variants side by side."""

    name: str
    family: str
    full: str
    share: float
    shape: tuple[int, int, int]
    shared_input: bool
    body: tuple[str, ...]
    x: Callable[[np.random.Generator], np.ndarray]
    p: Callable[[np.random.Generator], Arrays]
    one: Callable[[Tensor, Tensors], Tensor]
    witness: Callable[[np.ndarray, Rep], Witness]
    groups: Callable[[np.ndarray], dict[str, np.ndarray]]
    paired: bool = False

    def rep(self, seed: int, i: int) -> Rep:
        """Repetition `i`: the same arrays at every R and on every run of `seed`."""
        tag = zlib.crc32(self.name.encode())
        xi = 0 if self.shared_input else i
        x = self.x(np.random.default_rng([seed, tag, xi, 0]))
        return Rep(x, self.p(np.random.default_rng([seed, tag, i, 1])))


def f16(a) -> np.ndarray:
    return np.asarray(a, np.float32).astype(np.float16)


def init(rng: np.random.Generator, *shape: int) -> np.ndarray:
    """ViT's seeded weight init (`vit_tiny.seeded_model`)."""
    return f16(rng.standard_normal(shape) * 0.02)


def cut_rows(rows: int) -> list[int]:
    return sorted({r for r in CUTS if r < rows} | {rows - 1})


def same_rows(o: np.ndarray, rows: list[int], axis: int) -> tuple:
    """Rows `rows` of `o` along `axis`, and the first of them in their place."""
    got = np.take(o, rows, axis=axis)
    return got, np.broadcast_to(np.take(o, rows[:1], axis=axis), got.shape)


def row_tiles(o: np.ndarray, axis: int) -> dict[str, np.ndarray]:
    n = o.shape[axis]
    return {
        f"rows{a}": np.take(o, range(a, min(a + TILE, n)), axis=axis)
        for a in range(0, n, TILE)
    }


def heads(o: np.ndarray, axis: int, width: int) -> dict[str, np.ndarray]:
    """Per head of `width` along `axis`."""
    n = o.shape[axis] // width
    return {
        f"head{h}": np.take(o, range(h * width, (h + 1) * width), axis=axis)
        for h in range(n)
    }


def linear(
    name: str, family: str, full: str, share: float, k: int, n: int, epilogue: str
) -> Case:
    """`x @ w.T` with `epilogue`: "bias", "bias_res" (bias and a residual input),
    "gelu" (bias then GELU) or "none" (a partial output: no bias, no residual)."""
    picks = [r * ONEHOT_STRIDE % k for r in ONEHOT]

    def x(rng):
        a = f16(rng.standard_normal((1, TOKENS, k)))
        a[0, cut_rows(TOKENS)] = a[0, 2]
        for r, j in zip(ONEHOT, picks, strict=True):
            a[0, r] = 0
            a[0, r, j], a[0, r, j - 1] = 1, PEAK
        return a

    def p(rng):
        got = {"w": init(rng, n, k)}
        got["w"][:, [j - 1 for j in picks]] = 0
        if epilogue != "none":
            got["b"] = init(rng, n)
        if epilogue == "bias_res":
            res = f16(rng.standard_normal((1, TOKENS, n)))
            res[0, cut_rows(TOKENS)] = res[0, 2]
            res[0, list(ONEHOT)] = 0
            got["res"] = res
        return got

    def one(x, p):
        y = x.linear(p["w"].T, p.get("b"))
        if epilogue == "gelu":
            return y.gelu()
        return p["res"] + y if epilogue == "bias_res" else y

    def witness(o, rep):
        got = [("same_rows", *same_rows(o, cut_rows(TOKENS), 1))]
        if epilogue == "gelu":
            return got
        # A one-hot row is its MXFP7 weight column (f16-exact), plus the bias in
        # the vector lane's E8M15; a residual row here is zero.
        w = operand(rep.p["w"].astype(np.float32), -1)
        for r, j in zip(ONEHOT, picks, strict=True):
            want = w[:, j].astype(np.float64)
            if "b" in rep.p:
                want = e8m15.value(want + rep.p["b"].astype(np.float64))
            got.append((f"onehot{r}", o[0, r], f16(want)))
        return got

    return Case(
        name,
        family,
        full,
        share,
        (TOKENS, k, n),
        True,
        ("matmul",),
        x,
        p,
        one,
        witness,
        lambda o: row_tiles(o, 1),
    )


def attention(name: str, n_heads: int) -> Case:
    """The QKV projection of `n_heads` heads and whole-head attention over the
    tokens, as ViT's block writes it: head 0's V is zero, the last head (of three
    or more) a copy of head 1."""
    width = n_heads * HEAD

    def x(rng):
        a = f16(rng.standard_normal((1, TOKENS, WIDTH)))
        a[0, cut_rows(TOKENS)] = a[0, 2]
        return a

    def p(rng):
        w, b = init(rng, 3 * width, WIDTH), init(rng, 3 * width)
        w[2 * width : 2 * width + HEAD] = b[2 * width : 2 * width + HEAD] = 0
        if n_heads >= 3:
            for s in range(3):
                to, of = s * width + (n_heads - 1) * HEAD, s * width + HEAD
                w[to : to + HEAD], b[to : to + HEAD] = (
                    w[of : of + HEAD],
                    b[of : of + HEAD],
                )
        return {"w": w, "b": b}

    def one(x, p):
        qkv = x.linear(p["w"].T, p["b"]).reshape(1, TOKENS, 3, n_heads, HEAD)
        q, k, v = (qkv[:, :, i].transpose(1, 2) for i in range(3))
        scores = (q @ k.transpose(-1, -2)) * 0.125
        a = scores.softmax(-1) @ v
        return a.transpose(1, 2).reshape(1, TOKENS, width).contiguous()

    def witness(o, rep):
        got = [
            ("same_rows", *same_rows(o, cut_rows(TOKENS), 1)),
            ("zero_v_head0", o[0, :, :HEAD], np.zeros_like(o[0, :, :HEAD])),
        ]
        if n_heads >= 3:
            got.append(("copied_head", o[0, :, -HEAD:], o[0, :, HEAD : 2 * HEAD]))
        return got

    return Case(
        name,
        "attn",
        "attn.h12",
        n_heads / 12,
        (n_heads, TOKENS, TOKENS),
        True,
        (),
        x,
        p,
        one,
        witness,
        lambda o: heads(o, 2, HEAD),
    )


def layernorm(name: str, rows: int) -> Case:
    """`nn.LayerNorm(768, eps=1e-6)` over `rows` token rows; row 2 is zero."""

    def x(rng):
        a = f16(rng.standard_normal((1, rows, WIDTH)))
        a[0, cut_rows(rows)] = a[0, 3]
        a[0, 2] = 0
        return a

    def p(rng):
        return {"g": f16(np.ones(WIDTH)), "b": f16(np.zeros(WIDTH))}

    def one(x, p):
        return x.layernorm(eps=1e-6) * p["g"] + p["b"]

    def witness(o, rep):
        return [
            ("same_rows", *same_rows(o, cut_rows(rows), 1)),
            ("zero_row", o[0, 2], np.zeros_like(o[0, 2])),
        ]

    return Case(
        name,
        "ln",
        "ln.r197",
        rows / TOKENS,
        (1, rows, WIDTH),
        False,
        (),
        x,
        p,
        one,
        witness,
        lambda o: row_tiles(o, 1),
    )


def softmax(name: str, n_heads: int, rows: int) -> Case:
    """Scaled softmax of `n_heads` heads' scores as ViT's block makes them, `rows`
    query rows against every key: the input holds Q's rows then K's. Query row 1
    is zero, so its scores are."""

    def x(rng):
        a = f16(rng.standard_normal((1, n_heads, rows + TOKENS, HEAD)))
        s = a[0]
        s[:, cut_rows(rows)] = s[:, 3:4]
        s[:, 1] = 0
        return a

    def p(rng):
        return {}

    def one(x, p):
        q, k = x[:, :, :rows], x[:, :, rows:]
        return ((q @ k.transpose(-1, -2)) * 0.125).softmax(-1)

    def witness(o, rep):
        flat = o[0, :, 1]
        return [
            ("same_rows", *same_rows(o, cut_rows(rows), 2)),
            ("constant_row", flat, np.broadcast_to(flat[:, :1], flat.shape)),
        ]

    return Case(
        name,
        "softmax",
        "softmax.h12r197",
        n_heads * rows / (12 * TOKENS),
        (n_heads, rows, TOKENS),
        False,
        ("dsl",),
        x,
        p,
        one,
        witness,
        lambda o: heads(o, 1, 1),
    )


def producer(name: str, n: int) -> Case:
    """FC1 + GELU of `n` hidden columns and its consumer FC2 (a partial output
    below the full width): the S0.2 pair runs it with and without producer packs."""
    full = n == FFN

    def x(rng):
        a = f16(rng.standard_normal((1, TOKENS, WIDTH)))
        a[0, cut_rows(TOKENS)] = a[0, 2]
        return a

    def p(rng):
        got = {"w1": init(rng, n, WIDTH), "b1": init(rng, n), "w2": init(rng, WIDTH, n)}
        if full:
            got["b2"] = init(rng, WIDTH)
            res = f16(rng.standard_normal((1, TOKENS, WIDTH)))
            res[0, cut_rows(TOKENS)] = res[0, 2]
            got["res"] = res
        return got

    def one(x, p):
        y = x.linear(p["w1"].T, p["b1"]).gelu().linear(p["w2"].T, p.get("b2"))
        return p["res"] + y if full else y

    def witness(o, rep):
        return [("same_rows", *same_rows(o, cut_rows(TOKENS), 1))]

    return Case(
        name,
        "s02",
        f"s02.n{FFN}",
        n / FFN,
        (TOKENS, WIDTH, n),
        True,
        ("matmul", "quant"),
        x,
        p,
        one,
        witness,
        lambda o: row_tiles(o, 1),
        paired=True,
    )


def table() -> dict[str, Case]:
    """Every S0.1/S0.2 case by name."""
    out = []
    for family, k, ns, epilogue in (
        ("qkv", WIDTH, (2304, 1152, 576), "bias"),
        ("proj", WIDTH, (768, 384, 192), "bias_res"),
        ("fc1", WIDTH, (3072, 1536, 768), "gelu"),
        ("fc2", FFN, (768, 384, 192), "bias_res"),
    ):
        full = f"{family}.n{ns[0]}"
        out += [
            linear(f"{family}.n{n}", family, full, n / ns[0], k, n, epilogue)
            for n in ns
        ]
    out += [
        linear("kalt.k192", "proj", "proj.n768", 0.25, 192, WIDTH, "none"),
        linear("kalt.k768", "fc2", "fc2.n768", 0.25, WIDTH, WIDTH, "none"),
    ]
    out += [attention(f"attn.h{h}", h) for h in (12, 6, 3, 10)]
    out += [layernorm(f"ln.r{r}", r) for r in (TOKENS, 49, 50, 51, 52)]
    out += [softmax(f"softmax.h12r{r}", 12, r) for r in (TOKENS, 49, 50, 51, 52)]
    out.append(softmax(f"softmax.h3r{TOKENS}", 3, TOKENS))
    out += [producer(f"s02.n{n}", n) for n in (FFN, FFN // 2, FFN // 4)]
    return {c.name: c for c in out}
