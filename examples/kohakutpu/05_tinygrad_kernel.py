"""LEVEL 5 meeting LEVEL 4: your own kernel, called from tinygrad.

`03_tinygrad.py` is the automatic path -- tinygrad writes the expression and the
matcher finds a kernel for it. This is the other direction: you write the kernel
and hand it to tinygrad, which is what you do when the matcher cannot see what
you meant, or when your kernel is better than the one it would pick.

`ktpugrad.bridge` is the whole seam. It works on ANY `@kernel` -- the library's
or yours -- and the four named hatches (`ktpugrad.softmax` and friends) are the
same function with their arguments already bound. `spread=` is there for
operands that arrive per channel; both of these are full shape, so it is unused.
"""

import ktpugrad
import numpy as np
from kohakuaccel.lang import dims, units
from kohakutpu.lang import kernel
from tinygrad.tensor import Tensor

from kohakutpu import lang as L

M, N = dims("M, N")
LOG2E = 1.4426950408889634

ktpugrad.install()  # without the seam, reading a KTPU tensor has no program


# ----------------------------------------------------------------- your kernel
# `part` is the row split; the compiler cannot pick it from the shape alone.
@kernel
def swiglu(x=L.In(..., M, N), g=L.In(..., M, N), y=L.Out(..., M, N), *, part=8192):
    """`x * sigmoid(g)` -- one vector pass, no intermediate in memory."""
    with units(x.parts(part)) as e:
        y[e] <<= x[e] * L.recip(L.exp2(g[e] * -LOG2E) + 1.0)


# ------------------------------------------------------------------ the bridge
# One Tensor per input port in signature order; knobs stay keywords.
swiglu_t = ktpugrad.bridge(swiglu)

print("bridged  :", swiglu_t.__name__, "--", swiglu_t.__doc__)
print("reads    :", [p.name for p in swiglu.signature.inputs])
print("knobs    :", swiglu.signature.knobs)

# --------------------------------------------------------------------- call it
# Tensors in, Tensor out, so it composes with tinygrad on both sides.
xa = np.random.randn(64, 128).astype(np.float16)
ga = np.random.randn(64, 128).astype(np.float16)

x = Tensor(xa, device=ktpugrad.NAME)
g = Tensor(ga, device=ktpugrad.NAME)

out = swiglu_t(x, g)  # your kernel, one dispatch
after = (out * 2).contiguous()  # tinygrad again, on the result

print("\nout      :", out.shape, out.dtype, "on", out.device)
print("composes :", after.shape, "-- tinygrad kept scheduling after the kernel")

ref = xa.astype(np.float32) / (1.0 + np.exp(-ga.astype(np.float32)))
got = out.numpy().astype(np.float32)
err = np.abs(got - ref).max() / np.abs(ref).max()
print(f"\nrel err  : {err:.2e}   (fp16 in, fp16 out)")

# ------------------------------------------------------- what the seam refuses
# The bridge checks what it can before the kernel does, and names the port.
try:
    swiglu_t(x)
except ValueError as exc:
    print("\nwrong count ->", exc)

try:
    swiglu_t(Tensor(xa.astype(np.float32), device=ktpugrad.NAME), g)
except ValueError as exc:
    print("wrong dtype ->", str(exc).split(";")[0])

try:
    ktpugrad.bridge(swiglu, spread=("bias",))
except ValueError as exc:
    print("bad spread  ->", exc)

print("\nBoth rungs are reachable and neither is a fallback: a kernel the")
print("matcher refuses still RAISES, and this is how you answer it yourself.")
