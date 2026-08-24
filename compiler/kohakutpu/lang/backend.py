"""What the statements mean in memory and in bytes.

Level 2 down to 0, on this project's behalf. It chooses each buffer's byte order
by reading the statements that touch it, inserts a relayout where two stages
disagree, materialises folded scalars, and turns each stage into instruction
words.
"""

import itertools

import numpy as np
from kohakuaccel.dispatch import deal
from kohakuaccel.lang import Dim, Kernel, Stmt, resolve
from kohakuaccel.lang import kernel as _traced
from kohakutpu.hw import vector as V
from kohakutpu.isa import ISA
from kohakutpu.isa.vecemit import (
    BATCH_BYTES,
    BATCH_ELEMS,
    DESCRIPTORS,
    FWD,
    OUT_REG,
    REDUCE,
    BandKernel,
    Chain,
    ResidentEpilogueKernel,
    RowReduceKernel,
    Spread,
    VecEmitError,
    forward,
    konst,
)
from kohakutpu.lang import cluster, vector
from kohakutpu.lang.buffers import PART, In, Out
from kohakutpu.lang.errors import CannotFuse, LangError
from kohakutpu.lang.vector import Const, Ref, Resident

from kohakutpu import layout as LO

#: The emitter's own default, and the widest pass a band builds.
MAX_CHUNKS = 8

FP16_ENTRY_BYTES = LO.LANES * LO.KBLOCK * 2

#: One lane of the entry stream. The finest offset a fill can start at, and a
#: quarter of an entry -- which is what a 3x3 tap needs and `Entry` cannot give.
LANE_BYTES = FP16_ENTRY_BYTES // LO.LANES

#: `dflags` bit 0 is `signal_on_complete`. Without it the receiver never answers
#: and nothing can sequence its RUN behind the transfer (isa/cluster.md §9.2).
DFLAG_SIGNAL = 1

#: Granules the cluster gathers into one CU_DATA descriptor, so one drain of `n`
#: sub-tiles is `ceil(n / WBURST)` bursts and that many acknowledgements.
WBURST = 8

#: L1 is two banks of 256 entries a side, not one flat 512 (isa/cluster.md §4.6).
BANKS = 2
BANK_ENTRIES = 256


class TpuBackend:
    """What a KohakuTPU kernel's statements mean in memory and in bytes."""

    #: One RUN stores a whole batch whatever `nelem` says, so a batched buffer
    #: whose stride is under this has each element written over by the next.
    batch_grain = BATCH_BYTES

    def __init__(self, isa=ISA) -> None:
        self.isa = isa

    def prune(self, compiled, stmts: list) -> list:
        """Drop a FILL of what is already in that L1 slot, unchanged.

        Two sweeps sharing an operand -- a gated MLP's halves reading one `x` --
        expand independently, so each fills it. The second is not merely
        redundant: it lands in the side the sweep in flight is still reading,
        which `model.py` reports and silicon would not.

        A slot is `(side, tile, chunk)`; `i` and `j` are fixed within one
        instance, so nothing else can be occupying it. The whole arg set has to
        match, not a chosen few -- a 3x3 tap fills ONE plane at nine `lane`
        offsets, and comparing without it drops eight of the nine pixels.
        """
        out, live = [], {}
        for s in stmts:
            if s.kind != "fill":
                out.append(s)
                continue
            slot = (s.args["sel"], s.args["tile"], s.args["chunk"])
            if live.get(slot) == s.args:
                continue
            live[slot] = dict(s.args)
            out.append(s)
        return out

    def expand(self, compiled, stmt) -> list:
        """A whole-buffer statement as the placed ones it stands for."""
        if stmt.kind != "matmul":
            return [stmt]
        gm = compiled.knobs.get("gm", 2)
        gn = compiled.knobs.get("gn", 1)
        nk = compiled.knobs.get("nk", 2)
        chunks = -(-stmt.args["kblocks"] // nk)
        out = []
        for c in range(chunks):
            for name, sel, tile, groups in (
                (stmt.args["a"], 0, stmt.args["i"], gm),
                (stmt.args["b"], 1, stmt.args["j"], gn),
            ):
                out.append(
                    Stmt(
                        "fill",
                        {
                            "operand": name,
                            "sel": sel,
                            "tile": tile,
                            "chunk": c,
                            "step": c,
                            "groups": groups,
                            "blocks": nk,
                            "chunks": chunks,
                        },
                    )
                )
            out.append(Stmt("gemm", {"gm": gm, "gn": gn, "nk": nk, "acc": c}))
        out.append(
            Stmt(
                "drain",
                {
                    "result": stmt.args["result"],
                    "i": stmt.args["i"],
                    "j": stmt.args["j"],
                    "gm": gm,
                    "gn": gn,
                },
            )
        )
        return out

    def unit_of(self, kinds: set) -> str:
        """Which unit runs a stage of these statement kinds."""
        if "matmul" in kinds:
            kinds = (kinds - {"matmul"}) | {"gemm"}
        if kinds <= cluster.KINDS:
            return "MG"
        if kinds <= vector.KINDS:
            return "VC"
        raise LangError(
            f"one stage mixes {sorted(kinds)}, which run on different units; one "
            f"stage is one program for one unit. Either write the elementwise "
            f"work ON the accumulator -- `y[i, j] <<= acc * sigmoid(acc)`, which "
            f"drains over the NoC into a vector core -- or open a second "
            f"`with units(...)` and let the value cross through a temp"
        )

    def sequences(self, before, after) -> bool:
        """Whether ONE vector program holds both, so `after` reads a register.

        A band runs its chains in order and hands a result forward, so a chain
        reading exactly the region an earlier chain of the SAME instance wrote
        is sequenced rather than racing it. Reading any other part of that
        buffer is another instance's work and still a hazard.
        """
        if not (_bandable(before) and _bandable(after)):
            return False
        return _wrote(before) in {
            _leaf_key(leaf) for leaf in after.args["leaves"] if isinstance(leaf, Ref)
        }

    def band(self, stmt) -> str:
        """What may share a stage with this statement.

        A `reduce` and an `apply` are both chains a vector core runs, and a band
        holds them together, so they answer alike and `may_join` decides. A kind
        no band takes keeps its own name and stays on its own.
        """
        return "vector" if _bandable(stmt) else stmt.kind

    def relax(self, exc, knobs: dict) -> dict | None:
        """Knobs to retry with after `exc`, or None to let it through.

        Only :class:`CannotFuse`, and only once: whether a drain reaches a
        vector core depends on the machine and the shape, so the author writes
        the fused form and the COMPILER stages it where that is impossible.
        Every other refusal is about the expression and no retiling fixes it.
        """
        if not isinstance(exc, CannotFuse) or not knobs.get("fuse", True):
            return None
        return {**knobs, "fuse": False}

    def bands(self, compiled) -> list:
        """Temps to place adjacently so their band drains them ONCE.

        Only where the run is over the descriptor budget and sharing would
        bring it under: adjacency costs reuse, and a band that already fits
        gains nothing for it.
        """
        out: list = []
        for stage in compiled.stages:
            if stage.unit != "VC" or not stage.instances:
                continue
            for run in _runs(stage.instances[0].stmts):
                if _budget(run) <= DESCRIPTORS:
                    continue
                held = list(dict.fromkeys(s.args["result"] for s in run))
                if len(held) > 1 and 2 * len(_slots(run)) + 2 <= DESCRIPTORS:
                    out.append(tuple(held))
        return out

    def apart(self, compiled, stage, before, after) -> bool:
        """Whether `after` reads no byte `before` wrote, extents included.

        A region is ``[part*stride + off, ... + span)``. Two of one buffer are
        disjoint when those intervals miss, which is how p, corr, m and l share
        one buffer at row offsets. A PERIODIC read is never claimed apart: it
        reaches a whole group per group start and the interval understates it.
        """
        if not (_bandable(before) and _bandable(after)):
            return False
        stride = _stride(compiled, stage, before, _per(compiled))
        span = _span(compiled, before, stride)
        lo = before.args["part"] * stride + before.args["off"]
        for leaf in after.args["leaves"]:
            if not isinstance(leaf, Ref) or leaf.name != before.args["result"]:
                continue
            if leaf.period:
                return False
            at = leaf.part * stride + leaf.off
            if at < lo + span and lo < at + span:
                return False
        return True

    def may_join(self, held: list, stmt, extents: dict) -> bool:
        """Whether ONE vector program holds `held` and then `stmt`.

        The span a run walks is a layout decision taken after the stage is
        formed, so this counts DESCRIPTORS -- the budget that binds first -- and
        for a run that FOLDS resolves the row against `extents`, since a band
        steps a row at a time and a width `VRED` cannot fold is not a band.
        """
        run = [*held, stmt]
        if not all(_bandable(s) for s in run):
            return False
        depends = [s for s in held if s.writes is not None and s.writes in stmt.reads]
        if not all(self.sequences(s, stmt) for s in depends):
            return False
        if _budget(run) > DESCRIPTORS:
            return False
        return _one_row(run, extents)

    def handle(self, port, knobs: dict):
        """What the kernel body sees for a port, with its name filled in."""
        made = Out(*port.shape) if port.role == "out" else In(*port.shape)
        made.name = port.name
        return made

    def layouts(self, compiled) -> dict:
        """Each buffer's byte order, READ OFF the statements that touch it.

        Raises :class:`LangError` for a port no statement touches.
        """
        _placeable(compiled)
        _pair(compiled)
        wanted = self._requirements(compiled)
        missing = [p.name for p in compiled.signature.ports if p.name not in wanted]
        if missing:
            raise LangError(
                f"{compiled.name}: port {missing} is declared but never touched; "
                f"every operand must be read and every result written"
            )
        compiled.conversions = _conversions(compiled, wanted)
        compiled.layouts = {name: needs[0][1] for name, needs in wanted.items()}
        return compiled.layouts

    def validate(self, compiled) -> None:
        """Refuse what only the finished layouts reveal.

        Separate from :meth:`layouts` so the hazard guard, which needs those
        layouts for a region's extent, still reports before these do.
        """
        _agree(compiled)
        _reachable(compiled)

    def _requirements(self, compiled) -> dict:
        """Buffer -> the order each stage needs it in, in stage order.

        A drain, a fill and a reduction each pin an order; an elementwise stage
        adopts whatever the buffer is already in.
        """
        wanted: dict = {}

        def pin(name, layout, at):
            needs = wanted.setdefault(name, [])
            if not needs or needs[-1][1] != layout:
                needs.append((at, layout))

        for stage in compiled.stages:
            at = stage.index
            for s in _stmts(stage):
                match s.kind:
                    case "drain":
                        pin(
                            s.args["result"],
                            LO.Tile(stage.body_grid, s.args["gm"], s.args["gn"]),
                            at,
                        )
                    case "fill":
                        if s.args["sel"] is None:
                            raise LangError(
                                f"{compiled.name}: {s.args['operand']!r} fills a "
                                f"region that is never multiplied, so which L1 "
                                f"side it feeds was never decided"
                            )
                        pin(
                            s.args["operand"],
                            s.args.get("order")
                            or LO.Entry(s.args["groups"], s.args["blocks"]),
                            at,
                        )
                    case "reduce":
                        for name in (s.args["source"], s.args["result"]):
                            pin(name, LO.Flat(), at)
                    case "apply":
                        read = [le for le in s.args["leaves"] if isinstance(le, Ref)]
                        touched = [le.name for le in read]
                        if _folds(s):
                            # A fold runs ALONG a row, so it pins true row order
                            # exactly as a `reduce` pass of its own does.
                            for name in [s.args["result"], *touched]:
                                pin(name, LO.Flat(), at)
                            continue
                        # A fused epilogue's result is the tile the drain just
                        # handed over, so it adopts the order that drain pinned.
                        seen = (
                            [s.args["result"], *touched]
                            if s.args.get("resident")
                            else touched
                        )
                        order = next(
                            (wanted[n][-1][1] for n in seen if n in wanted),
                            LO.Flat(),
                        )
                        pin(s.args["result"], order, at)
                        if s.args.get("resident"):
                            # The one operand such an epilogue may read beside
                            # the tile is PER-CHANNEL: one word a column group.
                            for name in touched:
                                pin(name, LO.ChannelBias(s.args["gn"]), at)
                            continue
                        for le in read:
                            # A SPREAD is its own 1-D buffer read again per
                            # group, so it never takes the result's order.
                            pin(le.name, LO.Flat() if le.period else order, at)
        return wanted

    def regrid(self, compiled, stage) -> tuple | None:
        """The extent an elementwise stage needs, now that its layout is known.

        `Buffer.parts` sized the grid on the buffer's LOGICAL element count and
        the pass walks the PADDED image, so a drained buffer's grid is short by
        whole instances -- `_stride` then makes each instance cover more, which
        is correct and leaves the rest of the cores idle. Sized from the room,
        every instance is `part=` long again and there are as many as there is
        work: `linear_add` at `M=2, N=6144` runs 24 where it ran 2.

        Returns None for anything but a plain elementwise pass. A reduction's
        grid of ONE is deliberate -- a `RowReduceKernel` covers whole rows in a
        single pass -- and a band that folds inside an expression steps one row
        at a time, so neither may be split finer.
        """
        stmts = stage.instances[0].stmts if stage.instances else []
        if not stmts or len(stage.body_grid) != 1:
            return None
        if any(s.kind != "apply" or s.args.get("resident") or _folds(s) for s in stmts):
            return None
        per = _per(compiled)
        room = max(_room(compiled, s) for s in stmts)
        return (max(1, -(-room // per)),)

    def constants(self, compiled) -> dict:
        """Every folded scalar, as an array one instance's STRIDE long.

        Each batch steps every operand base forward, so a broadcast scalar has to
        cover the whole stride -- not one element, and not one batch. A statement
        the descriptor budget already refuses gets no array: its band seeds the
        scalar into a register instead, and a run holding it costs no less.
        """
        out: dict = {}
        for stage in compiled.stages:
            for s in _stmts(stage):
                if s.kind != "apply" or s.args.get("resident"):
                    continue
                if _seeds(s):
                    continue
                # The instance STRIDE, not `part=`: a pass over a padded buffer
                # steps further, and a shorter array is read past its end.
                wide = _stride(compiled, stage, s, _per(compiled))
                for leaf in s.args["leaves"]:
                    if not isinstance(leaf, Const):
                        continue
                    held = out.get(_const(compiled, leaf))
                    if held is None or held[0].size < wide:
                        out[_const(compiled, leaf)] = (
                            np.full(wide, _scalar(compiled, leaf), np.float16),
                            LO.Flat(),
                        )
        return out

    def encode(self, compiled, stage, addrs: dict) -> dict:
        """Grid coordinate -> instruction words, for one stage."""
        emit = self._cluster if stage.unit == "MG" else self._vector
        return {i.at: emit(compiled, stage, i, addrs) for i in stage.instances}

    @staticmethod
    def _base(compiled, stage, inst, addrs: dict, name: str) -> int:
        """Where this instance's batch element of `name` starts."""
        stride = getattr(compiled.layouts[name], "stride", 0)
        return addrs[name] + stage.element(inst.at) * stride

    def _cluster(self, compiled, stage, inst, addrs: dict) -> list:
        words: list[int] = []
        #: (side, bank) -> where the last FILL landed. A SHARED operand keeps
        #: the offset of the sweep that filled it, not the one reading it.
        placed: dict = {}
        #: (side, operand) -> its own entry offset; `free` is the next one per
        #: side. Interleaved sweeps must not land on the operand being read.
        region: dict = {}
        free: dict = {}
        for s in inst.stmts:
            match s.kind:
                case "fill":
                    span = s.args["groups"] * s.args["blocks"]
                    at = (s.args["tile"] * s.args["chunks"] + s.args["chunk"]) * span
                    off = int(s.args.get("lane") or 0) * LANE_BYTES
                    base = self._base(compiled, stage, inst, addrs, s.args["operand"])
                    _fits(compiled, span, s.args["operand"])
                    _within(compiled, s.args["operand"], at, span, off)
                    bank = int(s.args.get("step", s.args["chunk"])) % BANKS
                    sel, name = s.args["sel"], s.args["operand"]
                    if (sel, name) not in region:
                        region[sel, name] = free.get(sel, 0)
                        free[sel] = region[sel, name] + span
                    eoff = region[sel, name]
                    _in_bank(compiled, name, eoff, span)
                    placed[(s.args["sel"], bank)] = eoff
                    words.append(
                        self.isa.fill(
                            addr=base + at * FP16_ENTRY_BYTES + off,
                            n=span,
                            sel=s.args["sel"],
                            eoff=eoff,
                            # The sweep step, not the chunk: a chunk index that
                            # is not the counter fills a bank no GEMM reads.
                            fbank=bank,
                        )
                    )
                case "gemm":
                    # `acc` counts K chunks, so it also picks the bank the fill
                    # ahead of this sweep wrote.
                    bank = int(s.args["acc"]) % BANKS
                    words.append(
                        self.isa.gemm(
                            gm=s.args["gm"],
                            gn=s.args["gn"],
                            nk=s.args["nk"],
                            acc=int(s.args["acc"] > 0),
                            aoff=placed.get((0, bank), 0),
                            boff=placed.get((1, bank), 0),
                            abank=bank,
                            bbank=bank,
                        )
                    )
                case "drain" if s.args.get("node"):
                    dst = _receiver(compiled, stage, inst.at)
                    agent = compiled.machine.agent
                    words.append(
                        self.isa.drain(
                            # A node-addressed drain sends addr[20:5] as the
                            # descriptor's granule off, so this is an L1 word.
                            addr=ResidentEpilogueKernel.peer_word(
                                s.args["gm"] * s.args["gn"], s.args.get("slot", 0)
                            )
                            * LO.WORD_BYTES,
                            n=s.args["gm"] * s.args["gn"],
                            dnode=1,
                            dst_x=dst[0],
                            dst_y=dst[1],
                            dbuf=0,
                            dflags=DFLAG_SIGNAL,
                            dack_x=agent[0],
                            dack_y=agent[1],
                        )
                    )
                case "drain":
                    span = s.args["gm"] * s.args["gn"]
                    at = (s.args["i"] * stage.body_grid[1] + s.args["j"]) * span
                    base = self._base(compiled, stage, inst, addrs, s.args["result"])
                    words.append(self.isa.drain(addr=base + at * LO.WORD_BYTES, n=span))
                case _:
                    raise LangError(f"no lowering for {s.kind!r} on a cluster")
        return words

    def _vector(self, compiled, stage, inst, addrs: dict) -> list:
        """One program per RUN of chain statements, not one per statement.

        A fused epilogue keeps its own image and closes the run before it, and
        so does a lone reduction, which is the program it has always been.
        """
        words: list[int] = []
        run: list = []

        def flush() -> None:
            if not run:
                return
            if len(run) == 1 and run[0].kind == "reduce":
                words.extend(self._fold(compiled, stage, inst, addrs, run[0]))
            else:
                words.extend(self._band(compiled, stage, inst, addrs, list(run)))
            run.clear()

        for s in inst.stmts:
            if _bandable(s):
                why = _cannot(compiled, [*run, s]) if run else None
                if why is not None:
                    _severable(compiled, run, s, why)
                    flush()
                run.append(s)
                continue
            flush()
            span = s.args["gm"] * s.args["gn"]
            at = (s.args["i"] * stage.body_grid[1] + s.args["j"]) * span
            base = self._base(compiled, stage, inst, addrs, s.args["result"])
            made = _epilogue(compiled, s)
            side = 0
            if made.operand is not None:
                # `ChannelBias` is one word a column group, so tile `j` starts
                # `j * gn` words in.
                leaf = s.args["leaves"][made.operand]
                side = (
                    self._base(compiled, stage, inst, addrs, leaf.name)
                    + s.args["j"] * s.args["gn"] * LO.WORD_BYTES
                )
            words += made.flits(base + at * LO.WORD_BYTES, side)
        flush()
        return words

    def _fold(self, compiled, stage, inst, addrs: dict, stmt) -> list:
        """One reduction on its own: the pass a `RowReduceKernel` has always run.

        A band of exactly one reduce chain lowers here rather than through
        :class:`BandKernel`, so every shipped reduction keeps its bytes.
        """
        kernel = RowReduceKernel(
            stmt.args["fold"], stmt.args["rows"], stmt.args["cols"]
        )
        return kernel.flits(
            self._base(compiled, stage, inst, addrs, stmt.args["source"]),
            self._base(compiled, stage, inst, addrs, stmt.args["result"]),
        )

    def _band(self, compiled, stage, inst, addrs: dict, stmts: list) -> list:
        """One vector program for a run of chain statements.

        Raises :class:`LangError` naming the operands when the run does not fit
        a core even on its own, or for a chain that folds a row with no shape.
        """
        stride = _stride(compiled, stage, stmts[0], _per(compiled))
        _spellable(compiled, stmts)
        try:
            kernel, keys, span = _build(compiled, stmts, stride)
        except VecEmitError as exc:
            raise LangError(_why(compiled, stmts, exc)) from None
        # Each leaf carries its OWN part, so an expression can read one block
        # of a long buffer while writing another.
        srcs = [
            (
                addrs[_const(compiled, leaf)]
                if isinstance(leaf, Const)
                else self._base(compiled, stage, inst, addrs, leaf.name)
                + _at(compiled, leaf, stride) * 2
            )
            for leaf in keys
        ]
        where = [
            self._base(compiled, stage, inst, addrs, s.args["result"])
            + (s.args["part"] * stride + s.args["off"]) * 2
            for s in stmts
        ]
        return kernel.flits(srcs, _shared(compiled, stmts, where, span), span)


def _at(compiled, leaf, stride: int) -> int:
    """Elements into its buffer where this leaf's instance starts.

    The INSTANCE start is what has to land on a group boundary; a spread's own
    `off` is WITHIN the group -- it names which sub-row of each group is read --
    and rides the descriptor's base without moving the walk. Testing their sum
    would refuse exactly the read that folds an odd sub-row in.

    Raises :class:`LangError` when a spread's instance does not start on a group
    boundary: the walk resumes at group starts, so an instance beginning part way
    through one would read every group after it at the wrong offset.
    """
    if _broadcast(compiled, leaf):
        # A BROADCAST is the whole buffer read again for every group, so its
        # base does not move with the instance. Advancing it reads past the end
        # of a buffer that is exactly one period long.
        return leaf.off
    at = leaf.part * stride
    if leaf.period and at % leaf.period:
        raise LangError(
            f"{compiled.name}: {leaf.name!r} is spread over {leaf.period}-element "
            f"groups but this instance starts {at} elements in, which is not a "
            f"group boundary. Raise `part=` to a multiple of the group"
        )
    return at + leaf.off


def _bandable(stmt) -> bool:
    """Whether a band can hold this statement: a chain over DRAM operands.

    A fused epilogue reads a tile the NoC delivered rather than filling one, so
    it does not share a program with a run yet.
    """
    return stmt.kind in ("apply", "reduce") and not stmt.args.get("resident")


def _folds(stmt) -> bool:
    """Whether any chain of this statement reduces a row rather than walking lanes.

    A LIFTED chain counts. `(x + 1) * (x - row_sum(x))` lifts the half holding
    the fold, and reading only the final chain leaves the band at VLMAX folding
    two rows into one answer -- measured at 103% error, reported as success.
    """
    return any(
        kind in REDUCE
        for ops in (stmt.args.get("chain", ()), *stmt.args.get("lifted", ()))
        for kind, _ in ops
    )


def _seeds(stmt) -> bool:
    """Whether this statement's band holds its constants in registers.

    A scalar costs a register and no descriptor, so it moves out of DRAM exactly
    when the descriptor budget cannot afford it. The budget only grows as a run
    takes more statements, so a statement that seeds alone seeds in any run.
    """
    return _budget([stmt]) > DESCRIPTORS


def _spellable(compiled, stmts: list) -> None:
    """Refuse a fold this run carries no row width for.

    A `reduce` names its rows and columns and a fold inside an expression
    carries them from the buffer it folds; a buffer with no row shape carries
    neither, and a band folds exactly `vl` lanes. Raises :class:`LangError`.
    """
    blind = [
        s
        for s in stmts
        if s.kind == "apply" and _folds(s) and s.args.get("cols") is None
    ]
    if not blind:
        return
    raise LangError(
        f"{compiled.name}: the chain writing {blind[0].args['result']!r} folds a "
        f"row inside an expression and nothing it reads has a row width. A band "
        f"folds exactly `vl` lanes, so it would fold two rows into one answer "
        f"and report success. Declare the operand as `(..., ROWS, COLS)`, or "
        f"write the reduction on its own line"
    )


def _severable(compiled, run: list, stmt, why) -> None:
    """Refuse to end a run at a statement that reads what the run wrote.

    The stage guard allowed the dependency because ONE program sequences it;
    cutting the run here puts it across two, and a unit does not wait between
    the programs of one stage. Raises :class:`LangError` carrying `why`.
    """
    held = {_wrote(s) for s in run}
    stale = [
        leaf.name
        for leaf in stmt.args["leaves"]
        if isinstance(leaf, Ref) and _leaf_key(leaf) in held
    ]
    if stale:
        raise LangError(
            f"{compiled.name}: the chain writing {stmt.args['result']!r} reads "
            f"{stale[0]!r}, and ONE vector program cannot hold both ({why}) -- so "
            f"the second would run as its own and read stale bytes. Give one of "
            f"them a `with units(...)` of its own, or narrow the run"
        )


def _build(compiled, stmts: list, stride: int | None = None) -> tuple:
    """``(kernel, operands, span)`` -- one vector program for these statements.

    `stride` is what one instance steps; the kernel's `part=` when the caller is
    only asking whether a run FITS, since that answer does not turn on it.

    Raises :class:`VecEmitError` for a run no vector core runs, and
    :class:`ValueError` for one whose L1 footprint `require_l1` refuses.
    """
    chains, keys, span, consts = _spec(compiled, stmts, stride)
    nin = len(keys)
    walks = [_walk(compiled, leaf) for leaf in keys]
    groups = _regions(stmts)
    if not any(_folds(s) for s in stmts):
        if any(walks):
            return _fit(chains, nin, span, V.VLMAX, consts, walks, groups), keys, span
        made = BandKernel(
            chains, nin, chunks=_chunks(span), consts=consts, groups=groups
        )
        return made, keys, span
    span = _folded(stmts, span)
    kernel = _fit(chains, nin, span, _width(stmts, span), consts, walks, groups)
    return kernel, keys, span


def _one_row(run: list, extents: dict) -> bool:
    """Whether a run that folds names ONE row `VRED` can walk, resolved.

    Answered before any layout exists, so it is the conservative half of
    `_width`: one row shape across the run, a width the fold tree takes, and an
    extent that is a number here rather than after the grid is built.
    """
    shapes = {(s.args.get("rows"), s.args.get("cols")) for s in run if _folds(s)}
    if not shapes:
        return True
    if len(shapes) > 1 or None in shapes.pop():
        return False
    cols = {_extent(s.args["cols"], extents) for s in run if _folds(s)}
    if len(cols) > 1 or None in cols:
        return False
    wide = cols.pop()
    return not wide % V.LANES and wide <= V.VLMAX


def _extent(value, extents: dict) -> int | None:
    """A dim as this compilation's number, or None when nothing resolves it."""
    if isinstance(value, int):
        return value
    try:
        return int(resolve(value, extents))
    except (KeyError, TypeError, ValueError):
        return None


def _folded(stmts: list, span: int) -> int:
    """The elements a FOLDING instance really walks, padding left out.

    An instance steps a padded stride -- a batch element of 64 sits in 1024 --
    and folding padding saturates on values nobody wrote. A fold covers whole
    rows, so the walk stops at the last real one.
    """
    shapes = {(s.args["rows"], s.args["cols"]) for s in stmts if _folds(s)}
    if len(shapes) > 1:
        return span
    rows, cols = shapes.pop()
    if not isinstance(rows, int) or not isinstance(cols, int):
        return span
    return min(span, rows * cols)


def _walk(compiled, leaf):
    """A leaf's periodic read as the emitter's own spec, or None if contiguous.

    The buffer's LENGTH goes with it: a spread over a buffer that is exactly one
    period is re-read from the start for every group, and one over a full-length
    buffer advances. Only the length tells them apart.
    """
    if isinstance(leaf, Const) or not leaf.period:
        return None
    return Spread(
        int(leaf.period), int(leaf.take), _held(compiled, leaf.name) - leaf.off
    )


def _width(stmts: list, span: int) -> int:
    """Elements one step covers when this run folds: the ROW, not VLMAX.

    `VRED` folds exactly VL lanes, so a reducing band steps a row at a time.
    An instance needs WHOLE ROWS, not the whole array: a row is folded by one
    step, so which instance takes it does not change the answer. Raises
    :class:`VecEmitError` when the run names two row shapes, when its span is
    not whole rows, or when the row is not a width the tree folds.
    """
    shapes = {(s.args["rows"], s.args["cols"]) for s in stmts if _folds(s)}
    if len(shapes) > 1:
        raise VecEmitError(
            f"this run folds {sorted(shapes)}; one program walks one row shape"
        )
    rows, cols = shapes.pop()
    if cols % V.LANES or cols > V.VLMAX:
        raise VecEmitError(
            f"a {cols}-wide row: VRED needs a multiple of {V.LANES} at most "
            f"{V.VLMAX}, or the tree carries a partial across steps. A wider row "
            f"folds hierarchically instead -- `kernels.wide` takes it as "
            f"`(-1, {V.VLMAX})` with `rows=K.split(cols)`"
        )
    if span % cols or span > rows * cols:
        raise VecEmitError(
            f"this run covers {span} elements against {rows}x{cols} folded "
            f"rows; a step that is not one whole row folds two into one answer"
        )
    return cols


def _fit(
    chains, nin: int, span: int, vl: int, consts=(), walks=None, groups=None
) -> BandKernel:
    """The widest RUN of this band a core holds, stepping `vl` elements at once.

    A RUN stores its WHOLE batch whatever `nelem` says, so the step count must
    DIVIDE the run or the last one writes over what follows it. Raises the
    emitter's own refusal for a band no step count fits.
    """
    steps = max(1, span // vl)
    why: Exception = VecEmitError(f"a {steps}-step band fits no RUN")
    for tile in range(steps, 0, -1):
        if steps % tile:
            continue
        try:
            return BandKernel(
                chains,
                nin,
                chunks=tile,
                vl=vl,
                consts=consts,
                walks=walks,
                groups=groups,
            )
        except (VecEmitError, ValueError) as exc:
            why = exc
    raise why


def _why(compiled, stmts: list, exc) -> str:
    """The message for a run no vector core runs, in the author's own terms."""
    if any(_folds(s) for s in stmts):
        return (
            f"{compiled.name}: this stage folds rows and cannot be one program "
            f"({exc}). A band steps one ROW at a time when it reduces, so an "
            f"instance has to cover whole rows of one width -- raise `part=` "
            f"until one instance holds the whole array, or split the statements"
        )
    leaves = stmts[0].args["leaves"]
    return (
        f"{compiled.name}: this expression reads {len(leaves)} operands "
        f"({_names(leaves)}); the core has eight descriptors and a chain needs "
        f"two per operand plus two, so split it across statements. ({exc})"
    )


def _leaf_key(leaf) -> tuple:
    """What a leaf reads, in the terms a written result is named by.

    A `Ref` is a buffer, a part, an offset and a period -- the same tuple a
    statement's result carries, which lets one chain recognise another's output.
    A PERIODIC read never matches a write: the register holds what the chain
    computed, and the spread is in the walk that brought the operand in.

    A constant keys on its SYMBOL, not the value `_const` names its array by:
    this only tells one leaf of ONE compilation from another, where a symbol
    resolves the same way every time, and `may_join` asks it with no
    compilation to resolve against.
    """
    if isinstance(leaf, Const):
        return ("const", repr(leaf.value))
    return (leaf.name, leaf.part, leaf.off, leaf.period, leaf.take)


def _wrote(stmt) -> tuple:
    """The region a chain statement writes, keyed as a leaf reading it would be."""
    return (stmt.args["result"], stmt.args["part"], stmt.args["off"], 0, 0)


def _shared(compiled, stmts: list, where: list, span: int) -> list:
    """One DRAM base per drain: the first result of each region.

    A shared drain writes the region's results consecutively from that base, so
    it is only the same bytes when their addresses really are `span` elements
    apart. Raises :class:`LangError` rather than draining over a buffer the
    grouping only assumed was next.
    """
    out: list = []
    for at, g in enumerate(_regions(stmts)):
        if at and g == _regions(stmts)[at - 1]:
            want = out[-1] + (at - _regions(stmts).index(g)) * span * 2
            if where[at] != want:
                raise LangError(
                    f"{compiled.name}: {stmts[at].args['result']!r} is written "
                    f"at {where[at]} and one drain would put it at {want}; "
                    f"results sharing a drain sit {span} elements apart"
                )
            continue
        out.append(where[at])
    return out


def _runs(stmts: list) -> list[list]:
    """Maximal runs of statements a band could hold: the chain kinds, in order."""
    out: list[list] = []
    for s in stmts:
        if not _bandable(s):
            out.append([])
            continue
        if not out:
            out.append([])
        out[-1].append(s)
    return [r for r in out if len(r) > 1]


def _slots(stmts: list) -> set:
    """The leaves a run fills: what an earlier chain wrote is forwarded."""
    slots: set = set()
    made: set = set()
    for s in stmts:
        slots |= {_leaf_key(le) for le in s.args["leaves"]} - made
        made.add(_wrote(s))
    return slots


def _regions(stmts: list) -> list[int]:
    """Drain group per statement: consecutive writes to ONE buffer share one.

    The hand-written flash step fits three operands and FOUR results in eight
    descriptors by putting p, corr, m and l in one buffer at offsets and
    draining it once (`hw/veckernels.py:145`).
    """
    out: list[int] = []
    for at, s in enumerate(stmts):
        same = at and s.args["result"] == stmts[at - 1].args["result"]
        out.append(out[-1] if same else (out[-1] + 1 if out else 0))
    return out


def _budget(stmts: list) -> int:
    """Descriptors ONE program holding these chains would need.

    Two per filled operand, one store between them all, and one drain per
    REGION. A leaf an earlier chain of the run wrote fills nothing.
    """
    return 2 * len(_slots(stmts)) + 1 + len(set(_regions(stmts)))


def _spec(compiled, stmts: list, stride: int | None = None) -> tuple:
    """``(chains, operands, span, consts)`` for a run of chain statements.

    `operands` is the leaves the band fills, in slot order; a leaf an earlier
    chain of this run wrote takes no slot and is forwarded in a register, and so
    does a subexpression the statement lifted. `consts` are the scalars a band
    over the descriptor budget seeds into registers instead of filling.

    Every STATEMENT stores -- whether a later stage reads its result is not
    known here and a wrong elision is a silent stale read. A lifted chain does
    not, because it was never a buffer for anything to read.

    Raises :class:`VecEmitError` for a run whose statements cover different
    lengths, or one reading more operands than the chain encoding can name.
    """
    stride = _per(compiled) if stride is None else stride
    span = _span(compiled, stmts[0], stride)
    seeds = any(_seeds(s) for s in stmts)
    operands: list = []
    consts: list = []
    slots: dict = {}
    made: dict = {}
    chains: list = []
    for s in stmts:
        if _span(compiled, s, stride) != span:
            raise VecEmitError(
                f"{s.args['result']!r} covers {_span(compiled, s, stride)} elements "
                f"against this band's {span}; one program walks one length"
            )
        leaves = s.args["leaves"]
        if len(leaves) > OUT_REG:
            raise VecEmitError(
                f"a chain reading {len(leaves)} operands cannot name them all; "
                f"source {OUT_REG} is the running result"
            )
        remap: dict = {}
        for n, leaf in enumerate(leaves):
            key = _leaf_key(leaf)
            if key in made:
                remap[n] = forward(made[key])
            elif seeds and isinstance(leaf, Const):
                if key not in slots:
                    slots[key] = konst(len(consts))
                    consts.append(_scalar(compiled, leaf))
                remap[n] = slots[key]
            else:
                if key not in slots:
                    slots[key] = len(operands)
                    operands.append(leaf)
                remap[n] = slots[key]
        base = len(chains)
        for ops in s.args.get("lifted", ()):
            chains.append(Chain(_sources(ops, remap, base), store=False))
        chains.append(Chain(_sources(s.args["chain"], remap, base)))
        made[_wrote(s)] = len(chains) - 1
    return chains, operands, span, consts


def _sources(ops, remap: dict, base: int) -> tuple:
    """One chain's ops with every source in the band's own numbering.

    A statement numbers its lifted chains from zero, so they are rebased onto
    the chains the run has already emitted.
    """
    return tuple((kind, [_source(x, remap, base) for x in srcs]) for kind, srcs in ops)


def _source(src: int, remap: dict, base: int) -> int:
    """One chain source: the running result, a lifted chain, or a leaf's slot."""
    if src == OUT_REG:
        return src
    if src >= FWD:
        return forward(base + src - FWD)
    return remap[src]


def _cannot(compiled, stmts: list):
    """Why ONE vector program cannot hold these statements, or None if it can."""
    try:
        _spellable(compiled, stmts)
        _build(compiled, stmts)
    except ValueError as exc:
        return exc
    return None


def _fused(stage) -> bool:
    """Whether this stage hands its tiles over the NoC rather than to memory."""
    return any(
        s.kind == "drain" and s.args.get("node")
        for i in stage.instances
        for s in i.stmts
    )


def _receiver(compiled, stage, at: tuple) -> tuple:
    """Which vector core instance `at` drains into, by `dispatch.deal`."""
    cores = compiled.machine.coords("VC")
    return deal([i.at for i in stage.instances], cores)[at]


def _epilogue(compiled, stmt) -> ResidentEpilogueKernel:
    """The vector program for one fused epilogue statement.

    Raises :class:`LangError` for a leaf that is neither the accumulator nor a
    constant, and for a tile this core cannot hold.
    """
    leaves = stmt.args["leaves"]
    held = [n for n, le in enumerate(leaves) if isinstance(le, Resident)]
    consts: dict = {}
    side: list[int] = []
    for n, leaf in enumerate(leaves):
        if isinstance(leaf, Const):
            consts[n] = (
                float(resolve(leaf.value, compiled.extents))
                if isinstance(leaf.value, Dim)
                else float(leaf.value)
            )
        elif n not in held:
            side.append(n)
    if len(side) > 1:
        raise LangError(
            f"{compiled.name}: a fused epilogue reads "
            f"{sorted(leaves[n].name for n in side)} beside the accumulator, and "
            f"one core fills ONE of them; lift the rest into their own statement"
        )
    # Slot order is the order the drains were sequenced in, which is the order
    # the accumulator held the tiles -- not the order the chain reads them.
    slots = stmt.args.get("residents")
    if slots:
        at = {leaves[n].at: n for n in held}
        held = [at[le.at] for le in slots]
    try:
        return ResidentEpilogueKernel(
            [(kind, list(srcs)) for kind, srcs in stmt.args["chain"]],
            held,
            consts,
            stmt.args["gm"] * stmt.args["gn"],
            operand=side[0] if side else None,
            gm=stmt.args["gm"],
            gn=stmt.args["gn"],
        )
    except (VecEmitError, ValueError) as exc:
        raise CannotFuse(
            f"{compiled.name}: this epilogue does not fit a vector core alongside "
            f"a {stmt.args['gm']}x{stmt.args['gn']} tile ({exc}). Retile, or drain "
            f"into a temp and write the elementwise pass separately"
        ) from None


def _pair(compiled) -> None:
    """Bind each fused drain to the core that will run its epilogue.

    The pairing is injective: a receiver holds one open stream, and two senders'
    bursts interleaved on the wire merge with no arbitration (isa/cluster.md
    §9.2). Raises :class:`LangError` when it cannot be.
    """
    # Asked for only once a stage needs one: a machine with no vector core runs
    # an unfused matmul perfectly well, and `coords` raises rather than answering.
    fused = [(at, s) for at, s in enumerate(compiled.stages) if _fused(s)]
    if not fused:
        return
    cores = compiled.machine.coords("VC")
    for at, stage in fused:
        # SIMULATED, mm_mesh_peer: left at zero the answer goes to the SENDING
        # cluster, which drops it, and no status register ever moves.
        if compiled.machine.agent is None:
            raise CannotFuse(
                f"{compiled.name}: a fused epilogue's transfer is acknowledged to "
                f"the orchestrator and this machine does not say where it is; set "
                f"MachineSpec.agent, or drain into a temp and write the "
                f"elementwise pass separately"
            )
        if len(stage.instances) > len(cores):
            raise CannotFuse(
                f"{compiled.name}: {len(stage.instances)} tiles drain into "
                f"{len(cores)} vector cores, and a core takes one open stream at "
                f"a time. Raise gm/gn until the grid fits, or drain into a temp "
                f"and write the elementwise pass separately"
            )
        after = compiled.stages[at + 1] if at + 1 < len(compiled.stages) else None
        held = [s for s in _stmts(after or stage) if s.args.get("resident")]
        if after is None or after.unit != "VC" or not held:
            raise LangError(
                f"{compiled.name}: a drain names a node but no epilogue follows it "
                f"on a vector core; write the elementwise work on the accumulator"
            )
        stage.acks = {
            i.at: (_receiver(compiled, stage, i.at), _bursts(i.stmts))
            for i in stage.instances
        }
        after.nodes = cores
        for s in held:
            _epilogue(compiled, s)


def _bursts(stmts) -> int:
    """Acknowledgements one instance's node-addressed drains will produce.

    UNVERIFIED against hardware: it reads `dflags` reaching every burst of a
    drain off isa/cluster.md §10, and a NODE_STATUS poll is an equality.
    """
    return sum(
        -(-(s.args["gm"] * s.args["gn"]) // WBURST)
        for s in stmts
        if s.kind == "drain" and s.args.get("node")
    )


def _fits(compiled, span: int, operand: str) -> None:
    """Raise :class:`LangError` unless one chunk of `operand` fits in one bank."""
    if span > BANK_ENTRIES:
        raise LangError(
            f"{compiled.name}: one chunk of {operand!r} is {span} L1 entries and a "
            f"bank holds {BANK_ENTRIES}; lower `nk` or the tile. A fill wider than "
            f"a bank wraps onto the chunk the sweep before it is still reading"
        )


def _in_bank(compiled, operand: str, eoff: int, span: int) -> None:
    """Raise :class:`LangError` unless an operand's own region fits its bank.

    Every distinct operand on one L1 side holds its own entries, so two sweeps
    need room for both -- and a fill past the end lands in the next side.
    """
    if eoff + span > BANK_ENTRIES:
        raise LangError(
            f"{compiled.name}: {operand!r} takes entries {eoff}..{eoff + span} "
            f"of a {BANK_ENTRIES}-entry bank. Operands sharing one instance each "
            f"hold their own region, so lower `nk` or the tile, or put the "
            f"sweeps in separate tilings"
        )


def _placeable(compiled) -> None:
    """Refuse a kernel with a stage this mesh has no unit for.

    Dispatch asks the same question, so without this a kernel compiles on a mesh
    it cannot run on and only says so once it is handed operands. Raises
    :class:`LangError` carrying `MachineSpec.coords`'s message, which names the
    meshes that DO carry the unit.
    """
    for stage in compiled.stages:
        try:
            compiled.machine.coords(stage.unit)
        except ValueError as exc:
            raise LangError(f"{compiled.name} stage {stage.index}: {exc}") from None


def _within(compiled, operand: str, at: int, span: int, off: int = 0) -> None:
    """Raise :class:`LangError` for a fill that reaches past `operand`.

    A tile index off the end is a legal instruction reading whatever the arena
    put next, which is what a `block` wider than the sequence produces. A lane
    offset reaches further still, and a tap past the last entry is the one thing
    the tail padding exists to absorb -- so it is counted in BYTES.
    """
    held = _held(compiled, operand) * 2
    end = at * FP16_ENTRY_BYTES + off + span * FP16_ENTRY_BYTES
    if end > held:
        raise LangError(
            f"{compiled.name}: a fill of {operand!r} reaches byte {end} of {held} "
            f"({end / FP16_ENTRY_BYTES:.2f} entries of "
            f"{held // FP16_ENTRY_BYTES}). The grid asks for more tiles or "
            f"K-chunks than the operand has -- a block wider than the axis it "
            f"walks does this, and so does a tap with no tail padding behind it"
        )


def _conversions(compiled, wanted: dict) -> list:
    """Every relayout the stages imply, as ``(before stage, name, from, to)``.

    Only an internal buffer is converted; a port is the caller's array, so a
    port needed in two orders raises :class:`LangError`.
    """
    out: list = []
    for name, needs in wanted.items():
        if name not in compiled.temps:
            if len(needs) > 1:
                raise LangError(
                    f"{compiled.name}: port {name!r} is needed as "
                    f"{[lay.key for _, lay in needs]}; a kernel cannot reorder an "
                    f"operand in place, so give it a temp of its own"
                )
            continue
        for (_, before), (at, after) in itertools.pairwise(needs):
            out.append((at, name, before, after))
    return out


def _stmts(stage):
    for inst in stage.instances:
        yield from inst.stmts


def _per(compiled) -> int:
    """Elements one vector instance handles: this kernel's `part=`, or PART."""
    return compiled.knobs.get("part", PART)


def _chunks(span: int) -> int:
    """Chunks one pass should walk to cover `span` and no more.

    A RUN stores its WHOLE batch whatever `nelem` says, so a short statement at
    an offset writes over what follows it. Sized to the span this is exact once
    the span is a whole `VLMAX`; at `span >= BATCH_ELEMS` it is the old constant
    and the program is unchanged.
    """
    return max(1, min(MAX_CHUNKS, -(-span // V.VLMAX)))


def _span(compiled, stmt, stride: int) -> int:
    """Elements one instance of an elementwise statement covers.

    Each operand measured from ITS OWN base. Identical to taking the room first
    and stepping once while every leaf shares the statement's part, and correct
    when they do not -- results interleaved per instance index by `part*n + k`.
    """
    at = stmt.args["part"] * stride + stmt.args["off"]
    room = [_held(compiled, stmt.args["result"]) - at]
    room += [
        _reach(compiled, leaf) - leaf.part * stride
        for leaf in stmt.args["leaves"]
        if isinstance(leaf, Ref) and not _broadcast(compiled, leaf)
    ]
    return max(0, min(stride, *room))


def _room(compiled, stmt) -> int:
    """Elements this pass has to cover, PADDING INCLUDED.

    The room in the result and in every operand, since a row-offset view writes
    into a longer buffer from a shorter one and would otherwise walk off the end
    of both.
    """
    room = [_held(compiled, stmt.args["result"]) - stmt.args["off"]]
    room += [
        _reach(compiled, leaf) for leaf in stmt.args["leaves"] if isinstance(leaf, Ref)
    ]
    return min(room)


def _stride(compiled, stage, stmt, per: int) -> int:
    """Elements between one instance of an elementwise pass and the next.

    This kernel's `part=`, unless THE GRID IS SHORT FOR WHAT THE PASS COVERS:
    `Buffer.parts` sizes the extent on the buffer's LOGICAL element count, while
    the pass walks the bytes its layout really holds -- and a drained buffer is
    padded to whole sub-tiles, with the padding BETWEEN instances rather than at
    the end. `linear_add` at `M=2, N=6144` ran 2 instances over a 24-part image
    and left 180,224 of 196,608 elements unwritten, reported as success.

    Rounded up to whole RUNs: a RUN stores its whole batch whatever `nelem`
    says, so a shorter stride is written over by the instance behind it.
    """
    parts = stage.body_grid[-1] if len(stage.body_grid) == 1 else 0
    room = _room(compiled, stmt)
    if parts < 1 or parts * per >= room:
        return per
    want = -(-room // parts)
    return -(-want // BATCH_ELEMS) * BATCH_ELEMS


def _reach(compiled, leaf) -> int:
    """Elements a leaf covers from its offset on, the period counted in.

    A spread delivers a WHOLE group per group start it can reach, so it covers
    more elements than it holds -- which is the point, and is what lets it agree
    with the full-length operand beside it.
    """
    held = _held(compiled, leaf.name) - leaf.off
    if not leaf.period:
        return held
    return max(0, -(-held // leaf.period) * leaf.period)


def _broadcast(compiled, leaf) -> bool:
    """Whether this leaf is its WHOLE buffer, read again for every group.

    A per-channel bias against an `M*N` result: stride 0 in the AGU, so it
    covers any length and is not one of the lengths a pass must agree on. A
    `per_group` spread is NOT this -- its buffer spans the region already, and
    `_reach` rounding it to whole groups is what makes it agree.
    """
    return (
        bool(leaf.period)
        and leaf.period == leaf.take
        and _held(compiled, leaf.name) - leaf.off == leaf.period
    )


def _agree(compiled) -> None:
    """Refuse an elementwise statement whose operands are different lengths.

    One pass reads one length, so a shorter operand is read past its end into
    whatever the arena put next. Raises :class:`LangError` naming both.
    """
    for stage in compiled.stages:
        for s in _stmts(stage):
            if s.kind != "apply" or s.args.get("resident"):
                continue
            # A leaf this statement also WRITES is exempt: carrying state at row
            # offsets of one buffer reaches past what the pass covers, on purpose.
            sizes = {
                (leaf.name, leaf.period): _reach(compiled, leaf)
                for leaf in s.args["leaves"]
                if isinstance(leaf, Ref)
                and leaf.name != s.args["result"]
                and not _broadcast(compiled, leaf)
            }
            if len(set(sizes.values())) > 1:
                spelled = ", ".join(
                    f"{n} holds {v}" for (n, _), v in sorted(sizes.items())
                )
                raise LangError(
                    f"{compiled.name}: the pass writing {s.args['result']!r} reads "
                    f"operands of different lengths ({spelled}). An elementwise "
                    f"pass walks one length, so the shorter one would be read past "
                    f"its end; give them a common shape"
                )


def _reachable(compiled) -> None:
    """Refuse a spread over a buffer that does not hold whole sub-rows.

    `_reach` rounds a periodic operand UP to whole groups, which is only the
    same as one group per group start PRESENT while the buffer divides into
    sub-rows -- and `_held` counts a layout's padding, which need not. A padded
    tail would put the last group start past the end. Raises :class:`LangError`.
    """
    for stage in compiled.stages:
        for s in _stmts(stage):
            for leaf in s.args.get("leaves", ()):
                if not isinstance(leaf, Ref) or not leaf.period:
                    continue
                held = _held(compiled, leaf.name) - leaf.off
                if held % leaf.take:
                    raise LangError(
                        f"{compiled.name}: {leaf.name!r} is spread {leaf.take} "
                        f"elements at a time but holds {held}, which is not whole "
                        f"sub-rows -- its layout pads. The last group start would "
                        f"land past its end; spread an unpadded buffer"
                    )


def _held(compiled, name: str) -> int:
    """Elements ONE BATCH ELEMENT holds, PADDING INCLUDED.

    A drained buffer is padded to whole sub-tiles and the padding sits between
    instances rather than at the end, so an elementwise pass has to cover the
    whole block and not the logical element count.
    """
    layout = compiled.layouts[name]
    stride = getattr(layout, "stride", None)
    if stride is not None:
        return stride // 2
    return layout.nbytes(compiled.block(name)) // 2


def _names(leaves) -> str:
    return ", ".join(
        leaf.name if isinstance(leaf, Ref) else repr(leaf.value) for leaf in leaves
    )


def _const(compiled, leaf: Const) -> str:
    """The DRAM array a folded scalar lives in, named by its VALUE.

    Named by the SYMBOL it arrived as, an extent gave `constName(name='N')` at
    EVERY N while `_scalar` resolved the array per compilation, so a second
    shape read the first shape's array -- measured 0.334 against 8.8e-4 on
    `layernorm` at 64x128 behind 64x64, silent. What the array holds is the
    number, so that is what names it.
    """
    return f"const{_scalar(compiled, leaf)!r}"


def _scalar(compiled, leaf: Const) -> float:
    """A folded constant's value, resolving an extent against this compilation.

    `resolve` ends in `int(value)`, so only an EXTENT goes through it -- log2 e
    would come back as 1.
    """
    if isinstance(leaf.value, Dim):
        return float(resolve(leaf.value, compiled.extents))
    return float(leaf.value)


BACKEND = TpuBackend()


def kernel(fn) -> Kernel:
    """Decorator: a kernel on this machine, on whichever units its stages need."""
    return _traced(fn, backend=BACKEND)
