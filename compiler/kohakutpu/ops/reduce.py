"""Row reductions: `REDUCE_AXIS ADD` and `REDUCE_AXIS MAX`, broadcast back.

ONE instance whatever the elementwise ops split into. `VRED` folds a whole row
in a pass, so a finer grid would fold two rows into one answer -- which is why
these state `units(1)` rather than a `part`.
"""

from kohakuaccel.lang import dims, units
from kohakutpu.lang import kernel

from kohakutpu import lang as L

M, N = dims("M, N")


@kernel
def row_sum(x=L.In(..., M, N), y=L.Out(..., M, N)):
    """The sum of each row, broadcast back across it."""
    with units(1) as e:
        y[e] <<= L.row_sum(x[e])


@kernel
def row_max(x=L.In(..., M, N), y=L.Out(..., M, N)):
    """The maximum of each row, broadcast back across it."""
    with units(1) as e:
        y[e] <<= L.row_max(x[e])
