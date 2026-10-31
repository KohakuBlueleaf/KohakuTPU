"""Flat elementwise classes use fixed 128-element rows and padded row trips."""

from kohakutpu.application.kernels import vector as V
from kohakutpu.application.kernels.classes import rows


def kernel(e, dtypes, n, out_dtype, cores, scalars, shape_class):
    if n <= 0:
        raise ValueError("flat classes require a positive element count")
    lanes = dict(scalars)
    operands = tuple(
        (
            V.Operand("scalar", dtype=dtype, offset=lanes[i])
            if i in lanes
            else V.Operand("full", dtype=dtype, pitch=128)
        )
        for i, dtype in enumerate(dtypes)
    )
    result, given, _ = rows.kernel(
        e,
        operands,
        -(-n // 128),
        128,
        V.Operand("full", dtype=out_dtype, pitch=128),
        cores,
        bands=None,
        shape_class=shape_class,
        flat=True,
    )
    result.class_info = dict(result.class_info, flat=True, padded_elements=(-n) % 128)
    return result, given
