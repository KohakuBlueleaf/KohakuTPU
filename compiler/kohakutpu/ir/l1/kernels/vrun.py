"""Vector-core RUN images over a core's local state, in the clusters' drained
sub-tile layout: the vector phases of a scan.

The spec ``("vr", gm, state, runs)`` and its layout: docs/projects/kohakutpu/
ir/l3.md §5. A row reduction folds the columns into two partials, reduces each
lane group of four (rotate 1 and 2: lane 4i exact) and broadcasts lane 4i to
its group with three predicated rotates; a quantiser-order drain is
`attention`'s 4x4 granule transpose. Per-row state writes land at the RUN's
end, after every read.
"""

from functools import cache

from kohakutpu.hw import vector as V
from kohakutpu.ir.l1 import vsched
from kohakutpu.ir.l1.kernels.stream import walk_offsets
from kohakutpu.ir.l1.kernels.vgen import (
    BINARY,
    CMP,
    REDUCE,
    UNARY,
    OutOfRegisters,
    VgenError,
    _alu,
    _combine,
    _fold,
    _Pool,
)
from kohakutpu.ir.l1.vector import (
    Bar,
    Desc,
    Dims,
    Halt,
    Image,
    Run,
    Seti,
    Setmode,
    Setvl,
    Vdrain,
    Vfill,
    Vld,
    Vshuf,
    Vst,
)

# Descriptors: the fill, the drain, per-row state, the index words' broadcast,
# their fill, then one column walk a tile width.
AD_IN, AD_OUT, AD_ST, AD_IX, AD_IF = 0, 1, 2, 3, 4
AD_W = (5, 6, 7)
S_VL = 0
IMEM = 512


def widths(state, runs) -> tuple:
    """The tile widths the RUNs walk: the tile state's and the fills'."""
    ws = {w for _, k, w, _ in state if k == "W"} | {r[1] for r in runs if r[1]}
    if len(ws) > len(AD_W):
        raise VgenError(f"{len(ws)} tile widths; {len(AD_W)} descriptors walk them")
    return tuple(sorted(ws))


def walks(gm: int, state, runs) -> dict:
    """Every load/store descriptor's walk, by descriptor."""
    out = {AD_W[k]: ((w, gm),) for k, w in enumerate(widths(state, runs))}
    out[AD_ST] = ((1, gm),)
    out[AD_IX] = ((0, gm),)
    return out


class _Run:
    def __init__(self, gm, state, run, ad_w) -> None:
        self.gm, self.ad_w = gm, ad_w
        self.state = {n: (k, w, at) for n, k, w, at in state}
        self.name, self.fill, nodes, self.outs = run
        self.vregs = _Pool(16, "vector")
        self.code: list = []
        self.sreg: list = []
        self.pred_word = None
        self.analyse(nodes)

    # ------------------------------------------------------------ analysis
    def kind_of(self, a) -> str:
        match a:
            case ("in",):
                return "W"
            case ("st", n):
                return self.state[n][0]
            case ("v", n):
                return self.kind[n]
        return "S"

    def width_of(self, a) -> int:
        match a:
            case ("in",):
                return self.fill
            case ("st", n):
                return self.state[n][1]
            case ("v", n):
                return self.width[n]
        return 0

    def analyse(self, nodes) -> None:
        self.kind, self.width, value, folded = {}, {}, {}, []
        for name, op, args in nodes:
            for a in args:
                if a == ("in",) and not self.fill:
                    raise VgenError(f"run {self.name} reads an input it is not filled")
                if a[0] == "st" and a[1] not in self.state:
                    raise VgenError(f"{a[1]} is not in the core's state")
            args = tuple(
                ("s", value[a[1]]) if a[0] == "v" and self.kind[a[1]] == "S" else a
                for a in args
            )
            ks = [self.kind_of(a) for a in args]
            if op in CMP or op == "select":
                raise VgenError(f"{name} = {op}: the predicates hold lane groups here")
            if op in REDUCE:
                if ks != ["W"]:
                    raise VgenError(f"{name} = {op} of a value not a tile")
                self.kind[name] = "P"
            elif all(k == "S" for k in ks):
                self.kind[name], value[name] = "S", _fold(op, [a[1] for a in args])
                continue
            elif "W" in ks:
                ws = {
                    self.width_of(a) for a, k in zip(args, ks, strict=True) if k == "W"
                }
                if len(ws) != 1:
                    raise VgenError(f"{name} = {op} of tiles {sorted(ws)} wide")
                self.kind[name], self.width[name] = "W", ws.pop()
            else:
                self.kind[name] = "P"
            if (
                op not in UNARY
                and op not in BINARY
                and op not in REDUCE
                and op != "fma"
            ):
                raise VgenError(f"no vector lowering for {op!r}")
            folded.append((name, op, args))
        self.nodes = tuple(folded)
        self.at = {}
        for name, op, args in folded:
            ready = [self.at[a[1]] for a in args if a[0] == "v"]
            self.at[name] = max(ready, default=0) + (1 if op in REDUCE else 0)
        self.passes = max(self.at.values(), default=0) + 1
        self.st_w, self.st_p, self.drain = {}, {}, None
        for o in self.outs:
            if o == ("ix",):
                continue
            if o[0] == "drain":
                if self.drain is not None:
                    raise VgenError(f"run {self.name} drains two tiles")
                self.drain = o
                if o[2] == "quant" and self.width[o[1][1]] % 8:
                    raise VgenError("a quantiser-order tile is whole 32-column blocks")
            elif o[1][0] == "v":
                n, target = o[1][1], o[2]
                tk = self.state[target][0]
                if self.kind[n] != tk:
                    raise VgenError(f"{target} is {tk}; {n} is {self.kind[n]}")
                (self.st_w if tk == "W" else self.st_p)[n] = target
                late = [
                    m
                    for m, _, args in folded
                    if tk == "W" and ("st", target) in args and self.at[m] > self.at[n]
                ]
                if late:
                    raise VgenError(f"{target} is read after its update, by {late}")
        for m, op, args in folded:
            for a in args:
                p = self.at[m] - (1 if op in REDUCE else 0)
                if a[0] == "v" and self.kind[a[1]] == "W" and p > self.at[a[1]]:
                    raise VgenError(f"{a[1]} is read a pass after its own")

    # -------------------------------------------------------------- emission
    def sr(self, key) -> tuple:
        """A placeholder S register: a constant, a rotation or a compare value."""
        if key not in self.sreg:
            self.sreg.append(key)
        return key

    def predicates(self, word: int) -> list:
        """P1..P3 = (index word `word` == 1, 2, 3), unless they hold it."""
        if self.pred_word == word:
            return []
        self.pred_word = word
        t = self.vregs.get()
        out = [Vld(t, AD_IX, self.state["$ix"][2] + word)]
        out += [
            _alu(
                "VCMPEQ",
                t,
                [(V.SRC_V, t), (V.SRC_S, self.sr(("k", k)))],
                pr=k,
                fields=("va", "vb"),
            )
            for k in (1, 2, 3)
        ]
        self.vregs.put(t)
        return out

    def alu(self, op, args, regs, vd) -> list:
        srcs = [
            (V.SRC_S, self.sr(("c", a[1]))) if a[0] == "s" else (V.SRC_V, regs[a])
            for a in args
        ]
        if op in UNARY:
            return [_alu(UNARY[op], vd, srcs[:1], fields=("va",))]
        if op in BINARY:
            nm, f = BINARY[op]
            return [_alu(nm, vd, srcs, fields=("va", f))]
        return [_alu("VFMA", vd, srcs)]

    def col(self, a, c: int) -> tuple:
        """``(descriptor, word)`` of tile value `a`'s column `c`."""
        if a[0] == "in":
            return self.ad_w[self.fill], self.state["$in"][2] + c
        _, w, at = self.state[a[1]]
        return self.ad_w[w], at + c

    def emit(self) -> list:
        code = self.code
        if self.fill:
            code += [Vfill(AD_IN, self.state["$in"][2]), Bar()]
        if ("ix",) in self.outs:
            code += [Vfill(AD_IF, self.state["$ix"][2]), Bar()]
        for o in self.outs:
            if o[0] == "st" and o[1][0] == "s":
                code += self.constant(o[2], o[1][1])
        persist: dict = {}
        self.per_row(0, persist)
        for pas in range(self.passes):
            accs: dict = {}
            mine = [
                m for m in self.nodes if self.kind[m[0]] == "W" and self.at[m[0]] == pas
            ]
            folds = [
                (n, args[0])
                for n, op, args in self.nodes
                if op in REDUCE and self.at[n] == pas + 1
            ]
            width = max(
                [self.width[m[0]] for m in mine] + [self.width_of(a) for _, a in folds],
                default=0,
            )
            held: list = []
            for c in range(width):
                self.column(c, mine, folds, persist, accs, held)
            for n, op, _ in self.nodes:
                if op in REDUCE and self.at[n] == pas + 1:
                    self.finish(n, accs, persist)
            self.per_row(pas + 1, persist)
            self.release(pas, persist)
        code += [Vst(persist[n], AD_ST, self.state[t][2]) for n, t in self.st_p.items()]
        if self.drain is not None:
            code.append(Vdrain(AD_OUT, self.state["$out"][2]))
        return code

    def per_row(self, ready: int, persist: dict) -> None:
        """The per-row ops whose reductions are done by pass `ready`."""
        for name, op, args in self.nodes:
            if self.kind[name] == "P" and op not in REDUCE and self.at[name] == ready:
                regs = {
                    a: persist[a[1]] if a[0] == "v" else self.load_p(a, persist)
                    for a in args
                    if a[0] in ("v", "st")
                }
                vd = self.vregs.get()
                self.code += self.alu(op, args, regs, vd)
                persist[name] = vd

    def constant(self, target, value) -> list:
        k, w, at = self.state[target]
        r = self.vregs.get()
        out = [_alu("VMOV", r, [(V.SRC_S, self.sr(("c", value)))], fields=("va",))]
        if k == "P":
            out.append(Vst(r, AD_ST, at))
        else:
            out += [Vst(r, self.ad_w[w], at + c) for c in range(w)]
        self.vregs.put(r)
        return out

    def load_p(self, a, persist) -> int:
        key = ("$st", a[1])
        if key not in persist:
            r = self.vregs.get()
            self.code.append(Vld(r, AD_ST, self.state[a[1]][2]))
            persist[key] = r
        return persist[key]

    def column(self, c, mine, folds, persist, accs, held) -> None:
        mine = [m for m in mine if c < self.width[m[0]]]
        folds = [f for f in folds if c < self.width_of(f[1])]
        drained = self.drain[1] if self.drain is not None else None
        count: dict = {}
        for _, _, args in mine:
            for a in args:
                if self.kind_of(a) == "W":
                    count[a] = count.get(a, 0) + 1
        for n, _, _ in mine:
            if n in self.st_w or ("v", n) == drained:
                count[("v", n)] = count.get(("v", n), 0) + 1
        for _, a in folds:
            count[a] = count.get(a, 0) + 1
        regs: dict = {}
        stores = []

        def load(a):
            if a not in regs:
                regs[a] = self.vregs.get()
                self.code.append(Vld(regs[a], *self.col(a, c)))

        def done(a):
            count[a] -= 1
            if count[a] == 0 and a in regs:
                self.vregs.put(regs.pop(a))

        for name, op, args in mine:
            for a in args:
                if self.kind_of(a) == "P":
                    regs[a] = persist[a[1]] if a[0] == "v" else self.load_p(a, persist)
                elif self.kind_of(a) == "W" and a[0] != "v":
                    load(a)
            view = dict(regs)
            dying = [
                a
                for a in dict.fromkeys(args)
                if a in regs and self.kind_of(a) == "W" and count[a] == args.count(a)
            ]
            if dying:
                vd = regs.pop(dying[0])
                count[dying[0]] = 0
            else:
                vd = self.vregs.get()
            self.code += self.alu(op, args, view, vd)
            regs[("v", name)] = vd
            for a in args:
                if self.kind_of(a) == "P":
                    regs.pop(a, None)
                elif self.kind_of(a) == "W" and count.get(a):
                    done(a)
            if name in self.st_w:
                stores.append(
                    (("v", name), Vst(vd, *self.col(("st", self.st_w[name]), c)))
                )
            if ("v", name) == drained and self.drain[2] == "tiles":
                ad = self.ad_w[self.width[name]]
                stores.append((("v", name), Vst(vd, ad, self.state["$out"][2] + c)))
        for n, a in folds:
            load(a)
            key = (n, c % 2)
            if key not in accs:
                if count[a] == 1:
                    accs[key] = regs.pop(a)
                    count[a] = 0
                    continue
                accs[key] = self.vregs.get()
                self.code.append(
                    _alu("VMOV", accs[key], [(V.SRC_V, regs[a])], fields=("va",))
                )
            else:
                self.code.append(
                    _combine(self.reduce_op(n), accs[key], accs[key], regs[a])
                )
            done(a)
        for a, st in stores:
            self.code.append(st)
            done(a)
        if drained is not None and self.drain[2] == "quant" and drained in regs:
            held.append(regs.pop(drained))
            if len(held) == 4:
                self.transpose(c // 4, held, self.width[drained[1]])
                held.clear()
        for r in regs.values():
            if r not in persist.values():
                self.vregs.put(r)

    def reduce_op(self, n) -> str:
        return REDUCE[next(op for m, op, _ in self.nodes if m == n)]

    def transpose(self, h: int, xs: list, width: int) -> None:
        """Four columns (16 keys) to the quantiser's order: output `i` takes
        granule `i` of input `j` into granule `j`, a rotation of 4(i-j) lanes."""
        self.code += self.predicates(0)
        at = self.state["$out"][2]
        for i in range(4):
            out = self.vregs.get()
            for j in range(4):
                lanes = 4 * ((i - j) % 4)
                kw = {"pr": j, "pm": 1} if j else {}
                if lanes:
                    self.code.append(Vshuf(out, xs[j], self.sr(("r", lanes)), **kw))
                else:
                    self.code.append(
                        _alu("VMOV", out, [(V.SRC_V, xs[j])], fields=("va",), **kw)
                    )
            self.code.append(
                Vst(out, self.ad_w[width], at + ((h // 2) * 4 + i) * 2 + h % 2)
            )
            self.vregs.put(out)
        for x in xs:
            self.vregs.put(x)

    def finish(self, n, accs, persist) -> None:
        op = self.reduce_op(n)
        live = [accs.pop((n, u)) for u in (0, 1) if (n, u) in accs]
        if len(live) == 2:
            self.code.append(_combine(op, live[0], live[0], live[1]))
            self.vregs.put(live[1])
        acc = live[0]
        self.code += self.predicates(1)
        t = self.vregs.get()
        for r in (1, 2):
            self.code += [Vshuf(t, acc, self.sr(("r", r))), _combine(op, acc, acc, t)]
        self.vregs.put(t)
        bc = self.vregs.get()
        self.code.append(_alu("VMOV", bc, [(V.SRC_V, acc)], fields=("va",)))
        self.code += [
            Vshuf(bc, acc, self.sr(("r", 16 - k)), pr=k, pm=1) for k in (1, 2, 3)
        ]
        self.vregs.put(acc)
        persist[n] = bc

    def release(self, pas, persist) -> None:
        """Per-row values no later pass reads, but those the state takes."""
        for key in list(persist):
            n = key[1] if isinstance(key, tuple) else key
            if not isinstance(key, tuple) and n in self.st_p:
                continue
            arg = ("st", n) if isinstance(key, tuple) else ("v", n)
            later = [
                m
                for m, _, args in self.nodes
                if arg in args
                and (
                    (self.kind[m] == "W" and self.at[m] > pas)
                    or (self.kind[m] == "P" and self.at[m] > pas + 1)
                )
            ]
            if not later:
                self.vregs.put(persist.pop(key))


def _resolve(code: list, keys: list, gm: int) -> list:
    """The head that sets the run's S registers, and the code naming them."""
    if len(keys) > 15:
        raise VgenError(f"{len(keys)} constants and rotations; 15 S registers")
    names = {k: s for s, k in enumerate(keys, start=1)}
    head = [Seti(S_VL, 16 * gm)]
    for key, s in names.items():
        match key:
            case ("c", v):
                head.append(Seti(s, V.e8m15(v)))
            case ("r", n):
                head.append(Seti(s, n))
            case ("k", k):
                head.append(Seti(s, V.e8m15(float(k))))
    head += [Setvl(S_VL), Setmode(V.FLAT)]

    def fix(inst):
        if isinstance(inst, Vshuf) and isinstance(inst.srot, tuple):
            return Vshuf(inst.vd, inst.va, names[inst.srot], inst.pr, inst.pm)
        kw = {
            f: names[getattr(inst, f)]
            for f in ("va", "vb", "vc")
            if isinstance(getattr(inst, f, None), tuple)
        }
        return type(inst)(**{**inst.__dict__, **kw}) if kw else inst

    return head, [fix(i) for i in code]


@cache
def images(spec: tuple) -> tuple:
    """``((name, image, pc, cycles), ...)``: every RUN's image, its place in
    instruction memory and its modelled cycles."""
    tag, gm, state, runs = spec
    if tag != "vr" or not 1 <= gm <= 8:
        raise VgenError(f"not a run program of 1..8 bands: {spec[:2]}")
    ws = widths(state, runs)
    ad_w = {w: AD_W[k] for k, w in enumerate(ws)}
    l1_walks = {ad: (0, walk_offsets(d)) for ad, d in walks(gm, state, runs).items()}
    out, pc = [], 0
    for run in runs:
        r = _Run(gm, state, run, ad_w)
        try:
            code = r.emit()
        except OutOfRegisters as e:
            raise VgenError(f"run {run[0]}: {e}") from e
        head, body = _resolve(code, r.sreg, gm)
        drain = r.width[r.drain[1][1]] if r.drain is not None else 0
        l1 = vsched.L1Map(
            walks=l1_walks,
            fills={AD_IN: gm * max(run[1], 1), AD_OUT: gm * max(drain, 1), AD_IF: 2},
        )
        img = tuple(head + vsched.schedule(body, l1) + [Halt()])
        out.append((run[0], img, pc, vsched.cycles(img, l1), drain))
        pc += sum(len(i.words()) for i in img)
    if pc > IMEM:
        raise VgenError(
            f"the run images take {pc} of instruction memory's {IMEM} words"
        )
    return tuple(out)


def setup_ops(spec: tuple, ix_at: int) -> list:
    """The images and the descriptors every RUN shares, and the index words
    filled: once a core."""
    _, gm, state, runs = spec
    ops = [Image(img, pc) for _, img, pc, _, _ in images(spec)]
    for ad, dims in walks(gm, state, runs).items():
        ops += [Desc(ad, 0), Dims(ad, dims)]
    return ops + [Desc(AD_IF, ix_at), Dims(AD_IF, ((V.WORD_BYTES, 2),))]


def run_ops(spec: tuple, name: str, in_at=None, out_at=None) -> list:
    """One RUN: its input's and its drain's walks, then the RUN."""
    _, gm, _, runs = spec
    fill = next(r[1] for r in runs if r[0] == name)
    _, _, pc, _, drain = next(i for i in images(spec) if i[0] == name)
    ops = []
    if fill:
        ops += [Desc(AD_IN, in_at), Dims(AD_IN, ((V.WORD_BYTES, gm * fill),))]
    if drain:
        ops += [Desc(AD_OUT, out_at), Dims(AD_OUT, ((V.WORD_BYTES, gm * drain),))]
    return ops + [Run(pc)]


def cycles(spec: tuple, name: str) -> float:
    return next(i[3] for i in images(spec) if i[0] == name)


__all__ = ["VgenError", "cycles", "images", "run_ops", "setup_ops"]
