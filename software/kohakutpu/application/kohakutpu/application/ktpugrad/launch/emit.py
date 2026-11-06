"""Multi-output elementwise package emission for a fused group's members.

Consecutive flat stages over one domain share a loop. A member reading an
earlier member's output reads its packed value (`Load(-1 - k)` until emitted,
then its operand after the reads): what the store writes, read back in the
trip. A member may overwrite what an earlier one reads, every read being
loaded before any store. A whole copy of a read or an output is one more
member. An output dead after the group and read by no later stage is not
stored, unless a member reads its value and no copy of it is stored (a pack is
read back from its store's bytes). A loop holds at most `vc.descriptors`
memory views. A loop whose member's buffer has consumer packings
(`packings`) writes them checked: as rows over the [N, K] they pack, a band a
trip (`fused::Step::Packed`), or as quant steps after the loop reading the
stored buffer (`fused::Step::Quant`).
"""

import json
import math
from functools import lru_cache

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.runtime import forms as FM
from kohakutpu.application.runtime import rows as RW
from kohakutpu.language import machine as MF
from kohakutpu.language.config import ENV
from kohakutpu.stack.rs import _rs


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


#: Whether a writer writes its buffer's consumer packings (`packings`), else
#: each consumer packs.
WRITER_PACKS = ENV.get("KTPU_WRITER_PACKS", "1") == "1"


def _demand(key: tuple) -> dict:
    """The generator's packing (`matmul::QDemand`) of a `forms.packed` key."""
    return {"side": key[1], "g": key[7], "nk": key[8], "rp": key[5], "cp": key[6]}


def _rows_operands(shape: tuple, lanes: dict, reads: int) -> list:
    """A flat loop's `reads` as rows over `shape` [N, K]: a whole read the
    [N, K] view, a lane read its word's value."""
    n, k = shape
    return [
        (
            Operand("scalar", offset=lanes[i])
            if i in lanes
            else Operand("full", pitch=k, rows=n)
        )
        for i in range(reads)
    ]


def _fits(dev, shape: tuple, lanes: dict, reads: int, outs: int, demands) -> bool:
    """Whether a packed rows step over `shape` writes every one of `demands`
    (`_rs.fused_packs`)."""
    args = {
        "R": shape[0],
        "W": shape[1],
        "operands": [vars(o) for o in _rows_operands(shape, lanes, reads)],
        "outs": outs,
        "q": list(demands),
    }
    machine = FM.generated.rs.machine_json(dev.machine)
    return _packs_decided(json.dumps(args, sort_keys=True), machine)


@lru_cache(maxsize=4096)
def _packs_decided(args: str, machine: str) -> bool:
    return _rs.fused_packs(args, machine)


#: A writer's packings: "a" packed rows (`fused::Step::Packed`) where they fit, "b"
#: quant steps after its loop (`fused::Step::Quant`), "auto" the cheapest of both and none.
WRITER_PACK_FORM = ENV.get("KTPU_WRITER_PACK_FORM", "auto")

#: The form `packings` took for each (shape, packings): "a" or "b".
CHOSEN: dict = {}


def packings(dev, st, n: int, lanes: dict, reads: int) -> tuple | None:
    """The consumer packings demanded of `st` (`forms.updated_site`) that a flat
    writer of its `n` values (`reads` reads, `lanes` theirs) writes, their forms
    allocated: ``(shape, ((demand, q address), ...), keys)``, `shape` the
    [N, K] they pack (form "a"), or None for quant steps after the loop (form
    "b", each demand its `forms.writer_quant`); None where none is demanded or
    no form writes them all (or `WRITER_PACKS` is off)."""
    if not WRITER_PACKS:
        return None
    keys = [k for k in FM.demands(dev).get(FM.updated_site(st), ()) if k[3] * k[4] == n]
    if not keys:
        return None
    shape = (keys[0][3], keys[0][4])
    keys = [k for k in keys if (k[3], k[4]) == shape]
    quants: list = [FM.writer_quant(dev, k) for k in keys]
    # Packed rows hold a view a packing beside the writer's reads and its
    # output (as `_Loop.fits` counts): demands past the descriptors are not taken.
    room = MF.load(dev.machine.board).vc.descriptors - reads - 1
    a_keys = keys[: max(room, 0)]
    packed = [_demand(k) for k in a_keys]
    a = (
        WRITER_PACK_FORM != "b"
        and bool(a_keys)
        and _fits(dev, shape, lanes, reads, 1, packed)
    )
    b = WRITER_PACK_FORM != "a" and all(q is not None for q in quants)
    if WRITER_PACK_FORM == "auto" and (a or b):
        priced = _Priced(dev, shape, lanes, reads)
        separate = [_standalone(dev, k) for k in keys]
        a_calls = (
            [
                priced.call(shape, packed),
                *[c for seq in separate[len(a_keys) :] for c in seq],
            ]
            if a
            else []
        )
        b_calls = [priced.call(None, quants)] if b else []
        if a and b:
            b = not priced.cheaper(a_calls, b_calls)
            a = not b
        # The separate path writes the same complete set of consumer packings.
        alone = [priced.call(None, None), *[c for seq in separate for c in seq]]
        if not priced.cheaper(b_calls if b else a_calls, alone):
            a = b = False
    if not (a or b):
        return None
    if not b:
        keys = a_keys
    CHOSEN[(shape, tuple(keys))] = "b" if b else "a"
    demands = quants if b else packed
    qs = tuple((d, st.form(k, k[5] * k[6]).addr) for d, k in zip(demands, keys))
    return (None if b else shape), qs, tuple(keys)


def _standalone(dev, key: tuple) -> list[dict]:
    """The production launches selected for a flat writer's consumer."""
    _, side, src, rows, cols, rp, cp, g, nk, batches = key
    if cols % 16 == 0 and src.pitch % 16 == 0 and src.offset % 16 == 0:
        return [_quant(dev, src, rows, cols, rp, cp, side, g, nk, batches)]
    r, dp = batches * rows, -(-cols // 16) * 16
    walk, dwalk = ((r, src.pitch), (cols, 1)), ((r, dp), (cols, 1))
    step = FM.Step(None, batches * rows * cols, walk, src.offset, dwalk, 0, r * dp)
    laid = Operand("full", pitch=dp)
    mode, call, _ = FM.quant_production(
        dev, (step,), laid, rows, dp, rp, cp, side, g, nk, batches
    )
    if mode != "materialized":
        return [{"gen": "matmul.quant_kernel", "args": call}]
    made = FM.generated.relayout_plan(
        "relayout.plan", rows * cols, walk, src.offset, dwalk, 0, r * dp, False, 0
    )
    plan = FM.generated.planned(dev, made)
    gen = "relayout.relayout_kernel"
    if "unplanned" in plan:
        made = FM.generated.relayout_plan(
            "lanes.plan", rows * cols, walk, src.offset, dwalk, 0, r * dp, False
        )
        gen = "lanes.relayout_kernel"
    laid = Operand("full", pitch=dp, rows=r)
    return [
        {"gen": gen, "args": {"pl": made}},
        _quant(dev, laid, rows, dp, rp, cp, side, g, nk, batches),
    ]


def _quant(dev, src, rows, cols, rp, cp, side, g, nk, batches):
    recipe = FM.costs.quant_recipe(src, rows, cols, rp, cp, side, g, nk, batches)
    strategy = (
        "staged"
        if FM.costs.choose("staged_padding", recipe, dev.machine.board)
        else "native"
    )
    return {
        "gen": "matmul.quant_kernel",
        "args": FM._quant_call(src, rows, cols, rp, cp, side, g, nk, batches, strategy),
    }


class _Priced:
    """A writer of `reads` reads over [N, K] = `shape` priced by its package's
    cycles (`_cost_decided`), a copy member standing for its math."""

    def __init__(self, dev, shape: tuple, lanes: dict, reads: int):
        self.dev, self.shape, self.lanes, self.reads = dev, shape, lanes, reads

    def call(self, over, demands) -> dict:
        """Its package: packed rows over `over`, or quant steps after its loop
        (`over` None), or no packing (`demands` None)."""
        binds = {f"in{i}": 2**20 * (i + 1) for i in range(self.reads)}
        binds["out"] = 0
        packs = None
        if demands is not None:
            packs = (over, tuple((d, 2**30 + 2**20 * i) for i, d in enumerate(demands)))
        stage = (
            "flat",
            X.Load(0),
            ("f16",) * self.reads,
            math.prod(self.shape),
            "f16",
            tuple(self.lanes.items()),
            binds,
            {},
            packs,
        )
        norm = [_normal(stage)]
        views = 1 + self.reads + len(demands or ())
        merged = _merge(self.dev, norm, _Loop(self.dev, norm, set(), views))
        package, _ = _package(self.dev, merged)
        return {"gen": "fused.kernel", "args": package}

    def cheaper(self, read: list, launches: list) -> bool:
        """Whether all of `read` costs no more than `launches`, including gaps."""
        return _cost_decided(
            json.dumps(read, sort_keys=True),
            json.dumps(launches, sort_keys=True),
            FM.generated.rs.machine_json(self.dev.machine),
        )


def published(st, held: bool, packs: tuple | None) -> None:
    """After a whole write of `st`: where its values replaced values it held
    (a parameter's update), its consumers' packings are learned at it
    (`forms.updated_site`); the packings written with it (`packings`) valid."""
    if held:
        st.site = (FM.updated_site(st), None)
    for k in packs[2] if packs else ():
        st.made(k)


class _Loop:
    """What decides a flat loop's stores: what each stage reads, the spans
    dead after the group, and the views a loop holds."""

    def __init__(self, dev, stages: list, dead: set, views: int):
        self.dev = dev
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

    def fits(
        self, ins: list, outs: list, writers: list, exprs: list, packs: dict, quanted
    ) -> list | None:
        """Each output's stored flag (a packed or `quanted` member's always),
        None past the loop's views (a packing one)."""
        members = set(writers)
        base = [
            k in packs or k in quanted or self.kept(o, w, members)
            for k, (o, w) in enumerate(zip(outs, writers, strict=True))
        ]
        kept = _stores(exprs, base)
        views = len(ins) + sum(kept) + sum(map(len, packs.values()))
        return kept if views <= self.views else None


def _extent(n: int, dt: str) -> tuple:
    """`(bytes, element width)` a flat loop's whole access of `n` values covers
    in the package: whole 32-byte words when staged (`n` not whole rows of
    128), else whole rows."""
    item = 4 if dt == "f32" else 2
    if n % 128:
        return -(-(item * n) // 32) * 32, item
    return 128 * item * -(-n // 128), item


def _flat_spans(n: int, ins: list, dts: list, lanes: dict, outs: list, out_dt) -> list:
    """`(start, end, element, written)` of each access of a flat loop over `n`
    values: a lane read covers its word and has no element; a whole access
    covers its package extent and reads or writes element `i` in trip `i`,
    the same element only at the same start and width."""
    spans = []
    for i, (a, dt) in enumerate(zip(ins, dts, strict=True)):
        if i in lanes:
            spans.append((a, a + 32, None, False))
            continue
        size, item = _extent(n, dt)
        spans.append((a, a + size, (a, item), False))
    size, item = _extent(n, out_dt)
    return spans + [(o, o + size, (o, item), True) for o in outs]


def _crossed(first: list, second: list) -> bool:
    """Whether two access lists share a byte one of them writes, anywhere but
    a read and a write of the same element: a trip would read or overwrite
    what another trip writes."""
    return any(
        lo < hi2 and lo2 < hi and (w or w2) and (e is None or e != e2 or (w and w2))
        for lo, hi, e, w in first
        for lo2, hi2, e2, w2 in second
    )


def _loop_spans(st: dict) -> list:
    return _flat_spans(
        st["n"], st["ins"], st["dts"], st["lanes"], st["outs"], st["out_dt"]
    )


def _join_flat(prev: dict, st: dict, idx: int, loop: _Loop) -> bool:
    """`st` (one member) into the flat loop `prev`; False when it cannot share it."""
    out = st["outs"][0]
    if prev["n"] != st["n"] or prev["out_dt"] != st["out_dt"]:
        return False
    if None not in (prev["over"], st["over"]) and prev["over"] != st["over"]:
        return False
    if _crossed(_loop_spans(prev), _loop_spans(st)):
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
    packs, quants = dict(prev["packs"]), dict(prev["quants"])
    if st["packs"]:
        packs[len(prev["exprs"])] = st["packs"][0]
    if st["quants"]:
        quants[len(prev["exprs"])] = st["quants"][0]
    return _commit(
        prev,
        idx,
        loop,
        _remap_expr_loads(st["exprs"][0], mapping),
        out,
        (ins, dts, lanes),
        prev["over"] or st["over"],
        (packs, quants),
    )


def _join_copy(prev: dict, st: dict, idx: int, loop: _Loop) -> bool:
    """A whole copy of one of `prev`'s reads or outputs as one more member."""
    src, dst = st["ins"][0], st["outs"][0]
    if st["nbytes"] != 2 * prev["n"] or dst in prev["outs"]:
        return False
    # The source is one of the loop's accesses, so a destination crossing it
    # crosses the loop.
    mine = _flat_spans(prev["n"], [src], ["f16"], {}, [dst], "f16")
    if _crossed(_loop_spans(prev), mine):
        return False
    if src in prev["outs"]:
        expr = X.Load(-1 - _root(prev["exprs"], prev["outs"].index(src)))
    elif src in prev["ins"] and prev["ins"].index(src) not in prev["lanes"]:
        expr = X.Load(prev["ins"].index(src))
    else:
        return False
    reads = (prev["ins"], prev["dts"], prev["lanes"])
    packings = (prev["packs"], prev["quants"])
    return _commit(prev, idx, loop, expr, dst, reads, prev["over"], packings)


def _root(exprs: list, k: int) -> int:
    """The member whose value member `k` holds (itself, or what it copies)."""
    while (j := _copy_of(exprs[k])) is not None:
        k = j
    return k


def _commit(
    prev: dict,
    idx: int,
    loop: _Loop,
    expr,
    out: int,
    reads: tuple,
    shape: tuple | None,
    packings: tuple,
) -> bool:
    """The member into `prev` with `packings` (packed rows', quant steps');
    False past the loop's views or, with packed rows, where the step over
    `shape` does not write them all."""
    ins, dts, lanes = reads
    packs, quants = packings
    exprs = [*prev["exprs"], expr]
    outs, writers = [*prev["outs"], out], [*prev["writers"], idx]
    kept = loop.fits(ins, outs, writers, exprs, packs, set(quants))
    if kept is None:
        return False
    demands = [d for qs in packs.values() for d, _ in qs]
    if shape is not None and not _fits(
        loop.dev, shape, lanes, len(ins), sum(kept), demands
    ):
        return False
    prev.update(
        ins=ins,
        dts=dts,
        lanes=lanes,
        exprs=exprs,
        outs=outs,
        writers=writers,
        kept=kept,
        over=shape,
        packs=packs,
        quants=quants,
    )
    return True


def _row_spans(dev, st: dict) -> list:
    """A rows member's accesses: a member reads no other member's output, so
    no element of it is exempt."""
    return [(lo, hi, None, w) for _, lo, hi, w in _accesses(dev, st)]


def _maps(st: dict) -> list:
    """`(source, steps)` of each operand a rows member reads composed."""
    return sorted(
        (st["ins"][i], json.dumps(s, sort_keys=True)) for i, s in st["made"].items()
    )


def _join_rows(dev, prev: dict, st: dict) -> bool:
    """Independent rows members over one [R, W] domain, full outputs, reading
    the same composed maps (none, or each the same steps of one source): an
    operand both read is loaded once a band."""
    if not (
        prev["R"] == st["R"]
        and prev["W"] == st["W"]
        and len(prev["outs"]) == 1
        and len(st["outs"]) == 1
        and prev["out_ops"][0].kind == "full"
        and st["out_ops"][0].kind == "full"
        and prev["out_ops"][0].offset == st["out_ops"][0].offset == 0
        and _maps(prev) == _maps(st)
    ):
        return False
    if _crossed(_row_spans(dev, prev), _row_spans(dev, st)):
        return False
    comb_ops = list(prev["ops"])
    comb_ins = list(prev["ins"])
    comb_made = dict(prev["made"])
    mapping = {}
    for i, (a, op) in enumerate(zip(st["ins"], st["ops"], strict=True)):
        steps = st["made"].get(i)
        found = None
        for j, (pa, pop) in enumerate(zip(comb_ins, comb_ops, strict=True)):
            if pa == a and pop == op and comb_made.get(j) == steps:
                found = j
                break
        if found is None:
            found = len(comb_ins)
            comb_ins.append(a)
            comb_ops.append(op)
            if steps is not None:
                comb_made[found] = steps
        mapping[i] = found
    if len(comb_ops) + 2 > 7:
        return False
    prev["ops"] = comb_ops
    prev["ins"] = comb_ins
    prev["made"] = comb_made
    prev["exprs"].append(_remap_expr_loads(st["exprs"][0], mapping))
    prev["out_ops"].append(st["out_ops"][0])
    prev["outs"].append(st["outs"][0])
    return True


def _normal(st) -> dict:
    kind = st[0]
    if kind == "flat":
        _, expr, dts, n, out_dt, lanes, binds, _, packs = st
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
            "over": packs[0] if packs else None,
            "packs": {0: packs[1]} if packs and packs[0] is not None else {},
            "quants": {0: packs[1]} if packs and packs[0] is None else {},
        }
    if kind == "rows":
        _, expr, ops, R, W, out_op, binds, _, (made, _) = st
        return {
            "kind": "rows",
            "R": R,
            "W": W,
            "exprs": [expr],
            "ops": list(ops),
            "out_ops": [out_op],
            "ins": [binds[f"in{i}"] for i in range(len(ops))],
            "outs": [binds["out"]],
            "made": dict(made),
        }
    _, nbytes, src_a, dst_a, _ = st
    return {"kind": "copy", "nbytes": nbytes, "ins": [src_a], "outs": [dst_a]}


def _merge(dev, stages: list, loop: _Loop) -> list:
    """Each stage joined to the loop before it where no access of it crosses
    the loop's; a stage that cannot starts the next loop."""
    merged: list = []
    for idx, st in enumerate(stages):
        prev = merged[-1] if merged else None
        fresh = RW.MUTANT != "wrong_order"
        if prev is not None and fresh:
            if prev["kind"] == "flat":
                joined = (st["kind"] == "flat" and _join_flat(prev, st, idx, loop)) or (
                    st["kind"] == "copy" and _join_copy(prev, st, idx, loop)
                )
            else:
                joined = prev["kind"] == st["kind"] == "rows" and _join_rows(
                    dev, prev, st
                )
            if joined:
                prev["stages"].append(idx)
                continue
        if st["kind"] == "flat":
            st.update(writers=[idx], kept=[True])
        if st["kind"] == "copy":
            st["shape"] = (-(-st["nbytes"] // 32), 16)
        st["stages"] = [idx]
        merged.append(st)
    return merged


def flush_multi_stage(scope) -> list:
    """Compile and launch all stages recorded in `scope` as one DSL package."""
    if scope.flushed or not scope.stages:
        return []
    scope.flushed = True
    dev = scope.dev
    stages = list(scope.stages)
    if len(stages) == 1 and RW.MUTANT is None:
        return [_solo(dev, stages[0])]

    if RW.MUTANT == "drop_member" and len(stages) > 1:
        stages = stages[:-1]
    elif RW.MUTANT == "wrong_order" and len(stages) > 1:
        stages = list(reversed(stages))

    # Only the group's last flush sees every reader: an earlier one (a launch
    # the group makes at once reads what is staged so far) stores everything.
    last = scope.remaining is not None and scope.remaining <= 0
    return _packages(dev, stages, scope.dead_addrs if last else set(), scope.info)


def _packages(dev, stages: list, dead: set, info) -> list:
    """`stages` (in order) launched as packages; a package reading composed
    runs only where it lowers and costs no more than its stages apart
    (`_cheaper`), else its composed stages run alone (`_apart`)."""
    norm = [_normal(st) for st in stages]
    views = MF.load(dev.machine.board).vc.descriptors
    merged = _merge(dev, norm, _Loop(dev, norm, dead, views))
    _alike(dev, merged)

    if RW.MUTANT == "alias_members" and len(merged) >= 1:
        if len(merged[0]["outs"]) >= 2:
            merged[0]["outs"][1] = merged[0]["outs"][0]
        elif len(merged) >= 2:
            merged[1]["outs"][0] = merged[0]["outs"][0]

    runs = []
    for part in _segments(dev, merged):
        package, binds = _package(dev, part)
        own = [stages[i] for st in part for i in st["stages"]]
        if any(st.get("made") for st in part) and not _cheaper(dev, package, own):
            runs += _apart(dev, own, dead, info)
            continue
        runs.append(FM.generated.launch(dev, "fused.kernel", package, binds, info=info))
    return runs


def _alike(dev, merged: list) -> None:
    """A flat loop naming bytes a packed loop (`packings`) names runs over its
    [N, K] too, where it fits: the bytes one port shape, the loops one package
    (`_segments`)."""
    changed = True
    while changed:
        changed = False
        packed = [s for s in merged if s["kind"] == "flat" and s["over"] is not None]
        for st in merged:
            if st["kind"] != "flat" or st["over"] is not None:
                continue
            mine = {*st["ins"], *st["outs"]}
            over = next(
                (
                    p["over"]
                    for p in packed
                    if st["n"] == math.prod(p["over"])
                    and mine & {*p["ins"], *p["outs"]}
                    and _fits(
                        dev, p["over"], st["lanes"], len(st["ins"]), sum(st["kept"]), []
                    )
                ),
                None,
            )
            if over is not None:
                st["over"], changed = over, True


def _cheaper(dev, package: dict, own: list) -> bool:
    """Whether `package` lowers and its cycles are no more than its stages'
    own launches and their gaps (`_rs.read_composed` under cost)."""
    return _cost_decided(
        json.dumps({"gen": "fused.kernel", "args": package}, sort_keys=True),
        json.dumps([_solo_call(dev, s) for s in own], sort_keys=True),
        FM.generated.rs.machine_json(dev.machine),
    )


@lru_cache(maxsize=4096)
def _cost_decided(read: str, launches: str, machine: str) -> bool:
    return _rs.read_composed("cost", read, launches, machine)


def _apart(dev, own: list, dead: set, info) -> list:
    """`own` in order, each stage reading composed alone and the stages
    between them packaged."""
    runs: list = []
    between: list = []
    for st in [*own, None]:
        alone = st is None or (st[0] == "rows" and st[8][1] is not None)
        if alone and between:
            runs += (
                _packages(dev, between, dead, info)
                if len(between) > 1
                else [_solo(dev, between[0])]
            )
            between = []
        if st is not None:
            runs += [_solo(dev, st)] if alone else []
            between += [] if alone else [st]
    return runs


def _solo_call(dev, st) -> dict:
    """The generator call stage `st` makes as its own launch."""
    kind = st[0]
    if kind == "flat":
        _, expr, dts, n, _, lanes, _, _, _ = st
        args = FM.generated.flat_arguments(expr, dts, n, lanes)
        return {"gen": "vector.flat_kernel", "args": args}
    if kind == "rows":
        _, expr, ops, R, W, out_op, _, _, (_, solo) = st
        if solo is not None:
            return {"gen": "prepared.rows_kernel", "args": solo[0]}
        args = FM.generated.rows_arguments(expr, ops, R, W, out_op)
        return {"gen": "vector.rows_kernel", "args": args}
    return {"gen": "convert.copy_kernel", "args": {"nbytes": int(st[1])}}


def _solo(dev, st) -> object:
    """Stage `st` launched on its own."""
    kind = st[0]
    if kind == "flat":
        _, expr, dts, n, _, lanes, binds, info, packs = st
        if packs:
            (run,) = _packages(dev, [st], set(), info)
            return run
        return FM.generated.flat(dev, expr, dts, n, binds, info=info, scalars=lanes)
    if kind == "rows":
        _, expr, ops, R, W, out_op, binds, info, (_, solo) = st
        if solo is not None:
            read, binds = solo
            info = dict(info, prepared=read["composed"])
            return FM.generated.launch(
                dev, "prepared.rows_kernel", read, binds, info=info
            )
        cores = dev.machine.count("VC")
        return RW._launch(dev, expr, ops, binds, R, W, out_op, info, cores)
    _, nbytes, src_a, dst_a, info = st
    return FM.generated.copy(dev, nbytes, {"src": src_a, "dst": dst_a}, info=info)


def _accesses(dev, st: dict) -> list:
    """`(port key, start, end, written)` of each buffer a merged stage names,
    keyed as `_package` names its ports and scratches."""
    out = []
    if st["kind"] == "flat" and st["over"] is not None:
        return _packed_accesses(st)
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
        out += _q_accesses(st)
    elif st["kind"] == "rows":
        R, W = st["R"], st["W"]
        # A composed read's step over one row names its views by ports of its
        # own (`_owns`), its sources by their words.
        own = (id(st),) if _owns(st) else ()
        for ops, addrs, written in (
            (st["ops"], st["ins"], False),
            (st["out_ops"], st["outs"], True),
        ):
            for i, (a, o) in enumerate(zip(addrs, ops, strict=True)):
                steps = None if written else st["made"].get(i)
                if steps is None:
                    shape = tuple(FM.generated.port_shape(dev, o, R, W))
                    key = (a, shape, o.dtype, *own)
                else:
                    shape = _source_shape(steps)
                    key = (a, shape, "f16")
                size = (4 if key[2] == "f32" else 2) * math.prod(shape)
                out.append((key, a, a + size, written))
    else:
        size = 32 * st["shape"][0]
        for a, written in ((st["ins"][0], False), (st["outs"][0], True)):
            out.append(((a, st["shape"], "f16"), a, a + size, written))
    return out


def _packed_accesses(st: dict) -> list:
    """`_accesses` of a flat loop run as packed rows (`_packed_step`): its
    whole reads and outputs as [N, K] ports, its packings written."""
    shape = tuple(st["over"])
    size = 2 * math.prod(shape)
    ops = _rows_operands(shape, st["lanes"], len(st["ins"]))
    out = [
        (
            ((a, (16,), dt), a, a + 32, False)
            if o.kind == "scalar"
            else ((a, shape, "f16"), a, a + size, False)
        )
        for a, o, dt in zip(st["ins"], ops, st["dts"], strict=True)
    ]
    out += [
        ((o, shape, "f16"), o, o + size, True)
        for o, k in zip(st["outs"], st["kept"], strict=True)
        if k
    ]
    return out + _q_accesses(st)


def _q_accesses(st: dict) -> list:
    """`_accesses` of the packings a flat loop writes."""
    return [
        ((q, (d["side"],)), q, q + d["rp"] * d["cp"], True)
        for qs in [*st["packs"].values(), *st["quants"].values()]
        for d, q in qs
    ]


def _packed_step(st: dict, buf_name, q_port) -> dict:
    """A flat loop with packings as one `fused::Step::Packed` over [N, K]: its
    members as the flat step's, port names from `buf_name`, each packing's
    from `q_port`."""
    shape = tuple(st["over"])
    ops = _rows_operands(shape, st["lanes"], len(st["ins"]))
    members = _emitted(st)
    at = {k: len(st["ins"]) + j for j, k in enumerate(members)}
    return {
        "kind": "packed",
        "R": shape[0],
        "W": shape[1],
        "exprs": [FM.generated.expr(_member_refs(st["exprs"][k], at)) for k in members],
        "operands": [vars(o) for o in ops],
        "ins": [
            buf_name(a, (16,), dt) if o.kind == "scalar" else buf_name(a, shape)
            for a, o, dt in zip(st["ins"], ops, st["dts"], strict=True)
        ],
        "outs": [
            buf_name(st["outs"][k], shape) if st["kept"][k] else "" for k in members
        ],
        "packs": [
            dict(d, member=j, port=q_port(q))
            for j, k in enumerate(members)
            for d, q in st["packs"].get(k, ())
        ],
    }


def _owns(st: dict) -> bool:
    """Whether a rows step names its views by ports of its own: a composed
    read over one [1, W] row may tile it as [W / A, A] rows of whole source
    words, its ports reshaped (`fused::tiled_ports`)."""
    return bool(st["made"]) and st["R"] == 1


def _source_shape(steps: list) -> tuple:
    """The port a composed read takes: its source's whole words of 16 f16
    values (`prepared::input_ports`)."""
    n = steps[0]["made_by"]["args"]["src_n"]
    return (-(-n // 16), 16)


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


def _package(dev, merged: list) -> tuple:
    """`merged` (stages in order) as one `fused.kernel` call: `(arguments,
    binds)`."""
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
        addr: int,
        shape: tuple,
        dtype: str = "f16",
        n_flat: int | None = None,
        own: int | None = None,
    ) -> str:
        """The port or scratch of a buffer's access; `own` gives step `own`
        ports no other step names (a composed read's step tiles its ports)."""
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
        pkey = (addr, shape, dtype) + ((own,) if own is not None else ())
        if pkey not in port_of:
            is_out = addr in written_addrs and shape != (16,)
            port_of[pkey] = port(addr, shape, dtype, is_out)
        return port_of[pkey]

    def q_port(addr: int) -> str:
        name = f"q{sum(n[0] == 'q' for n in port_binds)}"
        port_binds[name] = addr
        return name

    def quant_steps(st: dict, *view) -> list:
        """The quant steps (`fused::Step::Quant`) of a flat loop's members'
        packings, each reading its member's stored buffer as `view`."""
        return [
            dict(d, kind="quant", x=get_buf_name(st["outs"][k], *view), port=q_port(q))
            for k, qs in sorted(st["quants"].items())
            for d, q in qs
        ]

    steps: list = []
    for st in merged:
        if st["kind"] == "flat" and st["over"] is not None:
            steps.append(_packed_step(st, get_buf_name, q_port))
            steps += quant_steps(st, tuple(st["over"]))
        elif st["kind"] == "flat":
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
            steps += quant_steps(st, (total, 128), out_dt, n_el)
        elif st["kind"] == "rows":
            R, W, made = st["R"], st["W"], st["made"]
            own = len(steps) if _owns(st) else None
            in_names = [
                (
                    get_buf_name(a, _source_shape(made[i]))
                    if i in made
                    else get_buf_name(
                        a, FM.generated.port_shape(dev, o, R, W), o.dtype, own=own
                    )
                )
                for i, (a, o) in enumerate(zip(st["ins"], st["ops"], strict=True))
            ]
            out_names = [
                get_buf_name(
                    oa, FM.generated.port_shape(dev, oo, R, W), oo.dtype, own=own
                )
                for oa, oo in zip(st["outs"], st["out_ops"], strict=True)
            ]
            step = {
                "kind": "rows",
                "R": R,
                "W": W,
                "exprs": [FM.generated.expr(e) for e in st["exprs"]],
                "operands": [vars(o) for o in st["ops"]],
                "ins": in_names,
                "out_operands": [vars(o) for o in st["out_ops"]],
                "outs": out_names,
            }
            if made:
                step["made"] = {str(i): s for i, s in made.items()}
            steps.append(step)
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
    return package, port_binds
