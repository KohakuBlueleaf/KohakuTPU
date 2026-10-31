"""Wide row folds: one column VLOOP per repeated node-level row band."""

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.language import dsl as kt


def kernel(e, operands, R, W, out, cores, bands, shape_class):
    if (
        not isinstance(e, X.Reduce)
        or e.op not in ("sum", "max", "prod")
        or X.reduces(e.arg)
    ):
        raise ValueError(
            "wide carry accepts a root sum/max/prod without nested reductions"
        )
    if out.dtype != "f16" or any(o.dtype != "f16" for o in operands):
        raise ValueError("wide fold staging currently requires f16 operands and output")
    if out.kind != "row":
        raise ValueError("wide carry needs a statistic output")
    height = V.STAT_ROWS
    trips = -(-R // height)
    used = cores if shape_class.cores == "all" else min(cores, trips)
    repeats = -(-trips // used)
    padded = repeats * used * height
    width = shape_class.kblock
    if W % width:
        raise ValueError("wide fold width must be whole column blocks")
    ports, given = {}, {}
    for i, operand in enumerate(operands):
        ports[f"in{i}"], given[f"in{i}"] = V.port(operand, R, W)
    out_shape = (max(out.rows, out.offset + V.stat_words(R) * 16),)
    ports["out"] = kt.Out(*out_shape)
    given["out"] = kt.TensorSpec.make(out_shape, "f16")
    advances = tuple(
        (
            used * height * (W * 2 if o.kind in ("full", "tile") else 2)
            if o.kind in ("full", "tile", "row")
            else 0
        )
        for o in operands
    ) + (used * height * 2,)

    def body(**p):
        bufs = {}
        for i, operand in enumerate(operands):
            src = p[f"in{i}"]
            if operand.kind in ("full", "tile"):
                buf = kt.buffer((padded, W), "f16")
                kt.fill_value(buf, 0)
                kt.copy(
                    buf,
                    src,
                    walk=((R, operand.pitch * 2), (W // 16, 32)),
                    at=operand.offset * 2,
                    dwalk=((R, W * 2), (W // 16, 32)),
                    classed=True,
                )
            elif operand.kind == "row":
                buf = kt.buffer((padded,), "f16")
                kt.fill_value(buf, 0)
                n = V.stat_words(R) * 16
                kt.copy(buf[:n], src[operand.offset : operand.offset + n])
            else:
                buf = src
            bufs[i] = buf
        dst = kt.buffer((padded,), "f16")
        for c in kt.par(
            used, on=kt.vc, classed=True, repeats=repeats, advances=advances
        ):
            row = c * height
            active = (min(used, trips) + used - 1 - c) // used
            for b in kt.range(W // width * active):
                vals = {}
                for i, operand in enumerate(operands):
                    if operand.kind in ("full", "tile"):
                        vals[i] = kt.load(
                            bufs[i][row : row + height, b * width : b * width + width],
                            gt4=True,
                        )
                    elif operand.kind == "col":
                        col = operand.offset + b * width
                        vals[i] = kt.load(bufs[i][col : col + width], gt4=True)
                    elif operand.kind == "row":
                        vals[i] = kt.load(bufs[i][row : row + height])
                    else:
                        start = operand.offset // 16 * 16
                        vals[i] = kt.unpack(
                            kt.load(bufs[i][start : start + 16]),
                            lane=operand.offset % 16,
                        )
                result = X.emit(e, vals, carry=True)
                kt.store(dst[row : row + height], result)
        n = V.stat_words(R) * 16
        kt.copy(p["out"][out.offset : out.offset + n], dst[:n])

    result = kt.Kernel.build(
        V.name(
            "rows_fold_class", repr(e), operands, R, W, out, cores, bands, shape_class
        ),
        body,
        ports,
    )
    result.class_info = {
        "width": width,
        "trip_rows": height,
        "cores": used,
        "padded_rows": padded - R,
        "padded_columns": 0,
        "fold": True,
        "wide": True,
        "core_policy": shape_class.cores,
        "lowering": "node-repeat-column-carry",
    }
    return result, given, W // width
