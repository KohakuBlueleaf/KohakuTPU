"""Vector-core streams generated from a VECTOR PROGRAM: what the L3 -> L2
compiler hands a core for a group of vector ops over a tile.

The spec, ``("vp", mode, geom, inputs, cols, nodes, out)``, its kinds, passes
and L1 slots: docs/projects/kohakutpu/ir/l3.md §5. A wide node read in a later
pass than its own goes through L1 as fp16. Of a few register configurations
(steps at once, partial accumulators a reduction) the one `vsched` times
fastest is kept.
"""

from dataclasses import dataclass
from functools import cache

import numpy as np
from kohakutpu.hw import vector as V
from kohakutpu.ir.l1 import vsched
from kohakutpu.ir.l1.kernels import stream
from kohakutpu.ir.l1.vector import (
    Alu,
    Desc,
    Dims,
    Seti,
    Setmode,
    Setvl,
    Vld,
    Vshuf,
    Vst,
)

ROWS = 8  # rows a step: one a chunk at VL 128
SLICE = 8  # words a VL=128 slice
NREG = 16
NPRED = 4
S_VL = 0
S_ROT = (1, 2, 3, 4)  # rotations 8, 4, 2, 1
S_FREE = tuple(range(5, 16))
#: The column vectors' walk descriptor.
AD_B = 5

UNARY = {
    "neg": "VNEG",
    "abs": "VABS",
    "exp2": "VEXP2",
    "log2": "VLOG2",
    "inv": "VINV",
    "rsqrt": "VRSQRT",
    "copy": "VMOV",
}
#: Opcode and the field the second operand takes (`vec_alu.v`'s muxes).
BINARY = {
    "add": ("VADD", "vc"),
    "sub": ("VSUB", "vc"),
    "mul": ("VMUL", "vb"),
    "max": ("VMAX", "vb"),
    "min": ("VMIN", "vb"),
}
CMP = {"cmp.lt": "VCMPLT", "cmp.gt": "VCMPGT", "cmp.eq": "VCMPEQ"}
REDUCE = {"reduce.sum": "add", "reduce.max": "max", "reduce.min": "min"}
HOST = {
    "neg": np.negative,
    "abs": np.abs,
    "exp2": np.exp2,
    "log2": np.log2,
    "inv": lambda x: 1.0 / x,
    "rsqrt": lambda x: 1.0 / np.sqrt(x),
    "copy": lambda x: x,
    "add": np.add,
    "sub": np.subtract,
    "mul": np.multiply,
    "max": np.maximum,
    "min": np.minimum,
    "fma": lambda a, b, c: a * b + c,
}


def kinds(nodes: tuple, kind_of=None) -> dict:
    """Each node's kind, "S", "P" or "W" (`plan`'s lattice); `kind_of(arg)`
    for an argument that is not a node (default: ``in``/``col`` wide, a
    constant scalar)."""
    out: dict = {}

    def k(a):
        if a[0] == "v":
            return out[a[1]]
        if kind_of is not None:
            return kind_of(a)
        return "S" if a[0] == "s" else "W"

    for name, op, args in nodes:
        ks = [k(a) for a in args]
        if op in REDUCE:
            out[name] = "P"
        elif all(x == "S" for x in ks):
            out[name] = "S"
        else:
            out[name] = "W" if "W" in ks else "P"
    return out


def contract(nodes: tuple, keep=(), kind_of=None) -> tuple:
    """Fewer per-element ops, as the hand-written kernels write them:

    - ``t = mul a, b`` read once, by ``add t, c`` (either side): ``fma a, b, c``;
    - ``d = sub a, p`` read once, by ``mul d, q``, `a` per element and `p`, `q`
      not: ``fma a, q, -(p*q)``, the product once a row.

    One rounding where the program wrote two. `keep`: names something else
    reads (stored, drained, held in state); `kind_of` as `kinds`'."""
    reads: dict = {}
    for _, _, args in nodes:
        for a in args:
            if a[0] == "v":
                reads[a[1]] = reads.get(a[1], 0) + 1
    kind = kinds(nodes, kind_of)

    def k(a):
        if a[0] == "v":
            return kind[a[1]]
        return kind_of(a) if kind_of is not None else ("S" if a[0] == "s" else "W")

    def once(a, defs):
        return a[0] == "v" and a[1] in defs and reads[a[1]] == 1 and a[1] not in keep

    subs = {n: args for n, op, args in nodes if op == "sub"}
    out, gone = [], set()
    for name, op, args in nodes:
        if op == "mul":
            for j in (0, 1):
                d, q = args[j], args[1 - j]
                if once(d, subs) and d[1] not in gone and k(q) != "W":
                    a, p = subs[d[1]]
                    if k(a) == "W" and k(p) != "W":
                        gone.add(d[1])
                        pq, npq = f"{name}.pq", f"{name}.npq"
                        out += [(pq, "mul", (p, q)), (npq, "neg", (("v", pq),))]
                        out.append((name, "fma", (a, q, ("v", npq))))
                        break
            else:
                out.append((name, op, args))
            continue
        out.append((name, op, args))
    out = [n for n in out if n[0] not in gone]
    reads = {}
    for _, _, args in out:
        for a in args:
            if a[0] == "v":
                reads[a[1]] = reads.get(a[1], 0) + 1
    muls = {n: args for n, op, args in out if op == "mul"}
    fused, gone = [], set()
    for name, op, args in out:
        if op == "add":
            for j in (0, 1):
                t = args[j]
                if once(t, muls) and t[1] not in gone:
                    gone.add(t[1])
                    fused.append((name, "fma", muls[t[1]] + (args[1 - j],)))
                    break
            else:
                fused.append((name, op, args))
            continue
        fused.append((name, op, args))
    return tuple(n for n in fused if n[0] not in gone)


class VgenError(ValueError):
    """A vector program the generator does not lower."""


class OutOfRegisters(VgenError):
    pass


# ------------------------------------------------------------------ analysis
@dataclass(frozen=True)
class Plan:
    mode: str
    geom: tuple
    inputs: int
    cols: int
    #: name -> "S" / "P" / "W"
    kind: dict
    #: (name, op, args) with scalar nodes folded into ("s", value) args
    nodes: tuple
    out: str
    #: wide node -> pass; per-row node -> the pass it is ready for
    at: dict
    passes: int
    #: wide node -> L1 slot it is stored to (read back in later passes)
    slot: dict
    scratch: int
    consts: tuple


def _fold(op: str, vals: list) -> float:
    if op not in HOST:
        raise VgenError(f"{op} of constants")
    return float(np.float32(HOST[op](*[np.float64(v) for v in vals])))


@cache
def plan(spec: tuple) -> Plan:
    """Kinds, passes and L1 slots of a vector program."""
    tag, mode, geom, inputs, cols, nodes, out = spec
    if tag != "vp" or mode not in ("rows", "flat"):
        raise VgenError(f"not a vector program: {spec[:2]}")
    if not 1 <= inputs <= 2:
        raise VgenError(f"{inputs} streamed inputs; a core's descriptors hold two")
    if cols and inputs > 1:
        raise VgenError(
            "two streamed inputs and a column vector need nine descriptors; "
            "a core has eight"
        )
    if mode == "flat" and cols and not geom[0]:
        raise VgenError("a column vector in a flat region needs its tile width")
    kind: dict = {}
    value: dict = {}
    folded = []

    def arg_kind(a):
        match a:
            case ("in", k):
                if not 0 <= k < inputs:
                    raise VgenError(f"input {k} of {inputs}")
                return "W"
            case ("col", k):
                if not 0 <= k < cols:
                    raise VgenError(f"column vector {k} of {cols}")
                return "W"
            case ("s", _):
                return "S"
            case ("v", n):
                return kind[n]
        raise VgenError(f"argument {a!r}")

    def resolve(a):
        if a[0] == "v" and kind[a[1]] == "S":
            return ("s", value[a[1]])
        return a

    for name, op, args in nodes:
        ks = [arg_kind(a) for a in args]
        args = tuple(resolve(a) for a in args)
        if op in REDUCE:
            if ks != ["W"]:
                raise VgenError(f"{name} = {op} of a value not a whole row")
            if mode != "rows":
                raise VgenError(f"{name} = {op}: reductions run in rows mode only")
            kind[name] = "P"
        elif all(k == "S" for k in ks):
            if op in CMP or op == "select":
                raise VgenError(f"{name} = {op} of constants")
            kind[name] = "S"
            value[name] = _fold(op, [a[1] for a in args])
            continue
        elif "W" in ks:
            kind[name] = "W"
        else:
            kind[name] = "P"
        if op == "select" and (
            args[0][0] != "v" or _op_of(nodes, args[0][1]) not in CMP
        ):
            raise VgenError(f"{name} = select: its predicate must be a cmp")
        if op in CMP and kind[name] != "W":
            raise VgenError(f"{name} = {op}: compares run on whole rows only")
        if op not in {**UNARY, **BINARY, **CMP, **REDUCE, "fma": 0, "select": 0}:
            raise VgenError(f"no vector lowering for {op!r}")
        folded.append((name, op, args))
    if kind.get(out) != "W":
        raise VgenError(f"the stored value {out!r} is not a value per element")
    # passes
    at: dict = {}
    for name, op, args in folded:
        ready = [0]
        for a in args:
            if a[0] == "v":
                ready.append(at[a[1]])
        at[name] = max(ready) + (1 if op in REDUCE else 0)
    passes = at[out] + 1
    # where each wide node is last read, and by which pass
    last: dict = {}
    for name, op, args in folded:
        for a in args:
            key = a if a[0] == "in" else (a[1] if a[0] == "v" else None)
            if key is None:
                continue
            p = at[name] - (1 if op in REDUCE else 0)
            last[key] = max(last.get(key, 0), p)
    slot: dict = {}
    occupant = {k: last.get(("in", k), 0) for k in range(inputs)}
    scratch = 0
    for name, _, _ in folded:
        if kind[name] != "W" or name == out or last.get(name, at[name]) <= at[name]:
            continue
        free = [s for s, used in occupant.items() if used <= at[name]]
        if free:
            s = min(free)
        else:
            s = inputs + scratch
            scratch += 1
        slot[name] = s
        occupant[s] = last[name]
    slot[out] = 0
    for name, op, _ in folded:
        if op in CMP and name in slot:
            raise VgenError(f"{name} = {op}: a predicate is read in its own pass only")
    consts = sorted(
        {a[1] for _, _, args in folded for a in args if a[0] == "s"}, key=float
    )
    if len(consts) > len(S_FREE):
        raise VgenError(f"{len(consts)} constants; {len(S_FREE)} S registers free")
    return Plan(
        mode,
        tuple(geom),
        inputs,
        cols,
        kind,
        tuple(folded),
        out,
        at,
        passes,
        slot,
        scratch,
        tuple(consts),
    )


def _op_of(nodes, name):
    for n, op, _ in nodes:
        if n == name:
            return op
    return None


def resident_words(spec: tuple, words: int) -> int:
    """The column vectors' resident block, in words."""
    p = plan(spec)
    if not p.cols:
        return 0
    return p.cols * (p.geom[0] // 16 if p.mode == "rows" else p.geom[0])


def slots(spec: tuple) -> int:
    """L1 slots a region takes: the inputs, then scratch."""
    p = plan(spec)
    return p.inputs + p.scratch


# ------------------------------------------------------------------ emission
class _Pool:
    """Free registers, handed out first-freed-last so neighbouring work lands
    on different registers and `vsched` can overlap it."""

    def __init__(self, n: int, what: str) -> None:
        self.free, self.what = list(range(n)), what

    def get(self) -> int:
        if not self.free:
            raise OutOfRegisters(f"out of {self.what} registers")
        return self.free.pop(0)

    def put(self, r: int) -> None:
        self.free.append(r)


def _alu(op, vd, srcs, pr=0, pm=0, fields=("va", "vb", "vc")) -> Alu:
    """`op` with sources ``(sel, reg)`` in `fields`; unused fields name `vd`."""
    kw = {"va": vd, "vb": vd, "vc": vd}
    sel = {}
    for f, (s, r) in zip(fields, srcs, strict=True):
        kw[f] = r
        sel["s" + f[1]] = s
    return Alu(op, vd=vd, pr=pr, pm=pm, **kw, **sel)


def _combine(op: str, vd: int, a: int, b: int) -> Alu:
    name, field = BINARY[op]
    return _alu(name, vd, [(V.SRC_V, a), (V.SRC_V, b)], fields=("va", field))


class _Gen:
    def __init__(self, p: Plan, words: int, slots_at, res: int, group: int, part: int):
        self.p, self.words, self.base, self.res = p, words, slots_at, res
        self.group, self.part = group, part
        self.vregs, self.preds = _Pool(NREG, "vector"), _Pool(NPRED, "predicate")
        self.sreg = {c: S_FREE[i] for i, c in enumerate(p.consts)}
        #: column-vector L1 word -> the register holding it the whole RUN
        self.held: dict = {}
        self.out: list = []
        self.uses: dict = {}
        for name, op, args in p.nodes:
            for a in args:
                if a[0] == "v":
                    self.uses.setdefault(a[1], []).append(name)

    # -- one value's source operand
    def src(self, a, regs):
        if a[0] == "s":
            return (V.SRC_S, self.sreg[a[1]])
        return (V.SRC_V, regs[a])

    def op(self, name, op, args, regs, vd, pr=None):
        srcs = [self.src(a, regs) for a in args]
        if op in UNARY:
            return [_alu(UNARY[op], vd, srcs[:1], fields=("va",))]
        if op in BINARY:
            nm, f = BINARY[op]
            return [_alu(nm, vd, srcs, fields=("va", f))]
        if op == "fma":
            return [_alu("VFMA", vd, srcs)]
        if op in CMP:
            return [_alu(CMP[op], vd, srcs, pr=pr, fields=("va", "vb"))]
        if op == "select":
            (_, pred), a, b = srcs
            return [
                _alu("VMOV", vd, [b], fields=("va",)),
                _alu("VMOV", vd, [a], pr=pred, pm=1, fields=("va",)),
            ]
        raise VgenError(f"no vector lowering for {op!r}")

    # -- one column (rows) or slice (flat) of one step: the wide nodes of `pas`
    def wide(self, pas: int, addr, col_addr, persist: dict, accs: dict, j: int):
        p = self.p
        mine = [
            (n, op, args)
            for n, op, args in p.nodes
            if p.kind[n] == "W" and p.at[n] == pas
        ]
        folds = [
            (n, args[0])
            for n, op, args in p.nodes
            if op in REDUCE and p.at[n] == pas + 1
        ]
        need = {n for n, _, _ in mine if n in p.slot}
        need |= {a[1] for _, a in folds if a[0] == "v"}
        for n, _, args in reversed(mine):
            if n in need:
                need |= {a[1] for a in args if a[0] == "v" and p.kind[a[1]] == "W"}
        mine = [m for m in mine if m[0] in need]
        if not mine and not folds:
            return
        count: dict = {}

        def want(a):
            count[a] = count.get(a, 0) + 1

        def held(a):
            """A column-vector word kept in a register the whole RUN."""
            return a[0] == "col" and col_addr(a[1]) in self.held

        for _, _, args in mine:
            for a in args:
                per_row = a[0] == "v" and p.kind[a[1]] == "P"
                if a[0] != "s" and not per_row and not held(a):
                    want(a)
        for n, _, _ in mine:
            if n in p.slot:
                want(("v", n))
        for _, a in folds:
            want(a)
        regs: dict = {}
        preds: dict = {}
        code, stores = [], []

        def load(a):
            if a in regs:
                return
            r = self.vregs.get()
            if a[0] == "in":
                code.append(Vld(r, stream.AD_L1, addr(a[1])))
            elif a[0] == "col":
                code.append(Vld(r, AD_B, col_addr(a[1])))
            else:
                code.append(Vld(r, stream.AD_L1, addr(p.slot[a[1]])))
            regs[a] = r

        def done(a):
            if a[0] == "s":
                return
            if (a[0] == "v" and p.kind[a[1]] == "P") or held(a):
                regs.pop(a, None)
                return
            count[a] -= 1
            if count[a]:
                return
            if a in preds:
                self.preds.put(preds.pop(a))
            elif a in regs:
                self.vregs.put(regs.pop(a))

        for name, op, args in mine:
            for a in args:
                if a[0] == "v" and p.kind[a[1]] == "P":
                    regs[a] = persist[a[1]]
                elif held(a):
                    regs[a] = self.held[col_addr(a[1])]
                elif a in preds:
                    pass
                elif a[0] in ("in", "col") or (
                    a[0] == "v" and a not in regs and p.at[a[1]] < pas
                ):
                    load(a)
            view = regs | preds
            if op in CMP:
                pr = self.preds.get()
                vd = next(view[a] for a in args if a[0] != "s")
                code += self.op(name, op, args, view, vd, pr=pr)
                preds[("v", name)] = pr
            else:
                # in place over an operand this op reads last
                dying = [
                    a
                    for a in dict.fromkeys(args)
                    if a in regs
                    and a not in preds
                    and not (a[0] == "v" and p.kind[a[1]] == "P")
                    and not held(a)
                    and count[a] == args.count(a)
                ]
                if dying and op != "select":
                    vd = regs.pop(dying[0])
                    count[dying[0]] = 0
                else:
                    vd = self.vregs.get()
                code += self.op(name, op, args, view, vd)
                regs[("v", name)] = vd
            for a in args:
                if count.get(a, 1) or a[0] == "s" or a in preds:
                    done(a)
                elif a[0] == "v" and p.kind[a[1]] == "P":
                    regs.pop(a, None)
            if name in p.slot:
                stores.append((name, vd))
            elif op not in CMP and count.get(("v", name), 0) == 0:
                self.vregs.put(regs.pop(("v", name)))
        for n, a in folds:
            load(a)
            r = regs[a]
            u = j % self.part
            key = (n, u)
            if key not in accs:
                if count[a] == 1:
                    accs[key] = r
                    regs.pop(a)
                    count[a] = 0
                    continue
                accs[key] = self.vregs.get()
                code.append(_alu("VMOV", accs[key], [(V.SRC_V, r)], fields=("va",)))
            else:
                code.append(
                    _combine(REDUCE[_op_of(p.nodes, n)], accs[key], accs[key], r)
                )
            done(a)
        for name, r in stores:
            code.append(Vst(r, stream.AD_L1, addr(p.slot[name])))
            done(("v", name))
        self.out += code

    def finish(self, n: str, accs: dict, persist: dict) -> None:
        """Partials into one, then the butterfly: every lane of a chunk holds
        its row's value."""
        op = REDUCE[_op_of(self.p.nodes, n)]
        live = [accs.pop((n, u)) for u in range(self.part) if (n, u) in accs]
        while len(live) > 1:
            nxt = []
            for i in range(0, len(live) - 1, 2):
                self.out.append(_combine(op, live[i], live[i], live[i + 1]))
                self.vregs.put(live[i + 1])
                nxt.append(live[i])
            if len(live) % 2:
                nxt.append(live[-1])
            live = nxt
        acc = live[0]
        t = self.vregs.get()
        for s in S_ROT:
            self.out.append(Vshuf(t, acc, s))
            self.out.append(_combine(op, acc, acc, t))
        self.vregs.put(t)
        persist[n] = acc

    def per_row(self, pas: int, persist: dict) -> None:
        """Per-row nodes ready for pass `pas` (their reductions are)."""
        p = self.p
        for name, op, args in p.nodes:
            if p.kind[name] != "P" or op in REDUCE or p.at[name] != pas:
                continue
            regs = {a: persist[a[1]] for a in args if a[0] == "v"}
            vd = self.vregs.get()
            self.out += self.op(name, op, args, regs, vd)
            persist[name] = vd

    def release(self, pas: int, persist: dict) -> None:
        """Per-row values no pass after `pas` reads."""
        p = self.p
        for n in list(persist):
            readers = self.uses.get(n, [])
            later = [
                r
                for r in readers
                if (p.kind[r] == "W" and p.at[r] > pas)
                or (p.kind[r] == "P" and p.at[r] > pas + 1)
            ]
            if not later:
                self.vregs.put(persist.pop(n))


def _rows_body(p: Plan, words: int, base: list, res: int, group: int, part: int):
    w = p.geom[0] // 16
    steps = words // w // ROWS
    g = _Gen(p, words, base, res, group, part)
    reds = [n for n, op, _ in p.nodes if op in REDUCE]
    for s0 in range(0, steps, group):
        mine = list(range(s0, min(s0 + group, steps)))
        persist = [dict() for _ in mine]
        for pas in range(p.passes):
            accs = [dict() for _ in mine]
            for j in range(w):
                for k, st in enumerate(mine):
                    g.wide(
                        pas,
                        lambda s, st=st, j=j: base[s] + st * ROWS * w + j,
                        lambda c, j=j: res + c * w + j,
                        persist[k],
                        accs[k],
                        j,
                    )
            for k in range(len(mine)):
                for n in reds:
                    if p.at[n] == pas + 1:
                        g.finish(n, accs[k], persist[k])
                g.per_row(pas + 1, persist[k])
                g.release(pas, persist[k])
        for k in range(len(mine)):
            for r in persist[k].values():
                g.vregs.put(r)
    return g.out


#: Registers a RUN may keep column-vector words in (`_flat_body`'s `hold`).
HOLD = 8


def _hoist_loads(code: list) -> list:
    """Each VLD moved up past what does not touch its register or the L1 words
    it reads, behind the VLDs before it; each run of VLDs then in address
    order, one input's words after another (their registers are distinct: a
    load stops at the last reader of its register)."""
    out: list = []
    for ins in code:
        k = len(out)
        if isinstance(ins, Vld):
            while k and not isinstance(out[k - 1], Vld):
                prev = out[k - 1]
                if ins.vd in vsched._regs_named(prev) or (
                    isinstance(prev, Vst) and (prev.ad, prev.off) == (ins.ad, ins.off)
                ):
                    break
                k -= 1
        out.insert(k, ins)
    i = 0
    while i < len(out):
        j = i
        while j < len(out) and isinstance(out[j], Vld):
            j += 1
        out[i:j] = sorted(out[i:j], key=lambda v: (v.ad, v.off))
        i = j + 1
    return out


def _flat_body(
    p: Plan, words: int, base: list, res: int, hold: int, part: int, group: int = 1
):
    """Slices of 8 words, emitted `group` at a time with the group's loads
    first; with `hold`, each column vector's distinct slice phases are loaded
    once a RUN into registers, when they number <= HOLD."""
    gn = p.geom[0]
    g = _Gen(p, words, base, res, hold, part)
    phases = sorted(
        {
            (s * SLICE) % gn if gn and gn % SLICE == 0 else 0
            for s in range(words // SLICE)
        }
    )
    words_held = [res + c * gn + ph for c in range(p.cols) for ph in phases]
    if hold and p.cols and len(words_held) <= HOLD:
        for at in words_held:
            g.held[at] = g.vregs.get()
            g.out.append(Vld(g.held[at], AD_B, at))
    for s in range(words // SLICE):
        if s % group == 0:
            start = len(g.out)
        phase = (s * SLICE) % gn if gn and gn % SLICE == 0 else 0
        g.wide(
            0,
            lambda slot, s=s: base[slot] + s * SLICE,
            lambda c, phase=phase: res + c * gn + phase,
            {},
            {},
            0,
        )
        if group > 1 and (s % group == group - 1 or s == words // SLICE - 1):
            g.out[start:] = _hoist_loads(g.out[start:])
    return g.out


def col_walk(spec: tuple):
    """`AD_B`'s walk: the column vectors' words in each chunk."""
    p = plan(spec)
    if not p.cols:
        return None
    if p.mode == "rows":
        return ((0, ROWS),)
    gn = p.geom[0]
    if gn % SLICE == 0:
        return ((1, SLICE),)
    if SLICE % gn == 0:
        return ((1, gn), (0, SLICE // gn))
    raise VgenError(f"a tile {gn} sub-tiles wide does not tile a {SLICE}-word slice")


def l1_dims(spec: tuple):
    p = plan(spec)
    return ((p.geom[0] // 16, ROWS),) if p.mode == "rows" else ((1, SLICE),)


def head(p: Plan) -> list:
    code = [Seti(S_VL, V.VLMAX)]
    if any(op in REDUCE for _, op, _ in p.nodes):
        code += [Seti(s, k) for s, k in zip(S_ROT, (8, 4, 2, 1), strict=True)]
    code += [Seti(S_FREE[i], V.e8m15(c)) for i, c in enumerate(p.consts)]
    return code + [Setvl(S_VL), Setmode(V.FLAT)]


def _check_words(p: Plan, words: int) -> None:
    if p.mode == "rows":
        w = p.geom[0] // 16
        if p.geom[0] % 16 or words % (w * ROWS):
            raise VgenError(f"{words} words are not whole 8-row steps of {p.geom[0]}")
    else:
        gn = p.geom[0]
        if words % SLICE or (gn and words % gn):
            raise VgenError(f"{words} words are not whole slices of tile rows")


CONFIGS = ((2, 4), (2, 2), (1, 4), (1, 2), (2, 1), (1, 1))
#: Flat mode's ``(hold, part, group)``. Of configurations `vsched` times
#: alike, the first is kept: slices four at a time, loads first, is the hand
#: binary kernel's order (MEASURED, card_v9_1n: add 262144 93.9k against
#: 102.6k one slice at a time, which the model times within 0.2%).
FLAT_CONFIGS = ((1, 1, 4), (0, 1, 4), (1, 1, 1), (0, 1, 1))
IMEM = 512


def run_cycles(progs, l1) -> float:
    """Modelled cycles of one steady-state RUN: the even image, and the
    compute image when it is `shared`."""
    t = vsched.cycles(progs[1], l1)
    return t + vsched.cycles(progs[5], l1) if len(progs) == 6 else t


@cache
def programs(spec: tuple, words: int, runs: int, install: float = 1.0) -> tuple:
    """``(images, l1 dims, resident words, inputs, setup, walks, slots)`` for
    `stream.stream_ops`: of the register configurations whose images fit
    instruction memory -- two parity images, or one shared compute image when
    two do not fit -- the one cheapest over `runs` RUNs: `vsched`'s cycles a
    RUN, and `install` of the images' install (`stream.INSTALL` a word)."""
    p = plan(spec)
    _check_words(p, words)
    dims = l1_dims(spec)
    resident = resident_words(spec, words)
    n = p.inputs + p.scratch
    walk = col_walk(spec)
    walks = {AD_B: walk} if walk else None
    setup = (Desc(AD_B, 0), Dims(AD_B, walk)) if walk else ()
    l1 = stream.l1_map(dims, words, p.inputs, resident, walks)
    body = _rows_body if p.mode == "rows" else _flat_body
    configs = CONFIGS if p.mode == "rows" else FLAT_CONFIGS
    best, why = None, "no configuration"
    res = stream.resident_at(words, n)
    for shared in (False, True):
        for cfg in configs:
            if p.mode == "rows" and cfg[1] > p.geom[0] // 16:
                continue
            try:
                progs = stream.programs(
                    head(p),
                    lambda s, cfg=cfg: body(p, words, s, res, *cfg),
                    words,
                    dims,
                    inputs=p.inputs,
                    resident=resident,
                    walks=walks,
                    scratch=p.scratch,
                    shared=shared,
                )
            except OutOfRegisters as e:
                why = str(e)
                continue
            size = stream.image_words(progs)
            if size > IMEM:
                why = f"{size} words of images; instruction memory holds {IMEM}"
                continue
            t = runs * run_cycles(progs, l1) + install * stream.INSTALL * size
            if best is None or t < best[0]:
                best = (t, progs)
        if best is not None:
            break
    if best is None:
        raise VgenError(f"no configuration fits: {why}")
    return tuple(best[1]), dims, resident, p.inputs, setup, walks, n


__all__ = ["VgenError", "plan", "programs", "resident_words", "slots"]
