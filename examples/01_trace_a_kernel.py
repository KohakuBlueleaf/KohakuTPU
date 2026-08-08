"""Write a kernel in the DSL and look at the graph it produces.

    python examples/01_trace_a_kernel.py

Shows tracing, compile-time vs runtime control flow, and that shape errors are
reported at the line that caused them.
"""

import ktpu.dsl as D
from ktpu.ir import FP16, FP32

X = D.TensorSpec((256, 1024), FP16)
G = D.TensorSpec((1024,), FP16)


def rmsnorm(x, g, eps: float = 1e-5):
    ms = (x * x).mean(axis=-1, keepdim=True)
    return x * D.rsqrt(ms + eps) * g


print("=== rmsnorm ===")
print(D.trace(rmsnorm, X, G))

print("\n=== sigmoid is four ops, the depth-4 chain the ISA is sized for ===")
print([o.kind.value for o in D.trace(D.sigmoid, X).ops if o.kind.value != "input"])

print("\n=== compile-time control flow unrolls, and specialises the cache ===")


@D.kernel
def scaled(x, *, n: int):
    for _ in range(n):
        x = x * 1.5
    return x


for n in (2, 4):
    g = scaled(X, n=n)
    print(f"  n={n}: {sum(o.kind.value == 'mul' for o in g.ops)} mul ops")

print("\n=== runtime control flow is refused, not guessed ===")


def bad(x):
    if x[0, 0] > 0:
        return x
    return -x


try:
    D.trace(bad, X)
except D.TracerControlFlowError as e:
    print("  " + str(e).replace("\n", "\n  "))

print("\n=== mixed float formats are ordinary ===")
mixed = D.trace(lambda a, b: a + b, X, D.TensorSpec((256, 1024), FP32))
print(f"  fp16 + fp32 -> {mixed.outputs[0].dtype}")

print("\n=== a shape error names the line that caused it ===")
try:
    D.trace(lambda a, b: a @ b, X, D.TensorSpec((512, 256), FP16))
except D.DSLError as e:
    print(f"  {type(e).__name__}: {str(e).splitlines()[0]}")
