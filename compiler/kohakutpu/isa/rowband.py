"""A band that folds rows, as a program with no VRED: rows across chunks.

A step covers `rb` rows (VL = 16 * rb, one row per 16-lane chunk). Register
`j` of a step holds word-column `j` of those rows -- a VLD through the L1
descriptor walks `rb` words `w` apart -- so every elementwise op keeps a row in
its own chunk. A fold is elementwise across the `w` word-columns into one
accumulator, then four rotate-and-combine steps (`VSHUF` by 8, 4, 2, 1 inside
each chunk) leave the row's result in all sixteen lanes of its chunk: the
folded value is already broadcast, with no mode switch and no `VRED`, and the
row may be any multiple of 16 wide.

A value is per-WORD (it differs across a row), per-ROW (one value a row, in
every lane of its chunk) or a constant. The chains become one graph with
equal values shared; a fold of a per-word value ends a PHASE. Phase `p` walks
the word-columns once, computing every per-word value its folds and stores
need -- reloading inputs from L1 and recomputing earlier per-word values
rather than holding `w` of each -- and folds them; per-row values are computed
once a step between phases.
"""

from dataclasses import dataclass

from kohakutpu.hw import vector as V
from kohakutpu.hw.ops import OpKind
from kohakutpu.hw.veckernels import S_VL, WORD_ELEMS, imem_flits, require_l1
from kohakutpu.isa.vecemit import (
    AD_L1,
    AGU_WALK,
    ALU_GAP,
    CONST_SREGS,
    DESCRIPTORS,
    FWD,
    IMEM_WORDS,
    KONST,
    KREG,
    LD_GAP,
    OUT_REG,
    REDUCE,
    REGISTERS,
    ST_GAP,
    Select,
    VecEmitError,
    _dims,
    _spans,
    fuse_compares,
    lower_word,
)

#: Rotation amounts of the in-chunk butterfly, and the scalar registers holding
#: them (S1..S3 are free here: nothing VREDs).
ROTATIONS = (8, 4, 2, 1)
S_ROT = (1, 2, 3, 4)
#: An ALU result is written back ALAT cycles after issue; `VSHUF` waits for the
#: lanes to go quiet (`vec_core.v` `ls_gate`), so one after an ALU word pays it.
ALAT = 14
#: Predicate registers: a fused compare-and-select holds one per member.
PREDICATES = 4

#: The combine op per fold kind, across word-columns and across lanes.
COMBINE = {
    OpKind.SUM: OpKind.ADD,
    OpKind.SUMSQ: OpKind.ADD,
    OpKind.RMAX: OpKind.MAX,
    OpKind.RMIN: OpKind.MIN,
}

WORD, ROW, CONST = "word", "row", "const"


@dataclass(frozen=True)
class Node:
    """One value of the graph: an input, a constant, an op or a fold."""

    what: str  # "in", "k", "op", "fold"
    op: object = None  # OpKind or Select
    args: tuple = ()
    index: int = 0  # the input or constant number


class RowBandKernel:
    """Chains that fold rows as ONE vector program, `rb` rows a step.

    `cols` is the row width (a multiple of 16); a RUN is `halves` batches of
    `chunks` steps, half h+1 filling while half h computes. Same interface as
    :class:`~kohakutpu.isa.vecemit.BandKernel` for `_fit` and the backend.

    Raises :class:`VecEmitError` for a band over the descriptors, the image, L1
    or the register file, and for a chain that reads what has not run.
    """

    def __init__(
        self,
        chains,
        nin: int,
        cols: int,
        rb: int = V.VLMAX // V.LANES,
        chunks: int = 1,
        consts=(),
        walks=None,
        groups=None,
        halves: int = 1,
        uniform=(),
    ) -> None:
        """`uniform` are the inputs whose every row holds one value, read once a step."""
        self.uniform = frozenset(uniform)
        if cols % WORD_ELEMS or cols <= 0:
            raise VecEmitError(
                f"a {cols}-wide row is not whole {WORD_ELEMS}-element words"
            )
        if rb not in (1, 2, 4, 8):
            raise VecEmitError(f"{rb} rows a step; a register holds 1, 2, 4 or 8")
        self.chains, self.nin, self.cols, self.rb = list(chains), nin, cols, rb
        self.chunks, self.halves = chunks, halves
        self.w = cols // WORD_ELEMS
        self.vl = rb * V.LANES
        self.consts = [float(c) for c in consts]
        self.walks = list(walks) if walks else [None] * nin
        if len(self.walks) != nin:
            raise VecEmitError(
                f"a band reading {nin} operands got {len(self.walks)} walks"
            )
        if nin >= OUT_REG:
            raise VecEmitError(
                f"a band reads {nin} operands; source {OUT_REG} is the running result"
            )
        self.batch = chunks * rb * cols
        self.bw = self.batch // WORD_ELEMS
        self.step_words = rb * self.w
        self.nout = sum(1 for c in self.chains if c.store)
        for i in range(nin):
            self._fill_dims(i)
        self.spans = _spans(groups, self.nout)
        # Never in place: a later phase reloads an input a store would have
        # overwritten.
        self.inplace = False
        self.slots = nin + self.nout
        per = nin + len(self.spans)
        if 1 + halves * per > DESCRIPTORS:
            raise VecEmitError(
                f"a band reading {nin} operands and draining {len(self.spans)} "
                f"regions over {halves} halves needs {1 + halves * per} "
                f"descriptors, have {DESCRIPTORS}"
            )
        require_l1(f"row band x{chunks}x{halves}", halves * self.slots * self.bw)
        if self.bw > AGU_WALK:
            raise VecEmitError(f"a {self.bw}-word fill exceeds the {AGU_WALK} limit")
        self.ad_fill = [[1 + h * per + i for i in range(nin)] for h in range(halves)]
        self.ad_drain = [
            [1 + h * per + nin + g for g in range(len(self.spans))]
            for h in range(halves)
        ]

        self._graph()
        self._registers_for_consts()
        self._plan()

        pre = [V.vseti(S_VL), self.vl]
        for s, n in zip(S_ROT, ROTATIONS, strict=True):
            pre += [V.vseti(s), n]
        for value, reg in self.sconst.items():
            pre += [V.vseti(reg), V.e8m15(value)]
        pre += [V.vsetvl(S_VL), V.vsetmode(V.FLAT)]
        body = self._fills(0) + [V.vbar()]
        for h in range(halves):
            if h + 1 < halves:
                body += self._fills(h + 1)
            body += self._half(h)
            body += self._drains(h)
            if h + 1 < halves:
                body.append(V.vbar())
        self.image = pre + body + [V.vhalt()]
        if len(self.image) > IMEM_WORDS:
            raise VecEmitError(
                f"a {len(self.image)}-word image over {IMEM_WORDS} instruction "
                f"words; take fewer steps a RUN"
            )

    # ------------------------------------------------------------------ graph
    def _graph(self) -> None:
        """The chains as one graph, equal values shared, each value's shape and phase."""
        self.nodes: list[Node] = []
        self.index: dict[Node, int] = {}

        def make(node: Node) -> int:
            got = self.index.get(node)
            if got is None:
                got = self.index[node] = len(self.nodes)
                self.nodes.append(node)
            return got

        konst = list(self.consts)

        def const(value: float) -> int:
            if value not in konst:
                konst.append(value)
            return make(Node("k", index=konst.index(value)))

        results: list[int] = []
        self.final: list[int] = []
        self.shape: dict[int, str] = {}
        self.level: dict[int, int] = {}

        def note(i: int) -> int:
            n = self.nodes[i]
            if i in self.shape:
                return i
            if n.what == "in":
                self.shape[i] = ROW if n.index in self.uniform else WORD
                self.level[i] = 0
            elif n.what == "k":
                self.shape[i], self.level[i] = CONST, 0
            elif n.what == "fold":
                (a,) = n.args
                self.shape[i] = ROW
                self.level[i] = self.level[a] + 1
            else:
                shapes = {self.shape[a] for a in n.args}
                self.shape[i] = WORD if WORD in shapes else ROW
                self.level[i] = max(self.level[a] for a in n.args)
            return i

        for k, chain in enumerate(self.chains):
            run = None
            for kind, srcs in fuse_compares(list(chain.ops)):
                args = []
                for s in srcs:
                    if s == OUT_REG:
                        if run is None:
                            raise VecEmitError(
                                f"chain {k} reads the running result first"
                            )
                        args.append(run)
                    elif s >= KONST:
                        args.append(note(make(Node("k", index=s - KONST))))
                    elif s >= FWD:
                        if s - FWD >= k:
                            raise VecEmitError(
                                f"chain {k} forwards chain {s - FWD}, which has not run"
                            )
                        args.append(results[s - FWD])
                    elif s < self.nin:
                        args.append(note(make(Node("in", index=s))))
                    else:
                        raise VecEmitError(
                            f"chain {k} reads source {s}, not an operand"
                        )
                if kind in REDUCE:
                    (a,) = args[:1]
                    if self.shape[a] == WORD:
                        run = note(make(Node("fold", op=kind, args=(a,))))
                    else:
                        run = self._fold_uniform(kind, a, const, make, note)
                elif kind is OpKind.DIV and self.shape[args[1]] != WORD:
                    # DIV is VINV then VMUL; a per-row divisor's VINV runs once a step.
                    inv = note(make(Node("op", op=OpKind.RECIP, args=(args[1],))))
                    run = note(make(Node("op", op=OpKind.MUL, args=(args[0], inv))))
                else:
                    run = note(make(Node("op", op=kind, args=tuple(args))))
            results.append(run)
            if chain.store:
                self.final.append(run)
        self.consts = konst

    @classmethod
    def stored_shapes(cls, chains, nin: int, uniform=(), consts=()) -> list[str]:
        """Each stored chain's shape -- "word", "row" or "const" -- and nothing emitted.

        What the compiler asks to learn which temps every writer leaves row-uniform.
        """
        made = cls.__new__(cls)
        made.chains, made.nin, made.cols = list(chains), nin, WORD_ELEMS
        made.consts, made.uniform = [float(c) for c in consts], frozenset(uniform)
        made._graph()
        return [made.shape[i] for i in made.final]

    def _fold_uniform(self, kind, a: int, const, make, note) -> int:
        """A fold of a value that is the same across the row, as plain ops."""
        if kind in (OpKind.RMAX, OpKind.RMIN):
            return a
        base = a
        if kind is OpKind.SUMSQ:
            base = note(make(Node("op", op=OpKind.MUL, args=(a, a))))
        cols = note(const(float(self.cols)))
        return note(make(Node("op", op=OpKind.MUL, args=(base, cols))))

    def _registers_for_consts(self) -> None:
        free = [s for s in CONST_SREGS if s not in S_ROT]
        distinct = [v for v in dict.fromkeys(self.consts) if v not in KREG]
        if len(distinct) > len(free):
            raise VecEmitError(
                f"{len(distinct)} distinct constants and {len(free)} scalar "
                f"registers to hold them; split the band"
            )
        self.sconst = dict(zip(distinct, free, strict=False))

    # ------------------------------------------------------------- schedule
    def _plan(self) -> None:
        """Per phase: the per-word program one member runs, and the register file.

        A MEMBER is one (step, word-column); members run `m` at a time, op-major,
        on disjoint registers. Every per-row value holds a register per step of
        a group.
        """
        word_final = [i for i in self.final if self.shape[i] == WORD]
        folds = [i for i, n in enumerate(self.nodes) if n.what == "fold"]
        self.phases = 1 + max(
            [self.level[i] for i in word_final]
            + [self.level[i] - 1 for i in folds]
            + [0]
        )
        self.rows = [
            i
            for i, n in enumerate(self.nodes)
            if self.shape.get(i) == ROW and n.what in ("in", "op", "fold")
        ]
        self.templates = [self._template(p) for p in range(self.phases)]
        selects = any(isinstance(n.op, Select) for n in self.nodes)
        live = max(t["live"] for t in self.templates)
        g_cap = max(1, min(self.chunks, 4 // self.w)) if self.w < 4 else 1
        for group in range(g_cap, 0, -1):
            spare = REGISTERS - group * len(self.rows)
            m = spare // max(1, live)
            if m >= 1:
                break
        else:
            raise VecEmitError(
                f"{len(self.rows)} per-row values and {live} live per-word values a "
                f"member; the file has {REGISTERS}; split the band"
            )
        self.group = group
        self.members = min(m, group * self.w, PREDICATES if selects else 8)
        self.r_row = [
            {i: g * len(self.rows) + n for n, i in enumerate(self.rows)}
            for g in range(group)
        ]
        base = group * len(self.rows)
        self.r_member = [
            [base + mm * live + s for s in range(live)] for mm in range(self.members)
        ]
        if base + self.members * live > REGISTERS:
            raise VecEmitError("register allocation overflowed the file")

    def _template(self, p: int) -> dict:
        """Phase `p`'s per-member program: ``(op, ...)`` entries over slot numbers."""
        targets = [
            (i, "fold")
            for i, n in enumerate(self.nodes)
            if n.what == "fold" and self.level[i] == p + 1
        ]
        stores = [
            (t, i)
            for t, i in enumerate(self.final)
            if self.shape[i] == WORD and self.level[i] == p
        ]
        order: list[int] = []
        seen: set = set()

        def visit(i: int) -> None:
            if i in seen or self.shape[i] != WORD:
                return
            seen.add(i)
            for a in self.nodes[i].args:
                visit(a)
            order.append(i)

        for i, _ in targets:
            visit(self.nodes[i].args[0])
        for _, i in stores:
            visit(i)
        prog: list[tuple] = []
        for i in order:
            prog.append(("val", i))
            prog += [("acc", f, i) for f, _ in targets if self.nodes[f].args[0] == i]
            prog += [("st", t, i) for t, s in stores if s == i]
        # Each per-word value's last reader; its slot is free from there on.
        last: dict[int, int] = {}
        for at, item in enumerate(prog):
            reads = self.nodes[item[1]].args if item[0] == "val" else (item[2],)
            for a in reads:
                last[a] = at
        slot: dict[int, int] = {}
        free: list[int] = []
        top = 0
        for at, item in enumerate(prog):
            if item[0] == "val":
                # Arguments read for the last time here give their slot to the result.
                for a in set(self.nodes[item[1]].args):
                    if a in slot and last.get(a) == at:
                        free.append(slot[a])
                if free:
                    slot[item[1]] = free.pop()
                else:
                    slot[item[1]], top = top, top + 1
                if item[1] not in last:
                    free.append(slot[item[1]])
            elif last.get(item[2]) == at:
                free.append(slot[item[2]])
        need_tmp = any(self.nodes[i].op is OpKind.DIV for i in order)
        live = top + int(need_tmp)
        return {
            "prog": prog,
            "slot": slot,
            "live": max(1, live),
            "tmp": top if need_tmp else None,
        }

    # ------------------------------------------------------------- emission
    def _l1(self, h: int, slot: int) -> int:
        return (h * self.slots + slot) * self.bw

    def _fills(self, h: int) -> list[int]:
        return [V.vfill(self.ad_fill[h][i], self._l1(h, i)) for i in range(self.nin)]

    def _drains(self, h: int) -> list[int]:
        return [
            V.vdrain(self.ad_drain[h][g], self._l1(h, self.nin + at))
            for g, (at, _) in enumerate(self.spans)
        ]

    def _operand(self, i: int, g: int, regs: dict) -> tuple[int, int]:
        """``(selector, register)`` of value `i` for a member of step `g`."""
        shape = self.shape[i]
        if shape == CONST:
            value = self.consts[self.nodes[i].index]
            if value in KREG:
                return V.SRC_K, KREG[value]
            return V.SRC_S, self.sconst[value]
        if shape == ROW:
            return V.SRC_V, self.r_row[g][i]
        return V.SRC_V, regs[i]

    def _half(self, h: int) -> list[int]:
        out: list[int] = []
        for s0 in range(0, self.chunks, self.group):
            steps = list(range(s0, min(s0 + self.group, self.chunks)))
            for p in range(self.phases):
                out += self._rows_at(p, steps, h)
                out += self._phase(p, steps, h)
                out += self._butterflies(p, steps)
            out += self._rows_at(self.phases, steps, h)
        return out

    def _rows_at(self, p: int, steps: list, h: int) -> list[int]:
        """Per-row ops whose inputs are ready at the start of phase `p`, and their stores."""
        out: list[int] = []
        if p == 0:
            # A row-uniform input: word 0 of each row, which every word repeats.
            for i in self.rows:
                n = self.nodes[i]
                if n.what != "in":
                    continue
                for g, s in enumerate(steps):
                    at = self._l1(h, n.index) + s * self.step_words
                    out.append(V.vld(self.r_row[g][i], AD_L1, at))
        made = [
            i for i in self.rows if self.nodes[i].what == "op" and self.level[i] == p
        ]
        tmp = self.r_member[0][0]
        for i in made:
            n = self.nodes[i]
            words = []
            for g in range(len(steps)):
                ops = [self._operand(a, g, {}) for a in n.args]
                words.append(
                    lower_word(n.op, ops, self.r_row[g][i], tmp, pr=g % PREDICATES)
                )
            for w in range(len(words[0])):
                out += [step[w] for step in words]
        for t, i in enumerate(self.final):
            if self.shape[i] != ROW or self._ready(i) != p:
                continue
            for g, s in enumerate(steps):
                for j in range(self.w):
                    at = self._l1(h, self.nin + t) + s * self.step_words + j
                    out.append(V.vst(self.r_row[g][i], AD_L1, at))
        return out

    def _ready(self, i: int) -> int:
        """The phase at whose start per-row value `i` exists."""
        return self.level[i]

    def _phase(self, p: int, steps: list, h: int) -> list[int]:
        """Every member of these steps through phase `p`'s program, `members` at a time."""
        t = self.templates[p]
        prog = t["prog"]
        if not prog:
            return []
        out: list[int] = []
        pairs = [(g, j) for g in range(len(steps)) for j in range(self.w)]
        for b0 in range(0, len(pairs), self.members):
            block = pairs[b0 : b0 + self.members]
            regs = [
                {i: self.r_member[m][s] for i, s in t["slot"].items()}
                for m in range(len(block))
            ]
            for item in prog:
                words = []
                for m, (g, j) in enumerate(block):
                    words.append(self._entry(item, g, j, steps[g], h, regs[m], m, t))
                for w in range(max(len(x) for x in words)):
                    out += [x[w] for x in words if w < len(x)]
        return out

    def _entry(self, item, g: int, j: int, s: int, h: int, regs: dict, m: int, t: dict):
        kind = item[0]
        if kind == "val":
            i = item[1]
            n = self.nodes[i]
            if n.what == "in":
                at = self._l1(h, n.index) + s * self.step_words + j
                return [V.vld(regs[i], AD_L1, at)]
            ops = [self._operand(a, g, regs) for a in n.args]
            tmp = self.r_member[m][t["tmp"]] if t["tmp"] is not None else regs[i]
            return lower_word(n.op, ops, regs[i], tmp, pr=m % PREDICATES)
        if kind == "acc":
            f, i = item[1], item[2]
            fold = self.nodes[f].op
            acc = self.r_row[g][f]
            x = regs[i]
            if fold is OpKind.SUMSQ:
                if j == 0:
                    return [V.alu("VMUL", vd=acc, va=x, vb=x)]
                return [V.alu("VFMA", vd=acc, va=x, vb=x, vc=acc)]
            if j == 0:
                return [V.alu("VMOV", vd=acc, va=x)]
            return lower_word(COMBINE[fold], [(V.SRC_V, acc), (V.SRC_V, x)], acc)
        tt, i = item[1], item[2]
        at = self._l1(h, self.nin + tt) + s * self.step_words + j
        return [V.vst(regs[i], AD_L1, at)]

    def _butterflies(self, p: int, steps: list) -> list[int]:
        """Each fold ending phase `p`, combined across the 16 lanes of every chunk."""
        folds = [
            i
            for i, n in enumerate(self.nodes)
            if n.what == "fold" and self.level[i] == p + 1
        ]
        pairs = [(g, f) for g in range(len(steps)) for f in folds]
        temps = [r for regs in self.r_member for r in regs]
        out: list[int] = []
        for b0 in range(0, len(pairs), len(temps)):
            block = pairs[b0 : b0 + len(temps)]
            for rot in S_ROT:
                for k, (g, f) in enumerate(block):
                    out.append(V.alu("VSHUF", vd=temps[k], va=self.r_row[g][f], vb=rot))
                for k, (g, f) in enumerate(block):
                    acc = self.r_row[g][f]
                    op = COMBINE[self.nodes[f].op]
                    out += lower_word(op, [(V.SRC_V, acc), (V.SRC_V, temps[k])], acc)
        return out

    # ----------------------------------------------------------- the wire
    def cycles(self, fill_word: float, fill_lat: float) -> float:
        """Core cycles one RUN takes, priced word by word over the image."""
        beats = self.rb
        total, first, imm, alu = 0.0, True, False, False
        for w in self.image:
            if imm:
                imm = False
                continue
            op = w >> 27
            if op <= V.OPS["VRSQRT"]:
                total += beats + ALU_GAP
                alu = True
                continue
            if op == V.OPS["VSHUF"]:
                total += beats + LD_GAP + (ALAT if alu else 0)
            elif op == V.OPS["VLD"]:
                total += beats + LD_GAP
            elif op == V.OPS["VST"]:
                total += beats + ST_GAP
            elif op in (V.OPS["VFILL"], V.OPS["VDRAIN"]):
                total += self.bw
            elif op == V.OPS["VBAR"] and first:
                total += fill_lat + self.nin * self.bw * fill_word
                first = False
            elif op == V.OPS["VSETI"]:
                imm = True
                total += 2
            else:
                total += 1
            alu = False
        return total

    def static_descs(self) -> list[int]:
        """The L1 window -- `rb` words `w` apart, a chunk a row -- and every walk."""
        out = [V.desc_flit(AD_L1, 0, 0)]
        out += _dims(AD_L1, [(self.w, self.rb)])
        for h in range(self.halves):
            for i in range(self.nin):
                out += _dims(self.ad_fill[h][i], self._fill_dims(i))
            for g, (_, n) in enumerate(self.spans):
                out += _dims(self.ad_drain[h][g], [(V.WORD_BYTES, n * self.bw)])
        return out

    def _fill_dims(self, i: int) -> list:
        walk = self.walks[i]
        if walk is None:
            return [(V.WORD_BYTES, self.bw)]
        return walk.dims(self.batch)

    def _restore(self) -> list[int]:
        out = []
        for h in range(self.halves):
            for i in range(self.nin):
                for n in range(1, len(self._fill_dims(i))):
                    out.append(V.desc_flit(self.ad_fill[h][i], n + 1, V.dim(0, 1)))
        return out

    def flits(self, srcs, dsts, nelem: int) -> list[int]:
        """Image, descriptors, then one RUN per `halves` batches."""
        if len(srcs) != self.nin or len(dsts) != len(self.spans):
            raise VecEmitError(
                f"this band fills {self.nin} operands and drains {len(self.spans)} "
                f"regions; got {len(srcs)} and {len(dsts)}"
            )
        nb = -(-nelem // self.batch)
        if nb % self.halves:
            raise VecEmitError(f"{nb} batches do not make whole RUNs of {self.halves}")
        out = imem_flits(self.image) + self.static_descs()
        step = self.batch * 2
        for r in range(nb // self.halves):
            for h in range(self.halves):
                b = r * self.halves + h
                for i in range(self.nin):
                    walk = self.walks[i]
                    at = b * step if walk is None else walk.at(self.batch, b) * 2
                    out.append(V.desc_flit(self.ad_fill[h][i], 0, srcs[i] + at))
                for g in range(len(self.spans)):
                    out.append(V.desc_flit(self.ad_drain[h][g], 0, dsts[g] + b * step))
            out.append(V.run_flit(0))
        return out + self._restore()
