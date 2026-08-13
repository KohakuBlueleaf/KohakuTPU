"""MXFP7, as a MODEL only.

Nothing here is used to produce what the driver uploads. Software uploads FP16;
the quantiser in `src/kohakumas/mx_quant.v` converts it on the way out of MAG,
so MXFP7 never crosses the driver boundary. This module exists so tests can
predict what the hardware will produce and check it.


The format is a 7-bit signed integer significand per element, plus an E5M3
scale shared by a block of 32 along the reduction dimension:

    value(i,k) = q[i,k] * scale[i],   scale = 2**(E - SBIAS) * (1 + M/8)

The hardware splits that in two. The exponents add, as they always did:

    shift = (Ea) + (Eb) - anchor,   anchor = 2*SBIAS, cancelling both biases

and the mantissas multiply, which is the part E8M0 did not need:

    (1 + Ma/8)(1 + Mb/8) = (m8a * m8b) / 64,   m8 = 8 + M

mx_acu_fp.v multiplies the integer partial sum by m8a*m8b and takes the /64 off
the exponent, so the mantissa costs one multiply and no precision.

See docs/compute/matmul.md s3.
"""

import numpy as np

# ---------------------------------------------------------------------------
# THE BLOCK SCALE IS E5M3, NOT E8M0.
#
# A power-of-two scale can only land the block peak somewhere in [32,64) of the
# int7 range, so between 0 and 1 bit of the significand is unused and which it
# is depends on where the peak falls inside its binade. Three mantissa bits on
# the scale let the peak land at 63 every time. Measured on correlated
# operands: per-element relative error p50 0.54% -> 0.38%, p99 48% -> 23%.
#
#   scale = 2**(E - SBIAS) * (1 + M/8)      field = {E[4:0], M[2:0]}
#
# E5 because the output is FP16: FP16's normal range spans 30 binades and E5
# gives 31, so it just covers it. E4 gives 16 and does not. The extra three
# exponent bits an E8M0 field spends are wasted on a datapath that cannot
# express the range anyway.
#
# SBIAS = 20 because scale = peak/63 with an FP16 peak spans 2**-20 .. 2**10.
SEBITS = 5
SMBITS = 3
SBIAS = 20
ANCHOR = 2 * SBIAS  # cancels both biases in sa + sb - anchor

KBLOCK = 32
# mx_quant.v clamps the MAGNITUDE to 63 and then applies the sign, so the
# range is symmetric -- there is no -64.
QMIN, QMAX = -63, 63


# Reciprocal table, round(4096 * 8 / m8) for the eight scale mantissas. The
# quantiser divides by the scale, and a divider is not worth a cycle here when
# the divisor has exactly eight values. mx_quant.v holds the same table, and
# the two must agree bit for bit or the model stops predicting the hardware.
RECIP = {8: 4096, 9: 3641, 10: 3277, 11: 2979, 12: 2731, 13: 2520, 14: 2341, 15: 2185}
RECIP_SHIFT = 12


def _scale_field(peak_bits):
    """The E5M3 field for a block whose FP16 peak has these bits.

    Chooses the SMALLEST representable scale that keeps peak/scale <= 63, so
    the peak lands as near the top of the int7 range as the grid allows. It has
    to round UP: a scale rounded down makes the peak exceed 63 and clip, which
    damages the largest element in the block -- the one that matters most.
    """
    ep_f = (peak_bits >> 10) & 0x1F
    frac = peak_bits & 0x3FF
    sig_p = np.where(ep_f == 0, frac, (1 << 10) | frac)
    ep = np.where(ep_f == 0, 1, ep_f)

    # A subnormal peak has no implicit one, so renormalise it into [1024,2048)
    # before deriving the scale -- otherwise the mantissa arithmetic below is
    # reading a significand that is not in the range it assumes.
    for _ in range(11):
        sub = (sig_p != 0) & (sig_p < 1024)
        sig_p = np.where(sub, sig_p * 2, sig_p)
        ep = np.where(sub, ep - 1, ep)

    # ceil(sig_p / 126): mant = sig_p/1008 in [1.016, 2.031), so m8 in [9,17]
    m8 = -(-sig_p // 126)
    es = np.where(m8 >= 16, ep - 20, ep - 21)
    m8 = np.where(m8 == 17, 9, np.where(m8 == 16, 8, m8))

    # E5 spans 31 binades and the useful range of peak/63 is exactly FP16's
    # NORMAL range, so a block whose peak is itself subnormal wants a scale the
    # field cannot hold. Clamping degrades it gracefully -- the peak lands
    # below 63 and the block keeps fewer bits -- where letting the exponent
    # wrap would corrupt the block completely.
    es = np.clip(es, -SBIAS, (2**SEBITS - 1) - SBIAS)

    # an all-zero block has no peak to scale from; unit scale keeps the field
    # legal and every q is zero regardless
    zero = sig_p == 0
    return (
        np.where(zero, 0, es).astype(np.int64),
        np.where(zero, 8, m8).astype(np.int64),
    )


def quantise_fp16(x):
    """Quantise exactly as ``src/kohakumas/mx_quant.v`` does.

    This is the only quantiser that predicts the hardware, and it takes FP16
    because FP16 is what memory holds -- the value the circuit actually sees.

    Returns ``(q, es, m8)``: the signed int7 significands, the unbiased scale
    exponents, and the scale mantissas in eighths (8..15 meaning 1.0..1.875).
    """
    h = np.asarray(x).astype(np.float16)
    if h.shape[-1] % KBLOCK:
        raise ValueError(f"K={h.shape[-1]} is not a multiple of {KBLOCK}")

    bits = h.view(np.uint16).astype(np.int64)
    blocks = bits.reshape(*bits.shape[:-1], -1, KBLOCK)

    sign = (blocks >> 15) & 1
    e_f = (blocks >> 10) & 0x1F
    frac = blocks & 0x3FF
    # FP16 subnormals are NOT flushed. They have no implicit leading one and an
    # effective exponent of 1, and handling them is one mux. Flushing costs
    # every element of any block whose peak is below ~2e-3, because then most
    # of the block IS subnormal -- a cliff that looks like the format being bad
    # at small tensors rather than like a dropped case.
    sig = np.where(e_f == 0, frac, (1 << 10) | frac)
    e = np.where(e_f == 0, 1, e_f)

    # magnitude order IS numeric order on bits[14:0]: sign-magnitude with the
    # exponent above the mantissa, so the peak is a plain unsigned max
    peak_bits = (blocks & 0x7FFF).max(axis=-1)
    es, m8 = _scale_field(peak_bits)

    recip = np.zeros_like(m8)
    for k, v in RECIP.items():
        recip = np.where(m8 == k, v, recip)

    # q = round(x / scale) = round(sig * recip / 2**(25 + es + RECIP_SHIFT - e))
    prod = sig * recip[..., None]
    sh = 25 + RECIP_SHIFT + es[..., None] - e
    safe = np.clip(sh, 0, 62)
    mag = np.where(sh > 40, 0, (prod >> safe) + ((prod >> np.maximum(safe - 1, 0)) & 1))
    mag = np.minimum(mag, QMAX)

    q = np.where(sign == 1, -mag, mag)
    return q.reshape(h.shape), es, m8


def scale_value(es, m8):
    """The real number a scale field stands for: 2**es * (m8/8)."""
    return (m8 / 8.0) * (2.0**es)


def value_fp16(x):
    """What the hardware's operand actually is, after quantisation."""
    q, es, m8 = quantise_fp16(x)
    blocks = q.reshape(*q.shape[:-1], -1, KBLOCK).astype(np.float64)
    return (blocks * scale_value(es, m8)[..., None]).reshape(q.shape)


def model_matmul(a, bt):
    """``C = A @ B.T`` as the machine should compute it.

    Both operands are quantised the way MAG quantises them, then multiplied in
    exact arithmetic. The gap between this and FP64 is the cost of MXFP7; the
    gap between this and the hardware is the hardware's own error. Reporting
    only hardware-vs-FP64 adds the two together and calls the sum a tolerance,
    which is how a real defect hides inside an expected quantisation loss.
    """
    return value_fp16(a) @ value_fp16(bt).T


def fp16_to_float(bits: int) -> float:
    """Decode one FP16, matching the accumulator's emission (no subnormals)."""
    exp = (bits >> 10) & 0x1F
    if exp == 0:
        return 0.0
    m = 1.0 + (bits & 0x3FF) / 1024.0
    v = m * (2.0 ** (exp - 15))
    return -v if bits & 0x8000 else v
