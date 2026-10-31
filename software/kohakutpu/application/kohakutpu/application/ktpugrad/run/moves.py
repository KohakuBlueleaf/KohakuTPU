"""A read as a data movement: gather, transpose, flip, coalesce."""

from dataclasses import replace

import numpy as np
from tinygrad.dtype import dtypes

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.ktpugrad import fold as F
from kohakutpu.application.ktpugrad import uops as U
from kohakutpu.application.ktpugrad.launch_config import (
    _active_scope,
    elide_rule,
    view_forms_enabled,
)
from kohakutpu.application.ktpugrad.lower import Access, Plan
from kohakutpu.application.ktpugrad.memo import memo
from kohakutpu.application.ktpugrad.run.common import (
    ROW_ALIGN,
    _dense,
    _half_target,
    _live,
    _mult,
    _n,
    _out_coef,
    _size,
    _source,
    _walk,
    _window_ok,
    _words,
    _wrote_flat,
)
from kohakutpu.application.runtime import forms as FM
from kohakutpu.application.runtime import rows as RW
from kohakutpu.application.runtime.storage import FLAT, Storage
from kohakutpu.language.numerics import fp16


def _move(dev, plan: Plan, bufs) -> list | None:
    """A read stored as it is, walked in whole words (a slice, a permute of
    the outer axes, a flip of one, a broadcast row): one mover copy."""
    acc = plan.accesses[0]
    src = _source(dev, plan, acc, bufs)
    if src is None:
        return None
    base, nbytes, item, soff = src
    dst, doff = FM.base_of(bufs[plan.out_slot])
    at = soff + acc.off * item
    if doff % 32 or not _words(plan, acc, item, at):
        return None
    levels = _walk(plan, dict(acc.coef), item)
    dlevels = _walk(plan, _out_coef(plan), item)
    assert levels is not None and dlevels is not None
    _window_ok(dev, dst, plan)
    out_item = np.dtype(F.np_dtype(plan.out_dtype)).itemsize
    if (
        elide_rule("identity_walk")
        and base + at == dst.addr + doff
        and tuple(levels) == tuple(dlevels)
        and len(levels) == 1
        and RW.MUTANT is None
    ):
        _wrote_flat(dst, plan)
        return []
    if (
        view_forms_enabled()
        and isinstance(plan.out_slot, int)
        and isinstance(acc.slot, int)
        and not doff
        and plan.out_strides is None
        and item == 2
        and dtypes.is_float(plan.out_dtype)
        and plan.out_n * out_item == dst.nbytes
        and tuple(dlevels) == ((-(-dst.nbytes // 32), 32),)
        and base != dst.addr
        and _active_scope(dev) is None
    ):
        if RW.MUTANT == "drop_member":
            return []
        walk_lvls = (
            tuple(reversed(levels))
            if RW.MUTANT == "wrong_order" and len(levels) > 1
            else tuple(levels)
        )
        at_use = (
            at + 32
            if RW.MUTANT == "wrong_order" and len(levels) == 1 and at + 32 < nbytes
            else at
        )
        coef_map = dict(acc.coef)
        coef_ext = tuple((U.extent(r), coef_map.get(r, 0)) for r in plan.out_ranges)
        vkey = FM.view_key(
            nbytes,
            walk_lvls,
            at_use,
            plan.out_shape,
            coef_ext,
            acc.off,
        )
        sst_base = FM.base_of(bufs[acc.slot])[0]
        target = sst_base if RW.MUTANT == "alias_members" else dst
        target.share_form(vkey, base, nbytes, src_st=sst_base)
        target.wrote(vkey)
        target.dtype = F.np_dtype(plan.out_dtype)
        return []
    run = FM.generated.walk(
        dev,
        nbytes,
        levels,
        at,
        dst.nbytes,
        dlevels,
        doff,
        {"src": base, "dst": dst.addr},
        "move",
        info={"plan": plan.name},
    )
    _wrote_flat(dst, plan)
    return [run]


def _transpose(dev, plan: Plan, bufs) -> list | None:
    """A read stored with its innermost axis moved (a transpose): the mover
    puts the two axes last, `kernels.transpose` swaps them on card
    (exact), the mover moves the result into place; an identity move is
    skipped. None when it is not one (or not f16)."""
    acc = plan.accesses[0]
    if plan.out_dtype != dtypes.half:
        return None
    live = [r for r in plan.out_ranges if U.extent(r) > 1]
    if len(live) < 2:
        return None
    r_in = live[-1]
    r_s = next((r for r in live if acc.c(r) == 1), None)
    if r_s is None or r_s is r_in:
        return None
    A, B = U.extent(r_in), U.extent(r_s)
    if A % 16:
        return None  # rows not of whole words: `_gather`
    # A side of B not whole words is read 16-padded (the extra lanes, inside
    # the buffer, dropped after): a narrow channel count's NHWC -> NCHW.
    Bp = -(-B // 16) * 16
    rsp = (
        r_s if Bp == B else r_s.replace(src=(r_s.src[0].replace(arg=Bp), *r_s.src[1:]))
    )
    others = [r for r in live if r not in (r_in, r_s)]
    nb = int(np.prod([U.extent(r) for r in others])) if others else 1
    n = nb * A * Bp
    runs: list = []
    found = _source(dev, plan, acc, bufs)
    if found is None:
        return None
    base, nbytes, item, off = found
    if Bp != B:
        last = off // item + acc.off + Bp - 1
        last += sum(
            c * (U.extent(r) - 1) for r, c in acc.coef if r is not r_s and c > 0
        )
        if last >= nbytes // item:
            return None
    read1 = replace(acc, coef=tuple((rsp if r is r_s else r, c) for r, c in acc.coef))
    order1 = [*others, r_in, rsp]
    sub = replace(
        plan,
        name=f"{plan.name}_p",
        out_slot=("tr1", id(plan)),
        out_dtype=dtypes.half,
        out_ranges=tuple(order1),
        out_shape=tuple(U.extent(r) for r in order1),
        out_strides=None,
        accesses=[read1],
    )
    staged = not (
        Bp == B
        and {r: c for r, c in acc.coef if U.extent(r) > 1} == _dense(order1)
        and item == 2
        and not (off + 2 * acc.off) % 32
    )
    order2 = [*others, r_s, r_in]
    dst, doff = FM.base_of(bufs[plan.out_slot])
    oc = {r: c for r, c in _out_coef(plan).items() if U.extent(r) > 1}
    direct = Bp == B and oc == _dense(order2) and not doff % 32
    # The first B of each Bp rows, read back into the output.
    coef2 = _dense([*others, rsp, r_in])
    key2 = ("tr2", id(plan))
    read = Access(
        key2,
        dtypes.half,
        tuple((r_s if r is rsp else r, c) for r, c in coef2.items()),
        0,
    )
    post = replace(plan, expr=X.Load(0), accesses=[read])
    # Every move in whole words before anything runs; else not one.
    if staged and not _words(sub, read1, item, off + item * acc.off):
        return None
    if not direct and (doff % 32 or not _words(post, read, 2, 0)):
        return None
    tmps = []
    try:
        if not staged:
            src = base + off + 2 * acc.off
        else:
            t1 = Storage(dev, 2 * n)
            tmps.append(t1)
            got = _move(dev, sub, {**bufs, sub.out_slot: t1})
            assert got is not None
            runs += got
            src = t1.addr
        if direct:
            _window_ok(dev, dst, plan)
            out = dst.addr + doff
        else:
            t2 = Storage(dev, 2 * n)
            tmps.append(t2)
            out = t2.addr
        runs.append(
            FM.generated.launch(
                dev,
                "transpose.transpose_kernel",
                {"nb": int(nb), "A": int(A), "B": int(Bp)},
                {
                    "x": src,
                    "mask": FM.table(dev, FM.generated.transpose_masks(dev), np.uint16),
                    "y": out,
                },
                kind="transpose",
                info={"plan": plan.name},
            )
        )
        if direct:
            _wrote_flat(dst, plan)
            return runs
        got = _move(dev, post, {**bufs, key2: tmps[-1]})
        assert got is not None
        return runs + got
    finally:
        for t in tmps:
            t.free()


def _gather(dev, plan: Plan, bufs) -> list | None:
    """Any read into an f16 output, element by element: the source's f16
    elements walked into the output's (`forms.relayout`, exact). A dense output from
    a word is written whole; any other (a window, an offset) keeps what its
    words held between its elements. None for a table, a non-f16 output or
    a buffer with no f16 form."""
    acc = plan.accesses[0]
    if acc.slot is None or plan.out_dtype != dtypes.half:
        return None
    live = [r for r in plan.out_ranges if U.extent(r) > 1]
    n = _size(live)
    item = np.dtype(F.np_dtype(acc.dtype)).itemsize
    if FM.base_of(bufs[acc.slot])[1] // item + acc.off < 0:
        return None
    coef, oc = dict(acc.coef), _out_coef(plan)
    walk = tuple((U.extent(r), coef.get(r, 0)) for r in live)
    dwalk = tuple((U.extent(r), oc[r]) for r in live)
    view = bufs[plan.out_slot]
    dst, doff = FM.base_of(view)
    # The gather writes whole words: a dense output owns the last one's rest
    # (a whole buffer ends in its allocation's tail).
    whole = (
        {r: oc[r] for r in live} == _dense(live)
        and doff % 32 == 0
        and (
            n % 16 == 0
            or view.nbytes >= 2 * -(-n // 16) * 16
            or (not doff and dst.nbytes == 2 * n)
        )
    )
    site = ("gather", acc.dtype, walk, acc.off, dwalk)
    held = None
    if whole and FM.hints(dev).get(site) == "rows":
        held = _rows_out(dst, doff, dwalk, n)
    if held is not None:
        # Its consumers read its rows padded to whole words (`forms.hints`):
        # written so, zero-padded, whole lines.
        key, at, dwalk, m = held
        if not _gather_to(dev, acc, bufs, walk, acc.off, dwalk, 0, m, at):
            return None
        dst.wrote(key)
        dst.zeroed.add(key)
        dst.dtype = F.np_dtype(plan.out_dtype)
        dst.site = (site, dwalk[-1][0])
        return []
    at = dst.addr + doff // 32 * 32
    dat = doff % 32 // 2
    m = n if whole else -(-(dat + 1 + sum((e - 1) * s for e, s in dwalk)) // 16) * 16
    if not whole:
        _window_ok(dev, dst, plan)
    if not _gather_to(dev, acc, bufs, walk, acc.off, dwalk, dat, m, at, keep=not whole):
        return None
    _wrote_flat(dst, plan)
    if whole and dwalk and dwalk[-1][1] == 1:
        dst.site = (site, dwalk[-1][0])
    return []


def _rows_out(dst: Storage, doff: int, dwalk, n: int) -> tuple | None:
    """``(form key, address, walk, values)`` of a dense output of a whole
    buffer held as padded rows: its innermost axis a row of w values (not
    whole words) padded to whole lines, each outer step of w one of the
    pitch; None when its rows are whole words or it is part of a buffer."""
    if doff or dst.nbytes != 2 * n or not dwalk or dwalk[-1][1] != 1:
        return None
    w = dwalk[-1][0]
    if w % 16 == 0 or n % w:
        return None
    wp = -(-w // ROW_ALIGN) * ROW_ALIGN
    walk = tuple((e, s // w * wp) for e, s in dwalk[:-1]) + (dwalk[-1],)
    if any(s % w for _, s in dwalk[:-1]):
        return None
    key = FM.rows_key(w, wp)
    m = n // w * wp
    return key, dst.form(key, 2 * m).addr, walk, m


def _gather_to(dev, acc, bufs, walk, first, dwalk, dat, n, dst, keep=False) -> bool:
    """`n` f16 values at `dst` from `acc`'s buffer as f16 (`forms.relayout`):
    element `first` (from `acc`'s view) walked by `walk` into elements
    walked by `dwalk` from `dat`, every other one zero (with `keep`, as `dst`
    held it). False when the buffer has no f16 form."""
    st, off = FM.base_of(bufs[acc.slot])
    dt = F.np_dtype(acc.dtype)
    N = _n(st, dt)
    first += off // np.dtype(dt).itemsize
    try:
        src = FM.f16(dev, st, dt, N)
    except FM.FormError:
        return False
    FM.relayout(dev, src, N, walk, first, dwalk, n, dst, dat, keep)
    return True


@memo
def _coalesce(plan: Plan) -> Plan:
    """Adjacent output ranges every read (and the output) walks as one run
    merged into one range: [h, w] read at strides (W*s, s) is one axis."""
    out = dict(_out_coef(plan))
    rs = [r for r in plan.out_ranges if U.extent(r) > 1]
    accs = list(plan.accesses)
    i = len(rs) - 2
    while i >= 0:
        r1, r2 = rs[i], rs[i + 1]
        e2 = U.extent(r2)
        if all(a.c(r1) == a.c(r2) * e2 for a in accs) and out[r1] == out[r2] * e2:
            r = r2.replace(src=(r2.src[0].replace(arg=U.extent(r1) * e2), *r2.src[1:]))
            accs = [
                replace(
                    a,
                    coef=tuple(
                        (r if q is r2 else q, c) for q, c in a.coef if q is not r1
                    ),
                )
                for a in accs
            ]
            out = {(r if q is r2 else q): c for q, c in out.items() if q is not r1}
            rs[i : i + 2] = [r]
        i -= 1
    if len(rs) == len([r for r in plan.out_ranges if U.extent(r) > 1]):
        return plan
    strides = tuple(out[r] for r in rs)
    dense = strides == tuple(_mult(rs))
    return replace(
        plan,
        out_ranges=tuple(rs),
        out_shape=tuple(U.extent(r) for r in rs),
        accesses=accs,
        out_strides=None if dense else strides,
    )


def _flip(dev, plan: Plan, bufs) -> list | None:
    """A read walking its innermost axis backwards (``x.flip(-1)``): the
    mover walks each row's words last to first, the vector core reverses
    each word's 16 lanes (`kernels.transpose.reverse_kernel`). None when it
    is not one."""
    acc = plan.accesses[0]
    if acc.slot is None or acc.dtype != plan.out_dtype or acc.dtype != dtypes.half:
        return None
    live = [r for r in plan.out_ranges if U.extent(r) > 1]
    if not live or acc.c(live[-1]) != -1:
        return None
    C = U.extent(live[-1])
    st, off = FM.base_of(bufs[acc.slot])
    start = off + 2 * (acc.off - 15)
    others = live[:-1]
    if (
        C % 16
        or start % 32
        or start < 0
        or any(2 * acc.c(r) % 32 for r in others)
        or FLAT not in st.valid
    ):
        return None  # not whole 32-byte words: `_gather`
    levels = (*((U.extent(r), 2 * acc.c(r)) for r in others), (C // 16, -32))
    n = int(np.prod([U.extent(r) for r in live]))
    dst, doff = FM.base_of(bufs[plan.out_slot])
    oc = {r: c for r, c in _out_coef(plan).items() if U.extent(r) > 1}
    whole = oc == _dense(live) and doff == 0 and 2 * n == dst.nbytes
    key = ("flip", id(plan))
    post = replace(
        plan, accesses=[Access(key, dtypes.half, tuple(_dense(live).items()), 0)]
    )
    if not whole and (doff % 32 or not _words(post, post.accesses[0], 2, 0)):
        return None  # its output not whole words: `_gather`
    t = None if whole else Storage(dev, 2 * n)
    try:
        arguments = {"src_bytes": st.nbytes, "levels": levels, "at": start, "n": n}
        out = dst.addr if t is None else t.addr
        runs = [
            FM.generated.launch(
                dev,
                "transpose.reverse_kernel",
                arguments,
                {"x": st.addr, "y": out},
                kind="flip",
                info={"plan": plan.name},
            )
        ]
        if t is None:
            _wrote_flat(dst, plan)
            return runs
        moved = _move(dev, post, {**bufs, key: t})
        assert moved is not None
        return runs + moved
    finally:
        if t is not None:
            t.free()


def _fill(dev, plan: Plan, bufs, value: float) -> list:
    """A box of the loop nest that is one value (a padding): the mover's
    fill over the output window where it is whole words, else the value
    moved in from a temporary (`_moved`: any window)."""
    item = np.dtype(F.np_dtype(plan.out_dtype)).itemsize
    dst, doff = FM.base_of(bufs[plan.out_slot])
    dlevels = _walk(plan, _out_coef(plan), item)
    if dlevels is None or doff % 32 or item == 8:
        live = _live(plan.out_ranges)
        t = Storage(dev, max(32, 2 * _size(live)))
        t.dtype = np.float16
        try:
            dev.write(
                t.addr, fp16.write(np.full(_size(live), value, np.float64)).tobytes()
            )
            tplan, tbufs, finish = _half_target(dev, plan, bufs)
            runs = _moved(dev, tplan, tbufs, t, _dense(live))
            finish()
        finally:
            t.free()
        return runs
    dtype = F.np_dtype(plan.out_dtype)
    encoded = fp16.write(value) if dtype == np.float16 else np.asarray(value, dtype)
    bits = encoded.reshape(1).view({1: np.uint8, 2: np.uint16, 4: np.uint32}[item])
    _window_ok(dev, dst, plan)
    run = FM.generated.fill(
        dev,
        dst.nbytes,
        dlevels,
        doff,
        bits[0],
        8 * item,
        {"dst": dst.addr},
        info={"plan": plan.name},
    )
    _wrote_flat(dst, plan)
    return [run]


def _bare(dev, plan: Plan, bufs) -> list | None:
    """A bare read into the output (`plan.expr` one `Load`): the mover's word
    walk, a transpose, a flip, else element by element (`_gather`); None
    where none is one (a table, a non-f16 output no word walk takes)."""
    one = _coalesce(plan)
    for way in (_move, _transpose, _flip, _gather):
        got = way(dev, one, bufs)
        if got is not None:
            return got
    return None


def _moved(dev, plan: Plan, bufs, t: Storage, coef: dict) -> list:
    """`t` (f16 values at element strides `coef` over `plan`'s output
    ranges) moved into `plan`'s f16 output by the bare-read kernels."""
    key = ("placed", id(plan))
    read = Access(key, dtypes.half, tuple(coef.items()), 0)
    sub = replace(
        plan,
        kind="vector",
        expr=X.Load(0),
        accesses=[read],
        reduce_ranges=(),
        reductions={},
    )
    runs = _bare(dev, sub, {**bufs, key: t})
    assert runs is not None  # an f16 output: `_gather` takes any read
    return runs
