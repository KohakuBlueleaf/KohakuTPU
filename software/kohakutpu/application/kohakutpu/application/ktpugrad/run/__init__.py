"""Running a plan on the device: each read classified as a `vector.Operand`
against the kernel's rows and columns, its form made (`runtime.forms`), the
generated DSL kernel compiled (cached) and run, the output's form recorded.

Where the forms are is known only when the kernel runs (a statistic a
reduction left is not row-major bytes), so the last choices are made here:
a kernel whose operands are statistics reads compact [n] values, 16 a
word; a row-major one reads the bytes.
"""

from dataclasses import replace

import numpy as np
from tinygrad.dtype import dtypes

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.ktpugrad import fold as F
from kohakutpu.application.ktpugrad import launch_schedule
from kohakutpu.application.ktpugrad import uops as U
from kohakutpu.application.ktpugrad.launch_config import (
    _active_scope,
    elide_contiguous_enabled,
    elide_rule,
    horizontal_fusion_enabled,
    knobs,
    multi_stage_scope,
    single_package_enabled,
    view_forms_enabled,
)
from kohakutpu.application.ktpugrad.launch_emit import flush_multi_stage
from kohakutpu.application.ktpugrad.lower import Plan, Refused
from kohakutpu.application.ktpugrad.run.common import _dense, _reform
from kohakutpu.application.ktpugrad.run.kernels import (
    _flat,
    _materialise,
    _matmul,
    _narrow,
    _product,
    _vector,
    _window,
)
from kohakutpu.application.ktpugrad.run.product import _ks
from kohakutpu.application.ktpugrad.run.reduce import (
    MAX_READS,
    _contraction,
    _epilogue_contraction,
    _leaf_reduce,
    _selectors,
)
from kohakutpu.application.ktpugrad.run.shape import _bind, classify
from kohakutpu.application.runtime import forms as FM
from kohakutpu.application.runtime import rows as RW
from kohakutpu.application.runtime.storage import FLAT


def execute(dev, plan: Plan, bufs: dict):
    """Run `plan` with `bufs` (param slot -> storage); returns the runs."""
    if (
        view_forms_enabled()
        and plan.kind == "vector"
        and not plan.reduce_ranges
        and isinstance(plan.expr, X.Load)
        and plan.out_strides is None
        and len(plan.accesses) == 1
    ):
        acc = plan.accesses[0]
        if (
            acc.slot is not None
            and acc.slot in bufs
            and dtypes.is_float(acc.dtype)
            and dtypes.is_float(plan.out_dtype)
        ):
            sst, soff = FM.base_of(bufs[acc.slot])
            dst, doff = FM.base_of(bufs[plan.out_slot])
            out_item = np.dtype(F.np_dtype(plan.out_dtype)).itemsize
            pkey = FM.padded(sst) if not soff else None
            if (
                pkey is not None
                and not doff
                and acc.off == 0
                and plan.out_n * out_item == dst.nbytes
                and {r: c for r, c in acc.coef if U.extent(r) > 1}
                == {r: c for r, c in _dense(plan.out_ranges).items() if U.extent(r) > 1}
                and RW.MUTANT != "drop_member"
            ):
                pf = sst.forms[pkey]
                target = sst if RW.MUTANT == "alias_members" else dst
                target.share_form(pkey, pf.addr, pf.nbytes, src_st=sst)
                target.wrote(pkey)
                target.dtype = F.np_dtype(plan.out_dtype)
                if pkey in sst.zeroed:
                    target.zeroed.add(pkey)
                return []
    if plan.kind not in ("vector", "copy"):
        return _execute(dev, plan, bufs)
    if plan.kind == "copy" and view_forms_enabled():
        runs = _execute(dev, plan, bufs)
        scope = _active_scope(dev)
        if scope is not None and scope.remaining is not None:
            scope.remaining -= 1
            if scope.remaining <= 0:
                runs += flush_multi_stage(scope)
        return runs
    plan, aliased = _reform(dev, plan, bufs)
    if plan.kind == "copy" and any(
        a.slot != plan.out_slot and isinstance(a.slot, tuple) for a in plan.accesses
    ):
        plan = replace(plan, kind="vector")
    try:
        runs = _execute(dev, plan, aliased)
        scope = _active_scope(dev)
        if scope is not None and scope.remaining is not None:
            scope.remaining -= 1
            if scope.remaining <= 0:
                runs += flush_multi_stage(scope)
        return runs
    finally:
        # An alias owns no bytes, only the forms a kernel made of it.
        for k, s in aliased.items():
            if k not in bufs:
                s.free()


def _execute(dev, plan: Plan, bufs: dict):
    out = bufs[plan.out_slot]
    ndt = F.np_dtype(plan.out_dtype)
    match plan.kind:
        case "const":
            st, off = FM.base_of(out)
            raw = np.ascontiguousarray(plan.value, ndt).tobytes()
            dev.write(st.addr + off, raw)
            st.wrote(FLAT)
            st.host = raw if not off else None
            st.dtype = ndt
            return []
        case "copy":
            return _copy(dev, plan, bufs)
        case "vector":
            return _vector(dev, plan, bufs)
        case "matmul":
            return _matmul(dev, plan, bufs)
    raise Refused(f"no plan kind {plan.kind}")


# ----------------------------------------------------------------- copy
def _copy(dev, plan: Plan, bufs) -> list:
    """A buffer's current form copied whole by the copy kernel: its bytes
    word to word, or its f16 / statistic / padded-rows form; any other copy
    (a sub-buffer off a word, a form of part of a buffer) is the bare read it
    is (`_vector`)."""
    acc = plan.accesses[0]
    src, dst = bufs[acc.slot], bufs[plan.out_slot]
    sst, soff = FM.base_of(src)
    dst_, doff = FM.base_of(dst)
    key = next((k for k in (FLAT, FM.F16) if k in sst.valid), None)
    key = key or FM.padded(sst)
    if key is None:
        raise Refused("a copy of a buffer with no current form")
    if key == FLAT:
        n = plan.out_n * np.dtype(F.np_dtype(plan.out_dtype)).itemsize
        a, b = sst.addr + soff, dst_.addr + doff
        whole = not (a % 32 or b % 32)
    else:
        n = sst.forms[key].nbytes
        a, b = sst.forms[key].addr, 0
        whole = not (soff or doff)
    if not whole:
        return _vector(dev, replace(plan, kind="vector"), bufs)
    if key != FLAT:
        b = dst_.form(key, n).addr
    if elide_rule("self_copy") and a == b and RW.MUTANT != "drop_member":
        dst_.wrote(key)
        dst_.dtype = F.np_dtype(plan.out_dtype)
        return []
    scope = _active_scope(dev)
    if scope is not None:
        scope.stages.append(("copy", int(n), int(a), int(b), {"plan": plan.name}))
        dst_.wrote(key)
        dst_.dtype = F.np_dtype(plan.out_dtype)
        if key == FLAT and sst.host is not None and not soff and not doff:
            dst_.host = sst.host
        return []
    run = FM.generated.copy(dev, n, {"src": a, "dst": b}, info={"plan": plan.name})
    dst_.wrote(key)
    dst_.dtype = F.np_dtype(plan.out_dtype)
    if key == FLAT and sst.host is not None and not soff and not doff:
        dst_.host = sst.host
    return [run]


def _is_fusable_elem(plan: Plan | None) -> bool:
    if plan is None or plan.kind != "vector" or plan.reduce_ranges:
        return False
    if isinstance(plan.expr, X.Load) or plan.out_strides is not None:
        return False
    if _selectors(plan) or len(plan.accesses) > MAX_READS:
        return False
    return not (_epilogue_contraction(plan) or _contraction(plan) is not None)


def plan_schedule(items: list, held_ids: set | None = None) -> list:
    """Only adjacent independent elementwise members share a launch."""
    if not horizontal_fusion_enabled():
        return [([i], set()) for i in range(len(items))]
    return launch_schedule.groups(
        items, _is_fusable_elem, ignore_hazards=RW.MUTANT == "ignore_alias"
    )


__all__ = [
    "RW",
    "_active_scope",
    "_bind",
    "_flat",
    "_ks",
    "_leaf_reduce",
    "_materialise",
    "_narrow",
    "_product",
    "_vector",
    "_window",
    "classify",
    "elide_contiguous_enabled",
    "elide_rule",
    "execute",
    "flush_multi_stage",
    "horizontal_fusion_enabled",
    "knobs",
    "multi_stage_scope",
    "plan_schedule",
    "single_package_enabled",
    "view_forms_enabled",
]
