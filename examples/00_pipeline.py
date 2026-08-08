"""The whole chain: a kernel you write, down to instructions the machine runs.

    python examples/00_pipeline.py

Read this first. Each stage prints what it produced, so you can see exactly what
the compiler decided and where.
"""

import ktpu.dsl as D
from ktpu.codegen import codegen, cost_of
from ktpu.ir import FP16
from ktpu.passes import fusion_report, lower
from ktpu.target import VU13P_8CU

M, K, N = 256, 1024, 256


def linear_gelu(x, w, bias):
    """One layer: `gelu(x @ w + bias)`."""
    return D.gelu(x @ w + bias)


print("=" * 70)
print("STAGE 0   what you write")
print("=" * 70)
print("""
    def linear_gelu(x, w, bias):
        return D.gelu(x @ w + bias)
""")

print("=" * 70)
print("STAGE 1   graph IR -- tensors and ops, no machine")
print("=" * 70)
graph = D.trace(
    linear_gelu,
    D.TensorSpec((M, K), FP16),
    D.TensorSpec((K, N), FP16),
    D.TensorSpec((N,), FP16),
)
print(graph)

print()
print("=" * 70)
print("STAGE 2   schedule IR -- engines, tiles, grid, fusion")
print("=" * 70)
sched = lower(graph, VU13P_8CU)
print(sched)
print()
for line in fusion_report(sched):
    print("  " + line)

print()
print("=" * 70)
print("STAGE 3   target program -- instructions with resolved addresses")
print("=" * 70)
prog = codegen(sched, M, K, N, VU13P_8CU)
print("memory map:")
print(prog.memory)
print()
print(prog.listing(limit=14))

print()
print()
for name, (n_inst, n_nodes) in prog.occupancy().items():
    have = {"matmul": VU13P_8CU.clusters, "vector": VU13P_8CU.vector_cores}.get(name, 1)
    print(f"  {name:<7} {n_inst:>4} instructions across {n_nodes} of {have} cores")

print()
print("=" * 70)
print("STAGE 4   what it costs -- counted from the stream, not estimated")
print("=" * 70)
print(cost_of(prog, sched, VU13P_8CU))
print("""
`overhead` is the number to watch: DRAM bytes moved per flop the KERNEL asked
for. The flop count does not change when the schedule does, so anything that
raises this bought nothing.""")
