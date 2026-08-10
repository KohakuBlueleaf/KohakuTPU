"""Candidate vector instructions, measured against the kernels that matter.

    python scripts/py/isa_study.py
    python scripts/py/isa_study.py --kernel attn-causal --listing

Each candidate names the level-2 ops its instruction would absorb; the rewriter
deletes them from the level-3 stream and then deletes whatever that makes dead.
Nothing here compiles, so a saving is what a perfect compiler would reach -- an
upper bound -- and `miss` counts sites where the pattern was there but blocked.

The cost model is ops PER ELEMENT rather than passes, because `vec_lanes`
retires 16 element-ops per cycle in every VMODE. Folding two passes onto the
chain wire therefore saves instructions and register traffic and NOT cycles.
"""

import argparse
import itertools
import sys
from dataclasses import dataclass, field, replace

import ktpu.dsl as D
from ktpu.codegen import codegen
from ktpu.dsl.nn import attention
from ktpu.interp.timing import cost_of_inst
from ktpu.ir import FP16
from ktpu.ir.graph import REDUCTION, OpKind
from ktpu.ir.program import Inst, Opcode, Program
from ktpu.ir.sched import Engine, Schedule, ScheduleError
from ktpu.passes import lower
from ktpu.target import VU13P_8CU

LOG2_E = 1.4426950408889634

#: Element-ops the sixteen lanes retire per cycle, in every VMODE. The one
#: number the whole cycle model rests on -- see docs/compute/vector-core.md s7.
LANES = 16

#: FP16 elements in one 256-bit L1 word, so one load beat feeds one FLAT cycle.
WORD = 16

#: Cycles to drain the sixteen rotating partial accumulators (s7.3) once a
#: reduction's stream ends. Charged per VRED execution, not per element.
RED_TAIL = 4

#: Ops per element a reduction costs: a plain tree is one FMA per input element
#: amortised, a fused leaf is that plus its own op (s7.3a).
RED_OPE = 1
FUSED_RED_OPE = 2

ISSUE = {Opcode.VSETVL: 1, Opcode.VSETMD: 1, Opcode.VLOOP: 1, Opcode.VSETI: 2}

SETUP = (Opcode.VSETVL, Opcode.VSETMD, Opcode.VSETI)
CLUSTER = (Opcode.FILL, Opcode.GEMM, Opcode.DRAIN)


# --------------------------------------------------------------------------
# the stream, parsed


@dataclass
class CoreProg:
    """One core's program for one band: setup, then a loop over its chunks."""

    node: tuple
    band: str
    vl: int
    mode: str
    setup: list = field(default_factory=list)
    loop: Inst | None = None
    body: list = field(default_factory=list)
    count: int = 1

    def insts(self) -> list:
        return [*self.setup, *([self.loop] if self.loop else []), *self.body]


@dataclass
class Profile:
    """What a stream costs. Cycles are a bracket, as timing.py's are."""

    insts: dict
    alu_slots: int = 0
    mem_slots: int = 0
    alu_cycles: int = 0
    mem_cycles: int = 0
    issue: int = 0
    serial: int = 0
    overlap: int = 0
    mm_cycles: int = 0

    @property
    def total(self) -> int:
        return sum(self.insts.values())

    def klass(self) -> dict:
        """Instructions by the three classes the study argues about."""
        g = {"arith": 0, "move": 0, "setup": 0, "cluster": 0}
        for k, v in self.insts.items():
            match k:
                case "valu" | "vred":
                    g["arith"] += v
                case "vld" | "vst":
                    g["move"] += v
                case "vsetvl" | "vsetmd" | "vseti" | "vloop":
                    g["setup"] += v
                case _:
                    g["cluster"] += v
        return g


def parse(prog: Program, vid_band: dict) -> tuple[list, list]:
    """Split the stream into per-core programs plus the cluster instructions.

    A core program is the setup since the last one, its VLOOP, and the `body`
    instructions the loop covers. Segmenting on VLOOP rather than on VSETVL is
    what survives codegen eliding a setup the core already holds; VL and VMODE
    are therefore read from per-node state rather than from the program itself.

    The owner is the band producing the first arithmetic result -- the loads and
    stores around it name values from neighbouring bands.
    """
    cps, cluster, pend = [], [], []
    state: dict = {}
    insts, i = prog.insts, 0
    while i < len(insts):
        inst = insts[i]
        if inst.engine is not Engine.VECTOR:
            cluster.append(inst)
            i += 1
            continue
        cur = state.setdefault(inst.node, {"vl": 0, "mode": ""})
        match inst.op:
            case Opcode.VSETVL:
                cur["vl"] = inst.fields["vl"]
                pend.append(inst)
            case Opcode.VSETMD:
                cur["mode"] = inst.fields["mode"]
                pend.append(inst)
            case Opcode.VSETI:
                pend.append(inst)
            case Opcode.VLOOP:
                n = inst.fields["body"]
                cps.append(
                    CoreProg(
                        inst.node,
                        "",
                        cur["vl"],
                        cur["mode"],
                        setup=pend,
                        loop=inst,
                        body=list(insts[i + 1 : i + 1 + n]),
                        count=inst.fields["count"],
                    )
                )
                pend = []
                i += n
            case _:
                raise ValueError(f"{inst} sits outside any VLOOP body")
        i += 1
    for cp in cps:
        owner = next(
            (vid_band.get(i.fields.get("vid")) for i in cp.body if i.op in ARITH), None
        )
        if owner is None:
            raise ValueError(f"core program on {cp.node} produces no arithmetic")
        cp.band = owner
    return cps, cluster


ARITH = (Opcode.VALU, Opcode.VRED)


def elems(i: Inst, vl: int) -> int:
    """Elements one load or store moves.

    A broadcast load reads few distinct values but still writes VL lanes, so it
    is charged the full pass; only an explicit `n` (a scalar store) is smaller.
    """
    return int(i.fields.get("n", vl))


def ope(i: Inst) -> int:
    """Ops per element an arithmetic instruction spends."""
    if i.op is Opcode.VRED:
        return FUSED_RED_OPE if i.fields.get("fused") else RED_OPE
    return int(i.fields.get("ope", 1))


def profile(cps: list, cluster: list, t) -> Profile:
    """Cost a parsed stream. Bands are serial, cores within a band are not."""
    counts: dict = {}
    for i in [x for cp in cps for x in cp.insts()] + cluster:
        counts[i.op.value] = counts.get(i.op.value, 0) + 1

    alu = mem = iss = 0
    per_band: dict = {}
    for cp in cps:
        a = sum(ope(i) * cp.vl for i in cp.body if i.op in ARITH)
        m = sum(elems(i, cp.vl) for i in cp.body if i.op in (Opcode.VLD, Opcode.VST))
        tail = RED_TAIL * sum(1 for i in cp.body if i.op is Opcode.VRED)
        s = sum(ISSUE.get(i.op, 1) for i in cp.setup) + (1 if cp.loop else 0)
        ac = cp.count * (-(-a // LANES) + tail)
        mc = cp.count * -(-m // WORD)
        alu += cp.count * a
        mem += cp.count * m
        iss += s
        key = (cp.band, cp.node)
        was = per_band.get(key, (0, 0, 0))
        per_band[key] = (was[0] + ac, was[1] + mc, was[2] + s)

    bands: dict = {}
    for (band, _), v in per_band.items():
        cur = bands.setdefault(band, [0, 0])
        cur[0] = max(cur[0], v[0] + v[1] + v[2])
        cur[1] = max(cur[1], max(v[0], v[1]) + v[2])

    mm: dict = {}
    for i in cluster:
        if i.op in CLUSTER:
            mm[i.node] = mm.get(i.node, 0) + cost_of_inst(i, t)

    return Profile(
        insts=counts,
        alu_slots=alu,
        mem_slots=mem,
        alu_cycles=-(-alu // LANES),
        mem_cycles=-(-mem // WORD),
        issue=iss,
        serial=sum(v[0] for v in bands.values()),
        overlap=sum(v[1] for v in bands.values()),
        mm_cycles=max(mm.values(), default=0),
    )


# --------------------------------------------------------------------------
# what a candidate names


@dataclass
class Ctx:
    """One lowered kernel, indexed the way the selectors need to read it."""

    name: str
    graph: object
    sched: Schedule
    prog: Program
    target: object
    tri: int = -1

    def __post_init__(self):
        self.vid_band = {o.out: b.name for b in self.sched.bands for o in b.ops}
        self.op_of = {o.out: o for b in self.sched.bands for o in b.ops}
        self.band_of = {b.name: b for b in self.sched.bands}
        self.vec = [b for b in self.sched.bands if b.engine is Engine.VECTOR]
        self.mm_out = {
            b.ops[0].out for b in self.sched.bands if b.engine is Engine.MATMUL
        }
        self.consts = {
            v: w
            for b in self.sched.bands
            for v, (k, w) in b.ext.items()
            if k == "const"
        }
        self.elems = {v: n for b in self.sched.bands for v, n in b.elems.items()}
        self.readers: dict = {}
        for b in self.sched.bands:
            for o in b.ops:
                for v in o.ins:
                    self.readers.setdefault(v, []).append(o.out)

    def band_ops(self, name: str) -> list:
        return list(self.band_of[name].ops)

    def view(self, cut) -> tuple[dict, dict]:
        """The schedule as an earlier cut left it: surviving ops and readers.

        Stacked candidates have to compose -- `vexpsum` only sees a subtract
        feeding its `exp2` once `scalefold` has deleted the multiply between
        them -- so every selector reads operands through this rather than
        through the pristine graph.
        """
        ops, readers = {}, {}
        for vid, op in self.op_of.items():
            if vid in cut.drop:
                continue
            ins = tuple(resolve(cut.rewire, v) for v in op.ins)
            ops[vid] = (op, ins)
            for v in ins:
                readers.setdefault(v, []).append(vid)
        return ops, readers


@dataclass
class Cut:
    """Ops a candidate absorbs, plus where it could not apply.

    `drop` disappears entirely; `fold[a] = b` means b's instruction now also
    produces a, so a's store survives and b inherits a's operands; `rewire`
    redirects every read of a value to another that is already in memory.
    """

    drop: set = field(default_factory=set)
    fold: dict = field(default_factory=dict)
    rewire: dict = field(default_factory=dict)
    fused: set = field(default_factory=set)
    hits: int = 0
    misses: int = 0
    notes: list = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.drop or self.fold or self.rewire or self.fused)


def resolve(rewire: dict, v: int) -> int:
    seen = set()
    while v in rewire and v not in seen:
        seen.add(v)
        v = rewire[v]
    return v


# --------------------------------------------------------------------------
# selectors


def sel_scalefold(ctx: Ctx, prior: Cut = None) -> Cut:
    """Fold `log2 e` into the constant an upstream scale already multiplies by.

    Legal only when both operands of the subtract that feeds an `exp2` descend
    from multiplies by ONE shared constant, and nothing else reads that cone --
    scaling a value that escapes changes a result. Bare softmax has no upstream
    scale to absorb the factor, so it is a miss rather than a saving.
    """
    cut = Cut()
    covariant = {OpKind.SUB, OpKind.MAX, OpKind.RMAX, OpKind.MIN, OpKind.RMIN}
    for vid, op in ctx.op_of.items():
        if op.kind is not OpKind.MUL:
            continue
        k = [ctx.consts.get(v) for v in op.ins]
        if not any(c is not None and abs(c - LOG2_E) < 1e-12 for c in k):
            continue
        if not any(
            ctx.op_of.get(u, op).kind is OpKind.EXP2 for u in ctx.readers.get(vid, [])
        ):
            continue
        src = next(v for v in op.ins if ctx.consts.get(v) is None)
        anchors, escapes, seen = set(), [], set()
        stack = [src]
        while stack:
            v = stack.pop()
            if v in seen:
                continue
            seen.add(v)
            up = ctx.op_of.get(v)
            if up is None:
                escapes.append(f"%{v} has no producer in the schedule")
                continue
            if up.kind is OpKind.MUL and any(
                ctx.consts.get(x) is not None for x in up.ins
            ):
                anchors.add(next(ctx.consts[x] for x in up.ins if x in ctx.consts))
                continue
            if up.kind not in covariant:
                escapes.append(f"%{v} is {up.kind.value}, not scale covariant")
                continue
            stack += list(up.ins)
        leaked = [
            u
            for v in seen
            for u in ctx.readers.get(v, [])
            if u not in seen and u != vid and ctx.op_of[u].kind not in covariant
        ]
        if escapes or len(anchors) != 1 or leaked:
            cut.misses += 1
            why = escapes[0] if escapes else (f"{len(anchors)} scale constants")
            cut.notes.append(f"%{vid}: {why if not leaked else 'the cone escapes'}")
            continue
        cut.hits += 1
        cut.drop.add(vid)
        cut.rewire[vid] = src
    return cut


def _const_mul_on(ctx: Ctx, pred) -> Cut:
    """Drop every `mul` by a constant whose other operand satisfies `pred`."""
    cut = Cut()
    for vid, op in ctx.op_of.items():
        if op.kind is not OpKind.MUL:
            continue
        if not any(v in ctx.consts for v in op.ins):
            continue
        src = next((v for v in op.ins if v not in ctx.consts), None)
        if src is None or not pred(src):
            continue
        cut.hits += 1
        cut.drop.add(vid)
        cut.rewire[vid] = src
    return cut


def sel_emit_scale(ctx: Ctx, prior: Cut = None) -> Cut:
    """FEATURES `emit_scale`: the accumulator applies the per-tile constant.

    Only a multiply reading a drained tile straight off a matmul qualifies --
    once another op has touched the value there is no EMIT left to fold into.
    """
    cut = _const_mul_on(ctx, lambda v: v in ctx.mm_out)
    if not cut.hits:
        cut.notes.append("no constant multiply sits directly on a drained tile")
    return cut


def sel_row_rescale(ctx: Ctx, prior: Cut = None) -> Cut:
    """FEATURES `row_rescale`: the accumulator applies a per-row 2^k in place.

    The multiply has to read a drained tile against a value with one element
    per row; flash attention's `acc * corr` is the shape this was written for.
    Correctness needs the driver to round the running max up in log2 space,
    which is a driver precondition this cannot check.
    """
    cut = Cut()
    for vid, op in ctx.op_of.items():
        if op.kind is not OpKind.MUL or len(op.ins) != 2:
            continue
        tile = [v for v in op.ins if v in ctx.mm_out]
        if not tile:
            continue
        other = next((v for v in op.ins if v != tile[0]), tile[0])
        wide, narrow = ctx.elems.get(tile[0], 0), ctx.elems.get(other, 0)
        # A scalar literal is a per-TILE constant, which is emit_scale's job;
        # this feature applies a value with one element per row.
        if other in ctx.consts or not 1 < narrow < wide:
            cut.misses += 1
            cut.notes.append(f"%{vid}: %{other} is not a per-row operand")
            continue
        cut.hits += 1
        cut.drop.add(vid)
        cut.rewire[vid] = tile[0]
    return cut


def sel_vldp(ctx: Ctx, prior: Cut = None) -> Cut:
    """Load a predicate register, so a 0/1 mask predicates instead of scaling.

    P0..P3 and the `pm` field exist and `VCMPLT` writes a predicate, but
    nothing loads one from memory -- so a causal mask, which is data, has to
    arrive as a vector and be multiplied in.
    """
    cut = Cut()
    if ctx.tri < 0:
        cut.notes.append("kernel has no mask operand")
        return cut
    for vid, op in ctx.op_of.items():
        if op.kind is OpKind.MUL and ctx.tri in op.ins:
            cut.hits += 1
            cut.drop.add(vid)
            cut.rewire[vid] = next(v for v in op.ins if v != ctx.tri)
    return cut


def _expsum_sites(ctx: Ctx, prior: Cut):
    """Every `exp2` whose result is summed, with what stands between them.

    Only a reader in the SAME band blocks the fusion. A later band reads the
    value out of memory, which is exactly what the writeback half of kind 5
    puts there, so a downstream GEMM consuming `p` is not an obstacle.
    """
    ops, readers = ctx.view(prior)
    for vid, (op, ins) in ops.items():
        if op.kind is not OpKind.EXP2:
            continue
        band = ctx.band_of[ctx.vid_band[vid]]
        after = [
            ops[u][0]
            for u in readers.get(vid, [])
            if ops[u][0].kind in REDUCTION and ctx.vid_band.get(u) == band.name
        ]
        others = [
            u
            for u in readers.get(vid, [])
            if ctx.vid_band.get(u) == band.name and ops[u][0].kind not in REDUCTION
        ]
        yield vid, ins, band, after, others


def sel_expsum(ctx: Ctx, prior: Cut = None) -> Cut:
    """VRED kind 5, `vec_reduce_writeback`: exp2 in the leaf, sum in the tree.

    A unary leaf leaves the tree intact (s7.3a), so this costs 221 LUT/core of
    writeback muxing and no new ALU op. It removes an instruction and a
    register round trip; it does NOT remove an op per element, so it cannot
    move the cycle count.
    """
    prior = prior or Cut()
    cut = Cut()
    for vid, _ins, _band, after, others in _expsum_sites(ctx, prior):
        if not after:
            cut.misses += 1
            cut.notes.append(f"%{vid}: exp2 result is never reduced")
            continue
        if others:
            cut.misses += 1
            kinds = {ctx.op_of[u].kind.value for u in others}
            cut.notes.append(f"%{vid}: {'/'.join(sorted(kinds))} sits before the sum")
            continue
        cut.hits += 1
        cut.drop.add(vid)
        cut.fold[vid] = after[0].out
        cut.fused.add(after[0].out)
    return cut


def sel_vexpsum(ctx: Ctx, prior: Cut = None) -> Cut:
    """`vd = exp2(a - b)` in the leaf while the tree sums it.

    A biased leaf is two stages, so it needs a new single-stage ALU op inside
    `vec_alu` -- ~900-1,250 LUT/core on top of expsum's 221. It applies only
    where the subtract feeds the exp2 alone and nothing sits between the exp2
    and the sum, so it inherits every block expsum has and adds its own.
    """
    prior = prior or Cut()
    out = sel_expsum(ctx, prior)
    _, readers = ctx.view(prior)
    for vid, ins, _band, after, others in _expsum_sites(ctx, prior):
        if not after or others:
            continue
        sub = ctx.op_of.get(ins[0])
        if sub is None or sub.kind is not OpKind.SUB:
            out.notes.append(f"%{vid}: exp2 reads no subtract, so only the leaf fuses")
            continue
        if len(readers.get(sub.out, [])) != 1:
            out.notes.append(f"%{sub.out}: the subtract has another reader")
            continue
        out.hits += 1
        out.drop.add(sub.out)
        out.fold[sub.out] = after[0].out
    return out


# --------------------------------------------------------------------------
# rewriting the stream


def survivors(ctx: Ctx, cut: Cut) -> tuple[dict, dict, dict]:
    """Per band, the ops that live, the values it must load, and what it stores.

    Recomputed from the schedule rather than patched into the instruction
    list, so an empty cut has to reproduce the original stream exactly -- which
    is what `--selftest` asserts before any candidate is believed.
    """
    live: dict = {}
    inherit: dict = {}
    for a, b in cut.fold.items():
        inherit.setdefault(b, []).extend(ctx.op_of[a].ins)

    for b in ctx.vec:
        keep = []
        for o in b.ops:
            if o.out in cut.drop:
                continue
            ins = [resolve(cut.rewire, v) for v in (*o.ins, *inherit.get(o.out, ()))]
            ins = [v for v in ins if cut.fold.get(v) != o.out]
            keep.append(replace(o, ins=tuple(dict.fromkeys(ins))))
        live[b.name] = keep

    wanted: dict = {}
    for name, ops in live.items():
        made = {o.out for o in ops}
        for o in ops:
            for v in o.ins:
                if v not in made:
                    wanted.setdefault(name, set()).add(v)

    outputs = {resolve(cut.rewire, v) for v in ctx.sched.outputs}
    reads: dict = {}
    stores: dict = {}
    for b in ctx.vec:
        cs = {v for v, (k, _) in b.ext.items() if k == "const"}
        reads[b.name] = wanted.get(b.name, set()) - cs
    # A GEMM reads its operands out of memory too, so a vector store feeding
    # one is live however few vector bands still want the value.
    mm_reads = {
        resolve(cut.rewire, v)
        for b in ctx.sched.bands
        if b.engine is Engine.MATMUL
        for o in b.ops
        for v in o.ins
    }
    needed = {v for s in reads.values() for v in s} | outputs | mm_reads
    for b in ctx.vec:
        made = {o.out for o in live[b.name]}
        folded = {a for a, t in cut.fold.items() if t in made}
        stores[b.name] = (made | folded) & needed
    return live, reads, stores


def rewrite(ctx: Ctx, cps: list, cut: Cut) -> list:
    """Filter the parsed stream down to what the cut leaves alive."""
    live, reads, stores = survivors(ctx, cut)
    out = []
    for cp in cps:
        ops = live.get(cp.band, [])
        if not ops:
            continue
        keep, loaded = [], set()
        made = {o.out for o in ops}
        for i in cp.body:
            vid = resolve(cut.rewire, i.fields.get("vid", -1))
            match i.op:
                case Opcode.VLD:
                    if vid in reads[cp.band] and vid not in loaded:
                        loaded.add(vid)
                        keep.append(i)
                case Opcode.VST:
                    if vid in stores[cp.band]:
                        keep.append(i)
                case Opcode.VALU | Opcode.VRED:
                    if i.fields.get("vid") in made:
                        f = dict(i.fields)
                        if i.fields.get("vid") in cut.fused:
                            f["fused"] = True
                        keep.append(replace(i, fields=f))
        if not any(i.op in ARITH for i in keep):
            continue
        loop = cp.loop
        if loop is not None:
            loop = replace(loop, fields={**loop.fields, "body": len(keep)})
        out.append(replace(cp, setup=list(cp.setup), loop=loop, body=keep))
    return out


def setup_dce(cps: list, per_round: bool = False) -> list:
    """Drop a VSETVL/VSETMD/VSETI that re-sets what the core already holds.

    Sound only while a band boundary leaves VL, VMODE and the scalar file
    alone, which nothing in the stream currently disturbs -- there is no
    barrier between bands. `per_round` is the cautious reading, where a host
    kick is assumed to reset the core.
    """
    state: dict = {}
    out = []
    for n, cp in enumerate(cps):
        key = cp.node if not per_round else (cp.node, n // 8)
        cur = state.setdefault(key, {})
        keep = []
        for i in cp.setup:
            match i.op:
                case Opcode.VSETVL:
                    tag, val = "vl", i.fields["vl"]
                case Opcode.VSETMD:
                    tag, val = "md", (i.fields["mode"], i.fields["depth"])
                case _:
                    tag, val = f"s{i.fields['sd']}", i.fields["imm"]
            if cur.get(tag) != val:
                cur[tag] = val
                keep.append(i)
        out.append(replace(cp, setup=keep))
    return out


def vloopx(cps: list) -> list:
    """VL and VMODE as fields on VLOOP, so the loop carries its own context."""
    out = []
    for cp in cps:
        keep = [i for i in cp.setup if i.op is Opcode.VSETI]
        out.append(replace(cp, setup=keep))
    return out


def bandfuse(ctx: Ctx, cps: list) -> list:
    """Compiler reference: merge a band into its predecessor when it may.

    Not an instruction -- the yardstick every candidate is ranked against. Two
    consecutive vector bands with the same grid, tile and VL, where everything
    the second reads the first produced, are one loop over data that never
    leaves the register file: the setup goes, and so does every store/load pair
    for a value neither band shares with anyone else.
    """
    _live, reads, _stores = survivors(ctx, Cut())
    order = [b.name for b in ctx.vec]
    pos = {n: i for i, n in enumerate(order)}
    merge: dict = {}
    for a, b in itertools.pairwise(order):
        ba, bb = ctx.band_of[a], ctx.band_of[b]
        if (ba.grid.size, ba.tile) != (bb.grid.size, bb.tile):
            continue
        made = {o.out for o in ba.ops}
        if not reads[b] or not reads[b] <= made:
            continue
        outside = {
            v
            for v in reads[b] & made
            if any(pos.get(ctx.vid_band.get(u), -1) > pos[b] for u in ctx.readers[v])
            or v in ctx.sched.outputs
        }
        merge[b] = (a, (reads[b] & made) - outside)

    vl_of = {cp.band: cp.vl for cp in cps}
    out = []
    for cp in cps:
        if cp.band not in merge:
            out.append(cp)
            continue
        prev, gone = merge[cp.band]
        if vl_of.get(prev) != cp.vl:
            out.append(cp)
            continue
        body = [
            i
            for i in cp.body
            if not (i.op is Opcode.VLD and i.fields.get("vid") in gone)
        ]
        out.append(replace(cp, setup=[], loop=None, body=body))
    trimmed = []
    for cp in out:
        dead = {v for b, (a, g) in merge.items() if a == cp.band for v in g}
        trimmed.append(
            replace(
                cp,
                body=[
                    i
                    for i in cp.body
                    if not (i.op is Opcode.VST and i.fields.get("vid") in dead)
                ],
            )
        )
    return trimmed


# --------------------------------------------------------------------------
# kernels


def spec(*shape):
    return D.TensorSpec(shape, FP16)


def _attn(ell, dh, block, causal, heads=1):
    def build():
        qs = (heads, ell, dh) if heads > 1 else (ell, dh)
        specs = [spec(*qs)] * 3 + ([spec(block, block)] if causal else [])

        def kern(q, k, v, tri=None):
            return attention(q, k, v, block=block, causal=causal, tri=tri)

        g = D.trace(kern, *specs)
        return g, (block if causal else ell), dh, dh, ("tri" if causal else None)

    return build


def _pointwise(fn, rows, cols, extra=()):
    def build():
        specs = [spec(rows, cols), *[spec(*s) for s in extra]]
        g = D.trace(fn, *specs)
        return g, rows, cols, cols, None

    return build


def _mlp(rows, chan, hidden):
    def build():
        g = D.trace(
            lambda x, w1, w2: D.gelu(x @ w1) @ w2,
            spec(rows, chan),
            spec(chan, hidden),
            spec(hidden, chan),
        )
        return g, rows, chan, chan, None

    return build


KERNELS = {
    "attn-full": _attn(128, 64, 64, False),
    "attn-causal": _attn(128, 64, 64, True),
    "attn-full-256": _attn(256, 64, 64, False),
    "attn-mha2": _attn(128, 64, 64, False, heads=2),
    "softmax": _pointwise(lambda x: D.softmax(x), 128, 128),
    "layernorm": _pointwise(
        lambda x, g, b: D.layernorm(x, g, b), 256, 128, [(128,), (128,)]
    ),
    "rmsnorm": _pointwise(lambda x, g: D.rmsnorm(x, g), 256, 128, [(128,)]),
    "gelu": _pointwise(D.gelu, 128, 256),
    "silu": _pointwise(D.silu, 128, 256),
    "mlp-gelu": _mlp(128, 256, 512),
}


def load(name: str, target, reorder: bool = True, absorb: bool = True) -> Ctx:
    graph, m, k, n, tri = KERNELS[name]()
    sched = lower(graph, target, reorder=reorder, absorb_scale=absorb)
    prog = codegen(sched, m, k, n, target)
    ctx = Ctx(name, graph, sched, prog, target)
    if tri:
        for op in graph.ops:
            if op.kind is OpKind.INPUT and op.attrs.get("name") == tri:
                ctx.tri = op.out.vid
    return ctx


# --------------------------------------------------------------------------
# candidates


@dataclass
class Candidate:
    """One thing that might be built, and what building it would take."""

    key: str
    what: str
    hardware: str
    sel: object = None
    post: tuple = ()
    stack: tuple = ()


CANDIDATES = [
    Candidate(
        "setup-dce",
        "drop redundant VSETVL/VSETMD/VSETI",
        "none (compiler)",
        post=("dce",),
    ),
    Candidate(
        "scalefold",
        "fold log2 e into the upstream scale",
        "none (compiler)",
        sel=sel_scalefold,
    ),
    Candidate(
        "bandfuse",
        "keep a tile resident across two bands",
        "none (compiler)",
        post=("fuse",),
    ),
    Candidate(
        "emit-scale",
        "FEATURES emit_scale, per-tile constant on EMIT",
        "cluster accumulator",
        sel=sel_emit_scale,
    ),
    Candidate(
        "row-rescale",
        "FEATURES row_rescale, per-row 2^k in place",
        "cluster accumulator",
        sel=sel_row_rescale,
    ),
    Candidate(
        "vloopx",
        "VL and VMODE as VLOOP fields",
        "vector decoder, wiring",
        post=("vloopx",),
    ),
    Candidate(
        "dce+vloopx",
        "drop the redundant setup, fold the rest into VLOOP",
        "vector decoder, wiring",
        post=("dce", "vloopx"),
    ),
    Candidate(
        "vldp",
        "load a predicate register; mask by pm not by multiply",
        "vector, small",
        sel=sel_vldp,
    ),
    Candidate(
        "expsum",
        "VRED kind 5: exp2 leaf, sum tree, one pass",
        "221 LUT/core, no new ALU op",
        sel=sel_expsum,
    ),
    Candidate(
        "vexpsum",
        "exp2(a - b) leaf while the tree sums",
        "+900-1250 LUT/core, new ALU op",
        sel=sel_vexpsum,
    ),
    Candidate(
        "scalefold+expsum",
        "the free rewrite, then the free leaf",
        "221 LUT/core",
        stack=("scalefold", "expsum"),
    ),
    Candidate(
        "scalefold+vexpsum",
        "the free rewrite, then the biased leaf",
        "+900-1250 LUT/core",
        stack=("scalefold", "vexpsum"),
    ),
    Candidate(
        "accum-epilogue",
        "scalefold + emit_scale + row_rescale",
        "cluster accumulator",
        stack=("scalefold", "emit-scale", "row-rescale"),
    ),
    Candidate(
        "vector-isa",
        "every vector ISA change, no compiler help",
        "vector core",
        stack=("vldp", "vexpsum"),
        post=("vloopx",),
    ),
    Candidate(
        "compiler-only",
        "every zero-hardware candidate together",
        "none",
        stack=("scalefold",),
        post=("dce", "fuse"),
    ),
    Candidate(
        "all-but-vexpsum",
        "everything except the biased leaf, keeping the free one",
        "no new ALU op",
        stack=("scalefold", "emit-scale", "row-rescale", "vldp", "expsum"),
        post=("dce", "fuse", "vloopx"),
    ),
    Candidate(
        "everything",
        "every candidate, stacked",
        "all of the above",
        stack=("scalefold", "emit-scale", "row-rescale", "vldp", "vexpsum"),
        post=("dce", "fuse", "vloopx"),
    ),
]

BY_KEY = {c.key: c for c in CANDIDATES}
SELECTORS = {c.key: c.sel for c in CANDIDATES if c.sel}


def evaluate(ctx: Ctx, cand: Candidate, cps: list) -> tuple[list, Cut]:
    """Apply one candidate to a parsed stream, returning the stream and the cut."""
    keys = cand.stack or ((cand.key,) if cand.sel else ())
    total = Cut()
    for k in keys:
        sel = SELECTORS.get(k)
        if sel is None:
            continue
        c = sel(ctx, total)
        total.drop |= c.drop
        total.fold |= c.fold
        total.rewire |= c.rewire
        total.fused |= c.fused
        total.hits += c.hits
        total.misses += c.misses
        total.notes += [f"{k}: {n}" for n in c.notes]
    out = rewrite(ctx, cps, total) if total else list(cps)
    for step in cand.post:
        match step:
            case "dce":
                out = setup_dce(out)
            case "vloopx":
                out = vloopx(out)
            case "fuse":
                out = bandfuse(ctx, out)
    return out, total


# --------------------------------------------------------------------------
# reporting


def selftest(ctx: Ctx, cps: list) -> list:
    """An empty cut must reproduce the stream, or every number after is wrong."""
    bad = []
    _live, reads, stores = survivors(ctx, Cut())
    for b in ctx.vec:
        got_ld, got_st = set(), set()
        for cp in cps:
            if cp.band != b.name:
                continue
            for i in cp.body:
                if i.op is Opcode.VLD:
                    got_ld.add(i.fields["vid"])
                elif i.op is Opcode.VST:
                    got_st.add(i.fields["vid"])
        if got_ld != reads[b.name]:
            bad.append(
                f"{ctx.name}/{b.name} loads {sorted(got_ld)} model "
                f"{sorted(reads[b.name])}"
            )
        if got_st != stores[b.name]:
            bad.append(
                f"{ctx.name}/{b.name} stores {sorted(got_st)} model "
                f"{sorted(stores[b.name])}"
            )
    rebuilt = rewrite(ctx, cps, Cut())
    a = sum(len(cp.insts()) for cp in cps)
    b2 = sum(len(cp.insts()) for cp in rebuilt)
    if a != b2:
        bad.append(f"{ctx.name}: empty rewrite changed {a} instructions to {b2}")
    return bad


def pct(new: int, old: int) -> str:
    if not old:
        return "     -"
    return f"{100.0 * (new - old) / old:+6.1f}%"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--kernel", action="append", help="restrict to these kernels")
    ap.add_argument("--candidate", action="append", help="restrict to these candidates")
    ap.add_argument("--listing", action="store_true", help="per-kernel detail")
    ap.add_argument("--notes", action="store_true", help="print every blocked site")
    ap.add_argument(
        "--reorder",
        default="both",
        choices=("on", "off", "both"),
        help="lower() reorder setting; the shipped default is off",
    )
    ap.add_argument(
        "--no-absorb",
        action="store_true",
        help="lower() without scalefold, to measure what landing it bought",
    )
    args = ap.parse_args()

    wanted = args.kernel or list(KERNELS)
    cands = [BY_KEY[c] for c in args.candidate] if args.candidate else CANDIDATES
    modes = {"on": [True], "off": [False], "both": [True, False]}[args.reorder]
    t = VU13P_8CU

    for reorder in modes:
        print(f"\n{'=' * 100}")
        print(f"lower(reorder={reorder})   target {t.name}   {LANES} element-ops/cycle")
        print("=" * 100)

        ctxs, streams, bases = {}, {}, {}
        problems, live = [], []
        for n in wanted:
            try:
                ctx = load(n, t, reorder=reorder, absorb=not args.no_absorb)
            except (ScheduleError, ValueError) as e:
                print(f"  skip {n}: {e}")
                continue
            cps, cluster = parse(ctx.prog, ctx.vid_band)
            problems += selftest(ctx, cps)
            ctxs[n], streams[n] = ctx, (cps, cluster)
            bases[n] = profile(cps, cluster, t)
            live.append(n)
        names = live
        if problems:
            print("SELFTEST FAILED -- the model does not reproduce the stream:")
            for p in problems:
                print("   ", p)
            return 1

        print(
            "\nbaseline   (serial = vector cycles, bands serial and cores parallel;"
            " ovl = loads hidden behind arithmetic)"
        )
        print(
            f"  {'kernel':<15} {'insts':>7} {'arith':>7} {'move':>7} {'setup':>7}"
            f" {'alu-cy':>8} {'mem-cy':>8} {'serial':>9} {'ovl':>9} {'mm-cy':>8}"
            f" {'vec%':>6}"
        )
        for n in names:
            p, k = bases[n], bases[n].klass()
            share = 100.0 * p.serial / max(1, p.serial + p.mm_cycles)
            print(
                f"  {n:<15} {p.total:>7} {k['arith']:>7} {k['move']:>7}"
                f" {k['setup']:>7} {p.alu_cycles:>8} {p.mem_cycles:>8}"
                f" {p.serial:>9} {p.overlap:>9} {p.mm_cycles:>8} {share:>5.1f}%"
            )

        rows = []
        for c in cands:
            tot = {"insts": 0, "alu": 0, "ser": 0, "ovl": 0}
            base = {"insts": 0, "alu": 0, "ser": 0, "ovl": 0}
            hit, miss, where, notes = 0, 0, [], []
            for n in names:
                ctx, (cps, cluster) = ctxs[n], streams[n]
                new, cut = evaluate(ctx, c, cps)
                p, b = profile(new, cluster, t), bases[n]
                hit += cut.hits
                miss += cut.misses
                notes += [f"{n}/{x}" for x in cut.notes]
                for key, got, was in (
                    ("insts", p.total, b.total),
                    ("alu", p.alu_cycles, b.alu_cycles),
                    ("ser", p.serial, b.serial),
                    ("ovl", p.overlap, b.overlap),
                ):
                    tot[key] += got
                    base[key] += was
                if p.total != b.total or p.serial != b.serial:
                    where.append(n)
            rows.append((c, tot, base, hit, miss, where, notes))

        print("\ncandidates, summed over every kernel above")
        print(
            f"  {'candidate':<19} {'insts':>15} {'alu cycles':>15}"
            f" {'cycles serial':>15} {'cycles ovl':>15}  {'hit':>4} {'miss':>5}"
            f"  hardware"
        )
        for c, tot, base, hit, miss, where, _ in rows:
            sites = (
                f"{hit:>4} {miss:>5}" if (c.sel or c.stack) else f"{'-':>4} {'-':>5}"
            )
            print(
                f"  {c.key:<19} {tot['insts']:>7}{pct(tot['insts'], base['insts'])}"
                f" {tot['alu']:>7}{pct(tot['alu'], base['alu'])}"
                f" {tot['ser']:>7}{pct(tot['ser'], base['ser'])}"
                f" {tot['ovl']:>7}{pct(tot['ovl'], base['ovl'])}"
                f"  {sites}  {c.hardware}"
            )

        print("\n  kernels each candidate changes at all")
        for c, _, _, _, _, where, _ in rows:
            print(f"    {c.key:<19} {', '.join(where) or 'NONE'}")

        if args.listing:
            for n in names:
                ctx, (cps, cluster) = ctxs[n], streams[n]
                print(f"\n  --- {n} ---")
                for c in cands:
                    new, cut = evaluate(ctx, c, cps)
                    p, b = profile(new, cluster, t), bases[n]
                    print(
                        f"    {c.key:<19} insts {b.total:>5} -> {p.total:<5}"
                        f" serial {b.serial:>7} -> {p.serial:<7}"
                        f" alu {b.alu_cycles:>6} -> {p.alu_cycles:<6}"
                        f" hit {cut.hits} miss {cut.misses}"
                    )

        if args.notes:
            print("\n  what each candidate absorbs, and what blocked it")
            for c in cands:
                for n in names:
                    new, cut = evaluate(ctxs[n], c, streams[n][0])
                    if not cut.drop and not cut.notes:
                        continue
                    took = ", ".join(
                        f"%{v}={ctxs[n].op_of[v].kind.value}"
                        f"({','.join('%' + str(i) for i in ctxs[n].op_of[v].ins)})"
                        for v in sorted(cut.drop)
                    )
                    print(f"    {c.key:<19} {n:<14} takes {took or 'nothing'}")
                    for x in dict.fromkeys(cut.notes):
                        print(f"    {'':<19} {'':<14} blocked {x}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
