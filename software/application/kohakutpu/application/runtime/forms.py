"""Making the form a kernel reads, and launching kernels.

A kernel reads f16: an f32 buffer has an f16 form (its values rounded
once), made from the host bytes it was uploaded from (packed on the way in)
or on the card from its f32 bytes. A row
statistic is read in the [n, 32] form (`kernels.convert.expand_kernel`); a
cluster operand in an MXFP7 packing of a 2-D view (`kernels.matmul.
quant_kernel`), kept for the next call while the buffer is unwritten.
"""

import hashlib
import os
import sys
from dataclasses import replace

import numpy as np

from kohakutpu.application.kernels import convert as C
from kohakutpu.application.kernels import matmul as MM
from kohakutpu.application.kernels import transpose as TP
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.runtime.storage import FLAT, Storage, StorageView
from kohakutpu.compiler import kernel as K

#: Set: each statistic-form transpose (`unexpand`) prints its size.
TRACE_ENV = "KOHAKUTPU_TRACE_FORMS"
F16 = ("f16",)
ROWSTAT = ("rowstat",)
#: ``("rows", n, np)``: rows of n f16 values held np apart (np whole 32-byte
#: words) -- what a kernel writes when n * 2 bytes is not whole words.
ROWS = "rows"


def rows_key(n: int, np_: int) -> tuple:
    return (ROWS, n, np_)


def padded(st: Storage) -> tuple | None:
    """The valid padded-rows form's key, when the row-major bytes are not."""
    if FLAT in st.valid:
        return None
    return next((k for k in st.valid if k[0] == ROWS), None)


class FormError(RuntimeError):
    """A form this machine cannot make from what the buffer holds."""


def launch(
    dev, kernel, given: dict, binds: dict, kind: str = "dsl", info=None, **consts
):
    """Compile `kernel` for `given` (cached) and run it with `binds`."""
    compiled = K.specialise(kernel, given, machine=dev.machine, **consts)
    run = dev.run(compiled, binds, kind=kind, info=info)
    run.info.setdefault("compiled", compiled)
    return run


def base_of(s):
    """``(storage, byte offset)`` of a storage or a view of one."""
    if isinstance(s, StorageView):
        return s.base, s.offset
    return s, 0


def f16(dev, s, dtype, n: int) -> int:
    """The address of `s`'s contents as f16 (`dtype` its element type, `n`
    its elements; a sub-buffer's inside its base's)."""
    st, off = base_of(s)
    item = np.dtype(dtype).itemsize
    if off and np.dtype(dtype) != np.float16:
        return f16(dev, st, dtype, st.nbytes // item) + off // item * 2
    if np.dtype(dtype) == np.float16:
        if FLAT in st.valid:
            return st.addr + off
        if ROWSTAT in st.valid:
            flatten(dev, st, st.nbytes // 2)
            return st.addr + off
        key = padded(st)
        if key is None:
            raise FormError("an f16 buffer with no current form, read row-major")
        compact(dev, st, key)
        return st.addr + off
    if F16 in st.valid:
        return st.forms[F16].addr
    f = st.form(F16, 2 * n)
    if ROWSTAT in st.valid:
        unexpand(dev, st.forms[ROWSTAT].addr, n, f.addr)
        st.made(F16)
        return f.addr
    key = padded(st)
    if key is not None:
        return compact(dev, st, key, np.dtype(dtype).itemsize)
    if st.host is not None:
        raw = np.frombuffer(st.host, dtype)[:n]
        dev.write(f.addr, raw.astype(np.float16).tobytes())
    elif FLAT in st.valid and np.dtype(dtype) == np.float32:
        k, given = C.cast_kernel(n, "f32", "f16", dev.machine.count("VC"))
        launch(dev, k, given, {"in0": st.addr, "out": f.addr}, kind="convert")
    else:
        raise FormError(f"{np.dtype(dtype)} values with no f16 form and no host bytes")
    st.made(F16)
    return f.addr


def expand(dev, src: int, n: int, dst: int) -> None:
    """n f16 values at `src` into their [n, 32] statistic form at `dst`
    (ceil16(n) * 64 bytes): every value a 64-byte row of its own."""
    k, given = C.expand_kernel(n, dev.machine.count("VC"))
    launch(dev, k, given, {"x": src, "out": dst}, kind="convert")


def unexpand(dev, src: int, n: int, dst: int) -> None:
    """The n values of an [n, 32] statistic form at `src` row-major at `dst`:
    the form transposed on card (`kernels.transpose`, exact) has them as its
    first row, copied over (whole words; the allocation's tail takes the
    rounding)."""
    np_ = -(-n // 16) * 16
    t = Storage(dev, np_ * 64)
    if os.environ.get(TRACE_ENV):
        print(
            f"unexpand n={n} np={np_} src={src:#x} dst={dst:#x}",
            file=sys.stderr,
            flush=True,
        )
    try:
        k, given = TP.transpose_kernel(1, np_, 32, dev.machine.count("VC"))
        mask = table(dev, TP.masks())
        launch(dev, k, given, {"x": src, "mask": mask, "y": t.addr}, kind="convert")

        k, given = C.copy_kernel(np_ * 2)
        launch(dev, k, given, {"src": t.addr, "dst": dst}, kind="convert")
    finally:
        t.free()


def stat_bytes(n: int) -> int:
    """Bytes of the [n, 32] statistic form of n values."""
    return -(-n // 16) * 16 * 64


def gather(
    dev,
    stat: int,
    n: int,
    walk: tuple,
    at: int,
    dwalk: tuple,
    m: int,
    dst: int,
    dat: int = 0,
    keep: bool = False,
) -> None:
    """`m` f16 values row-major at `dst` from the statistic form of `n`
    values at `stat` (`expand`): source element `at` walked by `walk`
    (``(count, element stride)``, outermost first) into destination elements
    walked by `dwalk` from element `dat`; a destination element no walk
    reaches is 0, or with `keep` what `dst` held (`m` whole words of it).
    One value is one 64-byte row there, so any strides are whole words."""
    g = Storage(dev, stat_bytes(m))
    try:
        if keep:
            expand(dev, dst, m, g.addr)
        else:
            k, given = C.fill_kernel(
                g.nbytes, ((g.nbytes // 1024, 1024), (32, 32)), 0, 0, 16
            )
            launch(dev, k, given, {"dst": g.addr}, kind="convert")

        levels = (*((c, 64 * s) for c, s in walk), (2, 32))
        dlevels = (*((c, 64 * s) for c, s in dwalk), (2, 32))
        k, given = C.walk_kernel(
            stat_bytes(n), levels, 64 * at, g.nbytes, dlevels, 64 * dat
        )
        launch(dev, k, given, {"src": stat, "dst": g.addr}, kind="convert")

        unexpand(dev, g.addr, m, dst)
    finally:
        g.free()


def flatten(dev, st: Storage, n: int) -> None:
    """A statistic's row-major bytes from its [n, 32] form (`unexpand`)."""
    unexpand(dev, st.forms[ROWSTAT].addr, n, st.addr)
    st.made(FLAT)


def repitch(
    dev, src: int, src_pitch: int, dst: int, dst_pitch: int, rows: int, w: int
) -> None:
    """`rows` rows of `w` f16 values from `src_pitch` apart to `dst_pitch`
    apart (both addresses on words): the mover where rows and pitches are
    whole 32-byte words, else through the statistic form (`gather`, exact;
    the destination between its rows 0)."""
    if not ((2 * w) % 32 or (2 * src_pitch) % 32 or (2 * dst_pitch) % 32):
        words = 2 * w // 32
        k, given = C.walk_kernel(
            rows * 2 * src_pitch + 32,
            ((rows, 2 * src_pitch), (words, 32)),
            0,
            rows * 2 * dst_pitch + 32,
            ((rows, 2 * dst_pitch), (words, 32)),
            0,
        )
        launch(dev, k, given, {"src": src, "dst": dst}, kind="convert")
        return
    n = (rows - 1) * src_pitch + w
    e = Storage(dev, stat_bytes(n))
    try:
        expand(dev, src, n, e.addr)
        walk, dwalk = ((rows, src_pitch), (w, 1)), ((rows, dst_pitch), (w, 1))
        gather(dev, e.addr, n, walk, 0, dwalk, (rows - 1) * dst_pitch + w, dst)
    finally:
        e.free()


def compact(dev, st: Storage, key: tuple, item: int = 2) -> int:
    """A padded-rows form (f16 values) made row-major (`repitch`): the flat
    bytes of an f16 buffer, else (`item`-byte elements) its f16 form.
    Returns that address."""
    _, w, wp = key
    rows = st.nbytes // (item * w)
    if item == 2:
        at, made = st.addr, FLAT
    else:
        at, made = st.form(F16, 2 * rows * w).addr, F16
    repitch(dev, st.forms[key].addr, wp, at, w, rows, w)
    st.made(made)
    return at


def rowstat(dev, s, dtype, n: int) -> int:
    """The address of the [n, 32] row-statistic form of `s` (of its base for
    a sub-buffer, whose values start ``offset / item`` rows in)."""
    st, _ = base_of(s)
    if ROWSTAT in st.valid:
        return st.forms[ROWSTAT].addr
    src = f16(dev, st, dtype, n)
    np_ = -(-n // 16) * 16
    f = st.form(ROWSTAT, np_ * 64)
    expand(dev, src, n, f.addr)
    st.made(ROWSTAT)
    return f.addr


def mx7(
    dev,
    s,
    dtype,
    src: Operand,
    rows: int,
    cols: int,
    mm: MM.Mm,
    side: str,
    batches: int,
) -> int:
    """The address of an MXFP7 packing of `batches` [rows, cols] blocks of
    `s` for matmul `mm` (A packing for `side` "a", tiles of tm, else B)."""
    g = mm.tm // 4 if side == "a" else mm.tn // 4
    rp = mm.mp if side == "a" else max(rows, mm.n)
    return packed(dev, s, dtype, src, rows, cols, rp, mm.kp, side, g, mm.nk, batches)


def packed(
    dev, s, dtype, src: Operand, rows, cols, rp, cp, side, g, nk, batches=1
) -> int:
    """The address of `batches` [rows, cols] blocks of `s` quantised into
    [batches * rp, cp] MXFP7 (A packing for `side` "a", else B; tiles of `g`
    lane groups, `nk` K-blocks a chunk), zero-padded; kept while `s` is
    unwritten."""
    st, off = base_of(s)
    item = np.dtype(dtype).itemsize
    src = replace(src, offset=src.offset + off // item)
    key = ("mx7", side, src, rows, cols, rp, cp, g, nk, batches)
    if key in st.valid:
        return st.forms[key].addr
    n = st.nbytes // item
    base = f16(dev, st, dtype, n)
    f = st.form(key, batches * rp * cp)

    tmps: list = []
    try:
        if cols % 16 or src.pitch % 16 or src.offset % 16:
            # Rows not of whole words: laid out ceil16(cols) apart first,
            # the extra columns zero (`gather`).
            R, dp = batches * rows, -(-cols // 16) * 16
            e = Storage(dev, stat_bytes(n))
            t = Storage(dev, 2 * -(-R * dp // 16) * 16)
            tmps += [e, t]
            expand(dev, base, n, e.addr)
            walk = ((R, src.pitch), (cols, 1))
            gather(
                dev, e.addr, n, walk, src.offset, ((R, dp), (cols, 1)), R * dp, t.addr
            )
            base, src, cols = t.addr, Operand("full", pitch=dp, rows=R), dp

        k, given = MM.quant_kernel(src, rows, cols, rp, cp, side, g, nk, batches)
        launch(dev, k, given, {"x": base, "q": f.addr}, kind="quant")
    finally:
        for t in tmps:
            t.free()

    st.made(key)
    return f.addr


def for_port(dev, s, dtype, shape: tuple, spec) -> int:
    """The address of `s` (`dtype` elements, logical `shape`, row-major) in
    the form a kernel port of `spec` reads: f16 (made if need be), f32 bytes
    as they are, or an MXFP7 packing of its last two axes."""
    st, off = base_of(s)
    n = int(np.prod(shape))
    if spec.dtype in ("mx7a", "mx7b"):
        if len(shape) < 2:
            raise FormError(f"an MXFP7 port takes a 2-D tensor, not {shape}")
        lay = spec.layout_dict
        rows, cols = int(np.prod(shape[:-1])), shape[-1]
        side = "a" if spec.dtype == "mx7a" else "b"
        g = lay["gm"] if side == "a" else lay["gn"]
        src = Operand("full", pitch=cols, rows=rows)
        return packed(
            dev,
            s,
            dtype,
            src,
            rows,
            cols,
            spec.shape[-2],
            spec.shape[-1],
            side,
            g,
            lay["nk"],
        )
    if spec.dtype == "f16":
        return f16(dev, s, dtype, n)
    if spec.dtype == "f32" and np.dtype(dtype) == np.float32 and FLAT in st.valid:
        return st.addr + off
    raise FormError(f"a {np.dtype(dtype)} tensor for a {spec.text()} port")


def table(dev, arr) -> int:
    """A folded constant uploaded once to `dev` (by content: `dev.tables`)."""
    a = np.ascontiguousarray(arr, np.float16)
    key = hashlib.sha1(a.tobytes() + repr(a.shape).encode()).hexdigest()
    got = dev.tables.get(key)
    if got is None:
        got = Storage(dev, max(a.nbytes, 32))
        dev.write(got.addr, a.tobytes())
        dev.tables[key] = got
    return got.addr


def read(dev, s, dtype, n: int) -> bytes:
    """`s`'s contents as `n` `dtype` values, from whichever form is current:
    row-major bytes, the f16 form, or column 0 of the statistic form."""
    st, off = base_of(s)
    dt = np.dtype(dtype)
    if FLAT in st.valid or off:
        return dev.read(st.addr + off, n * dt.itemsize)
    if F16 in st.valid:
        vals = np.frombuffer(dev.read(st.forms[F16].addr, 2 * n), np.float16)
        return vals.astype(dt).tobytes()
    if ROWSTAT in st.valid:
        raw = np.frombuffer(dev.read(st.forms[ROWSTAT].addr, n * 64), np.float16)
        return raw.reshape(n, 32)[:, 0].astype(dt).tobytes()
    key = padded(st)
    if key is not None:
        _, w, wp = key
        rows = -(-n // w)
        raw = np.frombuffer(dev.read(st.forms[key].addr, rows * wp * 2), np.float16)
        return raw.reshape(rows, wp)[:, :w].reshape(-1)[:n].astype(dt).tobytes()
    raise FormError("a buffer with no current form")


__all__ = [
    "F16",
    "ROWS",
    "ROWSTAT",
    "FormError",
    "base_of",
    "compact",
    "expand",
    "f16",
    "flatten",
    "for_port",
    "gather",
    "launch",
    "mx7",
    "packed",
    "padded",
    "read",
    "repitch",
    "rows_key",
    "rowstat",
    "stat_bytes",
    "table",
    "unexpand",
]
