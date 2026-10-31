"""Exact logical-size entry point for masked dense DSL attention."""

import numpy as np

from kohakutpu.application.api.tensor import Buf, Tensor
from kohakutpu.application.kernels import attention as A
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.runtime import forms as F
from kohakutpu.application.runtime.storage import Storage
from kohakutpu.language import dsl as kt


def attention(q: Tensor, k: Tensor, vt: Tensor, *, precise_scores=False) -> Tensor:
    """q [H,S,D], k [H,T,D], vt [H,D,T]; independent query/key tails.

    Device-side packing creates bounded zero-padded forms. Only the logical
    query rows are copied out; absent keys are masked before both reductions.
    """
    h, s, d = q.shape
    if k.ndim != 3 or vt.ndim != 3:
        raise ValueError("attention operands have three axes")
    t = k.shape[1]
    if k.shape != (h, t, d) or vt.shape != (h, d, t) or min(h, s, t) < 1:
        raise ValueError("attention head/key dimensions disagree")
    if d % 64 or not 64 <= d <= 256:
        raise ValueError("attention head width must be 64, 128, 192, or 256")
    for x in (q, k, vt):
        x.realize()
    dev = q.st.dev
    if any(x.st.dev is not dev for x in (k, vt)):
        raise ValueError("attention operands must belong to the same device")
    sp, tp = -(-s // 64) * 64, -(-t // 64) * 64
    params = (
        (q, s, d, sp, d, "a", 16),
        (k, t, d, tp, d, "b", 16),
        (vt, d, t, d, tp, "b", d // 4),
    )
    binds = {}
    for name, (x, r, c, rp, cp, side, g) in zip(("q", "k", "vt"), params):
        binds[name] = F.packed(
            dev,
            x.st,
            x.dtype,
            Operand("full", pitch=c, rows=h * r),
            r,
            c,
            rp,
            cp,
            side,
            g,
            2,
            h,
        )
    mask = np.broadcast_to(np.arange(tp) < t, (h * sp, tp))
    tiled_mask = mask.reshape(h * sp // 64, 16, 4, tp // 64, 16, 4).transpose(
        0, 3, 1, 4, 2, 5
    )
    binds["valid"] = F.table(dev, tiled_mask.copy().reshape(-1))
    given = {
        "q": kt.TensorSpec.make((h * sp, d)),
        "k": kt.TensorSpec.make((h * tp, d)),
        "vt": kt.TensorSpec.make((h * d, tp)),
        "valid": kt.TensorSpec.make((h * sp, tp)),
    }
    padded = Storage(dev, 2 * h * sp * d)
    out = Storage(dev, 2 * h * s * d)
    temporaries = []
    try:
        if precise_scores:
            dp = -(-d // 128) * 128
            for name, x, r, rp in (("q", q, s, sp), ("k", k, t, tp)):
                repeat = 16 if name == "q" else 1
                tmp = Storage(dev, 2 * h * rp * repeat * dp)
                temporaries.append(tmp)
                F.relayout(
                    dev,
                    F.f16(dev, x.st, x.dtype, x.size),
                    x.size,
                    ((h, r * d), (r, d), (repeat, 0), (d, 1)),
                    0,
                    ((h, rp * repeat * dp), (r, repeat * dp), (repeat, dp), (d, 1)),
                    h * rp * repeat * dp,
                    tmp.addr,
                )
                binds[name] = tmp.addr
                given[name] = kt.TensorSpec.make((h * rp * repeat, dp))
        binds["o"] = padded.addr
        F.launch(
            dev, A.kernel(h, sp, tp, d, precise_scores=precise_scores), given, binds
        )
        F.relayout(
            dev,
            padded.addr,
            h * sp * d,
            ((h, sp * d), (s * d, 1)),
            0,
            ((h, s * d), (s * d, 1)),
            h * s * d,
            out.addr,
        )
    except Exception:
        out.free()
        raise
    finally:
        for tmp in temporaries:
            tmp.free()
        padded.free()
    return Tensor((h, s, d), buf=Buf(out, np.float16))
