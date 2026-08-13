"""Writing a kernel: ports, tiling, the accumulator, folds, and staging.

READ THIS FILE. The explanation is in the inline comments, beside the syntax it
explains. Run it and it checks itself against numpy, which is all the output an
example should want.
"""

import numpy as np
from kohakuaccel.lang import dims, loop, units
from kohakutpu.lang import kernel

from kohakutpu import api
from kohakutpu import lang as L

# Symbols, not numbers. A kernel is traced ONCE and compiled per shape, so `M`
# is whatever the operand turns out to be.
M, K, N, F = dims("M, K, N, F")

LOG2E = 1.4426950408889634


@kernel
def scale(x=L.In(...), y=L.Out(...), *, by=2.0, part=8192):
    """`x * by`, at any rank."""
    # `In(...)` is "any shape at all": one trace serves (64,) and (2, 3, 8, 32).
    # `parts(part)` STATES the tiling -- `part` elements per instance.
    with units(x.parts(part)) as e:
        # `e` is this instance's slice. One statement, one pass over it.
        y[e] <<= x[e] * by


@kernel
def matmul(a=L.In(..., M, K), b=L.In(N, K), c=L.Out(..., M, N), *, gm=8, gn=8, nk=2):
    """`a @ b.T`. `b` is `[N][K]`, the order a torch Linear already stores."""
    # `...` on `a` but not `b` is the batching rule: `a`'s leading axes become a
    # grid axis and ONE `b` is shared. `tiles(gm)` counts 4-row lane groups.
    with units(a.tiles(gm), b.tiles(gn)) as (i, j):
        # The cluster's resident accumulator, `gm x gn` sub-tiles deep. It lives
        # in the cluster, never in memory.
        acc = L.tile(gm, gn, nk)
        # `chunks32(nk)` counts sweeps of `nk` 32-element K-blocks. The 32 is in
        # the NAME because it is the only granule the ISA has.
        for k in loop(a.chunks32(nk)):
            # `+=` on a tile is the GEMM; `a[i, k]` fills one L1 region.
            acc += a[i, k] @ b[j, k]
        # `<<=` drains the tile. A bare `acc` writes it straight back through MAG.
        c[i, j] <<= acc


@kernel
def linear_bias(
    x=L.In(..., M, K),
    w=L.In(N, K),
    bias=L.In(N),
    y=L.Out(..., M, N),
    *,
    gm=8,
    gn=8,
    nk=2,
):
    """`x @ w.T + bias`, the bias read PER CHANNEL beside the resident tile."""
    with units(x.tiles(gm), w.tiles(gn)) as (i, j):
        acc = L.tile(gm, gn, nk)
        for k in loop(x.chunks32(nk)):
            acc += x[i, k] @ w[j, k]
        # An EXPRESSION on `acc` drains over the NoC to a vector core, so the
        # intermediate never returns through MAG. `bias[j]` is one word, stride 0.
        y[i, j] <<= acc + bias[j]


@kernel
def layernorm(
    x=L.In(..., M, N),
    w=L.In(..., M, N),
    b=L.In(..., M, N),
    y=L.Out(..., M, N),
    *,
    eps=1e-5,
    part=8192,
):
    """`(x - mean) * rstd * w + b` in ONE tiling, two reductions deep."""
    with units(x.parts(part)) as e:
        # `row_sum` folds a whole row. Written INSIDE an expression it is a fold
        # in a chain, so the sum stays in a register and never lands in memory.
        mu = L.row_sum(x[e]) / N
        # An ordinary Python variable holding a VALUE, not an `L.temp`. That is
        # the whole discipline: a temp goes through MAG, a value stays a register.
        dev = x[e] - mu
        # The second fold READS the first's result, so a value is carried across
        # a VRED. `dev` is reached three times and is lifted once into a register.
        var = L.row_sum((dev / N) * dev)
        # Exactly 8 of 8 descriptors here. Landing `dev` in a temp and reading it
        # back would need a ninth, and the band would be REFUSED.
        y[e] <<= dev * L.rsqrt(var + eps) * w[e] + b[e]


@kernel
def mlp(
    x=L.In(..., M, K),
    up=L.In(F, K),
    down=L.In(N, F),
    y=L.Out(..., M, N),
    *,
    gm=8,
    gn=8,
    nk=2,
):
    """`down(silu(up(x)))` -- two GEMMs, so at least two stages.

    Run this file: at these shapes it prints THREE and two temps. The second is
    `relax` staging the activation, because 16 tiles do not fit 4 vector cores.
    """
    # `L.temp` is a buffer behind MAG, so declaring one declares a round trip.
    # Unavoidable: the second GEMM needs `h` in an L1 a vector core cannot write.
    h = L.temp(M, F)

    # Every `with units(...)` is a top-level stage, and a top-level stage is a
    # MAG round trip. Keeping work inside one block is the whole game.
    with units(x.tiles(gm), up.tiles(gn)) as (i, j):
        acc = L.tile(gm, gn, nk)
        for k in loop(x.chunks32(nk)):
            acc += x[i, k] @ up[j, k]
        # The activation rides the drain, so it costs no pass of its own.
        h[i, j] <<= acc * L.recip(L.exp2(acc * -LOG2E) + 1.0)

    # Stage two reads `h` back through MAG.
    with units(h.tiles(gm), down.tiles(gn)) as (i, j):
        acc = L.tile(gm, gn, nk)
        for k in loop(h.chunks32(nk)):
            acc += h[i, k] @ down[j, k]
        y[i, j] <<= acc


def main() -> None:
    dev = api.script_device("sim")
    rng = np.random.default_rng(0)
    x = np.asarray(rng.normal(0, 0.3, (64, 128)), np.float16)
    w = np.asarray(rng.normal(0, 0.3, (64, 128)), np.float16)
    g = np.asarray(rng.normal(0, 0.1, (64, 128)) + 1, np.float16)
    b = np.asarray(rng.normal(0, 0.1, (64, 128)), np.float16)
    bias = np.asarray(rng.normal(0, 0.5, (64,)), np.float16)
    up = np.asarray(rng.normal(0, 0.2, (128, 128)), np.float16)
    down = np.asarray(rng.normal(0, 0.2, (64, 128)), np.float16)

    # `.astype`, not `np.float64(...)`: the latter is the SCALAR constructor and
    # a type checker rejects `@` on it, however well it works at runtime.
    xf, wf = x.astype(np.float64), w.astype(np.float64)
    ref = xf @ wf.T

    def close(got, want, tol=5e-2) -> str:
        err = np.abs(np.asarray(got.numpy()).astype(np.float64) - want).max()
        return "ok" if err / np.abs(want).max() < tol else f"FAILED ({err:.3e})"

    dv = xf - xf.mean(1, keepdims=True)
    ln = dv / np.sqrt((dv**2).mean(1, keepdims=True) + 1e-5)
    hidden = xf @ up.astype(np.float64).T
    hidden = hidden / (1.0 + np.exp(-hidden))

    print("scale      ", close(scale(dev.tensor(x)), xf * 2.0))
    print("matmul     ", close(matmul(dev.tensor(x), dev.tensor(w)), ref))
    print(
        "linear_bias",
        close(
            linear_bias(dev.tensor(x), dev.tensor(w), dev.tensor(bias)),
            ref + bias.astype(np.float64),
        ),
    )
    print(
        "layernorm  ",
        close(
            layernorm(dev.tensor(x), dev.tensor(g), dev.tensor(b)),
            ln * g.astype(np.float64) + b.astype(np.float64),
        ),
    )
    print(
        "mlp        ",
        close(
            mlp(dev.tensor(x), dev.tensor(up), dev.tensor(down)),
            hidden @ down.astype(np.float64).T,
        ),
    )

    # `temps` counts the buffers a kernel needed that are not operands. A fused
    # epilogue has none; `mlp` has one, and that one IS the round trip.
    for name, made in (
        ("matmul", matmul.plan(dev.tensor(x), dev.tensor(w))),
        (
            "linear_bias",
            linear_bias.plan(dev.tensor(x), dev.tensor(w), dev.tensor(bias)),
        ),
        ("layernorm", layernorm.plan(dev.tensor(x), dev.tensor(g), dev.tensor(b))),
        ("mlp", mlp.plan(dev.tensor(x), dev.tensor(up), dev.tensor(down))),
    ):
        print(
            f"{name:12s} stages={[s.unit for s in made.stages]} "
            f"temps={sorted(made.temps)}"
        )


if __name__ == "__main__":
    main()
