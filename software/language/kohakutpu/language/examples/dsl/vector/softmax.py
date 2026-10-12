"""Row softmax in one pass over each band: the row max, the shifted base-2
exponential, the row sum and the scale never leave the registers.
`temperature` divides the logits (folded into the log2(e) factor)."""

import kohakutpu.language.dsl as kt
from kohakutpu.language.examples.dsl.vector.rows import per_core


@kt.kernel
def softmax(
    x=kt.In("R", "W"), y=kt.Out("R", "W"), *, temperature=1.0, rows=kt.auto(16, 8, 4)
):
    R, W = x.shape
    per = per_core(R, W, rows)
    k = kt.LOG2E / temperature
    for c in kt.par(kt.machine().vc, on=kt.vc):
        for t in kt.range(per // rows):
            r = c * per + t * rows
            s = kt.load(x[r : r + rows, :], gt4=True) * k
            p = kt.exp2(s - kt.rmax(s))
            kt.store(y[r : r + rows, :], p * kt.recip(kt.rsum(p)))


__all__ = ["softmax"]
