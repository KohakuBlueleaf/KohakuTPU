"""Compile-time routing: a mixture of experts, end to end.

    python examples/06_mixture_of_experts.py

`05_attention.py` is about carrying state through a loop. This one is
about the other thing a Python-traced DSL gives you for free: routing decided
when the kernel is written, not when it runs.

Every expert is evaluated and weighted by its gate, so the `for e in ...` loop
unrolls into E independent feed-forward networks plus a weighted sum. Top-k
sparsity is deliberately NOT here -- choosing experts per token is a gather with
computed indices, which this machine does not do. Route and permute on the host,
then call this on the grouped tokens.
"""

import numpy as np

import ktpu.dsl as D
from ktpu.codegen import codegen, cost_of
from ktpu.dsl.library import silu, softmax
from ktpu.interp import run_one
from ktpu.interp.mesh import Mesh
from ktpu.ir import FP16
from ktpu.passes import lower
from ktpu.target import VU13P_8CU as TARGET

T_LEN, C, N_EXP = 64, 64, 8


def moe_dense(x, wg, w1, w2):
    """Every expert evaluated, weighted by its gate. `x` is (T, C), `wg` is
    (C, E), `w1`/`w2` are lists of E weights. Returns (T, C).

    Top-k sparsity is deliberately NOT here: choosing experts per token is a
    gather with computed indices, which this machine does not do. Route and
    permute on the host, then call this on the grouped tokens.
    """
    gate = softmax(x @ wg)  # (T, E)
    out = None
    for e, (a, b) in enumerate(zip(w1, w2, strict=True)):
        y = silu(x @ a) @ b  # this expert's FFN
        weighted = y * gate[:, e : e + 1]  # ONE COLUMN of the gate
        out = weighted if out is None else out + weighted
    return out


def rule(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def spec(*s):
    return D.TensorSpec(s, FP16)


# ---------------------------------------------------------------------------
rule("1.  the kernel")
print("""
  The kernel is at the top of this file -- the code that actually runs below.

  Two things here are worth more than they look.

  `w1` and `w2` are ordinary Python LISTS. The tracer walks them at trace time,
  so the expert count is structural: it changes the graph, not a runtime index.
  That is the whole of "compile-time routing".

  `gate[:, e:e+1]` is a single column -- ONE VALUE PER ROW. It does not become
  a materialised (T, C) operand; it lowers to a broadcast read at stride E.
  Section 3 shows that in the schedule.""")

# ---------------------------------------------------------------------------
rule("2.  trace and lower")

graph = D.trace(
    moe_dense,
    spec(T_LEN, C),
    spec(C, N_EXP),
    [spec(C, C)] * N_EXP,
    [spec(C, C)] * N_EXP,
)
sched = lower(graph, TARGET)
mm = [b for b in sched.bands if b.engine.value == "matmul"]
print(f"""
  {N_EXP} experts -> {len(graph.ops)} ops, {len(sched.bands)} bands,
  {len(mm)} matmuls = 1 gate + 2 per expert.

  The gate matmul is {T_LEN} x {C} x {N_EXP}; the experts are {T_LEN} x {C} x {C}.
  DIFFERENT SHAPES in one graph, which matters more than it sounds:""")
for b in mm[:3]:
    a = b.ops[0].attrs
    print(f"    {b.name:4} {a['m']} x {a['k']} x {a['n']}   tile {b.tile}")

print("""
  `x` is the A operand of the gate AND of every expert. An operand is packed
  into L1-entry order FOR A TILE, and a region holds one layout -- so those
  matmuls are not free to choose their blocking independently. A pass at level 2
  makes them agree; if they could not, codegen raises rather than emitting a
  program that reads x laid out for the wrong matmul.""")

strided = [
    (o.ins, o.strides)
    for b in sched.bands
    for o in b.ops
    if any(s != 1 for s in o.strides or ())
]
print(f"""
  And the gate column, as promised -- stride {strided[0][1][1] if strided else "?"}, not a copy:
    {strided[0] if strided else "none"}""")

# ---------------------------------------------------------------------------
rule("3.  run it")

prog = codegen(sched, T_LEN, C, C, TARGET)
rng = np.random.default_rng(0)
x = rng.standard_normal((T_LEN, C)) * 0.3
wg = rng.standard_normal((C, N_EXP)) * 0.3
w1 = [rng.standard_normal((C, C)) * 0.3 for _ in range(N_EXP)]
w2 = [rng.standard_normal((C, C)) * 0.3 for _ in range(N_EXP)]
arrays = [x, wg, *w1, *w2]

mesh = Mesh(prog, TARGET)
mesh.upload(sched.bands[0], x, wg)
for value, arr in list(zip(graph.inputs, arrays, strict=True))[2:]:
    mesh.bind(graph.producer(value).attrs["name"], arr)
mesh.run()

got, want = mesh.result(), run_one(graph, *arrays)
rel = np.abs(got - want).max() / np.abs(want).max()
print(f"""
  relative error   {rel:.2e}       {"OK" if rel < 5e-3 else "FAIL"}
""")
print(cost_of(prog, sched, TARGET))

# ---------------------------------------------------------------------------
rule("4.  a warning about test shapes")
print(f"""
  This example uses E = {N_EXP} on purpose.

  A sub-tile is 4 x 4, so a (T, E) gate laid out in sub-tile order is the SAME
  as row-major exactly when E == 4. At E = 4 an incorrect index into the gate
  gives the right answer; at E = 8 it does not. That bug lived here, passed a
  green suite, and was invisible until the expert count moved.

  If a test shape makes two different quantities equal -- E and the lane count,
  M and N, the tile and the tensor -- it cannot distinguish correct from
  coincidental. Parametrise past it.""")
