"""Packed products use node tile/chunk loops and a fixed four-row epilogue."""

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.language import dsl as kt


def kernel(mm, e, extras, out, clusters, cores, shape_class):
    if out.kind != "full" or X.reduces(e):
        raise ValueError("class product epilogue requires a full elementwise output")
    if mm.tn > shape_class.max_block or mm.tn % 16:
        raise ValueError(
            "class product tile width must fit a VC row and whole DMA words"
        )
    if any(o.kind not in ("full", "col", "scalar") for o in extras):
        raise ValueError("class product extras require full, column, or scalar layout")
    ntm, ntn = mm.mp // mm.tm, mm.n // mm.tn
    total = mm.bt * ntm * ntn * mm.tm
    trips = total // 4
    used = cores if shape_class.cores == "all" else min(cores, trips)
    mg_used = (
        clusters if shape_class.cores == "all" else min(clusters, mm.bt * ntm * ntn)
    )
    la, lb = {"gm": (mm.tm // 4), "nk": mm.nk}, {"gn": (mm.tn // 4), "nk": mm.nk}
    ash, bsh = (mm.bt * mm.mp, mm.kp), (mm.n if mm.shared else mm.bt * mm.n, mm.kp)
    ports = {
        "a": kt.In(*ash, dtype="mx7a", layout=la),
        "b": kt.In(*bsh, dtype="mx7b", layout=lb),
    }
    given = {
        "a": kt.TensorSpec.make(ash, "mx7a", la),
        "b": kt.TensorSpec.make(bsh, "mx7b", lb),
    }
    for i, operand in enumerate(extras):
        ports[f"in{i + 1}"], given[f"in{i + 1}"] = V.port(operand, mm.bt * mm.mp, mm.n)
    _, spec = V.port(out, mm.bt * mm.mp, mm.n)
    ports["out"], given["out"] = kt.Out(*spec.shape, dtype=out.dtype), spec

    def body(**p):
        h = kt.buffer((total, mm.tn), "f16", tile=((mm.tm // 4), (mm.tn // 4)))
        kt.gemm_tiles(
            h,
            p["a"],
            p["b"],
            batches=mm.bt,
            rows=mm.mp,
            cols=mm.n,
            clusters=mg_used,
            shared=mm.shared,
        )
        staged = {}
        for i, operand in enumerate(extras):
            if operand.kind == "scalar":
                staged[i] = p[f"in{i + 1}"]
                continue
            item = {"f16": 2, "f32": 4}[operand.dtype]
            buf = kt.buffer((total, mm.tn), operand.dtype)
            row_stride = operand.pitch * item if operand.kind == "full" else 0
            kt.copy(
                buf,
                p[f"in{i + 1}"],
                at=operand.offset * item,
                walk=(
                    (mm.bt * ntm, mm.tm * row_stride),
                    (ntn, mm.tn * item),
                    (mm.tm, row_stride),
                    (mm.tn * item // 32, 32),
                ),
                dwalk=(
                    (mm.bt * ntm, ntn * mm.tm * mm.tn * item),
                    (ntn, mm.tm * mm.tn * item),
                    (mm.tm, mm.tn * item),
                    (mm.tn * item // 32, 32),
                ),
                classed=True,
                node_loop=True,
            )
            staged[i] = buf
        dst = kt.buffer((total, mm.tn), out.dtype)
        for c in kt.par(used, on=kt.vc, classed=True):
            for t in kt.range((trips + used - 1 - c) // used):
                row = (c + t * used) * 4
                vals = {0: kt.load(h[row : row + 4, :], gt4=True)}
                for i, operand in enumerate(extras):
                    if operand.kind == "scalar":
                        start = operand.offset // 16 * 16
                        vals[i + 1] = kt.unpack(
                            kt.load(staged[i][start : start + 16]),
                            lane=operand.offset % 16,
                        )
                    else:
                        vals[i + 1] = kt.load(staged[i][row : row + 4, :], gt4=True)
                V._store_over(dst, row, 0, 4, mm.tn, X.emit(e, vals))
        item = {"f16": 2, "f32": 4}[out.dtype]
        kt.copy(
            p["out"],
            dst,
            dat=out.offset * item,
            walk=(
                (mm.bt * ntm, ntn * mm.tm * mm.tn * item),
                (ntn, mm.tm * mm.tn * item),
                (mm.tm, mm.tn * item),
                (mm.tn * item // 32, 32),
            ),
            dwalk=(
                (mm.bt * ntm, mm.tm * out.pitch * item),
                (ntn, mm.tn * item),
                (mm.tm, out.pitch * item),
                (mm.tn * item // 32, 32),
            ),
            classed=True,
            node_loop=True,
        )

    result = kt.Kernel.build(
        V.name("mm_class", mm, e, extras, out, clusters, cores, shape_class),
        body,
        ports,
    )
    result.class_info = {
        "mm": True,
        "width": mm.tn,
        "trip_rows": 4,
        "cores": used,
        "clusters": mg_used,
        "tm": mm.tm,
        "tn": mm.tn,
        "nk": mm.nk,
        "kblock": shape_class.kblock,
        "padded_rows": mm.bt * (mm.mp - mm.m),
        "padded_columns": 0,
        "core_policy": shape_class.cores,
    }
    return result, given
