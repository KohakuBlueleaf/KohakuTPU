"""Form conversions on the card: a copy by the mover, a vector of values to
the row-statistic form, an element type to another.

The row-statistic form of n values is [n, 32] f16, value r in all 32
columns of row r: a GT4 band reads four rows of it as one register, the
value of each row in every lane (what `rmax` / `rsum` leave). `expand_kernel`
makes it from n contiguous f16 values (16 a word, so the rows go 16 a trip).
"""

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.language import dsl as kt


def copy_kernel(nbytes: int):
    """`nbytes` (whole words, the allocation's tail covering the rest) from
    ``src`` to ``dst`` by the mover. Returns ``(kernel, given)``."""
    words = -(-nbytes // 32)
    ports = {"src": kt.In(words, 16), "dst": kt.Out(words, 16)}
    given = {k: kt.TensorSpec.make((words, 16)) for k in ports}

    def body(src, dst):
        kt.copy(dst, src)

    return kt.Kernel.build(V.name("copy", words), body, ports), given


def walk_kernel(
    src_bytes: int, levels: tuple, at: int, dst_bytes: int, dlevels: tuple, dat: int
):
    """``dst`` walked by `dlevels` from byte `dat` := ``src`` walked by
    `levels` from byte `at` (``(count, byte stride)``, outermost first, the
    last of 32-byte words), by the mover. Returns ``(kernel, given)``."""
    sw, dw = -(-src_bytes // 32), -(-dst_bytes // 32)
    ports = {"src": kt.In(sw, 16), "dst": kt.Out(dw, 16)}
    given = {"src": kt.TensorSpec.make((sw, 16)), "dst": kt.TensorSpec.make((dw, 16))}

    def body(src, dst):
        kt.copy(dst, src, walk=levels, at=at, dwalk=dlevels, dat=dat)

    return (
        kt.Kernel.build(V.name("walk", sw, levels, at, dw, dlevels, dat), body, ports),
        given,
    )


def fill_kernel(dst_bytes: int, dlevels: tuple, dat: int, value: int, ew: int):
    """``dst`` walked by `dlevels` from byte `dat` := `value` (its low `ew`
    bits replicated), by the mover. Returns ``(kernel, given)``."""
    dw = -(-dst_bytes // 32)
    ports = {"dst": kt.Out(dw, 16)}
    given = {"dst": kt.TensorSpec.make((dw, 16))}

    def body(dst):
        kt.fill_value(dst, value, ew=ew, walk=dlevels, at=dat)

    return (
        kt.Kernel.build(V.name("fill", dw, dlevels, dat, value, ew), body, ports),
        given,
    )


def expand_kernel(n: int, cores: int = 2):
    """n f16 values -> their [n, 32] row-statistic form."""
    rows = 16
    nb = -(-n // rows)
    rp = nb * rows
    ports = {"x": kt.In(rp), "out": kt.Out(rp, 32)}
    given = {"x": kt.TensorSpec.make((rp,)), "out": kt.TensorSpec.make((rp, 32))}

    def body(x, out):
        ncore, start, trips = V.split(nb, cores)
        for c in kt.par(ncore, on=kt.vc):
            for t in kt.range(trips(c)):
                r = start(c) * rows + t * rows
                kt.store(out[r : r + rows, :], kt.unpack(kt.load(x[r : r + rows])))

    return kt.Kernel.build(V.name("expand", n), body, ports), given


def cast_kernel(n: int, src: str, dst: str, cores: int = 2):
    """n values of `src` (f16 / f32) rewritten as `dst`."""
    return V.flat_kernel(X.Load(0), (src,), n, dst, cores)


__all__ = ["cast_kernel", "copy_kernel", "expand_kernel", "fill_kernel", "walk_kernel"]
