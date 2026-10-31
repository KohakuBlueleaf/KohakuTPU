"""Padded and windowed reads as lane copies."""

import numpy as np
from tinygrad.dtype import dtypes

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.ktpugrad import uops as U
from kohakutpu.application.ktpugrad.planning import fold as F
from kohakutpu.application.ktpugrad.planning import window as W
from kohakutpu.application.ktpugrad.planning.lower import Access, Plan, Refused
from kohakutpu.application.ktpugrad.run.common import _dense, _n, _size
from kohakutpu.application.ktpugrad.run.moves import _gather_to, _move
from kohakutpu.application.ktpugrad.run.shape import _f16_walk
from kohakutpu.application.runtime import forms as FM
from kohakutpu.application.runtime.memory.storage import FLAT, Storage
from kohakutpu.language.config import ENV

#: Whether a window product may run with its sides swapped (B's outer ranges
#: its rows) when that is the output's order.
WINDOW_SWAP = ENV.get("KTPU_WINDOW_SWAP", "0") == "1"


def _window_swapped(plan: Plan, c) -> bool:
    """Whether window contraction `c` runs as ``[batch.., B outer.., A
    outer..]``: the output's innermost range is one of A's outer ranges
    (normally the product's rows), so the product in its own order would
    reach the output only transposed."""
    live = [r for r in plan.out_ranges if U.extent(r) > 1]
    ma = {r for r in c.ma if U.extent(r) > 1}
    return WINDOW_SWAP and bool(live) and live[-1] in ma


def _window_lead(plan: Plan, c) -> list:
    """A's outer ranges before every B outer range in the output's order:
    a swapped product's batch ranges (B, not reading them, read the same in
    each batch)."""
    live = [r for r in plan.out_ranges if U.extent(r) > 1]
    nb = [live.index(r) for r in c.nb if r in live]
    if not nb:
        return []
    first = min(nb)
    return [r for r in c.ma if r in live and live.index(r) < first]


def _lay(dev, plan: Plan, bufs, tmps, c, side, outer, lane, key, defer=None) -> list:
    """`side`'s read dense at ``bufs[key]`` as f16 ``[outer.., c.ks..,
    lane]``: its padded source (`_padded`) walked by the mover, every step
    whole words (the lane innermost, whole words). Both steps a
    `forms.Pending` for the packing's kernel when `forms.PREPARED`."""
    if defer is None:
        defer = FM.PREPARED and FM.PREPARED_QUANT
    src, strides, off, step = _padded(
        dev, plan, bufs, tmps, c, side, side is c.a, defer
    )
    order = [*outer, *c.ks, lane]
    held = {r for d in side.dims for r in d.ranges()}
    # A range the side does not read (a swapped product's batch): stride 0.
    coef = {
        r: side.mult(r) * strides[side.dim(r)] if r in held else 0
        for r in [*outer, *c.ks]
    }
    coef[lane] = 1
    if step is None and coef == {r: s for r, s in _dense(order).items()} and off == 0:
        bufs[key] = bufs[src]
        return []
    if defer:
        live = [r for r in order if U.extent(r) > 1]
        dense = _dense(live)
        walk = tuple((U.extent(r), coef[r]) for r in live)
        dwalk = tuple((U.extent(r), dense[r]) for r in live)
        n = _size(live)
        if step is not None:
            steps = (
                (step,)
                if walk == dwalk and off == 0 and step.m == n
                else (step, FM.Step(None, step.m, walk, off, dwalk, 0, n))
            )
        else:
            st, voff = FM.base_of(bufs[src])
            N = st.nbytes // 2
            at = FM.f16(dev, st, np.float16, N)
            steps = (FM.Step(at, N, walk, off + voff // 2, dwalk, 0, n),)
        if FM.COMPOSED == "off" and not FM.costs.has_steps(steps, dev.machine.board):
            return _lay(dev, plan, bufs, tmps, c, side, outer, lane, key, defer=False)
        pend = FM.Pending(dev, 2 * max(n, 16), steps)
        tmps.append(pend)
        bufs[key] = pend
        return []
    t = Storage(dev, max(32, 2 * _size(order)))
    tmps.append(t)
    t.wrote(FLAT)
    bufs[key] = t
    sub = Plan(
        "vector",
        plan.ast,
        f"{plan.name}_w",
        key,
        dtypes.half,
        tuple(U.extent(r) for r in order),
        tuple(order),
        expr=X.Load(0),
        accesses=[Access(src, dtypes.half, tuple(coef.items()), off)],
    )
    runs = _move(dev, sub, bufs)
    if runs is None:
        raise Refused("a window operand not walked in whole words")
    return runs


def _padded(dev, plan: Plan, bufs, tmps, c, side, copies=False, defer=False) -> tuple:
    """``(slot, {coordinate: element stride}, element of coordinate 0,
    step)`` of `side`'s source with its lane innermost in whole words and
    zero outside its gate's box: the buffer itself when it is so already
    (step None), else gathered (`_gather_to`) into ``[coordinates.., lane]``
    -- with `defer`, that gather's `forms.Step` (slot None), not run. A lane
    of one with `copies` (the other side's lanes zero) holds 16 copies of
    each value (`_lane_copies`): no transpose."""
    acc = side.acc
    axes = [d for d in side.dims if d is not side.lane]
    full = all(d.box == (0, d.span) for d in side.dims)
    st, voff = FM.base_of(bufs[acc.slot])
    lane = side.lane
    if (
        full
        and lane is not None
        and lane.stride == 1
        and U.extent(lane.ranges()[0]) == c.pad
        and all(d.stride % W.WORD == 0 for d in axes)
        and (voff // 2 + acc.off) % W.WORD == 0
        and acc.dtype == dtypes.half
        and FLAT in st.valid
    ):
        return acc.slot, {d: d.stride for d in side.dims}, acc.off, None
    key = ("pad", id(side), id(plan))
    return _pad_copy(dev, bufs, tmps, acc, axes, key, lane, c.pad, copies, defer)


def _pad_copy(
    dev, bufs, tmps, acc, axes, key, lane=None, pad=1, copies=False, defer=False
):
    """``(key, {coordinate: element stride}, element of coordinate 0, step)``:
    `acc`'s source over its coordinates (`axes` outermost first, then the
    `lane` innermost, `pad` elements wide) gathered dense as f16 into
    ``bufs[key]``, every element outside the coordinates' boxes zero (with
    `defer`, that gather as a `forms.Step`, key None, nothing run). A lane
    of one with `copies` holds 16 copies of each value (`_lane_copies`)."""
    ext = [d.span for d in axes] + [pad]
    dense = [int(np.prod(ext[i + 1 :])) for i in range(len(ext))]
    strides = dict(zip(axes, dense))
    if lane is not None:
        strides[lane] = 1
    walk, dwalk = [], []
    first, dat = acc.off, 0
    for d in axes + ([lane] if lane is not None else []):
        a, b = d.box
        walk.append((b - a, d.stride))
        dwalk.append((b - a, strides[d]))
        first += d.stride * (d.lo + a)
        dat += strides[d] * a
    n = int(np.prod(ext))
    origin = -sum(strides[d] * d.lo for d in axes)
    if defer:
        st, off = FM.base_of(bufs[acc.slot])
        dt = F.np_dtype(acc.dtype)
        if lane is None and copies:
            walk, dwalk = [*walk, (16, 0)], [*dwalk, (16, 1)]
        first += off // np.dtype(dt).itemsize
        try:
            src, N, w, first, dw = _f16_walk(
                dev, st, dt, tuple(walk), first, tuple(dwalk)
            )
        except FM.FormError:
            raise Refused("a padded operand with no f16 form") from None
        m = -(-n // 16) * 16
        step = FM.Step(src, N, w, first, dw, dat, m)
        return None, strides, origin, step
    s = Storage(dev, 2 * -(-n // 16) * 16)
    tmps.append(s)
    if lane is None and copies:
        _lane_copies(dev, acc, bufs, walk, first, dwalk, dat, s)
    elif not _gather_to(
        dev, acc, bufs, tuple(walk), first, tuple(dwalk), dat, n, s.addr
    ):
        raise Refused("a padded operand with no f16 form")
    s.wrote(FLAT)
    s.dtype = np.float16
    bufs[key] = s
    return key, strides, origin, None


def _lane_copies(dev, acc, bufs, walk, first, dwalk, dat, dst: Storage) -> None:
    """`dst` (16-lane words, positions `dwalk` elements apart from `dat`)
    := each value `walk` reaches from `first` in all 16 lanes, every other
    word zero (`forms.relayout`, each value read 16 times)."""
    st, off = FM.base_of(bufs[acc.slot])
    dt = F.np_dtype(acc.dtype)
    N = _n(st, dt)
    first += off // np.dtype(dt).itemsize
    src = FM.f16(dev, st, dt, N)
    copies = (*walk, (16, 0)), (*dwalk, (16, 1))
    FM.relayout(
        dev, src, N, copies[0], first, copies[1], dst.nbytes // 2, dst.addr, dat
    )
