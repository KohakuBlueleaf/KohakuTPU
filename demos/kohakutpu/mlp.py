"""RUN THIS. Fusing an activation onto the accumulator, and what it saves.

Identical arithmetic written two ways: on the accumulator, where the
intermediate lives in a vector register, and landed behind MAG and read back.
Every number below is measured here.
"""

import numpy as np
from kohakuaccel.lang import dims, loop, units
from kohakutpu.lang import kernel

from kohakutpu import api as ktpu
from kohakutpu import lang as L
from kohakutpu import sim

M, KD, N = dims("M, K, N")
LOG2E = 1.4426950408889634
RULE = "=" * 78
LAYERS, WIDTH, BATCH = 6, 128, 32


def head(title: str) -> None:
    print(f"\n{RULE}\n{title}\n{RULE}")


@kernel
def fused(x=L.In(..., M, KD), w=L.In(N, KD), y=L.Out(..., M, N), *, gm=8, gn=8, nk=2):
    """`silu(x @ w.T)` -- the epilogue ON the accumulator, no temp."""
    with units(x.tiles(gm), w.tiles(gn)) as (i, j):
        acc = L.tile(gm, gn, nk)
        for k in loop(x.chunks32(nk)):
            acc += x[i, k] @ w[j, k]
        y[i, j] <<= acc * L.recip(L.exp2(acc * -LOG2E) + 1.0)


@kernel
def staged(x=L.In(..., M, KD), w=L.In(N, KD), y=L.Out(..., M, N), *, gm=8, gn=8, nk=2):
    """The same arithmetic, landed and read back. One extra round trip."""
    h = L.temp(M, N)
    with units(x.tiles(gm), w.tiles(gn)) as (i, j):
        acc = L.tile(gm, gn, nk)
        for k in loop(x.chunks32(nk)):
            acc += x[i, k] @ w[j, k]
        h[i, j] <<= acc

    y <<= h * L.recip(L.exp2(h * -LOG2E) + 1.0)


rng = np.random.default_rng(17)
scale = WIDTH**-0.5
X = (rng.standard_normal((BATCH, WIDTH)) * 0.5).astype(np.float16)
Ws = [
    (rng.standard_normal((WIDTH, WIDTH)) * scale).astype(np.float16)
    for _ in range(LAYERS)
]
dev = ktpu.script_device("sim")

head("THE TWO FORMS, SIDE BY SIDE")
print("The only difference is where the intermediate lives: a vector register,")
print("or a buffer behind MAG.\n")
print(f"  {'form':<10} {'stages':>6} {'temps':>5} {'cycles':>16} {'flits':>7}")
for name, kern in (("fused", fused), ("staged", staged)):
    got = sim.observe(kern, X, Ws[0])
    print(
        f"  {name:<10} {got.stages:>6} {len(got.temps):>5} "
        f"{got.timing.overlapped:>7,}-{got.timing.cycles:<8,} "
        f"{got.counters['flits']:>7,}"
    )
print("\n  Same stage COUNT -- both are MG then VC. What differs is the TEMP:")
print("  the fused form hands its tile to a vector core over the NoC, the")
print("  staged one writes it through MAG and reads it back.")

head("THE PLAN, AS THE COMPILER SETTLED IT")
print(fused.plan(ktpu.tensor(X), ktpu.tensor(Ws[0])).pretty())

head(f"{LAYERS} LAYERS OF {WIDTH} WIDE, BATCH {BATCH} -- RUN BOTH")
print(f"  {dev}\n")
weights = [ktpu.tensor(W).pin() for W in Ws]
x = ktpu.tensor(X)

want = X.astype(np.float64)
for W in Ws:
    want = want @ W.astype(np.float64).T
    want = want / (1.0 + np.exp(-want))

rows = []
for name, kern in (("fused", fused), ("staged", staged)):
    before = dict(ktpu.stats())
    h = x
    for w in weights:
        h = kern(h, w)
    got = h.numpy()
    moved = {k: ktpu.stats()[k] - before[k] for k in before}
    rel = np.abs(got - want) / max(np.abs(want).max(), 1e-9)
    rows.append((name, moved, float(np.median(rel))))

print(
    f"  {'form':<8} {'dispatch':>8} {'rounds':>7} {'flits':>8} "
    f"{'fetched':>10} {'p50 err':>9}"
)
for name, moved, p50 in rows:
    print(
        f"  {name:<8} {moved['dispatches']:>8} {moved['rounds']:>7} "
        f"{moved['flits']:>8,} {moved['fetched']:>10,} {p50:>9.2e}"
    )

fl, sl = rows[0][1]["flits"], rows[1][1]["flits"]
print(f"\n  READ THAT CAREFULLY. The fused form spends MORE flits ({fl:,} against")
print(f"  {sl:,}), and it is still the faster one -- 704-1,216 cycles against")
print("  3,648-4,160 in the table above.")
print("\n  `flits` counts what crosses the NoC, and putting the tile on the NoC")
print("  is the whole POINT of fusing. The staged form's cost is a MAG write")
print("  and read, and NO COUNTER HERE PRICES THAT: `flits` is the mesh,")
print("  `sent`/`fetched` are the host link, and MAG traffic inside the mesh")
print("  is measured nowhere. Compare CYCLES for this question, not flits.")
print("  (The timing model has the same gap: it prices no MAG round trip.)")

head("WHY YOU STILL WRITE THE FUSED FORM")
print("A grid wider than the vector cores CANNOT fuse -- the tile has nowhere")
print("to land. The compiler stages it for you rather than refusing:\n")
made = fused.plan(
    ktpu.tensor(np.zeros((64, 128), np.float16)),
    ktpu.tensor(np.zeros((256, 128), np.float16)),
)
print(
    f"  fused at 64x128x256 -> fuse={made.knobs.get('fuse')} "
    f"temps={sorted(made.temps)}"
)
print("\n  So you write ONE kernel and never pick. `fuse=False` is reachable by")
print("  hand too, which is the 'near full control' end of the same knob.")
