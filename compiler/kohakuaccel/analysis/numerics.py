"""How far a result is from a reference, in the figures every numerics report
prints.

`max_rel` is the largest error over the largest reference magnitude, `rel_l2`
the error's norm over the reference's; `rel_p99` the 99th percentile of the
element-wise relative error over the elements whose magnitude is at least 1% of
the largest (an element near zero has no meaningful relative error).
"""

import numpy as np

FIELDS = ("max_abs", "mean_abs", "max_rel", "rel_l2", "rel_p99")


def errors(got, want) -> dict:
    got = np.asarray(got, np.float64)
    want = np.asarray(want, np.float64)
    if got.shape != want.shape:
        raise ValueError(f"shapes {got.shape} and {want.shape}")
    d = np.abs(got - want)
    mag = np.abs(want)
    peak = float(mag.max()) or 1.0
    big = mag >= 0.01 * peak
    return {
        "max_abs": float(d.max()),
        "mean_abs": float(d.mean()),
        "max_rel": float(d.max()) / peak,
        "rel_l2": float(np.linalg.norm(d) / (np.linalg.norm(want) or 1.0)),
        "rel_p99": float(np.percentile(d[big] / mag[big], 99)) if big.any() else 0.0,
    }


__all__ = ["FIELDS", "errors"]
