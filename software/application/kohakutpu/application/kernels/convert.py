"""Form conversions on the card: a copy, a walk or a fill by the mover."""

from collections import OrderedDict

from kohakutpu.application.kernels import vector as V
from kohakutpu.language import dsl as kt

KERNELS_KEPT = 1024
_kernels: OrderedDict = OrderedDict()


def copy_kernel(nbytes: int):
    """`nbytes` (whole words, the allocation's tail covering the rest) from
    ``src`` to ``dst`` by the mover. Returns ``(kernel, given)``."""
    words = -(-nbytes // 32)
    kkey = ("copy", words, kt.Kernel.build)
    hit = _kernels.get(kkey)
    if hit is not None:
        _kernels.move_to_end(kkey)
        return hit
    ports = {"src": kt.In(words, 16), "dst": kt.Out(words, 16)}
    given = {k: kt.TensorSpec.make((words, 16)) for k in ports}

    def body(src, dst):
        kt.copy(dst, src)

    got = (kt.Kernel.build(V.name("copy", words, None), body, ports), given)
    if KERNELS_KEPT > 0:
        _kernels[kkey] = got
        while len(_kernels) > KERNELS_KEPT:
            _kernels.popitem(last=False)
    return got


def walk_kernel(
    src_bytes: int,
    levels: tuple,
    at: int,
    dst_bytes: int,
    dlevels: tuple,
    dat: int,
):
    """``dst`` walked by `dlevels` from byte `dat` := ``src`` walked by
    `levels` from byte `at` (``(count, byte stride)``, outermost first, the
    last of 32-byte words), by the mover. Returns ``(kernel, given)``."""
    levels = tuple((int(a), int(b)) for a, b in levels)
    dlevels = tuple((int(a), int(b)) for a, b in dlevels)
    sw, dw = -(-src_bytes // 32), -(-dst_bytes // 32)
    kkey = ("walk", sw, levels, at, dw, dlevels, dat, kt.Kernel.build)
    hit = _kernels.get(kkey)
    if hit is not None:
        _kernels.move_to_end(kkey)
        return hit
    ports = {"src": kt.In(sw, 16), "dst": kt.Out(dw, 16)}
    given = {"src": kt.TensorSpec.make((sw, 16)), "dst": kt.TensorSpec.make((dw, 16))}

    def body(src, dst):
        kt.copy(dst, src, walk=levels, at=at, dwalk=dlevels, dat=dat)

    got = (
        kt.Kernel.build(
            V.name("walk", sw, levels, at, dw, dlevels, dat, None), body, ports
        ),
        given,
    )
    if KERNELS_KEPT > 0:
        _kernels[kkey] = got
        while len(_kernels) > KERNELS_KEPT:
            _kernels.popitem(last=False)
    return got


def fill_kernel(dst_bytes: int, dlevels: tuple, dat: int, value: int, ew: int):
    """``dst`` walked by `dlevels` from byte `dat` := `value` (its low `ew`
    bits replicated), by the mover. Returns ``(kernel, given)``."""
    dlevels = tuple((int(a), int(b)) for a, b in dlevels)
    dw = -(-dst_bytes // 32)
    kkey = ("fill", dw, dlevels, dat, value, ew, kt.Kernel.build)
    hit = _kernels.get(kkey)
    if hit is not None:
        _kernels.move_to_end(kkey)
        return hit
    ports = {"dst": kt.Out(dw, 16)}
    given = {"dst": kt.TensorSpec.make((dw, 16))}

    def body(dst):
        kt.fill_value(dst, value, ew=ew, walk=dlevels, at=dat)

    got = (
        kt.Kernel.build(V.name("fill", dw, dlevels, dat, value, ew), body, ports),
        given,
    )
    if KERNELS_KEPT > 0:
        _kernels[kkey] = got
        while len(_kernels) > KERNELS_KEPT:
            _kernels.popitem(last=False)
    return got


__all__ = ["copy_kernel", "fill_kernel", "walk_kernel"]
