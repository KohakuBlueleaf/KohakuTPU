"""Access arithmetic the runner's stages share: strides, sizes, walks."""

from dataclasses import replace

import numpy as np
from tinygrad.dtype import dtypes

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.ktpugrad import uops as U
from kohakutpu.application.ktpugrad.launch.config import view_forms_enabled
from kohakutpu.application.ktpugrad.launch.views import _remap_view_access
from kohakutpu.application.ktpugrad.plan import fold as F
from kohakutpu.application.ktpugrad.plan.lower import Access, Plan, Refused
from kohakutpu.application.runtime import forms as FM
from kohakutpu.application.runtime import rows as RW
from kohakutpu.application.runtime.memory.storage import FLAT, Storage, StorageView


def _reform_one(dev, plan: Plan, a: Access | None, bufs: dict) -> Access | None:
    if a is None or a.slot is None:
        return a
    s = bufs.get(a.slot)
    st, off = FM.base_of(s) if s is not None else (None, 0)
    if st is None or off or not dtypes.is_float(a.dtype):
        return a
    if view_forms_enabled():
        vkey = FM.viewed(st)
        if (
            vkey is not None
            and a.slot != plan.out_slot
            and not isinstance(plan.expr, X.Load)
        ):
            remapped = _remap_view_access(a, vkey)
            if remapped is not None:
                coef, new_off = remapped
                if RW.MUTANT == "wrong_order" and len(coef) >= 2:
                    coef = tuple(reversed(coef))
                alias = ("view", a.slot)
                if alias not in bufs:
                    vf = st.forms[vkey]
                    bufs[alias] = Storage(dev, vf.nbytes, addr=vf.addr, owned=False)
                return replace(a, slot=alias, coef=coef, off=new_off, dtype=dtypes.half)
    key = FM.padded(st)
    allow_self = view_forms_enabled() and plan.kind == "vector"
    if key is None or (a.slot == plan.out_slot and not allow_self):
        return a
    _, n, np_ = key
    inner = [(r, c) for r, c in a.coef if c % n and U.extent(r) > 1]
    span = a.off % n + sum(c * (U.extent(r) - 1) for r, c in inner)
    if any(c < 0 for _, c in inner) or span >= n:
        return a
    coef = tuple((r, c if c % n else c // n * np_) for r, c in a.coef)
    off_el = a.off // n * np_ + a.off % n
    alias = ("rows", a.slot)
    if alias not in bufs:
        f = st.forms[key]
        bufs[alias] = Storage(dev, f.nbytes, addr=f.addr, owned=False)
    # The form holds f16 values whatever the buffer's element type.
    return replace(a, slot=alias, coef=coef, off=off_el, dtype=dtypes.half)


def _reform(dev, plan: Plan, bufs: dict) -> tuple:
    """Reads of a buffer held as padded rows (rows of n, np apart) or a view
    descriptor: each read through an alias of that form with its strides
    remapped; a read that crosses a row stays as it is."""
    bufs = dict(bufs)
    accesses = [_reform_one(dev, plan, a, bufs) for a in plan.accesses]
    return replace(plan, accesses=accesses), bufs


# ------------------------------------------------------------- operands
def _live(rs):
    return [r for r in rs if U.extent(r) > 1]


def _mult(rs) -> list:
    out, step = [], 1
    for r in reversed(rs):
        out.insert(0, step)
        step *= U.extent(r)
    return out


def _n(st, dtype) -> int:
    return st.nbytes // np.dtype(dtype).itemsize


def _addr(dev, acc, o: Operand, bufs) -> tuple:
    """``(address, operand)`` of a read in its f16 form: a value a row or one
    value from its base's (the sub-buffer's offset in the operand's, so the
    kernel reads whole words), rows of values from the view's."""
    if acc.slot is None:
        return FM.table(dev, acc.table), o
    s = bufs[acc.slot]
    dt = F.np_dtype(acc.dtype)
    st, off = FM.base_of(s)
    if o.kind in ("row", "scalar"):
        at = FM.f16(dev, st, dt, _n(st, dt))
        return at, replace(o, offset=o.offset + off // np.dtype(dt).itemsize)
    return FM.f16(dev, s, dt, _n(st, dt) if not off else s.nbytes // dt.itemsize), o


def _out(dev, plan: Plan, bufs) -> tuple:
    """``(address, key)`` the kernel writes the output to: the f16 bytes of
    an f16 buffer, else its f16 form (the rest current first)."""
    s = bufs[plan.out_slot]
    st, off = FM.base_of(s)
    n = plan.out_n
    dt = np.dtype(F.np_dtype(plan.out_dtype))
    if dt == np.float16:
        return st.addr + off, FLAT
    total = st.nbytes // dt.itemsize
    if (off or n != total) and (st.dtype is not None or st.host is not None):
        # Part of a written buffer: the rest kept, in its f16 form.
        FM.f16(dev, st, dt, total)
    return st.form(FM.F16, 2 * total).addr + off // dt.itemsize * 2, FM.F16


def _wrote(dev, plan: Plan, bufs, key) -> None:
    st, _ = FM.base_of(bufs[plan.out_slot])
    st.wrote(key)
    st.dtype = F.np_dtype(plan.out_dtype)


def _walk(plan: Plan, coef: dict, item: int) -> list | None:
    """``[(count, byte stride)]`` of element strides `coef` over the output's
    ranges, adjacent levels merged, the last whole 32-byte words; None if
    not words."""
    lv = [
        (U.extent(r), coef.get(r, 0) * item) for r in plan.out_ranges if U.extent(r) > 1
    ]
    merged: list = []
    for n, s in reversed(lv):
        if merged and s == merged[-1][0] * merged[-1][1]:
            m, t = merged.pop()
            merged.append((n * m, t))
        else:
            merged.append((n, s))
    merged.reverse()
    if not merged:
        merged = [(1, item)]
    n, s = merged[-1]
    if s != item or (n * item) % 32 or any(t % 32 for _, t in merged[:-1]):
        return None
    return [*merged[:-1], (n * item // 32, 32)]


def _words(plan: Plan, acc: Access, item: int, at: int) -> bool:
    """Whether the mover walks read `acc` (from byte `at`, `item`-byte
    elements) into `plan`'s output in whole 32-byte words."""
    return (
        not at % 32
        and at >= 0
        and _walk(plan, dict(acc.coef), item) is not None
        and _walk(plan, _out_coef(plan), item) is not None
    )


def _source(dev, plan: Plan, acc: Access, bufs) -> tuple | None:
    """``(address, bytes, element size, byte offset)`` of the values a mover
    walks for `acc`: the buffer's own bytes when the output keeps its type,
    else (an f16 output of float data) its f16 form, made if need be."""
    if acc.slot is None:
        return None
    s = bufs[acc.slot]
    st, off = FM.base_of(s)
    dt = F.np_dtype(acc.dtype)
    if acc.dtype == plan.out_dtype and FLAT in st.valid:
        return st.addr, st.nbytes, np.dtype(dt).itemsize, off
    if plan.out_dtype != dtypes.half or not dtypes.is_float(acc.dtype):
        return None
    try:
        addr = FM.f16(dev, s, dt, _n(st, dt))
    except FM.FormError:
        return None
    if np.dtype(dt) == np.float16:
        return st.addr, st.nbytes, 2, off
    return addr, 2 * _n(st, dt), 2, 0


def _dense(rs) -> dict:
    return dict(zip(rs, _mult(rs)))


#: Values a held row is padded to a multiple of: whole memory lines.
ROW_ALIGN = 32


def _window_ok(dev, dst: Storage, plan: Plan) -> None:
    """A plan writing part of `dst` keeps the rest: its flat bytes made
    current first (an f16 buffer's from its current form, `forms.f16`)."""
    item = np.dtype(F.np_dtype(plan.out_dtype)).itemsize
    if FLAT in dst.valid or (
        plan.out_strides is None and plan.out_n * item == dst.nbytes
    ):
        return
    if dst.dtype is None or np.dtype(dst.dtype) == np.float16:
        FM.f16(dev, dst, np.float16, dst.nbytes // 2)
        return
    raise Refused(
        f"part of a {np.dtype(dst.dtype)} buffer written while its bytes are not "
        "current (its f16 form is)"
    )


def _wrote_flat(dst: Storage, plan: Plan) -> None:
    dst.wrote(FLAT)
    dst.dtype = F.np_dtype(plan.out_dtype)


def _out_coef(plan: Plan) -> dict:
    strides = plan.out_strides or tuple(_mult(plan.out_ranges))
    return dict(zip(plan.out_ranges, strides))


def _size(rs) -> int:
    return int(np.prod([U.extent(r) for r in rs])) if rs else 1


def _half_target(dev, plan: Plan, bufs) -> tuple:
    """``(plan, bufs, finish)``: `plan` writing f16 values -- as it is for an
    f16 output, else into its output's f16 form (the rest of the buffer
    current first when it writes part of a written one); `finish()` records
    the form written."""
    st, off = FM.base_of(bufs[plan.out_slot])
    dt = np.dtype(F.np_dtype(plan.out_dtype))
    if dt == np.float16:
        return plan, bufs, lambda: None
    total = st.nbytes // dt.itemsize
    part = off or plan.out_strides is not None or plan.out_n != total
    if part and (st.dtype is not None or st.host is not None):
        FM.f16(dev, st, dt, total)
    f = st.form(FM.F16, 2 * total)
    alias = Storage(dev, f.nbytes, addr=f.addr, owned=False)
    key = ("f16", plan.out_slot)
    nbytes = bufs[plan.out_slot].nbytes // dt.itemsize * 2
    view = StorageView(alias, off // dt.itemsize * 2, nbytes)

    def finish() -> None:
        st.wrote(FM.F16)
        st.dtype = dt

    out = replace(plan, out_slot=key, out_dtype=dtypes.half)
    return out, {**bufs, key: view}, finish
