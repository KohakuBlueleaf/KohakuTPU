"""Run a kernel on the simulated machine and check the numbers.

    python examples/04_run_it_and_check_it.py

`00_pipeline.py` shows what the compiler DECIDES. This one runs what it decided
and compares the answer against the reference, which is the loop you actually
want while writing a kernel: change the maths, run it, see the error move.

Nothing here needs RTL. The level-3 simulator executes the same instruction
stream the machine gets -- same addresses, same L1 spans, same loop counts,
same operand bindings.
"""

import numpy as np

import ktpu.dsl as D
from ktpu.codegen import codegen
from ktpu.interp import run_one
from ktpu.interp.mesh import Mesh, MeshFault
from ktpu.ir import FP16
from ktpu.passes import lower
from ktpu.target import VU13P_8CU as TARGET

M, K, N = 64, 128, 64


def build(fn, *specs, fold=True):
    """Trace `fn`, lower it, and generate a program. Returns (graph, sched, prog)."""
    graph = D.trace(fn, *specs)
    sched = lower(graph, TARGET, fold_epilogue=fold)
    return graph, sched, codegen(sched, M, K, N, TARGET)


def execute(graph, sched, prog, x, w, *rest):
    """Upload the operands, run, and return what came back as an M x N array.

    `x` and `w` are the GEMM operands and go through the packer, which lays them
    out tile-major in L1-entry order. Anything after them -- a bias, a scale --
    is bound by name, the way the host would hand it to the machine.
    """
    mesh = Mesh(prog, TARGET)
    mesh.upload(sched.bands[0], x, w)
    for v, val in zip(graph.inputs[2:], rest, strict=True):
        mesh.bind(graph.producer(v).attrs["name"], val)
    mesh.run()
    return mesh.result(), mesh


def report(name, got, want, mesh):
    err = np.abs(got - want).max()
    rel = err / max(1e-30, np.abs(want).max())
    print(f"  {name:<22} max abs err {err:9.2e}   relative {rel:8.2e}", end="")
    print(f"   clamped {mesh.saturated}" if mesh.saturated else "")


# ---------------------------------------------------------------------------

print("=" * 72)
print("1.  a kernel, run and checked")
print("=" * 72)


def linear_gelu(x, w, bias):
    return D.gelu(x @ w + bias)


rng = np.random.default_rng(0)
x = rng.standard_normal((M, K)) * 0.2
w = rng.standard_normal((K, N)) * 0.2
bias = rng.standard_normal(N) * 0.1

graph, sched, prog = build(
    linear_gelu,
    D.TensorSpec((M, K), FP16),
    D.TensorSpec((K, N), FP16),
    D.TensorSpec((N,), FP16),
)
got, mesh = execute(graph, sched, prog, x, w, bias)
want = run_one(graph, x, w, bias)

print(f"\n  {len(prog.insts)} instructions over {len(prog.rounds)} rounds")
for engine, (n, nodes) in prog.occupancy().items():
    print(f"    {engine:<7} {n:>4} instructions on {nodes} cores")
print()
report("gelu(x @ w + bias)", got, want, mesh)
print("""
  The error is E8M15 rounding accumulated over 16 chained ops plus the FP16
  store, not a mistake. If it were a mistake it would be O(1), not O(1e-3).
""")

# ---------------------------------------------------------------------------

print("=" * 72)
print("2.  what epilogue folding is actually for")
print("=" * 72)
print("""
  Folding does NOT save the trip through DRAM -- nothing moves a tile from a
  cluster to a vector core. What it saves is the CONVERSION: a folded matmul
  drains ACC24, so the intermediate is rounded and clamped once, at the end,
  instead of twice.

  That only shows up when the intermediate leaves FP16's range. Here every
  matmul output is 40*40*128 = 204,800, and FP16 stops at 65,504:
""")


def scaled(x, w):
    return (x @ w) * 0.001


big_x = np.full((M, K), 40.0)
big_w = np.full((K, N), 40.0)
specs = (D.TensorSpec((M, K), FP16), D.TensorSpec((K, N), FP16))

for label, fold in (("folded  (acc24)", True), ("not folded (fp16)", False)):
    g, s, p = build(scaled, *specs, fold=fold)
    out, m = execute(g, s, p, big_x, big_w)
    report(label, out, run_one(g, big_x, big_w), m)

print(f"""
  The true answer is {(big_x @ big_w).max() * 0.001:.1f}. Unfolded, the matmul
  output is clamped to 65,504 BEFORE the epilogue scales it, so the answer comes
  back as 65.5 -- and nothing raises. `mesh.saturated` counts it because a
  silent clamp is the failure this machine is most able to hide (task #49).
""")

# ---------------------------------------------------------------------------

print("=" * 72)
print("3.  where a mistake shows up")
print("=" * 72)
print("""
  The simulator faults rather than returning a plausible number. Reading L1
  that was never filled, an address outside every region, an operand nothing is
  bound to -- each is an exception with the instruction that caused it.
""")

g, s, p = build(
    linear_gelu,
    D.TensorSpec((M, K), FP16),
    D.TensorSpec((K, N), FP16),
    D.TensorSpec((N,), FP16),
)
broken = Mesh(p, TARGET)
broken.upload(s.bands[0], x, w)
try:
    broken.run()
except MeshFault as exc:
    print(f"    forgot to bind the bias ->  {type(exc).__name__}: {exc}")

print("""
  Which is the point: a wrong ANSWER is expensive to find, a crash is not.
""")
