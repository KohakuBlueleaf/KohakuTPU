"""The product runner: packing plans, pending forms, table boxes."""

from dataclasses import replace
from typing import cast

import numpy as np
from tinygrad.dtype import dtypes

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import matmul as MM
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.ktpugrad import uops as U
from kohakutpu.application.ktpugrad.launch.config import elide_rule
from kohakutpu.application.ktpugrad.planning import fold as F
from kohakutpu.application.ktpugrad.planning import window as W
from kohakutpu.application.ktpugrad.planning.lower import Access, Plan, Refused
from kohakutpu.application.ktpugrad.run.common import (
    _dense,
    _half_target,
    _held,
    _hold,
    _mult,
    _out,
    _out_coef,
    _read_id,
    _size,
    _wrote,
)
from kohakutpu.application.ktpugrad.run.moves import _bare, _moved
from kohakutpu.application.ktpugrad.run.reduce import _prune, _rebuild, _table_axes
from kohakutpu.application.ktpugrad.run.shape import _f16_walk, _laid_plan, classify
from kohakutpu.application.runtime import forms as FM
from kohakutpu.application.runtime import rows as RW
from kohakutpu.application.runtime.memory.storage import FLAT, Storage, StorageView
from kohakutpu.language import machine as MF
from kohakutpu.language.config import ENV


def _box(plan: Plan, box: dict, bufs, sel: dict) -> tuple:
    """``(plan, buffers, every selector constant)`` of `plan` over `box`
    (``{range: (start, end)}``): ranges narrowed, reads moved, the output a
    window of its buffer."""
    new = {
        r: r.replace(src=(r.src[0].replace(arg=e - s), *r.src[1:]))
        for r, (s, e) in box.items()
    }
    consts: dict = {}
    accesses = []
    for i, a in enumerate(plan.accesses):
        off = a.off + sum(c * box[r][0] for r, c in a.coef if r in box)
        coef = tuple((new.get(r, r), c) for r, c in a.coef)
        table = a.table
        if a.slot is None and any(r in box for r, _ in a.coef):
            rs, arr = _table_axes(a)
            sl = tuple(slice(*box[r]) if r in box else slice(None) for r in rs)
            arr = np.ascontiguousarray(arr[sl])
            keep = [new.get(r, r) for r in rs]
            coef = tuple(zip(keep, _mult(keep)))
            table, off = arr.reshape(-1), 0
            if i in sel:
                if arr.size and np.all(arr == arr.flat[0]):
                    consts[i] = float(arr.flat[0])
                else:
                    return plan, bufs, False
        accesses.append(replace(a, off=off, coef=coef, table=table))
    reductions: dict = {}

    def fold(x, walk):
        if isinstance(x, X.Load):
            return X.Const(consts[x.i]) if x.i in consts else x
        if isinstance(x, X.Op):
            args = tuple(walk(a) for a in x.args)
            if x.name == "where" and isinstance(args[0], X.Const):
                return args[1] if args[0].value else args[2]
            return X.Op(x.name, args)
        if isinstance(x, X.Reduce):
            out = X.Reduce(x.op, walk(x.arg))
            reductions[id(out)] = tuple(new.get(r, r) for r in plan.reductions[id(x)])
            return out
        return None

    e = _rebuild(plan.expr, fold)
    if not isinstance(e, X.Const):
        e, accesses, remap = _prune(e, accesses)
        reductions = {k: reductions[v] for k, v in remap.items()}
    else:
        accesses = []
    mult = _out_coef(plan)
    out_ranges = tuple(new.get(r, r) for r in plan.out_ranges)
    first = sum(mult[r] * s for r, (s, _) in box.items())
    item = np.dtype(F.np_dtype(plan.out_dtype)).itemsize
    st, off = FM.base_of(bufs[plan.out_slot])
    span = 1 + sum((U.extent(new.get(r, r)) - 1) * mult[r] for r in plan.out_ranges)
    view = StorageView(st, off + first * item, span * item)
    sub = replace(
        plan,
        out_ranges=out_ranges,
        out_shape=tuple(U.extent(r) for r in out_ranges),
        expr=e,
        accesses=accesses,
        reductions=reductions,
        reduce_ranges=tuple(new.get(r, r) for r in plan.reduce_ranges),
        out_strides=tuple(mult[r] for r in plan.out_ranges),
    )
    # Dense when the ranges left (a box of one value has no stride) are.
    live = [(r, new.get(r, r)) for r in plan.out_ranges if U.extent(new.get(r, r)) > 1]
    dense = _dense([n for _, n in live])
    if all(mult[r] == dense[n] for r, n in live):
        sub = replace(sub, out_strides=None)
    return sub, {**bufs, plan.out_slot: view}, True


# --------------------------------------------------------------- matmul
def _ks(k) -> tuple:
    """A matmul's K: one range or several (laid out as one)."""
    return tuple(k) if isinstance(k, tuple) else (k,)


def _pending(dev, plan: Plan, acc: Access, order, bufs, side) -> FM.Pending | None:
    """`acc` as f16 dense over the ranges `order`, for a packing alone to
    read: a `forms.Pending` of one relayout step from the buffer's f16
    values (made inside the packing's kernel), None for a read that is not
    a buffer's float values (a table, a gated read)."""
    if (
        not (FM.PREPARED and FM.PREPARED_QUANT)
        or acc.slot is None
        or acc.gate
        or not dtypes.is_float(acc.dtype)
    ):
        return None
    read = ("pending", side, *_read_id(acc, order))
    held = _held(dev, bufs[acc.slot], read)
    if held is not None:
        return held
    # Not coalesced: each destination level a row's, a row's or a block's
    # (the packing remaps them, `forms._into_packing`).
    one = _laid_plan(plan, ("pend", id(acc)), acc, order)
    a = one.accesses[0]
    live = [r for r in one.out_ranges if U.extent(r) > 1]
    st, off = FM.base_of(bufs[a.slot])
    dt = F.np_dtype(a.dtype)
    first = a.off + off // np.dtype(dt).itemsize
    if first < 0:
        return None
    coef, dense = dict(a.coef), _dense(live)
    walk = tuple((U.extent(r), coef.get(r, 0)) for r in live)
    dwalk = tuple((U.extent(r), dense[r]) for r in live)
    n = _size(live)
    made = FM.deferred(st)
    if made is not None:
        # A deferred source (`forms.defer`): its step, then this read of it.
        steps = (made, FM.Step(None, made.m, walk, first, dwalk, 0, n))
    else:
        try:
            src, N, walk, first, dwalk = _f16_walk(dev, st, dt, walk, first, dwalk)
        except FM.FormError:
            return None
        steps = (FM.Step(src, N, walk, first, dwalk, 0, n),)
    mm = _packing_plan(dev, plan)
    rows = mm.m if side == "a" else U.extent(plan.mnk[1])
    rp = mm.mp if side == "a" else mm.n
    group = mm.tm // 4 if side == "a" else mm.tn // 4
    batches = mm.bt if side == "a" or not mm.shared else 1
    if (
        FM.COMPOSED == "off"
        and FM.costs.prepared_quant(
            steps,
            Operand("full", pitch=mm.k),
            rows,
            mm.k,
            rp,
            mm.kp,
            side,
            group,
            mm.nk,
            batches,
            dev.machine.board,
        )
        is None
    ):
        return None
    return _hold(dev, bufs[acc.slot], read, FM.Pending(dev, 2 * n, steps))


def _packing_plan(dev, plan: Plan):
    """One tiling decision shared by deferred-read admission and execution."""
    m, n, k = plan.mnk
    batch = [r for r in plan.out_ranges if r in plan.batch]
    bt = _size(batch)
    machine = MF.load(dev.machine.board)
    block = machine.vc.vlmax // machine.mg.lanes
    Np = -(-U.extent(n) // block) * block
    reads = [classify(acc, list(plan.out_ranges[:-1]), [n]) for acc in plan.accesses]
    quantum = (
        V.STAT_ROWS if any(o is not None and o.kind == "row" for o in reads) else 4
    )
    return MM.plan(
        bt,
        U.extent(m),
        Np,
        _size(_ks(k)),
        not any(cast(Access, plan.b).c(r) for r in batch),
        quantum,
        machine_json=dev.machine.machine_json,
    )


def _rows_form(bufs, acc, o: Operand, N: int, Np: int) -> tuple | None:
    """``(address, operand)`` of a product's full read of whole logical rows
    of a buffer held as padded rows (`forms.padded`) at least `Np` wide and
    whole words: those rows as they are (no compaction, no re-padding)."""
    if o.kind != "full" or acc.slot is None:
        return None
    st, off = FM.base_of(bufs[acc.slot])
    key = FM.padded(st)
    if key is None or off:
        return None
    _, n, np_ = key
    start = o.offset // max(n, 1) * np_ + o.offset % max(n, 1)
    if o.pitch != n or o.offset % n + N > n or np_ < Np or np_ % 16 or start % 16:
        return None
    return st.forms[key].addr, replace(o, offset=start, pitch=np_)


def _product_run(
    dev, plan, bufs, mm, binds, extras, bt, M, N, Np, batch, m, n, f16
) -> list:
    # A batch's rows padded to mp (`MM.plan`): the product's rows are moved
    # out of an [bt, mp, Np] temporary.
    mp = mm.mp
    padded = Np != N or mp != M
    tmp: Storage | None = None
    dst, doff = FM.base_of(bufs[plan.out_slot])
    # Rows of N not whole words: the product stays in the output's padded
    # rows form (read back compacted; readers on card take its strides).
    whole = not doff and plan.out_strides is None
    narrow = (2 * N) % 32 != 0 or plan.out_dtype != dtypes.half
    in_form = Np != N and narrow and whole and dtypes.is_float(plan.out_dtype)
    if padded:
        if in_form and (
            mp == M or (bt == 1 and elide_rule("product_rows") and RW.MUTANT is None)
        ):
            out_Rows = mp if (mp != M and bt == 1 and elide_rule("product_rows")) else M
            out_at, key = (
                dst.form(FM.rows_key(N, Np), 2 * bt * out_Rows * Np).addr,
                None,
            )
        elif (
            elide_rule("product_flat")
            and bt == 1
            and Np == N
            and whole
            and dtypes.is_float(plan.out_dtype)
            and plan.out_n * np.dtype(F.np_dtype(plan.out_dtype)).itemsize == dst.nbytes
            and RW.MUTANT is None
        ):
            need_bytes = 2 * bt * mp * Np
            if np.dtype(F.np_dtype(plan.out_dtype)) == np.float16:
                dst.ensure_flat_bytes(need_bytes)
                out_at, key = dst.addr, FLAT
            else:
                out_at, key = (
                    dst.form(FM.F16, max(2 * plan.out_n, need_bytes)).addr,
                    FM.F16,
                )
            padded = False
        else:
            tmp = Storage(dev, 2 * bt * mp * Np)
            out_at, key = tmp.addr, None
    else:
        out_at, key = _out(dev, plan, bufs)
    binds["out"] = out_at
    try:
        arguments = {
            "mm": {
                "made_by": {
                    "gen": "matmul.plan",
                    "args": {
                        "bt": mm.bt,
                        "m": mm.m,
                        "n": mm.n,
                        "k": mm.k,
                        "shared": mm.shared,
                        "quantum": mm.quantum,
                        **MM.pairing_arg(),
                        **MM.plan_knobs(),
                    },
                }
            },
            "e": FM.generated.expr(plan.expr),
            "extras": [vars(o) for o in extras],
            "out": vars(Operand("full", pitch=Np, rows=bt * mp)),
            **MM.kernel_knobs(),
            # A is f16 only where its producer laid it out for the kernel.
            "activation_fp16": f16,
        }
        ring_depth = int(ENV.get("KTPU_MATMUL_RING_DEPTH", "0"))
        if ring_depth:
            arguments["ring_depth"] = ring_depth
            arguments["tail_bands"] = True
        pending = {int(k[2:]): v for k, v in binds.items() if isinstance(v, FM.Pending)}
        recipe = dict(
            mm=vars(mm),
            e=arguments["e"],
            extras=arguments["extras"],
            out=arguments["out"],
            steps={i: FM.costs.steps_recipe(v.steps) for i, v in pending.items()},
        )
        # A read in the kernel saves a launch, so it takes the DRAM product
        # (a kernel of no ring) over the ring.
        made = {str(i): [s.made_by() for s in v.steps] for i, v in pending.items()}
        read = dict(arguments, made=made, composed=sorted(pending), ring_depth=0)
        call = {"gen": "matmul.mm_kernel", "args": arguments}
        steps = [s for v in pending.values() for s in v.steps]
        if pending and FM.composed(
            dev,
            {"gen": "matmul.mm_kernel", "args": read},
            [*map(FM.relayout_call, steps), call],
        ):
            for i, v in pending.items():
                del binds[f"in{i}"]
                binds[f"p{i}_x"] = v.steps[0].src
            arguments = read
        elif pending and FM.costs.choose("prepared_mm", recipe, dev.machine.board):
            plans = {i: FM.plans_of(dev, v.steps) for i, v in pending.items()}
            arguments["ring_depth"] = 0
            arguments["made"] = {
                str(i): [s.made_by() for s in v.steps] for i, v in pending.items()
            }
            for i, v in pending.items():
                del binds[f"in{i}"]
                binds[f"p{i}_x"] = v.steps[0].src
                binds.update(FM.step_binds(dev, plans[i], f"p{i}_"))
        else:
            binds = {
                k: v.made().addr if isinstance(v, FM.Pending) else v
                for k, v in binds.items()
            }
        # Its own whole output: a site its consumers' packings are learned at.
        site = FM.mm_site(plan.expr, bt, M, N, mm.k)
        mine = not padded and not doff and plan.out_strides is None
        packs = _packs(dev, site, arguments, binds, dst) if mine else []
        run = FM.generated.launch(
            dev,
            "matmul.mm_kernel",
            arguments,
            binds,
            kind="matmul",
            info={"plan": plan.name, "mm": mm},
        )
        if not padded:
            _wrote(dev, plan, bufs, key)
            if mine:
                dst.site = (site, N)
            for k in packs:
                dst.made(k)
            return [run]
        if tmp is None:
            dst.wrote(FM.rows_key(N, Np))
            dst.dtype = F.np_dtype(plan.out_dtype)
            return [run]
        # The [.., M, N] window of the [.., mp, Np] product: into the output's
        # padded rows form, else the output itself (`_moved`: any window).
        cols = n if not in_form else W.ranges(plan, [Np])[0]
        coef, step = {}, 1
        for r, e in reversed(
            list(zip([*batch, m, cols], [*(U.extent(x) for x in batch), mp, Np]))
        ):
            coef[r] = step
            step *= e
        if not in_form:
            tplan, tbufs, finish = _half_target(dev, plan, bufs)
            moved = _moved(dev, tplan, tbufs, tmp, coef)
            finish()
            return [run, *moved]
        tkey, fkey = ("mmpad", id(plan)), ("mmform", id(plan))
        form = dst.form(FM.rows_key(N, Np), 2 * bt * M * Np)
        moves = {
            **bufs,
            tkey: tmp,
            fkey: Storage(dev, form.nbytes, addr=form.addr, owned=False),
        }
        read = Access(tkey, dtypes.half, tuple(coef.items()), 0)
        moved = _bare(dev, _laid_plan(plan, fkey, read, (*batch, m, cols)), moves)
        assert moved is not None  # an f16 output: `_gather` takes any read
        dst.wrote(FM.rows_key(N, Np))
        dst.dtype = F.np_dtype(plan.out_dtype)
        return [run, *moved]
    finally:
        if tmp is not None:
            tmp.free()


#: Whether a product's kernel writes its consumers' packings (`_packs`).
PRODUCER_PACKS = ENV.get("KTPU_PRODUCER_PACKS", "1") == "1"


def _packs(dev, site, arguments: dict, binds: dict, dst: Storage) -> list:
    """The `forms.packed` keys `site`'s consumers read that the product's kernel
    writes: each a `q{i}` of the call into its form of `dst`; none unless all."""
    if not PRODUCER_PACKS:
        return []
    keys = list(FM.demands(dev).get(site, ()))
    q = [{"side": k[1], "g": k[7], "nk": k[8], "rp": k[5], "cp": k[6]} for k in keys]
    if not q or not MM.resolved(dict(arguments, q=q), dev.machine.machine_json)["q"]:
        return []
    arguments["q"] = q
    for i, k in enumerate(keys):
        binds[f"q{i}"] = dst.form(k, k[5] * k[6]).addr
    return keys


def _batched(acc, batch, step: int, what: str) -> None:
    """Refuse a batched operand whose batches are not one row-major stack."""
    want = step
    for r in reversed(batch):
        if U.extent(r) > 1 and acc.c(r) != want:
            raise Refused(
                f"{what}'s batches are not one row-major stack of [rows, K] blocks"
            )
        want *= U.extent(r)
