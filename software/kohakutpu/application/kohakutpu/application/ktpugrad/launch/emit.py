"""Multi-output elementwise package emission for a fused group's members.

Consecutive flat stages over one domain share a loop. A member reading an
earlier member's output reads its packed value (`Load(-1 - k)` until emitted,
then its operand after the reads): what the store writes, read back in the
trip. A member may overwrite what an earlier one reads, every read being
loaded before any store. A whole copy of a read or an output is one more
member. An output dead after the group and read by no later stage is not
stored, unless a member reads its value and no copy of it is stored (a pack is
read back from its store's bytes). A loop holds at most `vc.descriptors`
memory views.
"""

import math

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.runtime import forms as FM
from kohakutpu.application.runtime import rows as RW
from kohakutpu.language import machine as MF


def _remap_expr_loads(e, mapping: dict):
    if isinstance(e, X.Load):
        return X.Load(mapping[e.i])
    if isinstance(e, X.Op):
        return X.Op(e.name, tuple(_remap_expr_loads(a, mapping) for a in e.args))
    if isinstance(e, X.Reduce):
        return X.Reduce(e.op, _remap_expr_loads(e.arg, mapping))
    return e


def _member_refs(e, at: dict):
    """`e` with each member reference `Load(-1 - k)` as operand `at[k]`."""
    if isinstance(e, X.Load):
        return X.Load(at[-1 - e.i]) if e.i < 0 else e
    if isinstance(e, X.Op):
        return X.Op(e.name, tuple(_member_refs(a, at) for a in e.args))
    return e


def _copy_of(e) -> int | None:
    """The member `e` is a bare copy of."""
    return -1 - e.i if isinstance(e, X.Load) and e.i < 0 else None


def _refs(e) -> set:
    """The members `e` reads."""
    if isinstance(e, X.Load):
        return {-1 - e.i} if e.i < 0 else set()
    if isinstance(e, X.Op):
        return set().union(*map(_refs, e.args))
    return set()


def _stores(exprs: list, base: list) -> list:
    """Each member's stored flag: `base`, and a member another reads by value
    stored where no copy of it is."""
    kept = list(base)
    read = set().union(*(_refs(e) for e in exprs if _copy_of(e) is None))
    for k in read:
        if not any(base[c] for c, e in enumerate(exprs) if _copy_of(e) == k):
            kept[k] = True
    return kept


def _emitted(st: dict) -> list:
    """The members a flat loop computes: stored, or stored through a copy."""
    return [
        k
        for k in range(len(st["exprs"]))
        if st["kept"][k]
        or any(st["kept"][c] and _copy_of(x) == k for c, x in enumerate(st["exprs"]))
    ]


class _Loop:
    """What decides a flat loop's stores: what each stage reads, the spans
    dead after the group, and the views a loop holds."""

    def __init__(self, stages: list, dead: set, views: int):
        self.reads = [st["ins"] for st in stages]
        self.dead, self.views = dead, views

    def kept(self, addr: int, writer: int, members: set) -> bool:
        """Whether a member's output is stored: it outlives the group, or a
        later stage outside the loop reads any byte of its buffer."""
        span = next(((lo, hi) for lo, hi in self.dead if lo <= addr < hi), None)
        if span is None:
            return True
        return any(
            span[0] <= a < span[1]
            for s, reads in enumerate(self.reads)
            if s > writer and s not in members
            for a in reads
        )

    def fits(self, ins: list, outs: list, writers: list, exprs: list) -> list | None:
        """Each output's stored flag, None past the loop's views."""
        members = set(writers)
        base = [self.kept(o, w, members) for o, w in zip(outs, writers, strict=True)]
        kept = _stores(exprs, base)
        return kept if len(ins) + sum(kept) <= self.views else None


def _span(st: dict, addr: int, lane: bool) -> tuple:
    """The bytes a flat loop's access at `addr` covers: a lane read its word,
    any other access the loop's `n` f16 values."""
    return (addr, addr + (32 if lane else 2 * st["n"]))


def _crossed(prev: dict, st: dict) -> bool:
    """Whether `st` touches bytes `prev`'s members write, or writes bytes they
    read, anywhere but at the same element: a trip would read what another
    trip writes."""
    lanes = [i in prev["lanes"] for i in range(len(prev["ins"]))]
    reads = [(a, _span(prev, a, ln)) for a, ln in zip(prev["ins"], lanes, strict=True)]
    writes = [(o, _span(prev, o, False)) for o in prev["outs"]]
    out = st["outs"][0]
    span = _span(st, out, False)

    def apart(a, s, b, t, same_ok):
        return s[0] < t[1] and t[0] < s[1] and not (same_ok and a == b)

    for i, a in enumerate(st["ins"]):
        lane = i in st["lanes"]
        s = _span(st, a, lane)
        if any(apart(a, s, b, t, not lane) for b, t in writes):
            return True
    if any(apart(out, span, b, t, False) for b, t in writes):
        return True
    return any(
        apart(out, span, b, t, not ln) for (b, t), ln in zip(reads, lanes, strict=True)
    )


def _join_flat(prev: dict, st: dict, idx: int, loop: _Loop) -> bool:
    """`st` (one member) into the flat loop `prev`; False when it cannot share it."""
    out = st["outs"][0]
    if prev["n"] != st["n"] or prev["out_dt"] != st["out_dt"] or _crossed(prev, st):
        return False
    ins, dts, lanes = list(prev["ins"]), list(prev["dts"]), dict(prev["lanes"])
    mapping = {}
    for i, (a, dt) in enumerate(zip(st["ins"], st["dts"], strict=True)):
        lane = st["lanes"].get(i)
        if a in prev["outs"]:
            if lane is not None or dt != prev["out_dt"]:
                return False
            mapping[i] = -1 - _root(prev["exprs"], prev["outs"].index(a))
            continue
        found = next(
            (
                j
                for j, (pa, pdt) in enumerate(zip(ins, dts, strict=True))
                if pa == a and pdt == dt and lanes.get(j) == lane
            ),
            None,
        )
        if found is None:
            found = len(ins)
            ins.append(a)
            dts.append(dt)
            if lane is not None:
                lanes[found] = lane
        mapping[i] = found
    return _commit(
        prev,
        idx,
        loop,
        _remap_expr_loads(st["exprs"][0], mapping),
        out,
        (ins, dts, lanes),
    )


def _join_copy(prev: dict, st: dict, idx: int, loop: _Loop) -> bool:
    """A whole copy of one of `prev`'s reads or outputs as one more member."""
    src, dst = st["ins"][0], st["outs"][0]
    if st["nbytes"] != 2 * prev["n"] or dst in prev["outs"]:
        return False
    if src in prev["outs"]:
        expr = X.Load(-1 - _root(prev["exprs"], prev["outs"].index(src)))
    elif src in prev["ins"] and prev["ins"].index(src) not in prev["lanes"]:
        expr = X.Load(prev["ins"].index(src))
    else:
        return False
    reads = (prev["ins"], prev["dts"], prev["lanes"])
    return _commit(prev, idx, loop, expr, dst, reads)


def _root(exprs: list, k: int) -> int:
    """The member whose value member `k` holds (itself, or what it copies)."""
    while (j := _copy_of(exprs[k])) is not None:
        k = j
    return k


def _commit(prev: dict, idx: int, loop: _Loop, expr, out: int, reads: tuple) -> bool:
    ins, dts, lanes = reads
    exprs = [*prev["exprs"], expr]
    outs, writers = [*prev["outs"], out], [*prev["writers"], idx]
    kept = loop.fits(ins, outs, writers, exprs)
    if kept is None:
        return False
    prev.update(
        ins=ins,
        dts=dts,
        lanes=lanes,
        exprs=exprs,
        outs=outs,
        writers=writers,
        kept=kept,
    )
    return True


def _join_rows(prev: dict, st: dict) -> bool:
    """Independent rows members over one [R, W] domain, full outputs."""
    if not (
        prev["R"] == st["R"]
        and prev["W"] == st["W"]
        and len(prev["outs"]) == 1
        and len(st["outs"]) == 1
        and prev["out_ops"][0].kind == "full"
        and st["out_ops"][0].kind == "full"
        and prev["out_ops"][0].offset == st["out_ops"][0].offset == 0
        and st["outs"][0] not in prev["outs"]
        and st["outs"][0] not in prev["ins"]
        and prev["outs"][0] not in st["ins"]
    ):
        return False
    comb_ops = list(prev["ops"])
    comb_ins = list(prev["ins"])
    mapping = {}
    for i, (a, op) in enumerate(zip(st["ins"], st["ops"], strict=True)):
        found = None
        for j, (pa, pop) in enumerate(zip(comb_ins, comb_ops, strict=True)):
            if pa == a and pop == op:
                found = j
                break
        if found is None:
            found = len(comb_ins)
            comb_ins.append(a)
            comb_ops.append(op)
        mapping[i] = found
    if len(comb_ops) + 2 > 7:
        return False
    prev["ops"] = comb_ops
    prev["ins"] = comb_ins
    prev["exprs"].append(_remap_expr_loads(st["exprs"][0], mapping))
    prev["out_ops"].append(st["out_ops"][0])
    prev["outs"].append(st["outs"][0])
    return True


def _normal(st) -> dict:
    kind = st[0]
    if kind == "flat":
        _, expr, dts, n, out_dt, lanes, binds, _ = st
        return {
            "kind": "flat",
            "n": n,
            "total": -(-n // 128),
            "out_dt": out_dt,
            "exprs": [expr],
            "dts": list(dts),
            "lanes": dict(lanes),
            "ins": [binds[f"in{i}"] for i in range(len(dts))],
            "outs": [binds["out"]],
        }
    if kind == "rows":
        _, expr, ops, R, W, out_op, binds, _ = st
        return {
            "kind": "rows",
            "R": R,
            "W": W,
            "exprs": [expr],
            "ops": list(ops),
            "out_ops": [out_op],
            "ins": [binds[f"in{i}"] for i in range(len(ops))],
            "outs": [binds["out"]],
        }
    _, nbytes, src_a, dst_a, _ = st
    return {"kind": "copy", "nbytes": nbytes, "ins": [src_a], "outs": [dst_a]}


def _merge(stages: list, loop: _Loop) -> list:
    """Each stage joined to the loop before it where it can share it."""
    merged: list = []
    for idx, st in enumerate(stages):
        prev = merged[-1] if merged else None
        fresh = RW.MUTANT != "wrong_order"
        if prev is not None and prev["kind"] == "flat" and fresh:
            if st["kind"] == "flat" and _join_flat(prev, st, idx, loop):
                continue
            if st["kind"] == "copy" and _join_copy(prev, st, idx, loop):
                continue
        if (
            prev is not None
            and prev["kind"] == st["kind"] == "rows"
            and fresh
            and _join_rows(prev, st)
        ):
            continue
        if st["kind"] == "flat":
            st.update(writers=[idx], kept=[True])
        if st["kind"] == "copy":
            st["shape"] = (-(-st["nbytes"] // 32), 16)
        merged.append(st)
    return merged


def flush_multi_stage(scope) -> list:
    """Compile and launch all stages recorded in `scope` as one DSL package."""
    if scope.flushed or not scope.stages:
        return []
    scope.flushed = True
    dev = scope.dev
    cores = dev.machine.count("VC")
    stages = list(scope.stages)
    if len(stages) == 1 and RW.MUTANT is None:
        st0 = stages[0]
        if st0[0] == "flat":
            _, expr, dts, n, _, lanes, binds, info = st0
            return [
                FM.generated.flat(dev, expr, dts, n, binds, info=info, scalars=lanes)
            ]
        if st0[0] == "rows":
            _, expr, ops, R, W, out_op, binds, info = st0
            return [RW._launch(dev, expr, ops, binds, R, W, out_op, info, cores)]
        if st0[0] == "copy":
            _, nbytes, src_a, dst_a, info = st0
            return [
                FM.generated.copy(dev, nbytes, {"src": src_a, "dst": dst_a}, info=info)
            ]

    if RW.MUTANT == "drop_member" and len(stages) > 1:
        stages = stages[:-1]
    elif RW.MUTANT == "wrong_order" and len(stages) > 1:
        stages = list(reversed(stages))

    norm = [_normal(st) for st in stages]
    views = MF.load(dev.machine.board).vc.descriptors
    # Only the group's last flush sees every reader: an earlier one (a launch
    # the group makes at once reads what is staged so far) stores everything.
    last = scope.remaining is not None and scope.remaining <= 0
    merged = _merge(norm, _Loop(norm, scope.dead_addrs if last else set(), views))

    if RW.MUTANT == "alias_members" and len(merged) >= 1:
        if len(merged[0]["outs"]) >= 2:
            merged[0]["outs"][1] = merged[0]["outs"][0]
        elif len(merged) >= 2:
            merged[1]["outs"][0] = merged[0]["outs"][0]

    return [_package(dev, part, scope.info) for part in _segments(dev, merged)]


def _accesses(dev, st: dict) -> list:
    """`(port key, start, end, written)` of each buffer a merged stage names,
    keyed as `_package` names its ports and scratches."""
    out = []
    if st["kind"] == "flat":
        n, total = st["n"], st["total"]
        staged = n % 128 != 0

        def flat(a, written):
            if staged:
                words = -(-(2 * n) // 32)
                return ((a, (words, 16), "f16"), a, a + 32 * words, written)
            return ((a, (total, 128), "f16"), a, a + 256 * total, written)

        for i, a in enumerate(st["ins"]):
            if i in st["lanes"]:
                out.append(((a, (16,), st["dts"][i]), a, a + 32, False))
            else:
                out.append(flat(a, False))
        out += [flat(o, True) for o, k in zip(st["outs"], st["kept"], strict=True) if k]
    elif st["kind"] == "rows":
        R, W = st["R"], st["W"]
        for ops, addrs, written in (
            (st["ops"], st["ins"], False),
            (st["out_ops"], st["outs"], True),
        ):
            for a, o in zip(addrs, ops, strict=True):
                shape = tuple(FM.generated.port_shape(dev, o, R, W))
                size = (4 if o.dtype == "f32" else 2) * math.prod(shape)
                out.append(((a, shape, o.dtype), a, a + size, written))
    else:
        size = 32 * st["shape"][0]
        for a, written in ((st["ins"][0], False), (st["outs"][0], True)):
            out.append(((a, st["shape"], "f16"), a, a + size, written))
    return out


def _segments(dev, merged: list) -> list:
    """`merged` cut where a stage would name bytes an earlier stage of its
    package names under another port, one of them written: a package orders
    its stages through each port, not through memory two ports share."""
    parts: list = []
    seen: list = []
    for st in merged:
        mine = _accesses(dev, st)
        clash = any(
            k != k2 and lo < hi2 and lo2 < hi and (w or w2)
            for k, lo, hi, w in mine
            for k2, lo2, hi2, w2 in seen
        )
        if not parts or clash:
            parts.append([])
            seen = []
        parts[-1].append(st)
        seen += mine
    return parts


def _package(dev, merged: list, info) -> object:
    """`merged` (stages in order) as one `fused.kernel` launch."""
    # Ports and scratch names.
    written_addrs = {
        a
        for st in merged
        for k, a in enumerate(st["outs"])
        if st["kind"] != "flat" or st["kept"][k]
    }
    read_addrs = {a for st in merged for a in st["ins"]}
    port_of: dict = {}
    ports: list = []
    port_binds: dict = {}
    scratch: list = []
    staged: list = []

    def port(addr: int, shape: tuple, dtype: str, written: bool) -> str:
        name = f"p{len(ports)}"
        ports.append({"name": name, "shape": shape, "dtype": dtype, "written": written})
        port_binds[name] = addr
        return name

    def get_buf_name(
        addr: int, shape: tuple, dtype: str = "f16", n_flat: int | None = None
    ) -> str:
        if n_flat is not None and n_flat % 128 != 0 and shape != (16,):
            item_b = 4 if dtype == "f32" else 2
            ext_w = -(-(n_flat * item_b) // 32)
            ext_shape = (ext_w, 32 // item_b)
            pkey = (addr, ext_shape, dtype)
            if pkey not in port_of:
                is_out = addr in written_addrs
                sname = f"s_{len(scratch)}"
                scratch.append({"name": sname, "shape": shape, "dtype": dtype})
                staged.append(
                    {
                        "scratch": sname,
                        "port": port(addr, ext_shape, dtype, is_out),
                        "words": ext_w,
                        "read": addr in read_addrs,
                        "written": is_out,
                    }
                )
                port_of[pkey] = sname
            return port_of[pkey]
        pkey = (addr, shape, dtype)
        if pkey not in port_of:
            is_out = addr in written_addrs and shape != (16,)
            port_of[pkey] = port(addr, shape, dtype, is_out)
        return port_of[pkey]

    steps: list = []
    for st in merged:
        if st["kind"] == "flat":
            n_el = st["n"]
            total = st["total"]
            out_dt = st["out_dt"]
            lanes = st["lanes"]
            in_names = [
                get_buf_name(
                    a,
                    (16,) if i in lanes else (total, 128),
                    st["dts"][i],
                    None if i in lanes else n_el,
                )
                for i, a in enumerate(st["ins"])
            ]
            members = _emitted(st)
            at = {k: len(st["ins"]) + j for j, k in enumerate(members)}
            kept = [st["kept"][k] for k in members]
            steps.append(
                {
                    "kind": "flat",
                    "total": total,
                    "dtypes": st["dts"],
                    "out_dtype": out_dt,
                    "exprs": [
                        FM.generated.expr(_member_refs(st["exprs"][k], at))
                        for k in members
                    ],
                    "ins": in_names,
                    "lanes": sorted(lanes.items()),
                    "outs": [
                        (
                            get_buf_name(st["outs"][k], (total, 128), out_dt, n_el)
                            if st["kept"][k]
                            else ""
                        )
                        for k in members
                    ],
                    "kept": kept,
                }
            )
        elif st["kind"] == "rows":
            R, W = st["R"], st["W"]
            in_names = [
                get_buf_name(a, FM.generated.port_shape(dev, o, R, W), o.dtype)
                for a, o in zip(st["ins"], st["ops"], strict=True)
            ]
            out_names = [
                get_buf_name(oa, FM.generated.port_shape(dev, oo, R, W), oo.dtype)
                for oa, oo in zip(st["outs"], st["out_ops"], strict=True)
            ]
            steps.append(
                {
                    "kind": "rows",
                    "R": R,
                    "W": W,
                    "exprs": [FM.generated.expr(e) for e in st["exprs"]],
                    "operands": [vars(o) for o in st["ops"]],
                    "ins": in_names,
                    "out_operands": [vars(o) for o in st["out_ops"]],
                    "outs": out_names,
                }
            )
        elif st["kind"] == "copy":
            sh = st["shape"]
            steps.append(
                {
                    "kind": "copy",
                    "src": get_buf_name(st["ins"][0], sh),
                    "dst": get_buf_name(st["outs"][0], sh),
                }
            )

    package = {"ports": ports, "scratch": scratch, "staged": staged, "steps": steps}
    return FM.generated.launch(dev, "fused.kernel", package, port_binds, info=info)
