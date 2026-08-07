"""Reference matmuls in other numeric formats, all accumulating in FP32.

These exist so a hardware result can be compared against something other than
FP64. FP64 answers "what is the true value"; these answer "what would the same
matmul have cost in a format we could have chosen instead", which is the
question that decides whether MXFP7 is the right format.

Every one of them accumulates in FP32 so the ONLY difference between them is
the operand format. Changing two things at once and comparing the results
tells you nothing about either.
"""

import numpy as np

KBLOCK = 32


# OCP MX v1.0 element formats. emax and max are SPELLED OUT rather than derived
# from the exponent width, because the reserved-encoding rule differs per format
# and a single formula gets four of these five wrong:
#
#   E5M2  is IEEE-like: the all-ones exponent is inf/NaN, so emax = 15.
#   E4M3  reserves only S.1111.111 as NaN, so exponent 1111 IS usable, emax = 8
#         and max = 448 -- not 240, which is what "reserve all-ones" would give.
#   FP6/FP4 have NO inf or NaN encodings at all, so every exponent code is
#         usable and emax = (2^e - 1) - bias.
#
# Getting emax wrong does not look like an error: the block scale simply
# normalises the peak one binade low, every value stays finite, and the format
# just quietly measures worse than it is.
#                 ebits mbits  bias  emax   max
ELEM = {
    "E4M3": {"e": 4, "m": 3, "bias": 7, "emax": 8, "max": 448.0},
    "E5M2": {"e": 5, "m": 2, "bias": 15, "emax": 15, "max": 57344.0},
    "E2M3": {"e": 2, "m": 3, "bias": 1, "emax": 2, "max": 7.5},
    "E3M2": {"e": 3, "m": 2, "bias": 3, "emax": 4, "max": 28.0},
    "E2M1": {"e": 2, "m": 1, "bias": 1, "emax": 2, "max": 6.0},
}


def _fp_round(y, kind):
    """Round to an OCP element format, with subnormals and saturation."""
    f = ELEM[kind]
    mbits, bias, emax, hi = f["m"], f["bias"], f["emax"], f["max"]
    emin = 1 - bias  # smallest NORMAL exponent
    a = np.abs(y)
    with np.errstate(divide="ignore"):
        ex = np.where(a > 0, np.floor(np.log2(np.maximum(a, 1e-300))), emin)
    # clamping the exponent at emin is exactly gradual underflow: below the
    # smallest normal the step stops shrinking, which is what subnormals are
    ex = np.clip(ex, emin, emax)
    step = 2.0 ** (ex - mbits)
    q = np.round(y / step) * step
    return np.clip(q, -hi, hi)


def grid(kind):
    """Every representable magnitude of an element format, for verification."""
    f = ELEM[kind]
    out = {0.0}
    for e in range(2 ** f["e"]):
        for m in range(2 ** f["m"]):
            if e == 0:
                v = (m / 2 ** f["m"]) * 2.0 ** (1 - f["bias"])  # subnormal
            else:
                v = (1 + m / 2 ** f["m"]) * 2.0 ** (e - f["bias"])
            if v <= f["max"]:
                out.add(v)
    return sorted(out)


def quantise_mx(x, kind="E4M3"):
    """An OCP MX format: E8M0 block scale over 32 + a floating element.

    The shared scale is the spec's:

        X = 2 ** (floor(log2(max|V|)) - emax_elem)

    which normalises the block peak to sit in the element format's TOP binade.
    Each element then carries its own exponent, which is the whole difference
    between an MX format and a block-scaled integer.
    """
    x = np.asarray(x, dtype=np.float64)
    if x.shape[-1] % KBLOCK:
        raise ValueError(f"K={x.shape[-1]} is not a multiple of {KBLOCK}")
    b = x.reshape(*x.shape[:-1], -1, KBLOCK)

    peak = np.abs(b).max(axis=-1, keepdims=True)
    with np.errstate(divide="ignore"):
        se = np.where(peak > 0, np.floor(np.log2(np.maximum(peak, 1e-300))), 0.0)
    scale = 2.0 ** (se - ELEM[kind]["emax"])
    scale = np.where(scale > 0, scale, 1.0)
    return (_fp_round(b / scale, kind) * scale).reshape(x.shape)


def matmul_fp32(a, bt):
    """A @ B.T with FP32 operands and FP32 accumulation."""
    return (np.asarray(a, np.float32) @ np.asarray(bt, np.float32).T).astype(np.float64)


def to_bf16(x):
    """Round FP32 to bfloat16, round-to-nearest-even, returned as FP32.

    numpy has no bfloat16, so this is done on the bits: bf16 is the top 16 bits
    of an FP32, and the rounding bias is 0x7FFF plus the low bit of the kept
    mantissa, which is what makes ties go to even rather than always up.
    Checked against torch.bfloat16 in tests/test_formats.py.
    """
    f = np.asarray(x, dtype=np.float32)
    u = f.view(np.uint32).astype(np.uint64)
    bias = ((u >> 16) & 1) + np.uint64(0x7FFF)
    r = ((u + bias) & np.uint64(0xFFFF0000)).astype(np.uint32)
    return r.view(np.float32).reshape(f.shape)


def matmul_fp16_fp32(a, bt):
    """FP16 operands, FP32 accumulation -- the usual GPU tensor-core path."""
    qa = np.asarray(a).astype(np.float16).astype(np.float32)
    qb = np.asarray(bt).astype(np.float16).astype(np.float32)
    return (qa @ qb.T).astype(np.float64)


def matmul_fp16_fp16(a, bt):
    """FP16 operands AND FP16 accumulation.

    Done as an explicit running sum in float16 rather than letting numpy pick
    an internal precision, because the whole point of this reference is the
    accumulator width -- a matmul that quietly accumulates in float32 would
    measure the operand format and be labelled as measuring the accumulator.

    This is the pessimistic accumulator: error grows with K here, unlike the
    FP32-accumulate references, because every partial sum is re-rounded.
    """
    qa = np.asarray(a).astype(np.float16)
    qb = np.asarray(bt).astype(np.float16)
    out = np.zeros((qa.shape[0], qb.shape[0]), dtype=np.float16)
    for k in range(qa.shape[1]):
        term = (qa[:, k : k + 1] * qb[:, k : k + 1].T).astype(np.float16)
        out = (out + term).astype(np.float16)
    return out.astype(np.float64)


def matmul_bf16_fp32(a, bt):
    """BF16 operands, FP32 accumulation.

    Same 8-bit exponent as FP32 with 7 mantissa bits, so it trades precision
    for range against FP16's 5-bit exponent and 10-bit mantissa -- which is
    why it loses to FP16 here and wins on tensors with wide dynamic range.
    """
    return (to_bf16(a) @ to_bf16(bt).T).astype(np.float64)


def matmul_mxfp8_fp32(a, bt, kind="E4M3"):
    """MXFP8 (E4M3 or E5M2 + E8M0/32) operands, FP32 accumulation."""
    qa = quantise_mx(a, kind).astype(np.float32)
    qb = quantise_mx(bt, kind).astype(np.float32)
    return (qa @ qb.T).astype(np.float64)


def matmul_fp64(a, bt):
    """The true value, as close as double gets."""
    return np.asarray(a, np.float64) @ np.asarray(bt, np.float64).T


# An 8-bit block scale, spent different ways. E8M0 is what OCP MX uses; E5M3 is
# what this machine settled on. Every mantissa bit taken from the exponent buys
# precision in WHERE the block peak lands, at the cost of dynamic range.
SCALE8 = {
    "E8M0": {"e": 8, "m": 0},
    "E7M1": {"e": 7, "m": 1},
    "E6M2": {"e": 6, "m": 2},
    "E5M3": {"e": 5, "m": 3},
}


def _round_scale_up(s, mbits, ebits=None):
    """Round a positive block scale UP to 2^e * m, m having `mbits` of mantissa.

    UP, not to-nearest: the scale is the divisor, so rounding it down makes
    peak/scale exceed mag_max and the block's largest element clips. Clipping
    the peak is far worse than losing a fraction of a step everywhere else.

    ``ebits`` bounds the exponent the way a real N-bit scale field would. Left
    None the exponent is unbounded, which isolates the mantissa's contribution.
    """
    if mbits is None:
        return s
    with np.errstate(divide="ignore"):
        e = np.floor(np.log2(np.maximum(s, 1e-300)))
    m = s / 2.0**e  # in [1, 2)
    step = 2.0**-mbits
    m = np.ceil(m / step) * step
    if ebits is not None:
        bias = 2 ** (ebits - 1) - 1
        e = np.clip(e, -bias, (2**ebits - 1) - bias)
    return m * 2.0**e


def quantise_mx_int(x, mag_max=63, scale="e8m0"):
    """Block-scaled INTEGER significand -- what KohakuTPU uses.

    ``scale`` picks how the shared block scale is represented. What matters is
    the scale's MANTISSA bits, not its exponent width: a power-of-two scale can
    only land the block peak somewhere in [mag_max/2, mag_max), so between 0
    and 1 bit of the significand goes unused, and which it is depends on where
    the peak happens to fall inside its binade. That is why the WORST elements
    improve so much more than the median when the scale gains a mantissa -- the
    median block was already lucky, the worst block was not.

      E8M0        power of two, the OCP MX choice
      E7M1/E6M2/E5M3   an 8-bit scale field spent with 1, 2 or 3 mantissa bits.
                  E5M3 is what the hardware does today.
      e4m3/e5m2   the real FP8 encodings, for comparison against the 8-bit set
      exact       a real number: the upper bound all of them are chasing
    """
    x = np.asarray(x, dtype=np.float64)
    b = x.reshape(*x.shape[:-1], -1, KBLOCK)
    peak = np.abs(b).max(axis=-1, keepdims=True)
    ideal = np.where(peak > 0, peak / mag_max, 1.0)

    if scale == "E8M0":
        with np.errstate(divide="ignore"):
            e = np.where(peak > 0, np.floor(np.log2(np.maximum(peak, 1e-300))), 0.0)
        step = 2.0**e / 2.0 ** np.floor(np.log2(mag_max))
    elif scale in SCALE8:
        f = SCALE8[scale]
        step = _round_scale_up(ideal, f["m"], f["e"])
    elif scale in ("e4m3", "e5m2"):
        # a real FP8 scale: limited exponent range as well as limited mantissa
        kind = "E4M3" if scale == "e4m3" else "E5M2"
        step = np.where(peak > 0, _fp_round(ideal, kind), 1.0)
        # _fp_round is round-to-nearest, which can round the divisor DOWN and
        # clip the peak; nudge those blocks to the next representable scale
        too_small = (peak / np.maximum(step, 1e-300)) > mag_max
        step = np.where(too_small, _fp_round(ideal * 1.125, kind), step)
    else:
        step = ideal
    step = np.where(step > 0, step, 1.0)

    q = np.clip(np.round(b / step), -mag_max, mag_max)
    return (q * step).reshape(x.shape)


def _mm(qa, qb):
    return (qa.astype(np.float32) @ qb.astype(np.float32).T).astype(np.float64)


# name -> operand quantiser. All accumulate in FP32 via _mm, so the ONLY
# difference between any two rows is the operand format.
#
# The default set is the design space we are actually choosing from: high
# precision at the top for context, then int7/int8 across every block-scale
# representation. The MX float formats stay available under MX_FORMATS but are
# not in the default table -- they lose to a block-scaled integer of the same
# width on every distribution measured, so carrying them dilutes the comparison
# that matters, which is how many bits the SCALE needs.
OPERAND_FORMATS = {
    "fp32": lambda x: np.asarray(x, np.float32),
    "fp16": lambda x: np.asarray(x).astype(np.float16).astype(np.float32),
    "bf16": to_bf16,
    "int8 + E8M0": lambda x: quantise_mx_int(x, 127, "E8M0"),
    "int8 + E6M2": lambda x: quantise_mx_int(x, 127, "E6M2"),
    "int8 + E5M3": lambda x: quantise_mx_int(x, 127, "E5M3"),
    "int8 + exact": lambda x: quantise_mx_int(x, 127, "exact"),
    "int7 + E8M0": lambda x: quantise_mx_int(x, 63, "E8M0"),
    "int7 + E7M1": lambda x: quantise_mx_int(x, 63, "E7M1"),
    "int7 + E6M2": lambda x: quantise_mx_int(x, 63, "E6M2"),
    "int7 + E5M3": lambda x: quantise_mx_int(x, 63, "E5M3"),
    "int7 + E5M2 (fp8)": lambda x: quantise_mx_int(x, 63, "e5m2"),
    "int7 + E4M3 (fp8)": lambda x: quantise_mx_int(x, 63, "e4m3"),
    "int7 + exact": lambda x: quantise_mx_int(x, 63, "exact"),
}

# The OCP MX float formats, kept for reference and verified against
# microxcaling and torchao in tests. Pass --formats to bring them back in.
MX_FORMATS = {
    "mxfp8 E4M3": lambda x: quantise_mx(x, "E4M3"),
    "mxfp8 E5M2": lambda x: quantise_mx(x, "E5M2"),
    "mxfp6 E2M3": lambda x: quantise_mx(x, "E2M3"),
    "mxfp6 E3M2": lambda x: quantise_mx(x, "E3M2"),
    "mxfp4 E2M1": lambda x: quantise_mx(x, "E2M1"),
}
ALL_FORMATS = {**OPERAND_FORMATS, **MX_FORMATS}


# Rows whose ACCUMULATOR differs, so they cannot be expressed as an operand
# quantiser plus the shared FP32 accumulation.
SPECIAL_MATMULS = {
    "fp16 acc fp16": matmul_fp16_fp16,
}


def matmul_names():
    return list(OPERAND_FORMATS) + list(SPECIAL_MATMULS)


def all_names():
    return list(ALL_FORMATS) + list(SPECIAL_MATMULS)


def matmul_in(name, a, bt):
    """Run the matmul in `name`. FP32 accumulation unless listed in SPECIAL."""
    if name in SPECIAL_MATMULS:
        return SPECIAL_MATMULS[name](a, bt)
    return _mm(ALL_FORMATS[name](a), ALL_FORMATS[name](bt))


REFERENCES = {
    "fp64": matmul_fp64,
    "fp16/fp32": matmul_fp16_fp32,
    "bf16/fp32": matmul_bf16_fp32,
    "mxfp8/fp32": matmul_mxfp8_fp32,
}
