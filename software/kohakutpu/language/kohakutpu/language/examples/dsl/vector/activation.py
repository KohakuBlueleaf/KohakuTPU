"""``y = act(x * scale + shift)`` over [R, W] rows: the activation and the
affine prologue are trace-time constants (an identity prologue traces to
nothing)."""

import kohakutpu.language.dsl as kt
from kohakutpu.language.examples.dsl.acts import act as pick
from kohakutpu.language.examples.dsl.vector.rows import per_core


@kt.kernel
def activation(
    x=kt.In("R", "W"),
    y=kt.Out("R", "W"),
    *,
    act="silu",
    scale=1.0,
    shift=0.0,
    rows=kt.auto(16, 8, 4),
):
    R, W = x.shape
    per = per_core(R, W, rows)
    f = pick(act)
    for c in kt.par(kt.machine().vc, on=kt.vc):
        for t in kt.range(per // rows):
            r = c * per + t * rows
            v = kt.load(x[r : r + rows, :], gt4=True)
            if scale != 1.0:  # a Python `if` on a constant: decided while tracing
                v = v * scale
            if shift != 0.0:
                v = v + shift
            kt.store(y[r : r + rows, :], f(v))


__all__ = ["activation"]
