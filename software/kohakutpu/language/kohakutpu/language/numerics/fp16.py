"""FP16 host writes saturate; reads name the hardware consumer (RUL.057)."""

import numpy as np

MAX = 65504.0


def write(x, *, preserve_infinity=True, flush_subnormals=False):
    """RNE with finite overflow saturated; vector packs preserve genuine infinity."""
    x = np.asarray(x, np.float64)
    out = np.clip(x, -MAX, MAX).astype(np.float16)
    if preserve_infinity:
        out = np.where(np.isinf(x), x, out).astype(np.float16)
    if flush_subnormals:
        out = np.where(np.abs(out) < 2.0**-14, np.copysign(0.0, out), out)
    return out.astype(np.float16)


def values(bits, *, consumer: str) -> np.ndarray:
    """Vector reads IEEE; cluster/SRU accumulator form flushes exponent 0,
    and treats exponent 31 as an ordinary binade."""
    b = np.asarray(bits, np.uint16)
    if consumer == "vector":
        return b.view(np.float16).astype(np.float64)
    if consumer not in ("cluster", "sru"):
        raise ValueError(f"no fp16 consumer {consumer}: vector, cluster or sru")
    exp = (b >> 10).astype(np.int64) & 0x1F
    v = (1.0 + (b & 0x3FF) / 1024.0) * np.exp2(exp - 15.0)
    v = np.where(exp == 0, 0.0, v)
    return np.where(b & 0x8000, -v, v)


__all__ = ["MAX", "values", "write"]
