"""Activations, each ONE pass. Composed out of ops the core has, never traced.

`sigmoid` is a helper rather than a kernel: nothing calls it alone, and as an
op it would cost a pass that `silu` and `gelu` fold into their own.
"""

from kohakuaccel.lang import units
from kohakutpu.lang import kernel

from kohakutpu import lang as L

#: log2(e). The core has `exp2` and no `exp`, so every exponent carries it.
LOG2E = 1.4426950408889634


def sigmoid(v: L.Value) -> L.Value:
    """``1 / (1 + 2**(-x log2 e))`` as one running result, no tree."""
    return L.recip(L.exp2(v * -LOG2E) + 1.0)


@kernel
def silu(x=L.In(...), y=L.Out(...), *, part=8192):
    """``x * sigmoid(x)``, at any rank."""
    with units(x.parts(part)) as e:
        y[e] <<= x[e] * sigmoid(x[e])


@kernel
def gelu(x=L.In(...), y=L.Out(...), *, part=8192):
    """The tanh-free GELU: ``x * sigmoid(1.702 x)``, at any rank."""
    # 1.702 and log2 e fold into one constant: a chain reads at most three
    # operands, since the core has eight descriptors and needs two per operand.
    with units(x.parts(part)) as e:
        y[e] <<= x[e] * L.recip(L.exp2(x[e] * (-1.702 * LOG2E)) + 1.0)


@kernel
def relu(x=L.In(...), y=L.Out(...), *, part=8192):
    """``max(x, 0)``, at any rank. ONE ALU word.

    Was ``(x + |x|) * 0.5`` -- three words -- while `VMAX`'s operand slot was
    still a guess. `vec_alu.v:144` settles it, and both forms fold one scalar,
    so the compose costs two extra words for nothing.
    """
    with units(x.parts(part)) as e:
        y[e] <<= L.maximum(x[e], 0.0)
