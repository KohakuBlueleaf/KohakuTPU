"""Generated contractions: ``out = epilogue(A @ B^T, extras)`` on the clusters
and the vector cores, as one DSL kernel.

A and B are MXFP7 forms the runtime makes once (`quant_kernel`: vector
packing with native padding, or a measured staged-padding alternative): A [Bt * Mp, Kp] in the A packing, B [Np, Kp] (or one a batch) in
the B packing. The clusters sweep tiles of tm x tn, K in chunks of 32 * nk,
and drain each tile into a ``tile=`` scratch; the vector cores read it a
band at a time, apply the epilogue (its `Load(0)` the product; bias,
residual, statistics as `vector.Operand`s) and store row-major f16.
"""

import json
import os
from collections import OrderedDict
from dataclasses import dataclass

from kohakutpu.application.kernels import prepared as P
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.kernels.classes import mm as class_mm
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.compiler import _rs
from kohakutpu.language import dsl as kt

KERNELS_KEPT = 1024
_kernels: OrderedDict = OrderedDict()


@dataclass(frozen=True)
class Mm:
    """One contraction's tiling."""

    bt: int  # batches
    m: int
    n: int
    k: int
    mp: int  # m padded to the tile
    kp: int  # k padded to the K chunk
    tm: int
    tn: int
    nk: int
    shared: bool  # one B for every batch
    #: Rows a batch's padding and an epilogue trip come in: 16 where the
    #: epilogue reads a statistic (16 values a word), else 4.
    quantum: int = 4

    @property
    def kc(self) -> int:
        return 32 * self.nk


def stack_pairing() -> str:
    """The pairing contractions are planned under (RUL.099): ``KTPU_MX7_PAIRING``,
    else ktpu-machine's ``PAIRING``."""
    return os.environ.get("KTPU_MX7_PAIRING", _rs.MX7_PAIRING)


def pairing_arg() -> dict:
    """`matmul.plan`'s recorded `pairing` argument: absent at the default."""
    p = stack_pairing()
    return {} if p == _rs.MX7_PAIRING else {"pairing": p}


#: Each contraction knob by name: its `KTPU_MM_*` override and the call that
#: records it (``plan``: `matmul.plan`, ``kernel``: `matmul.mm_kernel`).
KNOBS = {k["name"]: (k["env"], k["group"]) for k in json.loads(_rs.MM_KNOBS)}


def knobs(group: str) -> dict:
    """The `group` knobs the environment resolves (`_rs.mm_knobs`)."""
    every = json.loads(_rs.mm_knobs())
    return {k: v for k, v in every.items() if KNOBS[k][1] == group}


def plan_knobs() -> dict:
    return knobs("plan")


def kernel_knobs() -> dict:
    return knobs("kernel")


def plan(
    bt: int,
    m: int,
    n: int,
    k: int,
    shared: bool = True,
    quantum: int = 4,
    pairing: str | None = None,
    *,
    machine_json: str,
    **knob_args,
) -> Mm:
    """The tiling for an [M, K] x [N, K] product on the machine, `bt` of them
    (each batch's rows padded to `mp`, the epilogue's operands and output
    [bt * mp, N]), K blocks paired under `pairing` (`stack_pairing()` when
    None), the plan knobs resolved with `knob_args` over them: the native
    plan (`_rs.mm_plan`). Raises ValueError for an N that is not whole
    32-column blocks."""
    args = {
        "bt": bt,
        "m": m,
        "n": n,
        "k": k,
        "shared": shared,
        "quantum": quantum,
        "pairing": pairing or stack_pairing(),
        **plan_knobs(),
        **knob_args,
    }
    return Mm(**json.loads(_rs.mm_plan(json.dumps(args), machine_json)))


def quant_kernel(
    src: Operand,
    rows: int,
    cols: int,
    rp: int,
    cp: int,
    side: str,
    g: int,
    nk: int,
    batches: int = 1,
    *,
    shape_class=None,
    class_direct=None,
    finite=False,
    prepared=(),
    hw=None,
    pad_strategy="native",
):
    """`batches` row blocks of [rows, cols] (each `src.offset + b * rows *
    pitch`) quantised into [batches * rp, cp] in the A (`side` "a") or B
    packing, tiles of `g` lane groups. Padding rows and columns are zero."""
    finite_batch = int(os.environ.get("KTPU_PIPE_FINITE_BATCH", "4"))
    overlap = os.environ.get("KTPU_PIPE_OVERLAP", "1") == "1"
    classifier_overlap = os.environ.get("KTPU_QUANT_CLASSIFIER_OVERLAP", "0") == "1"
    pipeline = os.environ.get("KTPU_QUANT_PIPELINE", "1") == "1"
    zero_classifier = os.environ.get("KTPU_QUANT_ZERO_CLASSIFIER", "1") == "1"
    stream_axes = os.environ.get("KTPU_QUANT_STREAM_AXES", "1") == "1"
    fill_overlap = os.environ.get("KTPU_QUANT_FILL_OVERLAP", "1") == "1"
    zero_reuse = os.environ.get("KTPU_QUANT_ZERO_REUSE", "1") == "1"
    dtype = "mx7a" if side == "a" else "mx7b"
    lay = dict(gm=g, nk=nk) if side == "a" else dict(gn=g, nk=nk)
    if cols % 16 or src.pitch % 16 or src.offset % 16:
        raise ValueError(
            f"a quantised operand's rows are whole 32-byte words: {cols} columns, "
            f"pitch {src.pitch}, offset {src.offset}"
        )
    kkey = (
        "quant",
        tuple(p.shape() for p in prepared),
        hw,
        pad_strategy,
        src,
        rows,
        cols,
        rp,
        cp,
        side,
        g,
        nk,
        batches,
        shape_class,
        class_direct,
        finite,
        finite_batch,
        overlap,
        classifier_overlap,
        pipeline,
        zero_classifier,
        stream_axes,
        fill_overlap,
        zero_reuse,
        kt.Kernel.build,
    )
    hit = _kernels.get(kkey)
    if hit is not None:
        _kernels.move_to_end(kkey)
        return hit
    total = src.offset + batches * rows * src.pitch
    srows = -(-total // src.pitch)
    ports = {
        "x": kt.In(srows, src.pitch, dtype=src.dtype),
        "q": kt.Out(batches * rp, cp, dtype=dtype, layout=lay),
    }
    given = {
        "x": kt.TensorSpec.make((srows, src.pitch), src.dtype),
        "q": kt.TensorSpec.make((batches * rp, cp), dtype, lay),
    }
    direct = rows == rp and cols == cp and src.pitch == cp and src.offset == 0
    if shape_class is not None and class_direct is not None:
        if class_direct and not direct:
            raise ValueError("direct quant class requires unpadded contiguous input")
        direct = class_direct

    if prepared:
        ports.pop("x")
        given.pop("x")
        pp, pg = P.input_ports(prepared, hw, "p_")
        ports.update(pp)
        given.update(pg)

    def body(**p):
        x = (
            P.input_buffer(prepared, p, "p_", (srows, src.pitch), hw)
            if prepared
            else p["x"]
        )
        q = p["q"]
        r0, c0 = divmod(src.offset, src.pitch)
        # L2.MOV.018: a destination of K pairs quantises on the vector cores.
        vector = kt.machine().packing_backend == "vector_mx7" or nk >= 2
        batch_size, drain_overlap = (finite_batch, overlap) if vector else (1, False)
        if pad_strategy == "staged" or (not vector and (rows != rp or cols != cp)):
            z = kt.buffer((batches * rp, cp), "f16")
            kt.fill_value(z, 0)
            for b in range(batches):
                kt.copy(
                    z[b * rp : b * rp + rows, :cols],
                    x[r0 + b * rows : r0 + (b + 1) * rows, c0 : c0 + cols],
                )
            kt.quant(
                q,
                z,
                finite=finite,
                finite_batch=batch_size,
                classifier_overlap=classifier_overlap if vector else False,
                pipeline=pipeline if vector else False,
                zero_classifier=zero_classifier if vector else False,
                stream_axes=stream_axes if vector else False,
                fill_overlap=fill_overlap if vector else False,
                zero_reuse=zero_reuse if vector else False,
                overlap=drain_overlap,
                node_loop=shape_class is not None,
            )
            return
        # unpadded row blocks stack contiguously: one quant over the stack
        blocks = 1 if rows == rp else batches
        span = batches // blocks
        for b in range(blocks):
            kt.quant(
                q[b * span * rp : (b + 1) * span * rp, :],
                x[r0 + b * span * rows : r0 + (b + 1) * span * rows, c0 : c0 + cols],
                finite=finite,
                finite_batch=batch_size,
                classifier_overlap=classifier_overlap if vector else False,
                pipeline=pipeline if vector else False,
                zero_classifier=zero_classifier if vector else False,
                stream_axes=stream_axes if vector else False,
                fill_overlap=fill_overlap if vector else False,
                zero_reuse=zero_reuse if vector else False,
                overlap=drain_overlap,
                node_loop=shape_class is not None,
                pad=vector,
            )

    key = (src, rows, cols, rp, cp, side, g, nk, batches)
    result = kt.Kernel.build(
        V.name(
            "quant",
            *key,
            shape_class,
            *([tuple(p.shape() for p in prepared)] if prepared else []),
            *([pad_strategy] if pad_strategy != "native" else []),
            *(["classifier_overlap"] if classifier_overlap else []),
            *(["pipeline"] if pipeline else []),
            *(["zero_classifier"] if zero_classifier else []),
            *(["stream_axes"] if stream_axes else []),
            *(["fill_overlap"] if fill_overlap else []),
            *(["zero_reuse"] if zero_reuse else []),
        ),
        body,
        ports,
    )
    if shape_class is not None:
        result.class_info = {
            "quant": True,
            "direct": direct,
            "g": g,
            "nk": nk,
            "kblock": shape_class.kblock,
            "padded_rows": batches * (rp - rows),
            "padded_columns": cp - cols,
            "core_policy": shape_class.cores,
        }
    got = (result, given)
    if KERNELS_KEPT > 0:
        _kernels[kkey] = got
        while len(_kernels) > KERNELS_KEPT:
            _kernels.popitem(last=False)
    return got


def quant_walk_kernel(
    src_bytes: int,
    levels: tuple,
    at: int,
    rows: int,
    cols: int,
    rp: int,
    cp: int,
    side: str,
    g: int,
    nk: int,
    batches: int = 1,
    *,
    finite: bool = False,
):
    """`batches` [rows, cols] blocks walked from ``x`` (`src_bytes` bytes) by
    `levels` from byte `at`, staged and quantised into ``q: mx7a/b[batches * rp,
    cp]`` in one package. Returns ``(kernel, given)``."""
    levels = tuple((int(a), int(b)) for a, b in levels)
    sw = -(-src_bytes // 32)
    finite_batch = int(os.environ.get("KTPU_PIPE_FINITE_BATCH", "4"))
    overlap = os.environ.get("KTPU_PIPE_OVERLAP", "1") == "1"
    classifier_overlap = os.environ.get("KTPU_QUANT_CLASSIFIER_OVERLAP", "0") == "1"
    pipeline = os.environ.get("KTPU_QUANT_PIPELINE", "1") == "1"
    zero_classifier = os.environ.get("KTPU_QUANT_ZERO_CLASSIFIER", "1") == "1"
    stream_axes = os.environ.get("KTPU_QUANT_STREAM_AXES", "1") == "1"
    fill_overlap = os.environ.get("KTPU_QUANT_FILL_OVERLAP", "1") == "1"
    zero_reuse = os.environ.get("KTPU_QUANT_ZERO_REUSE", "1") == "1"
    kkey = (
        "quant_walk",
        finite_batch,
        overlap,
        classifier_overlap,
        pipeline,
        zero_classifier,
        stream_axes,
        fill_overlap,
        zero_reuse,
        sw,
        levels,
        at,
        rows,
        cols,
        rp,
        cp,
        side,
        g,
        nk,
        batches,
        finite,
        kt.Kernel.build,
    )
    hit = _kernels.get(kkey)
    if hit is not None:
        _kernels.move_to_end(kkey)
        return hit
    dtype = "mx7a" if side == "a" else "mx7b"
    layout = {("gm" if side == "a" else "gn"): g, "nk": nk}
    ports = {
        "x": kt.In(sw, 16),
        "q": kt.Out(batches * rp, cp, dtype=dtype, layout=layout),
    }
    given = {
        "x": kt.TensorSpec.make((sw, 16)),
        "q": kt.TensorSpec.make((batches * rp, cp), dtype, layout),
    }

    def body(x, q):
        # Arbitrary source walks still materialize their logical values, never
        # padding. The quant lowerer makes all padded entries in registers.
        z = kt.buffer((batches * rows, cols), "f16")
        kt.copy(
            z, x, walk=levels, at=at, dwalk=((batches * rows * cols // 16, 32),), dat=0
        )
        vector = kt.machine().packing_backend == "vector_mx7" or nk >= 2
        if not vector and (rows != rp or cols != cp):
            padded = kt.buffer((batches * rp, cp), "f16")
            kt.fill_value(padded, 0)
            for b in range(batches):
                kt.copy(
                    padded[b * rp : b * rp + rows, :cols],
                    z[b * rows : (b + 1) * rows, :],
                )
            kt.quant(q, padded, finite=finite)
        else:
            for b in range(batches):
                kt.quant(
                    q[b * rp : (b + 1) * rp, :],
                    z[b * rows : (b + 1) * rows, :],
                    finite=finite,
                    finite_batch=finite_batch if vector else 1,
                    classifier_overlap=classifier_overlap if vector else False,
                    pipeline=pipeline if vector else False,
                    zero_classifier=zero_classifier if vector else False,
                    stream_axes=stream_axes if vector else False,
                    fill_overlap=fill_overlap if vector else False,
                    zero_reuse=zero_reuse if vector else False,
                    overlap=overlap if vector else False,
                    pad=vector,
                )

    result = kt.Kernel.build(
        V.name(
            "quant",
            "walk",
            sw,
            levels,
            at,
            rows,
            cols,
            rp,
            cp,
            side,
            g,
            nk,
            batches,
            finite,
            *(["classifier_overlap"] if classifier_overlap else []),
            *(["pipeline"] if pipeline else []),
            *(["zero_classifier"] if zero_classifier else []),
            *(["stream_axes"] if stream_axes else []),
            *(["fill_overlap"] if fill_overlap else []),
            *(["zero_reuse"] if zero_reuse else []),
        ),
        body,
        ports,
    )
    got = (result, given)
    if KERNELS_KEPT > 0:
        _kernels[kkey] = got
        while len(_kernels) > KERNELS_KEPT:
            _kernels.popitem(last=False)
    return got


def mm_kernel(
    mm: Mm,
    e,
    extras: tuple,
    out: Operand,
    clusters: int,
    cores: int,
    *,
    shape_class=None,
    made=None,
    hw=None,
):
    """The kernel for `mm`: ports ``a`` (mx7a), ``b`` (mx7b), ``in1..`` the
    epilogue's operands (`Load(i)`), ``out``. Returns ``(kernel, given)``."""
    if shape_class is not None:
        return class_mm.kernel(mm, e, extras, out, clusters, cores, shape_class)
    extras = tuple(extras)
    made = dict(made or {})
    prepkey = tuple(sorted((i, tuple(p.shape() for p in ps)) for i, ps in made.items()))
    kkey = (
        "mm",
        prepkey,
        hw,
        mm,
        V.expr_key(e),
        extras,
        out,
        clusters,
        cores,
        V.L1_WORDS,
        V.L1_MARGIN,
        V.MAX_BLOCK,
        V.STAT_ROWS,
        V.bands_fit,
        V.port,
        V.split,
        kt.Kernel.build,
    )
    hit = _kernels.get(kkey)
    if hit is not None:
        _kernels.move_to_end(kkey)
        return hit
    ntm, ntn = mm.mp // mm.tm, mm.n // mm.tn
    tiles = ntm * ntn
    ncl = max(1, min(clusters, tiles))
    per = -(-tiles // ncl)
    lay_a = dict(gm=mm.tm // 4, nk=mm.nk)
    lay_b = dict(gn=mm.tn // 4, nk=mm.nk)
    nb_rows = mm.n if mm.shared else mm.bt * mm.n
    operands = (Operand("tile", pitch=mm.n, rows=mm.bt * mm.mp), *extras)
    wc = _block(mm, operands, out)
    bands = _bands(mm, operands, out, wc)
    pl = V.plan(operands, out.kind, mm.m, mm.n, bands=bands, wc=wc, quantum=mm.quantum)
    ports = {
        "a": kt.In(mm.bt * mm.mp, mm.kp, dtype="mx7a", layout=lay_a),
        "b": kt.In(nb_rows, mm.kp, dtype="mx7b", layout=lay_b),
    }
    given = {
        "a": kt.TensorSpec.make((mm.bt * mm.mp, mm.kp), "mx7a", lay_a),
        "b": kt.TensorSpec.make((nb_rows, mm.kp), "mx7b", lay_b),
    }
    for i, o in enumerate(extras, start=1):
        ports[f"in{i}"], given[f"in{i}"] = V.port(o, mm.bt * pl.rp, mm.n)
    ports["out"], given["out"] = V.port(out, mm.bt * pl.rp, mm.n)
    ports["out"] = kt.Out(*given["out"].shape)

    shapes = {i: given[f"in{i}"].shape for i in made}
    for i, plans in made.items():
        del ports[f"in{i}"], given[f"in{i}"]
        pp, pg = P.input_ports(plans, hw, f"p{i}_")
        ports.update(pp)
        given.update(pg)

    def batch(p, h, bt):
        for u in kt.par(ncl, on=kt.mg):
            for i in kt.range(per):
                t = u + ncl * i
                ti, tj = t // ntn, t % ntn
                if per * ncl != tiles:
                    with kt.when(t < tiles):
                        _tile(p, h, mm, bt, ti, tj)
                else:
                    _tile(p, h, mm, bt, ti, tj)
        bufs = {
            "in0": h,
            "out": p["out"],
            **{f"in{i}": p[f"in{i}"] for i in range(1, len(extras) + 1)},
        }
        V.rows_stage(
            bufs, e, operands, out, pl, cores, row0=bt * mm.mp, tile_rows=mm.tm
        )

    def body(**p):
        for i, plans in made.items():
            p[f"in{i}"] = P.input_buffer(plans, p, f"p{i}_", shapes[i], hw)
        h = kt.buffer((mm.bt * mm.mp, mm.n), "f16", tile=(mm.tm // 4, mm.tn // 4))
        if mm.bt == 1:
            batch(p, h, 0)
        else:
            for bt in kt.range(mm.bt):
                batch(p, h, bt)

    key = (mm, repr(e), extras, out, clusters, cores)
    got = (
        kt.Kernel.build(V.name("mm", *key, *([prepkey] if made else [])), body, ports),
        given,
    )
    if KERNELS_KEPT > 0:
        _kernels[kkey] = got
        while len(_kernels) > KERNELS_KEPT:
            _kernels.popitem(last=False)
    return got


def _tile(p, h, mm: Mm, bt, ti, tj) -> None:
    acc = kt.zero(mm.tm, mm.tn)
    arow = bt * mm.mp + ti * mm.tm
    brow = tj * mm.tn if mm.shared else bt * mm.n + tj * mm.tn
    for k in kt.range(mm.kp // mm.kc):
        a = kt.fill(p["a"][arow : arow + mm.tm, k * mm.kc : k * mm.kc + mm.kc])
        b = kt.fill(p["b"][brow : brow + mm.tn, k * mm.kc : k * mm.kc + mm.kc])
        acc = kt.gemm(a, b, acc)
    kt.drain(acc, h[arow : arow + mm.tm, tj * mm.tn : tj * mm.tn + mm.tn])


def _block(mm: Mm, operands, out) -> int:
    """The epilogue's column block: the widest block of 32-column blocks
    dividing the tile's width whose trip of `mm.quantum` rows fits the L1."""
    need = mm.quantum // 4
    blocks = [w for w in range(mm.tn, 31, -32) if mm.tn % w == 0]
    return next(
        (w for w in blocks if V.bands_fit(operands, out.kind, w, mm.n) >= need), 32
    )


def _bands(mm: Mm, operands, out, wc: int) -> int:
    """Bands a trip: the most that fit the L1 whose rows divide the tile, in
    whole quanta."""
    q = mm.quantum
    fit = V.plan(operands, out.kind, mm.m, mm.n, wc=wc, quantum=q).rows // 4
    return next(
        b
        for b in (4, 2, 1)
        if (b <= fit or 4 * b == q) and mm.tm % (4 * b) == 0 and (4 * b) % q == 0
    )


__all__ = [
    "KNOBS",
    "Mm",
    "kernel_knobs",
    "knobs",
    "mm_kernel",
    "plan",
    "plan_knobs",
    "quant_kernel",
    "quant_walk_kernel",
]
