"""Generated contractions: ``out = epilogue(A @ B^T, extras)`` on the clusters
and the vector cores, as one DSL kernel.

A and B are MXFP7 forms the runtime makes once (`quant_kernel`: the mover's
quantiser, through a zero-padded copy when the extents are not whole
groups): A [Bt * Mp, Kp] in the A packing, B [Np, Kp] (or one a batch) in
the B packing. The clusters sweep tiles of tm x tn, K in chunks of 32 * nk,
and drain each tile into a ``tile=`` scratch; the vector cores read it a
band at a time, apply the epilogue (its `Load(0)` the product; bias,
residual, statistics as `vector.Operand`s) and store row-major f16.
"""

from dataclasses import dataclass

from kohakutpu.application.kernels import vector as V
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.language import dsl as kt

ACC_SUBTILES = 4096
BANK = 256


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

    @property
    def kc(self) -> int:
        return 32 * self.nk


def plan(bt: int, m: int, n: int, k: int, shared: bool = True) -> Mm:
    """The tiling for an [M, K] x [N, K] product, `bt` of them. Raises
    ValueError for an N that is not whole 32-column blocks (the epilogue
    stores whole blocks) or a batched M that is not whole groups of 4."""
    if n % 32:
        raise ValueError(f"N = {n}: the product's columns are whole blocks of 32")
    if bt > 1 and m % 4:
        raise ValueError(f"M = {m} in a batch: whole groups of 4 rows")
    kp = -(-k // 32) * 32
    nk = 2 if kp % 64 == 0 else 1
    mp = -(-m // 4) * 4
    tm = next(
        t for t in range(min(256, mp), 3, -4) if mp % t == 0 and (t // 4) * nk <= BANK
    )
    tn = V.block(n)
    while (tm // 4) * (tn // 4) > ACC_SUBTILES:
        tn //= 2
    return Mm(bt, m, n, k, mp, kp, tm, tn, nk, shared)


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
):
    """`batches` row blocks of [rows, cols] (each `src.offset + b * rows *
    pitch`) quantised into [batches * rp, cp] in the A (`side` "a") or B
    packing, tiles of `g` lane groups. Padding rows and columns are zero."""
    dtype = "mx7a" if side == "a" else "mx7b"
    lay = dict(gm=g, nk=nk) if side == "a" else dict(gn=g, nk=nk)
    if cols % 16 or src.pitch % 16 or src.offset % 16:
        raise ValueError(
            f"a quantised operand's rows are whole 32-byte words: {cols} columns, "
            f"pitch {src.pitch}, offset {src.offset}"
        )
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

    def body(x, q):
        if direct:
            kt.quant(q, x[0 : batches * rp, :])
            return
        z = kt.buffer((batches * rp, cp), "f16")
        kt.fill_value(z, 0)
        r0, c0 = divmod(src.offset, src.pitch)
        for b in range(batches):
            kt.copy(
                z[b * rp : b * rp + rows, 0:cols],
                x[r0 + b * rows : r0 + b * rows + rows, c0 : c0 + cols],
            )
        kt.quant(q, z)

    key = (src, rows, cols, rp, cp, side, g, nk, batches)
    return kt.Kernel.build(V.name("quant", *key), body, ports), given


def mm_kernel(
    mm: Mm, e, extras: tuple, out: Operand, clusters: int = 4, cores: int = 2
):
    """The kernel for `mm`: ports ``a`` (mx7a), ``b`` (mx7b), ``in1..`` the
    epilogue's operands (`Load(i)`), ``out``. Returns ``(kernel, given)``."""
    ntm, ntn = mm.mp // mm.tm, mm.n // mm.tn
    tiles = ntm * ntn
    ncl = max(1, min(clusters, tiles))
    per = -(-tiles // ncl)
    lay_a = dict(gm=mm.tm // 4, nk=mm.nk)
    lay_b = dict(gn=mm.tn // 4, nk=mm.nk)
    nb_rows = mm.n if mm.shared else mm.bt * mm.n
    operands = (Operand("tile", pitch=mm.n, rows=mm.bt * mm.mp), *extras)
    bands = _bands(mm, operands, out)
    pl = V.plan(operands, out.kind, mm.m, mm.n, bands=bands, wc=mm.tn)
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
        h = kt.buffer((mm.bt * mm.mp, mm.n), "f16", tile=(mm.tm // 4, mm.tn // 4))
        if mm.bt == 1:
            batch(p, h, 0)
        else:
            for bt in kt.range(mm.bt):
                batch(p, h, bt)

    key = (mm, repr(e), extras, out, clusters, cores)
    return kt.Kernel.build(V.name("mm", *key), body, ports), given


def _tile(p, h, mm: Mm, bt, ti, tj) -> None:
    acc = kt.zero(mm.tm, mm.tn)
    arow = bt * mm.mp + ti * mm.tm
    brow = tj * mm.tn if mm.shared else bt * mm.n + tj * mm.tn
    for k in kt.range(mm.kp // mm.kc):
        a = kt.fill(p["a"][arow : arow + mm.tm, k * mm.kc : k * mm.kc + mm.kc])
        b = kt.fill(p["b"][brow : brow + mm.tn, k * mm.kc : k * mm.kc + mm.kc])
        acc = kt.gemm(a, b, acc)
    kt.drain(acc, h[arow : arow + mm.tm, tj * mm.tn : tj * mm.tn + mm.tn])


def _bands(mm: Mm, operands, out) -> int:
    """Bands a trip: the most that fit the L1 whose rows divide the tile."""
    fit = V.plan(operands, out.kind, mm.m, mm.n, wc=mm.tn).rows // 4
    return next(b for b in (4, 2, 1) if b <= fit and mm.tm % (4 * b) == 0)


__all__ = ["Mm", "mm_kernel", "plan", "quant_kernel"]
