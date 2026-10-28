"""Wide elementwise rows streamed in block-major scratch order."""

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.kernels.classes import fold
from kohakutpu.language import dsl as kt


def zero(buf, rows, words):
    seed = kt.buffer((16,), "f16")
    kt.fill_value(seed, 0)
    kt.copy(
        buf,
        seed,
        walk=((rows, 0), (words, 0)),
        dwalk=((rows, words * 32), (words, 32)),
        classed=True,
        node_loop=True,
    )


def kernel(e, operands, R, W, out, cores, bands, shape_class):
    if X.reduces(e) or out.kind == "row":
        return fold.kernel(e, operands, R, W, out, cores, bands, shape_class)
    width = shape_class.kblock
    if W % width:
        raise ValueError("wide rows contain whole machine column blocks")
    quantum = V.STAT_ROWS if V.holds_stat(operands, out.kind) else 4
    height = max(quantum, (bands or 1) * 4)
    if height % quantum:
        raise ValueError("wide trip rows must respect statistic packing")
    block_rows = -(-R // height) * height
    nblocks = W // width
    trips = nblocks * block_rows // height
    used = cores if shape_class.cores == "all" else min(cores, trips)
    total = -(-trips // used) * used * height
    ports, given = {}, {}
    for i, operand in enumerate(operands):
        ports[f"in{i}"], given[f"in{i}"] = V.port(operand, R, W)
    ports["out"] = kt.Out(*V.port(out, R, W)[1].shape, dtype=out.dtype)
    given["out"] = V.port(out, R, W)[1]

    def body(**p):
        bufs = {}
        for i, operand in enumerate(operands):
            src = p[f"in{i}"]
            if operand.kind in ("full", "tile", "col"):
                item = {"f16": 2, "f32": 4}[operand.dtype]
                buf = kt.buffer((total, width), operand.dtype)
                zero(buf, total, width * item // 32)
                count = block_rows if operand.kind == "col" else R
                stride = 0 if operand.kind == "col" else operand.pitch * item
                kt.copy(
                    buf,
                    src,
                    walk=(
                        (nblocks, width * item),
                        (count, stride),
                        (width * item // 32, 32),
                    ),
                    at=operand.offset * item,
                    dwalk=(
                        (nblocks, block_rows * width * item),
                        (count, width * item),
                        (width * item // 32, 32),
                    ),
                    classed=True,
                    node_loop=True,
                )
            elif operand.kind == "row":
                buf = kt.buffer((total,), operand.dtype)
                zero(buf, total // 16, 1)
                kt.copy(
                    buf,
                    src,
                    walk=((nblocks, 0), (V.stat_words(R), 32)),
                    at=operand.offset * 2,
                    dwalk=((nblocks, block_rows * 2), (V.stat_words(R), 32)),
                    classed=True,
                    node_loop=True,
                )
            else:
                buf = src
            bufs[i] = buf
        dst = kt.buffer((total, width), out.dtype)
        for c in kt.par(used, on=kt.vc, classed=True):
            for t in kt.range((trips + used - 1 - c) // used):
                row = (c + t * used) * height
                vals = {}
                for i, operand in enumerate(operands):
                    if operand.kind in ("full", "tile", "col"):
                        vals[i] = kt.load(bufs[i][row : row + height, :], gt4=True)
                    elif operand.kind == "row":
                        vals[i] = kt.load(bufs[i][row : row + height])
                    else:
                        start = operand.offset // V.STAT_ROWS * V.STAT_ROWS
                        vals[i] = kt.unpack(
                            kt.load(bufs[i][start : start + V.STAT_ROWS]),
                            lane=operand.offset % V.STAT_ROWS,
                        )
                V._store_over(dst, row, 0, height, width, X.emit(e, vals))
        item = {"f16": 2, "f32": 4}[out.dtype]
        kt.copy(
            p["out"],
            dst,
            walk=(
                (nblocks, block_rows * width * item),
                (R, width * item),
                (width * item // 32, 32),
            ),
            dwalk=(
                (nblocks, width * item),
                (R, out.pitch * item),
                (width * item // 32, 32),
            ),
            dat=out.offset * item,
            classed=True,
            node_loop=True,
        )

    result = kt.Kernel.build(
        V.name(
            "rows_wide_class", repr(e), operands, R, W, out, cores, bands, shape_class
        ),
        body,
        ports,
    )
    result.class_info = {
        "width": width,
        "trip_rows": height,
        "cores": used,
        "padded_rows": total - R * nblocks,
        "padded_columns": 0,
        "wide": True,
        "core_policy": shape_class.cores,
        "lowering": "block-major-serial-band1",
    }
    return result, given, nblocks
