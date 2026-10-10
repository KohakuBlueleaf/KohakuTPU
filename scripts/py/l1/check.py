"""Grading a kernel's output in detail, not by one max-error number.

Against the reference computed from what the hardware really computes on
(MXFP7-quantised operands, exact arithmetic), a correct result differs by
accumulator and fp16 rounding only. The worst elements are listed with their
index, so a localised fault -- a band edge, a halo, a tile seam -- shows as a
cluster of indices instead of hiding under the average.
"""

import numpy as np


def grade(name: str, got, model, exact=None, tol=4e-3, show=5) -> bool:
    """Print and return whether `got` matches `model` within `tol` of its scale.

    `model` is the reference on quantised operands; `exact` the fp64 one on the
    originals, reported for the MXFP7 cost only.
    """
    got = np.asarray(got, np.float64)
    model = np.asarray(model, np.float64)
    scale = np.abs(model).max() or 1.0
    diff = np.abs(got - model) / scale
    bad = diff > tol
    worst = np.argsort(diff, axis=None)[::-1][:show]
    idx = [tuple(int(v) for v in np.unravel_index(i, got.shape)) for i in worst]
    line = (
        f"  {name}: vs model max {diff.max():.2e} mean {diff.mean():.2e} "
        f"bad {int(bad.sum())}/{diff.size}  nonfinite {int((~np.isfinite(got)).sum())}"
    )
    if exact is not None:
        e = np.abs(got - exact).max() / (np.abs(exact).max() or 1.0)
        line += f"  vs fp64 {e:.2e}"
    print(line)
    print(
        "    worst "
        + ", ".join(f"{i}: got {got[i]:.4g} want {model[i]:.4g}" for i in idx)
    )
    if bad.any():
        where = np.argwhere(bad)
        print(
            "    bad index range per axis: "
            + " ".join(
                f"[{where[:, a].min()}..{where[:, a].max()}]"
                for a in range(where.shape[1])
            )
        )
    return not bad.any()
