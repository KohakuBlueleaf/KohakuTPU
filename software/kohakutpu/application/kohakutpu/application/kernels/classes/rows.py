"""Rows staged into class-width scratch, with one fixed trip and a masked tail."""

import os

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.kernels.classes import wide
from kohakutpu.language import dsl as kt


def bands_fit(operands, out, width):
    """Single-buffered class bands; shared by class emission and admission."""
    words = sum(
        width // 4 * (2 if operand.dtype == "f32" else 1)
        for operand in operands
        if operand.kind in ("full", "tile")
    )
    words += sum(1 for operand in operands if operand.kind in ("row", "scalar"))
    words += 1 if out.kind == "row" else width // 4 * (2 if out.dtype == "f32" else 1)
    prologue = sum(width // 16 for operand in operands if operand.kind == "col")
    return (V.L1_WORDS - V.L1_MARGIN - prologue) // words


def kernel(e, operands, R, W, out, cores, bands, shape_class, *, flat=False):
    if W > shape_class.max_block:
        return wide.kernel(e, operands, R, W, out, cores, bands, shape_class)
    class_band = int(os.environ.get("KTPU_CLASS_BAND", "4"))
    width = shape_class.width(W)
    quantum = V.STAT_ROWS if V.holds_stat(operands, out.kind) and not flat else 4
    fit = bands_fit(operands, out, width)
    need = quantum // 4
    selected = max(need, bands or min(4, fit))
    if selected > fit:
        raise ValueError(f"{selected} bands of {width} columns do not fit class L1")
    pl = V.Plan(width, 1, selected * 4, 1, selected * 4)
    trips = -(-R // pl.rows)
    used = cores if shape_class.cores == "all" else min(cores, trips)
    if R <= 0 or used <= 0:
        raise ValueError("class rows need positive rows and cores")
    padded = -(-trips // used) * used * pl.rows
    ports, given = {}, {}
    for i, operand in enumerate(operands):
        ports[f"in{i}"], given[f"in{i}"] = V.port(operand, R, W)
    out_shape = (
        (max(out.rows, V.stat_words(R) * V.STAT_ROWS),)
        if out.kind == "row"
        else V.port(out, R, W)[1].shape
    )
    ports["out"] = kt.Out(*out_shape, dtype=out.dtype)
    given["out"] = kt.TensorSpec.make(out_shape, out.dtype)

    direct = (
        os.environ.get("KTPU_CLASS_ROWS_DIRECT", "0") == "1"
        and not flat
        and W == width
        and R % pl.rows == 0
        and (out.kind != "row" or out.offset == 0)
    )

    def direct_body(**p):
        for c in kt.par(used, on=kt.vc, classed=True, class_band=class_band):
            pro = {}
            for i, operand in enumerate(operands):
                if operand.kind == "col":
                    start = operand.offset
                    pro[i] = kt.unpack(
                        kt.load(p[f"in{i}"][start : start + width], gt4=True)
                    )
            for t in kt.range((trips + used - 1 - c) // used):
                row = (c + t * used) * pl.rows
                V._rows_block(p, e, operands, out, pl, row, 0, pro)

    def body(**p):
        bufs = {}
        for i, operand in enumerate(operands):
            src = p[f"in{i}"]
            if operand.kind in ("full", "tile"):
                buf = kt.buffer((padded, width), operand.dtype)
                kt.fill_value(buf, 0)
                item = {"f16": 2, "f32": 4}[operand.dtype]
                kt.copy(
                    buf,
                    src,
                    walk=((R, operand.pitch * item), (W * item // 32, 32)),
                    at=operand.offset * item,
                    dwalk=((R, width * item), (W * item // 32, 32)),
                    classed=True,
                )
            elif operand.kind == "col":
                buf = kt.buffer((width,), operand.dtype)
                kt.fill_value(buf, 0)
                kt.copy(buf[:W], src[operand.offset : operand.offset + W])
            elif operand.kind == "row":
                buf = kt.buffer((padded,), operand.dtype)
                kt.fill_value(buf, 0)
                count = V.stat_words(R) * V.STAT_ROWS
                kt.copy(buf[:count], src[operand.offset : operand.offset + count])
            else:
                buf = src
            bufs[i] = buf
        dst = kt.buffer((padded,) if out.kind == "row" else (padded, width), out.dtype)
        for c in kt.par(used, on=kt.vc, classed=True, class_band=class_band):
            pro = {}
            for i, operand in enumerate(operands):
                if operand.kind == "col":
                    pro[i] = kt.unpack(kt.load(bufs[i][:width], gt4=True))
            for t in kt.range((trips + used - 1 - c) // used):
                row = (c + t * used) * pl.rows
                vals = dict(pro)
                for i, operand in enumerate(operands):
                    if operand.kind in ("full", "tile"):
                        vals[i] = kt.load(bufs[i][row : row + pl.rows, :], gt4=not flat)
                    elif operand.kind == "row":
                        vals[i] = kt.load(bufs[i][row : row + pl.rows])
                    elif operand.kind == "scalar":
                        start = operand.offset // V.STAT_ROWS * V.STAT_ROWS
                        vals[i] = kt.unpack(
                            kt.load(bufs[i][start : start + V.STAT_ROWS]),
                            lane=operand.offset % V.STAT_ROWS,
                        )
                value = X.emit(e, vals, valid_cols=W)
                if out.kind == "row":
                    kt.store(dst[row : row + pl.rows], value)
                else:
                    V._store_over(dst, row, 0, pl.rows, width, value)
        if out.kind == "row":
            count = V.stat_words(R) * V.STAT_ROWS
            kt.copy(p["out"][:count], dst[:count])
        else:
            item = {"f16": 2, "f32": 4}[out.dtype]
            kt.copy(
                p["out"],
                dst,
                walk=((R, width * item), (W * item // 32, 32)),
                dwalk=((R, out.pitch * item), (W * item // 32, 32)),
                dat=out.offset * item,
                classed=True,
            )

    key = V.name(
        "rows_class",
        repr(e),
        operands,
        R,
        W,
        out,
        pl,
        shape_class,
        cores,
        flat,
        class_band,
    )
    if direct:
        key = V.name("rows_class_direct", key)
    result = kt.Kernel.build(key, direct_body if direct else body, ports)
    result.class_info = {
        "direct": direct,
        "width": width,
        "trip_rows": pl.rows,
        "cores": used,
        "padded_rows": padded - R,
        "padded_columns": width - W,
        "lowering": f"serial-band{class_band}-hold-pin",
        "core_policy": shape_class.cores,
    }
    return result, given, 1
