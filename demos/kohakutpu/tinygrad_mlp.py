"""A two-layer MLP written as tinygrad tensor code, running on the card.

    python demos/kohakutpu/tinygrad_mlp.py
    python demos/kohakutpu/tinygrad_mlp.py --batch 32 --width 128 --out 64

`(x @ w1.T).silu() @ w2.T` and nothing else: no kernel is named and no tiling
chosen. The scheduler fuses layer one into `linear_silu` and layer two into
`matmul`, byte for byte what composing the library by hand produces.

The counters are the same ones every other demo prints, and they say what the
frontend costs as well as what it buys. Levels 5 and 4 only.
"""

import argparse
import time

import numpy as np

from kohakutpu import api as ktpu

WATCH = ("dispatches", "rounds", "flits", "sent", "fetched", "relayouts")


def costed(dev, run):
    """`(what the measured call moved, its result)`.

    `run` is called twice and only the second counted: a cold call compiles the
    kernels and uploads whatever scalars they folded.
    """
    run()
    before = {k: dev.counters[k] for k in WATCH}
    got = run()
    return {k: dev.counters[k] - before[k] for k in WATCH}, got


def report(label, cost):
    """Print one row of the cost table."""
    print(
        f"  {label:<22} {cost['dispatches']:>4} {cost['rounds']:>6} "
        f"{cost['flits']:>6} {cost['sent']:>9,} {cost['fetched']:>9,} "
        f"{cost['relayouts']:>6}"
    )


def boundary(ktpugrad, x, w):
    """Print where this frontend stops: matched, generated, or refused.

    `x` and `w` are KTPU tensors. This is the half of the story that decides
    whether to pick this frontend: a refusal RAISES and names the ops it saw,
    it never falls back to computing on the host.
    """

    def held(ast):
        """A matched library kernel, a generated chain, or None for neither."""
        got = ktpugrad.match_ast(ast)
        if isinstance(got, ktpugrad.Match):
            return got.kernel
        try:
            return f"chain x{len(ktpugrad.read_chain(ast).nodes)}"
        except ktpugrad.ChainRefused:
            return None

    print("\nwhere this frontend stops")
    print(f"  {'written':<24} {'kernels':>7}   dispatched, or refused having seen")
    for label, build in (
        ("(x @ w.T) + x", lambda: (x @ w.T) + x),
        ("(x @ w.T).relu()", lambda: (x @ w.T).relu()),
        ("((x @ w.T) + x).relu()", lambda: ((x @ w.T) + x).relu()),
        ("((x @ w.T) + x).gelu()", lambda: ((x @ w.T) + x).gelu()),
        ("(x @ w.T) * 0.125", lambda: (x @ w.T) * 0.125),
        ("x + x * (x @ w.T)", lambda: x + x * (x @ w.T)),
        ("(x @ w.T).softmax()", lambda: (x @ w.T).softmax()),
        ("x * 2 + 1", lambda: x * 2 + 1),
        ("x.relu()", lambda: x.relu()),
        ("x.sum(axis=-1)", lambda: x.sum(axis=-1)),
        ("x.softmax()", lambda: x.softmax()),
        ("x.sum(axis=0)", lambda: x.sum(axis=0)),
        ("x.sqrt()", lambda: x.sqrt()),
    ):
        # A FRESH EXPRESSION EACH TIME, and `capture` realises it: 0.13 has no
        # `Tensor.schedule`, so an ast exists only where lowering is handed one.
        try:
            asts = ktpugrad.capture(build().realize)
            got = ", ".join(n for n in map(held, asts) if n is not None)
        except ktpugrad.KTPUUnsupported as why:
            asts = []
            got = f"REFUSED: {str(why).rsplit('holds ', 1)[-1]}"
        print(f"  {label:<24} {len(asts):>7}   {got}")

    # `flash_attention` needs `block == Dv` and whole blocks, so the head is
    # square whatever the MLP's shape is.
    from tinygrad import Tensor

    sq = Tensor(np.zeros((64, 64), np.float16) + 0.25, device=ktpugrad.NAME)
    print("\n  and the escape hatch, for the kernels the scheduler splits")
    for label, fn in (
        ("ktpugrad.softmax(x)", lambda: ktpugrad.softmax(x)),
        ("ktpugrad.rmsnorm(x, w)", lambda: ktpugrad.rmsnorm(x, x)),
        ("ktpugrad.attention(q,k,v)", lambda: ktpugrad.attention(sq, sq, sq)),
    ):
        print(f"  {label:<24} {'':>7}   ran, shape {fn().numpy().shape}")


def main() -> int:
    """Run the MLP through tinygrad, grade it, and print what it cost."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=32)
    ap.add_argument("--width", type=int, default=128)
    ap.add_argument("--out", type=int, default=64)
    ktpu.add_device_flag(ap)
    args = ap.parse_args()

    # BEFORE the first device-touching line. A tinygrad tensor on KTPU opens a
    # device through `api.device()`, so a guard below first use is decoration.
    dev = ktpu.script_device(args.device)
    print(dev)
    print(f"  {args.batch}x{args.width} -> {args.width} -> {args.out}")

    import ktpugrad
    from kohakutpu.lang.errors import LangError
    from tinygrad import Tensor

    from kohakutpu import kernels as K
    from kohakutpu import ops as O

    ktpugrad.install()

    rng = np.random.default_rng(17)
    scale = args.width**-0.5
    X = (rng.standard_normal((args.batch, args.width)) * 0.5).astype(np.float16)
    W1 = (rng.standard_normal((args.width, args.width)) * scale).astype(np.float16)
    W2 = (rng.standard_normal((args.out, args.width)) * scale).astype(np.float16)

    def through_tinygrad():
        """The whole network as one tensor expression, no kernel named."""
        x = Tensor(X, device=ktpugrad.NAME)
        w1 = Tensor(W1, device=ktpugrad.NAME)
        w2 = Tensor(W2, device=ktpugrad.NAME)
        return ((x @ w1.T).silu() @ w2.T).numpy()

    def by_hand():
        """The same two kernels, composed out of the library directly.

        The tiling comes from `ktpugrad.plan`, which is what the frontend asks
        too. At `--width 256` the default `gm`/`gn` put 16 tiles into 4 vector
        cores and `linear_silu` refuses; choosing knobs is a level-4 decision
        either rung has to make, and the demo would not otherwise reach parity.
        """
        x, w1, w2 = ktpu.tensor(X), ktpu.tensor(W1), ktpu.tensor(W2)
        fused, knobs = ktpugrad.plan(
            K.linear_silu, args.batch, args.width, dev.machine.count("VC")
        )
        return O.matmul(fused(x, w1, **knobs), w2).numpy()

    t0 = time.time()
    try:
        cost, got = costed(dev, through_tinygrad)
    except LangError as why:
        print(f"\nLangError: {why}")
        print(
            "\n  This should no longer be reachable. `ktpugrad.tiling.plan` reads\n"
            "  gm/gn off the shape and the machine's vector-core count, and falls\n"
            "  back to the separated twin past the fused grid's ceiling. A shape\n"
            "  arriving here is one the planner does not cover."
        )
        print("\nFAIL")
        return 1
    wall = time.time() - t0
    hand_cost, hand = costed(dev, by_hand)

    head = (
        f"{'disp':>4} {'rounds':>6} {'flits':>6} {'sent':>9} {'back':>9} {'relay':>6}"
    )
    print(f"\n  {'path':<22} {head}")
    report("tinygrad", cost)
    report("library by hand", hand_cost)
    print(f"  identical bytes: {np.array_equal(got, hand)}")
    print(f"  wall {wall:.2f}s for the tinygrad path")

    want = X.astype(np.float64) @ W1.astype(np.float64).T
    want = want / (1.0 + np.exp(-want))
    want = want @ W2.astype(np.float64).T
    rel = np.abs(got - want) / max(np.abs(want).max(), 1e-9)
    print(f"  vs fp64: p50 {np.median(rel):.3e}  max {rel.max():.3e}")

    print(
        "\n  Same dispatches, same rounds, same flits, same bytes: the scheduler\n"
        "  fused layer one into `linear_silu` on its own. What it costs is in\n"
        "  `sent` and `fetched` -- every operand and every result crosses the\n"
        "  link twice, once into a flat tinygrad buffer and once into the device\n"
        "  layout a kernel operand is in.\n\n"
        "  The 0 relayouts is NOT a win over the hand path's 1. That counter\n"
        "  counts compiler-inserted conversions only; the tinygrad path pays a\n"
        "  full host round trip per kernel instead, which is what `fetched` says."
    )

    boundary(
        ktpugrad,
        Tensor(X, device=ktpugrad.NAME),
        Tensor(W1, device=ktpugrad.NAME),
    )
    print(
        "\n  A refusal raises and names the ops it saw. It does not compute on\n"
        "  the host and report device numbers. So this frontend runs a matmul,\n"
        "  and a matmul with one fused epilogue -- silu, relu, gelu, a bias, a\n"
        "  scalar scale, a gated residual, or an activation over a bias.\n"
        "\n  An elementwise chain with no matmul in it is GENERATED rather than\n"
        "  matched: the ast is translated onto the same `Value` algebra a\n"
        "  @kernel body is written in, and runs as ONE vector program. A matched\n"
        "  kernel always wins -- those are hand-tuned and witnessed.\n"
        "\n  A REDUCTION along the last axis generates too, so softmax and the\n"
        "  norms now RUN whole. What did not change is the SPLIT: tinygrad gives\n"
        "  a reduce a kernel of its own, so they are still 2 to 3 kernels against\n"
        "  1 of ours, and re-fusing across those boundaries is a pass over a\n"
        "  SCHEDULE, not a matcher over a tree. The ESCAPE HATCH is still how you\n"
        "  reach one of ours -- and attention still NEEDS it, since its score is\n"
        "  a batched contraction and a rank-3 reduce is refused, never guessed."
    )

    ok = np.array_equal(got, hand) and np.median(rel) < 5e-2 and rel.max() < 3e-1
    print("\nPASS" if ok else "\nFAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
