"""Column-offset pitched views at allocation ends retain their covering rows."""

from dataclasses import replace

import pytest

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import rows as RW


def witness(operands, bindings, rows, width, sizes, prefix):
    for index, operand in enumerate(operands):
        name = f"in{index}" if prefix == "in" else prefix
        spec = V.port(operand, rows, width)[1]
        assert bindings[name] + spec.nbytes() <= sizes[name]


def test_column_offset_last_allocation_bytes(monkeypatch):
    # Covers plain/wide launch inputs and output normalization, including the
    # real 8x197x192 token-buffer geometry and whole-row offsets before it.
    for kind in ("full", "tile"):
        for dtype, item in (("f16", 2), ("f32", 4)):
            for rows, width, column in ((8, 37632, 192), (3, 64, 16)):
                pitch = width + column
                for leading in (0, 2):
                    operand = V.Operand(
                        kind,
                        dtype=dtype,
                        pitch=pitch,
                        offset=leading * pitch + column,
                        rows=leading + rows,
                    )
                    for prefix in ("in", "out"):
                        name = "in0" if prefix == "in" else prefix
                        ops, binds = RW._based([operand], {name: 0}, prefix)
                        size = {name: (leading + rows) * pitch * item}
                        witness(ops, binds, rows, width, size, prefix)
                        assert ops[0].offset == column
                        assert (
                            binds[name]
                            + item * (ops[0].offset + (rows - 1) * pitch + width)
                            == size[name]
                        )
                        # Prior normalization moves the column too, even though
                        # the declared port still covers a full final row.
                        old = replace(operand, offset=0, rows=rows)
                        with pytest.raises(AssertionError):
                            witness(
                                [old],
                                {name: operand.offset * item},
                                rows,
                                width,
                                size,
                                prefix,
                            )
    # Real offline allocation/launch validation, not only byte arithmetic.
    dev = D.Device(kind="offline", board="vlt_v9t6s_1n")
    rows, width, column, pitch = 8, 37632, 192, 37824
    source = dev.alloc(rows * width * 2)
    bias = dev.alloc(width * 2)
    target = dev.alloc(rows * pitch * 2)
    out = V.Operand("full", pitch=pitch, offset=column)
    operands = [V.Operand("full", pitch=width), V.Operand("col")]
    expression = X.op("add", X.Load(0), X.Load(1))
    try:
        RW.run(
            dev,
            expression,
            operands,
            dict(in0=source, in1=bias),
            rows,
            width,
            out,
            target,
        )
        # A direct legacy launch reproduces the exact RUL.023 extent refusal.
        kernel, given, _ = V.rows_kernel(
            expression, tuple(operands), rows, width, replace(out, offset=0), 1
        )
        compiled = dev.specialise(kernel, given)
        with pytest.raises(Exception, match="RUL.023"):
            dev.run(compiled, dict(in0=source, in1=bias, out=target + column * 2))
    finally:
        for addr in (source, bias, target):
            dev.free(addr)
        dev.close()
