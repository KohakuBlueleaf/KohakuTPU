"""Writing tensor-level code: arrays live on the card, operators are kernels.

READ THIS FILE. This is the "near no control" end -- you say what to compute
and nothing about how. `02_kernel.py` is the other end, where you say all of it.

Two rules and that is the level:

* an operand is a DEVICE tensor, never a host array. `dev.tensor(...)` puts one
  there, `.numpy()` brings it back, and nothing crosses the link in between.
* every operator is one kernel, so a chain of them is a chain of passes. When
  that matters, reach for a fused kernel or write one.
"""

import numpy as np
from kohakuaccel.lang.kernel import KernelError

from kohakutpu import api, ops


def main() -> None:
    dev = api.script_device("sim")
    rng = np.random.default_rng(0)
    a = np.asarray(rng.normal(0, 1, (8, 64)), np.float16)
    b = np.asarray(rng.normal(0, 1, (8, 64)), np.float16)

    x, y = dev.tensor(a), dev.tensor(b)

    # Operators compose without reading anything back: each returns a tensor
    # still on the card, and only `.numpy()` costs a trip over the link.
    got = ops.mul(x, y)
    got = ops.sub(got, x)
    got = ops.mul(got, dev.tensor(np.full_like(a, 0.5)))

    af, bf = a.astype(np.float64), b.astype(np.float64)
    want = (af * bf - af) * 0.5
    err = np.abs(np.asarray(got.numpy()).astype(np.float64) - want).max()
    print("chain      ", "ok" if err / np.abs(want).max() < 5e-2 else "FAILED")

    # A host array is refused rather than uploaded behind your back: an
    # implicit copy per call is the kind of cost that hides.
    try:
        ops.mul(x, a)
        print("host array ", "FAILED (accepted)")
    except KernelError:
        print("host array ", "ok")

    # `api` is the same thing with the tiling chosen for you -- one name that
    # works at any row width. See `demos/` for what it picks and why.
    wide = dev.tensor(np.asarray(rng.normal(0, 1, (4, 1024)), np.float16))
    rows = np.asarray(api.softmax(wide).numpy()).astype(np.float64).sum(-1)
    print("api.softmax", "ok" if np.allclose(rows, 1.0, atol=2e-3) else "FAILED")


if __name__ == "__main__":
    main()
