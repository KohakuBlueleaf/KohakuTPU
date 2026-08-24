"""A vector band as a kernel image the core can run.

Four shapes: an elementwise chain over DRAM operands, a row reduction, an
epilogue over a tile the NoC delivered, and a BAND of several chains as one
program with the intermediates held in registers.

The operand slots are the ISA's FMA shape and not interchangeable: ``VMUL``
reads its second operand from `vb`, ``VADD`` from `vc`, and ``VFMA`` computes
``va*vb + vc``.
"""

from dataclasses import dataclass

from kohakutpu.hw import vector as V
from kohakutpu.hw.ops import OpKind
from kohakutpu.hw.veckernels import (
    CHUNK_WORDS,
    K_NEG1,
    K_ONE,
    K_ZERO,
    L1_SAFE,
    S_VL,
    WORD_ELEMS,
    Asm,
    imem_flits,
    require_l1,
)

#: The register the chain writes, matching `MapKernel`.
OUT_REG = 7
#: Scratch for a two-instruction lowering, matching `BODIES["div"]`.
TMP_REG = 8


class VecEmitError(ValueError):
    """An op with no single-word lowering this emitter is sure of."""


#: Elements one RUN stores at the default chunk count. A pass writes a WHOLE
#: batch whatever `nelem` says, so a tighter allocation is written past its end.
BATCH_ELEMS = 8 * V.VLMAX
BATCH_BYTES = BATCH_ELEMS * 2

#: `vec_agu` walks four dimensions, and `vec_core` faults F_LEN over 256 words.
AGU_DIMS = 4
AGU_WALK = 256

#: A dim reading as unused: bound 1, stride 0, which is `vec_agu`'s reset state.
DIM_UNUSED = (0, 1)


def entry_walk(rows, k, groups, blocks, lanes, kblock) -> list | None:
    """AGU dims placing a flat image in L1-entry order, innermost first.

    Returns ``[(stride, bound), ...]`` in 32-byte words, for a walk that visits
    flat word `i` in order and yields the entry word it belongs at. Returns
    None when the permutation needs more than the four dimensions `vec_agu`
    has, or more words than `vec_core` will walk.
    """
    wpb = kblock // WORD_ELEMS
    nt = rows // (groups * lanes)
    nc = k // (blocks * kblock)
    tile = groups * blocks * lanes * wpb
    # Flat order is row-then-column, and each side splits three ways: a row
    # into (tile, group, lane) and a column into (chunk, block, word).
    dims = [
        (nc * tile, nt),
        (blocks * lanes * wpb, groups),
        (wpb, lanes),
        (tile, nc),
        (lanes * wpb, blocks),
        (1, wpb),
    ]
    walk = _merge([(s, b) for s, b in dims if b > 1])
    total = 1
    for _, bound in walk:
        total *= bound
    if len(walk) > AGU_DIMS or total > AGU_WALK:
        return None
    return walk


def _merge(dims: list) -> list:
    """Adjacent dims as one wherever the outer exactly continues the inner.

    Takes them outermost first and returns them innermost first, which is the
    order `vec_agu` numbers them in.
    """
    out: list = []
    for stride, bound in reversed(dims):
        if out and stride == out[-1][0] * out[-1][1]:
            held, count = out[-1]
            out[-1] = (held, count * bound)
            continue
        out.append((stride, bound))
    return out


#: Unary ops taking `va` alone. No `OpKind.SQRT` row and there cannot be one:
#: `vec_alu.v` has no root, so `L.sqrt_approx` composes one from two of these.
UNARY = {
    OpKind.NEG: "VNEG",
    OpKind.ABS: "VABS",
    OpKind.EXP2: "VEXP2",
    OpKind.LOG2: "VLOG2",
    OpKind.RECIP: "VINV",
    OpKind.RSQRT: "VRSQRT",
}

#: Binary ops and the slot the second operand is read from: `vb` multiplies,
#: `vc` adds, MAX/MIN compare `s1_a` against `vb` -- `vec_alu.v:144-145`.
BINARY = {
    OpKind.MUL: ("VMUL", "vb"),
    OpKind.ADD: ("VADD", "vc"),
    OpKind.SUB: ("VSUB", "vc"),
    OpKind.MAX: ("VMAX", "vb"),
    OpKind.MIN: ("VMIN", "vb"),
}

#: The three that write P0..P3 and NO vector register, so they cannot be lowered
#: as if they returned a value. `vec_lanes.v:503`.
COMPARE = {OpKind.CMPLT: "VCMPLT", OpKind.CMPGT: "VCMPGT", OpKind.CMPEQ: "VCMPEQ"}

#: The predicate register a fused compare-and-select uses. Nothing else in this
#: emitter writes one, so one is enough and it is never live across an op.
PRED_REG = 0


class Select:
    """A comparison and the SELECT reading it, lowered as ONE sequence.

    Not an `OpKind`: it exists only between :func:`fuse_compares` and
    :func:`lower_op`. Sources are ``(a, b, on_true, on_false)``.
    """

    __slots__ = ("cmp",)

    def __init__(self, cmp: OpKind) -> None:
        self.cmp = cmp

    def __eq__(self, other) -> bool:
        return isinstance(other, Select) and other.cmp is self.cmp

    def __hash__(self) -> int:
        return hash((Select, self.cmp))

    def __repr__(self) -> str:
        return f"Select({self.cmp.name})"


def fuse_compares(ops: list) -> list:
    """`ops` with every COMPARE feeding a SELECT rewritten to one :class:`Select`.

    A comparison writes a predicate and no vector register, so the pair is the
    only shape either can be lowered in. Both callers pass sources in the same
    numbering -- `OUT_REG` means "what the previous op produced" and `_reg`
    leaves it alone, `FWD` being 32 -- so this matches identically at each.

    A comparison NOT consumed this way is left in place for `lower_op` to
    refuse, rather than dropped.
    """
    out: list = []
    skip = -1
    for n, (kind, srcs) in enumerate(ops):
        if n == skip:
            continue
        pair = ops[n + 1] if n + 1 < len(ops) else None
        if (
            kind in COMPARE
            and pair is not None
            and pair[0] is OpKind.SELECT
            and list(pair[1][:1]) == [OUT_REG]
        ):
            skip = n + 1
            out.append((Select(kind), [*srcs[:2], *pair[1][1:3]]))
            continue
        out.append((kind, srcs))
    return out


def lower_op(kind: OpKind, srcs: list[int], dst: int, sreg: int = 1) -> list[int]:
    """Instruction words for one elementwise op or row reduction.

    `sreg` is the scalar register a reduction lands in before it is broadcast
    back across the lanes; S1..S3 are reserved for that, and a constant placed
    there is eaten by the first VRED. Raises :class:`VecEmitError` for an op
    whose operand slots this emitter has not seen demonstrated by a kernel that
    has run -- guessing a slot produces a legal word that computes something
    else.
    """
    if kind in UNARY:
        return [V.alu(UNARY[kind], vd=dst, va=srcs[0])]
    if kind in BINARY:
        op, slot = BINARY[kind]
        return [V.alu(op, vd=dst, va=srcs[0], **{slot: srcs[1]})]
    if kind is OpKind.DIV:
        return [
            V.alu("VINV", vd=TMP_REG, va=srcs[1]),
            V.alu("VMUL", vd=dst, va=srcs[0], vb=TMP_REG),
        ]
    if kind is OpKind.FMA:
        return [V.alu("VFMA", vd=dst, va=srcs[0], vb=srcs[1], vc=srcs[2])]
    if kind is OpKind.SELECT:
        # `vec_alu.v:146` is `va = (|s1_c[22:0]) ? s1_a : s1_b`, so the
        # PREDICATE is `vc` and the chosen values are `va` and `vb`.
        return [V.alu("VSEL", vd=dst, va=srcs[1], vb=srcs[2], vc=srcs[0])]
    if isinstance(kind, Select):
        # Two PREDICATED moves, not a VSEL: the compare's result is in P0 and
        # never reaches a vector register (`vec_lanes.v:503`).
        return [
            V.alu(COMPARE[kind.cmp], vd=0, va=srcs[0], vb=srcs[1], pr=PRED_REG),
            V.alu("VMOV", vd=dst, va=srcs[2], pr=PRED_REG, pm=1),
            V.alu("VMOV", vd=dst, va=srcs[3], pr=PRED_REG, pm=2),
        ]
    if kind in COMPARE:
        raise VecEmitError(
            f"{COMPARE[kind]} writes a PREDICATE register (P0..P3) and no vector "
            f"register at all -- `vec_lanes.v:503` gates the vector writeback on "
            f"`!wb_cmp`. Lowering it as if it returned 0.0/1.0 emits a legal word "
            f"whose destination is never written, so a following VSEL reads a "
            f"stale register and silently picks its `vb` arm every time. Reaching "
            f"it needs the `pm`/`pr` fields: compare into P<n>, then predicate the "
            f"instruction that follows. Until that lands, use L.maximum/L.minimum "
            f"for a clamp and L.where over a VALUE condition"
        )
    if kind in REDUCE:
        return reduce_row(kind, srcs[0], dst, sreg)
    if kind is OpKind.SQRT:
        raise VecEmitError(
            "sqrt has no lowering here: this ALU carries OP_INV and OP_RSQRT and "
            "NO square root, so the only single word available is the RECIPROCAL "
            "root -- which is what this table used to return, and is a wrong "
            "number with no fault. A root is COMPOSED, at the DSL surface where "
            "the cost is visible: `L.sqrt_approx` is VRSQRT then VINV at 1.556 "
            "ulp, `L.sqrt_newton` refines it to 1.467 at six words "
            "(scripts/py/sqrt_paths.py). Ask for one of those, or for rsqrt"
        )
    raise VecEmitError(
        f"{kind.value} has no lowering here; the emitter covers "
        f"{sorted(k.value for k in (*UNARY, *BINARY, *REDUCE))} and div"
    )


#: L1 reduction kinds, by the op that asks for them.
REDUCE = {
    OpKind.SUM: "SUM",
    OpKind.RMAX: "MAX",
    OpKind.RMIN: "MIN",
    OpKind.SUMSQ: "SUMSQ",
}


def reduce_row(kind: OpKind, src: int, dst: int, sreg: int = 1) -> list[int]:
    """Reduce one row of `src` into every lane of `dst`, broadcast back.

    The mode is switched back immediately: a chained instruction in TREE that is
    not a VRED faults with F_OPCODE. Raises :class:`VecEmitError` for a kind
    that is not a row reduction.

    `vb` names `src` because SUMSQ's leaf is `va * vb` and squaring is that
    product against ITSELF. It is unread by SUM, MAX and MIN, and every caller
    before bands reduced register 0, so the word is unchanged for all of them.
    """
    if kind not in REDUCE:
        raise VecEmitError(f"{kind.value} is not a row reduction")
    return [
        V.vsetmode(V.TREE),
        V.vred(sreg, src, REDUCE[kind], vb=src),
        V.vsetmode(V.FLAT),
        V.vbcast(dst, sreg),
    ]


def rows_per_pass(rows: int, w: int) -> int:
    """Rows one RUN can hold in L1, as a divisor of `rows`.

    The whole array when it fits, so a shape that already ran keeps the program
    it ran. Raises :class:`VecEmitError` when `rows` has no divisor small enough.
    """
    try:
        require_l1("", 2 * rows * w)
    except ValueError:
        pass
    else:
        return rows
    for tile in range(min(rows, L1_SAFE // (2 * w)), 0, -1):
        if rows % tile == 0:
            return tile
    raise VecEmitError(
        f"a {rows}-row reduction of {w}-word rows splits into no pass that fits "
        f"{L1_SAFE} L1 words; {rows} has no small enough divisor"
    )


class RowReduceKernel:
    """One reduction per row of a `rows x cols` fp16 array.

    `cols` is the vector length, so it must be at most VLMAX and a multiple of
    16; `vec_core` raises F_REDVL otherwise. The result is broadcast across each
    row. Rows past what L1 holds are covered by further RUNs of the same image.
    """

    AD_FILL, AD_DRAIN, AD_IN, AD_OUT = 0, 1, 2, 3

    def __init__(self, kind: OpKind, rows: int, cols: int) -> None:
        if cols % V.LANES or cols > V.VLMAX:
            raise VecEmitError(
                f"a {cols}-wide row: VRED needs a multiple of {V.LANES} at most "
                f"{V.VLMAX}, or the tree carries a partial across passes. A wider "
                f"row folds hierarchically instead -- `kernels.wide` takes it as "
                f"`(-1, {V.VLMAX})` with `rows=K.split(cols)`"
            )
        self.kind, self.rows, self.cols = kind, rows, cols
        self.w = cols // WORD_ELEMS
        self.tile = rows_per_pass(rows, self.w)
        self.passes = rows // self.tile
        rw = self.tile * self.w
        require_l1(f"reduce {rows}x{cols}", 2 * rw)

        asm = Asm()
        pre = [V.vseti(S_VL), cols] + asm.preamble_consts()
        pre += [
            V.vsetvl(S_VL),
            V.vsetmode(V.FLAT),
            V.vfill(self.AD_FILL, 0),
            V.vbar(),
        ]
        body: list[int] = []
        for r in range(self.tile):
            off = r * self.w
            body.append(V.vld(0, self.AD_IN, off))
            body += reduce_row(kind, 0, 2)
            body.append(V.vst(2, self.AD_OUT, off))
        self.image = pre + body + [V.vdrain(self.AD_DRAIN, rw), V.vhalt()]
        self.rw = rw

    def static_descs(self) -> list[int]:
        return [
            V.desc_flit(self.AD_IN, 0, 0),
            V.desc_flit(self.AD_IN, 1, V.dim(1, self.w)),
            V.desc_flit(self.AD_OUT, 0, self.rw),
            V.desc_flit(self.AD_OUT, 1, V.dim(1, self.w)),
            V.desc_flit(self.AD_FILL, 1, V.dim(V.WORD_BYTES, self.rw)),
            V.desc_flit(self.AD_DRAIN, 1, V.dim(V.WORD_BYTES, self.rw)),
        ]

    def flits(self, src: int, dst: int) -> list[int]:
        """Image, descriptors, and one RUN per pass of rows."""
        out = imem_flits(self.image) + self.static_descs()
        step = self.tile * self.cols * 2
        for b in range(self.passes):
            out += [
                V.desc_flit(self.AD_FILL, 0, src + b * step),
                V.desc_flit(self.AD_DRAIN, 0, dst + b * step),
                V.run_flit(0),
            ]
        return out


#: Constants the core seeds into K registers, so they cost no scalar register
#: and no `VSETI`. `vec_core.v` writes 0x3F8000 for 1.0 and 0xBF8000 for -1.0.
KREG = {0.0: K_ZERO, 1.0: K_ONE, -1.0: K_NEG1}

#: Selector-and-slot pairs `_silu` and `_gelu` demonstrate on silicon. Outside
#: this table is a legal word computing something else, so it is refused.
DEMONSTRATED = {("va", V.SRC_K), ("vb", V.SRC_S), ("vb", V.SRC_K), ("vc", V.SRC_K)}


class ResidentEpilogueKernel:
    """A chain over a tile the NoC delivered, and optionally ONE DRAM operand.

    A cluster's `DRAIN` that names this core writes `words` L1 words of FP16
    sub-tiles -- byte-identical to what a memory drain would have written -- so
    the epilogue reads L1 directly.

    `operand` is a leaf index read per channel: a bias, laid out by
    `layout.ChannelBias` as one word per column group, filled once and re-read
    at stride 0. Without it this emits exactly what it always did.
    """

    #: Where slot 0's tile starts. Slot `r` starts `r * span_w` above it.
    PEER_WORD = 0
    #: A vector core carries eight, and every region below claims one.
    DESCRIPTORS = 8
    #: Clear of `OUT_REG` and `TMP_REG`.
    SIDE_REG = 9

    @staticmethod
    def peer_word(words: int, slot: int = 0) -> int:
        """L1 word where slot `slot`'s delivered tile starts.

        The sender needs this before the epilogue exists, so it is arithmetic on
        `words` rather than a field of a built kernel.
        """
        return slot * (-(-words // CHUNK_WORDS) * CHUNK_WORDS)

    def __init__(
        self,
        ops,
        resident,
        consts: dict,
        words: int,
        operand: int | None = None,
        gm: int = 0,
        gn: int = 0,
    ) -> None:
        if words > 256:
            raise VecEmitError(
                f"a {words}-word drain exceeds the 256-entry walk `vec_core` "
                f"raises F_LEN on; use a smaller gm*gn"
            )
        held = (resident,) if isinstance(resident, int) else tuple(resident)
        if not held:
            raise VecEmitError("a resident epilogue reads at least one accumulator")
        if len(held) > OUT_REG:
            raise VecEmitError(
                f"{len(held)} delivered tiles need v0..v{len(held) - 1}, which "
                f"reaches the output register v{OUT_REG}"
            )
        self.ops, self.residents, self.words = list(ops), held, words
        self.resident = held[0]
        self.operand, self.gm, self.gn = operand, gm, gn
        # A VLD walks a whole VLMAX chunk whatever the tail is, so each region
        # is padded to one and only `words` of it are drained.
        self.span_w = -(-words // CHUNK_WORDS) * CHUNK_WORDS
        self.AD_IN = list(range(len(held)))
        self.AD_OUT, self.AD_DRAIN = len(held), len(held) + 1
        self.AD_BFILL, self.AD_BREAD = len(held) + 2, len(held) + 3
        need = self.AD_BREAD + 1 if operand is not None else self.AD_DRAIN + 1
        if need > self.DESCRIPTORS:
            raise VecEmitError(
                f"{len(held)} tiles and {'a' if operand else 'no'} per-channel "
                f"operand need {need} descriptors; a vector core has "
                f"{self.DESCRIPTORS}"
            )
        self.out_word = len(held) * self.span_w
        self.side_word = self.out_word + self.span_w
        side = gn if operand is not None else 0
        require_l1(f"fused epilogue {words}w", self.side_word + side)

        asm = Asm()
        self.sources = {at: ("V", r) for r, at in enumerate(held)}
        if operand is not None:
            if not gn or not gm:
                raise VecEmitError("a per-channel operand needs the tile's gm/gn")
            self.sources[operand] = ("V", self.SIDE_REG)
        for at, value in consts.items():
            reg = KREG.get(float(value))
            self.sources[at] = (
                ("K", reg) if reg is not None else ("S", asm.const(value))
            )

        body: list[int] = []
        if operand is not None:
            body += [V.vfill(self.AD_BFILL, self.side_word), V.vbar()]
        for c in range(self.span_w // CHUNK_WORDS):
            off = c * CHUNK_WORDS
            for r, _ in enumerate(held):
                body.append(V.vld(r, self.AD_IN[r], off))
            if operand is not None:
                # The walk restarts at zero each VLD, so the chunk's own phase
                # into the `gn` words has to be the offset.
                body.append(V.vld(self.SIDE_REG, self.AD_BREAD, off % gn))
            for kind, srcs in self.ops:
                body += self._lower(kind, srcs)
            body.append(V.vst(OUT_REG, self.AD_OUT, off))

        pre = [V.vseti(S_VL), V.VLMAX] + asm.preamble_consts()
        pre += [V.vsetvl(S_VL), V.vsetmode(V.FLAT)]
        self.image = pre + body + [V.vdrain(self.AD_DRAIN, self.out_word), V.vhalt()]

    def _place(self, src: int, slot: str) -> dict:
        """One operand in one slot, as the `V.alu` keywords that name it."""
        kind, reg = ("V", OUT_REG) if src == OUT_REG else self.sources[src]
        sel = {"V": V.SRC_V, "S": V.SRC_S, "K": V.SRC_K}[kind]
        if sel is not V.SRC_V and (slot, sel) not in DEMONSTRATED:
            raise VecEmitError(
                f"a {kind} operand in the {slot} slot has never been demonstrated "
                f"by a kernel that has run; fold the constant or use a temp"
            )
        return {slot: reg, "s" + slot[1]: sel}

    def _lower(self, kind: OpKind, srcs: list[int]) -> list[int]:
        """One elementwise op, with each operand placed in the slot the ISA reads."""
        if kind in UNARY:
            return [V.alu(UNARY[kind], vd=OUT_REG, **self._place(srcs[0], "va"))]
        if kind in BINARY:
            op, slot = BINARY[kind]
            args = {**self._place(srcs[0], "va"), **self._place(srcs[1], slot)}
            return [V.alu(op, vd=OUT_REG, **args)]
        if kind is OpKind.DIV:
            return [
                V.alu("VINV", vd=TMP_REG, **self._place(srcs[1], "va")),
                V.alu("VMUL", vd=OUT_REG, **self._place(srcs[0], "va"), vb=TMP_REG),
            ]
        raise VecEmitError(
            f"{kind.value} has no lowering here; the emitter covers "
            f"{sorted(k.value for k in (*UNARY, *BINARY))} and div"
        )

    def static_descs(self) -> list[int]:
        """The L1 read and write windows, and the length of the drain."""
        out = []
        for r, _ in enumerate(self.residents):
            out += [
                V.desc_flit(self.AD_IN[r], 0, r * self.span_w),
                V.desc_flit(self.AD_IN[r], 1, V.dim(1, CHUNK_WORDS)),
            ]
        out += [
            V.desc_flit(self.AD_OUT, 0, self.out_word),
            V.desc_flit(self.AD_OUT, 1, V.dim(1, CHUNK_WORDS)),
            V.desc_flit(self.AD_DRAIN, 1, V.dim(V.WORD_BYTES, self.words)),
        ]
        if self.operand is None:
            return out
        # The `gm` dimension is at stride 0 and is NOT decoration: `_walk`
        # CLAMPS at its last index, so without it the read sticks at `gn - 1`.
        return out + [
            V.desc_flit(self.AD_BFILL, 1, V.dim(V.WORD_BYTES, self.gn)),
            V.desc_flit(self.AD_BREAD, 0, self.side_word),
            V.desc_flit(self.AD_BREAD, 1, V.dim(1, self.gn)),
            V.desc_flit(self.AD_BREAD, 2, V.dim(0, self.gm)),
        ]

    def flits(self, dst: int, side: int = 0) -> list[int]:
        """Image, descriptors and one RUN. `side` is the per-channel operand's
        address, ignored when this epilogue reads none."""
        out = imem_flits(self.image) + self.static_descs()
        if self.operand is not None:
            out.append(V.desc_flit(self.AD_BFILL, 0, side))
        return out + [V.desc_flit(self.AD_DRAIN, 0, dst), V.run_flit(0)]


class ElementwiseKernel:
    """A chain of elementwise ops over `nin` equally shaped fp16 arrays.

    One batch of every input in L1, followed by the output. `ops` is a list of
    ``(kind, source indices)`` where a source below `nin` is an input and
    :data:`OUT_REG` is the running result.
    """

    def __init__(self, ops, nin: int, chunks: int = 8, drain=None) -> None:
        self.ops, self.nin, self.chunks = list(ops), nin, chunks
        self.batch = chunks * V.VLMAX
        self.bw = self.batch // WORD_ELEMS
        self.drain = self._per_run(drain) if drain else None
        require_l1(f"elementwise x{chunks}", (nin + 1) * self.bw)
        if self.bw > 256:
            raise VecEmitError(f"a {self.bw}-word fill exceeds the 256 limit")

        self.ad_fill = list(range(nin))
        self.ad_ld = [nin + i for i in range(nin)]
        self.ad_st = 2 * nin
        self.ad_drain = 2 * nin + 1
        if self.ad_drain > 7:
            raise VecEmitError(f"needs {self.ad_drain + 1} descriptors, have 8")

        asm = Asm()
        body: list[int] = []
        for j in range(chunks):
            off = j * CHUNK_WORDS
            for i in range(nin):
                body.append(V.vld(i, self.ad_ld[i], off))
            for kind, srcs in fuse_compares(self.ops):
                body += lower_op(kind, srcs, OUT_REG)
            body.append(V.vst(OUT_REG, self.ad_st, off))

        pre = [V.vseti(S_VL), V.VLMAX]
        pre += asm.preamble_consts()
        pre += [V.vsetvl(S_VL), V.vsetmode(V.FLAT)]
        pre += [V.vfill(self.ad_fill[i], i * self.bw) for i in range(nin)]
        pre += [V.vbar()]
        self.image = pre + body + [V.vdrain(self.ad_drain, nin * self.bw), V.vhalt()]

    def static_descs(self) -> list[int]:
        """Descriptors that never move: the L1 read and write windows."""
        out = []
        for i in range(self.nin):
            out.append(V.desc_flit(self.ad_ld[i], 0, i * self.bw))
            out.append(V.desc_flit(self.ad_ld[i], 1, V.dim(1, CHUNK_WORDS)))
        out.append(V.desc_flit(self.ad_st, 0, self.nin * self.bw))
        out.append(V.desc_flit(self.ad_st, 1, V.dim(1, CHUNK_WORDS)))
        for i in range(self.nin):
            out.append(V.desc_flit(self.ad_fill[i], 1, V.dim(V.WORD_BYTES, self.bw)))
        if self.drain is None:
            out.append(V.desc_flit(self.ad_drain, 1, V.dim(V.WORD_BYTES, self.bw)))
            return out
        for n, (stride, bound) in enumerate(self.drain):
            out.append(
                V.desc_flit(self.ad_drain, n + 1, V.dim(stride * V.WORD_BYTES, bound))
            )
        return out

    def _per_run(self, dims: list) -> list:
        """`dims` narrowed to the words one RUN drains, outermost bound clamped.

        Raises :class:`VecEmitError` when a batch does not land on a whole
        number of the outermost dimension's steps, which would split one
        permutation across two RUNs and interleave them.
        """
        inner = 1
        for _, bound in dims[:-1]:
            inner *= bound
        stride, bound = dims[-1]
        if stride != inner or self.bw % inner or self.bw // inner > bound:
            raise VecEmitError(
                f"a {self.bw}-word batch does not divide this walk ({dims}); the "
                f"outermost dimension steps {stride} words over {inner}-word "
                f"blocks, so a RUN would drain half of one permutation"
            )
        return [*dims[:-1], (stride, self.bw // inner)]

    def batch_flits(self, srcs, dst: int, b: int) -> list[int]:
        """Move every DRAM base to batch `b` and run one pass."""
        step = self.batch * 2
        out = [
            V.desc_flit(self.ad_fill[i], 0, srcs[i] + b * step) for i in range(self.nin)
        ]
        out.append(V.desc_flit(self.ad_drain, 0, dst + b * step))
        out.append(V.run_flit(0))
        return out

    def flits(self, srcs, dst: int, nelem: int) -> list[int]:
        """The whole program: image, descriptors, then one RUN per batch."""
        nb = -(-nelem // self.batch)
        out = imem_flits(self.image) + self.static_descs()
        for b in range(nb):
            out += self.batch_flits(srcs, dst, b)
        return out


#: A band source naming another chain's result, which stays in a register. Clear
#: of every register number and every operand slot, so a source is unambiguous.
FWD = 32

#: A band source naming a folded scalar the preamble broadcasts into a vector
#: register. A band refuses at eleven held results, so no chain index reaches it.
KONST = 1024

#: Scalar registers a VRED may land in. S0 is VL and constants start at S4.
RED_SREGS = (1, 2, 3)

#: Descriptors the core has, and the registers a band may hold a result in.
DESCRIPTORS = 8


def _spans(groups, nout: int) -> list[tuple[int, int]]:
    """``(first result, count)`` per DRAIN, from a group id per result.

    Results sharing a group share a drain, so their L1 regions must be
    consecutive -- they are laid out in chain order. Raises
    :class:`VecEmitError` for a group that is not a contiguous run, which would
    drain a region belonging to another buffer.
    """
    if groups is None:
        return [(k, 1) for k in range(nout)]
    if len(groups) != nout:
        raise VecEmitError(f"{len(groups)} groups for {nout} results")
    out: list[list[int]] = []
    for k, g in enumerate(groups):
        if out and groups[k - 1] == g:
            out[-1][1] += 1
        else:
            if any(g == groups[j] for j in range(k - 1)):
                raise VecEmitError(
                    f"result {k} rejoins drain group {g!r} after another; a "
                    f"shared drain covers one consecutive run of L1 regions"
                )
            out.append([k, 1])
    return [(at, n) for at, n in out]


REGISTERS = 16

#: Instruction words one image may hold: `imem_flit` addresses nine bits. A
#: MACHINE may hold fewer, which nothing here can see.
IMEM_WORDS = 512


def forward(k: int) -> int:
    """The band source index naming chain `k`'s result."""
    return FWD + k


def konst(k: int) -> int:
    """The band source index naming folded constant `k`."""
    return KONST + k


@dataclass(frozen=True)
class Chain:
    """One expression of a band: its ops, and whether its result goes back through MAG.

    `ops` is ``(kind, sources)`` as `chain_of` produces. A source below the
    band's `nin` is a filled operand, :data:`OUT_REG` is the running result, and
    :func:`forward` names an earlier chain's result.
    """

    ops: tuple
    store: bool = True


@dataclass(frozen=True)
class Spread:
    """An operand read as ONE sub-row per group, repeated across the group.

    `vec_agu.v` calls this a broadcast and spells it stride 0: `period` is the
    group in elements, `take` is the sub-row at its start, and the repeat is a
    dimension between them. The chain never sees it -- a pass reads the lanes
    the walk delivered -- so the operand is periodic in the ADDRESS while the
    arithmetic stays translation-invariant.
    """

    period: int
    take: int
    #: Elements the buffer really holds. 0 means "at least as long as the
    #: region", which is what a `per_group` read of a full-length buffer is.
    held: int = 0

    @property
    def repeat(self) -> int:
        """Sub-rows one group's own sub-row is read for."""
        return self.period // self.take

    @property
    def wraps(self) -> bool:
        """Whether the buffer is ONE period, so every group re-reads the same bytes.

        A `repeated()` broadcast is this; a `per_group` read of a full-length
        buffer is not. Stepping a broadcast's base per group walks off the end
        of it -- MEASURED as 1,152 of 2,048 elements wrong, reported as success.
        """
        return bool(self.held) and self.held <= self.period

    def dims(self, batch: int) -> list:
        """``(stride, bound)`` in bytes, innermost first, for one RUN's fill.

        Raises :class:`VecEmitError` unless the batch and the group nest. A RUN
        covering part of a group would start its walk part way through a period,
        and every group after it would be off by the remainder.
        """
        if self.take % WORD_ELEMS or self.period % self.take:
            raise VecEmitError(
                f"a spread takes {self.take} elements of every {self.period}; the "
                f"sub-row must be whole {WORD_ELEMS}-element words and divide the "
                f"group, or the walk cannot be a bound"
            )
        take_w = self.take // WORD_ELEMS
        if batch <= self.period:
            if self.period % batch or batch % self.take:
                raise VecEmitError(
                    f"a {batch}-element RUN inside a {self.period}-element group "
                    f"of {self.take}: the RUN must divide the group and hold whole "
                    f"sub-rows, or its walk starts part way through a period"
                )
            return [(V.WORD_BYTES, take_w), (0, batch // self.take)]
        if batch % self.period:
            raise VecEmitError(
                f"a {batch}-element RUN over {self.period}-element groups: the RUN "
                f"must hold whole groups, or the group after it starts mid-walk"
            )
        return [
            (V.WORD_BYTES, take_w),
            (0, self.repeat),
            (
                0 if self.wraps else take_w * self.repeat * V.WORD_BYTES,
                batch // self.period,
            ),
        ]

    def at(self, batch: int, run: int) -> int:
        """Elements into the operand that RUN `run` reads from.

        The GROUP START of the group that RUN holds, which is `run * batch` only
        when a RUN covers whole groups; inside a group it stands still. A buffer
        that IS one period stands still always -- see :attr:`wraps`.
        """
        if self.wraps:
            return 0
        return (run * batch // self.period) * self.period


class BandKernel:
    """Several chains as ONE vector program, the intermediates in registers.

    `vl` is the elements one step covers -- VLMAX for elementwise work, and the
    ROW WIDTH when a chain reduces, since VRED folds exactly VL lanes.

    `consts` are scalars the preamble broadcasts into registers rather than
    filling from DRAM, named by :func:`konst`; they cost a register each and no
    descriptor, which is what buys a band its fourth and fifth operand. `walks`
    is one :class:`Spread` per operand, or None for a contiguous read.

    Raises :class:`VecEmitError` for a band needing more than the eight
    descriptors, more L1 than `require_l1` allows, more registers than the file
    has, a forward of a chain that has not run, or a walk the RUN cuts.
    """

    def __init__(
        self,
        chains,
        nin: int,
        chunks: int = 8,
        vl: int = V.VLMAX,
        consts=(),
        walks=None,
        groups=None,
    ) -> None:
        self.chains, self.nin, self.chunks, self.vl = list(chains), nin, chunks, vl
        self.consts = [float(c) for c in consts]
        self.walks = list(walks) if walks else [None] * nin
        if len(self.walks) != nin:
            raise VecEmitError(
                f"a band reading {nin} operands got {len(self.walks)} walks; one "
                f"per operand, None for a contiguous read"
            )
        self.batch = chunks * vl
        self.step_words = vl // WORD_ELEMS
        self.bw = self.batch // WORD_ELEMS
        self.nout = sum(1 for c in self.chains if c.store)
        # Priced here rather than at emission, so `_fit` sees the refusal while
        # it can still try a narrower RUN.
        for i in range(nin):
            self._fill_dims(i)
        if nin > OUT_REG:
            raise VecEmitError(
                f"a band reads {nin} operands and register {OUT_REG} is the "
                f"running result; the descriptor budget caps this well below it"
            )
        # Results sharing one DRAM region share one DRAIN, which is how the
        # hand-written flash step fits 3 operands and FOUR results in eight.
        self.spans = _spans(groups, self.nout)
        # ONE store descriptor however many results there are: the L1 output
        # regions are consecutive, so the VST offset reaches them all.
        need = 2 * nin + 1 + len(self.spans)
        if need > DESCRIPTORS:
            raise VecEmitError(
                f"a band of {len(self.chains)} chains reading {nin} operands and "
                f"writing {self.nout} in {len(self.spans)} regions needs {need} "
                f"descriptors, have {DESCRIPTORS}"
            )
        require_l1(f"band x{chunks}", (nin + self.nout) * self.bw)
        if self.bw > AGU_WALK:
            raise VecEmitError(f"a {self.bw}-word fill exceeds the {AGU_WALK} limit")

        self.ad_fill = list(range(nin))
        self.ad_ld = [nin + i for i in range(nin)]
        self.ad_st = 2 * nin
        self.ad_drain = [2 * nin + 1 + g for g in range(len(self.spans))]
        self.held, self.seeded = self._allocate()

        asm = Asm()
        sregs = [asm.const(value) for value in self.consts]
        body: list[int] = []
        for step in range(chunks):
            off = step * self.step_words
            for i in range(nin):
                body.append(V.vld(i, self.ad_ld[i], off))
            body += self._body(off)
        pre = [V.vseti(S_VL), vl]
        pre += asm.preamble_consts()
        pre += [V.vsetvl(S_VL), V.vsetmode(V.FLAT)]
        # After VSETVL: a broadcast writes the lanes VL names.
        pre += [V.vbcast(self.seeded[k], s) for k, s in enumerate(sregs)]
        pre += [V.vfill(self.ad_fill[i], i * self.bw) for i in range(nin)]
        pre += [V.vbar()]
        tail = [
            V.vdrain(self.ad_drain[g], (nin + at) * self.bw)
            for g, (at, _) in enumerate(self.spans)
        ]
        self.image = pre + body + tail + [V.vhalt()]
        if len(self.image) > IMEM_WORDS:
            raise VecEmitError(
                f"a {len(self.image)}-word image over {IMEM_WORDS} instruction "
                f"words; a band unrolls every step, so take fewer chunks"
            )

    def _allocate(self) -> tuple[dict, dict]:
        """``(held, seeded)`` -- a register per forwarded chain and per constant.

        Keyed by chain index and by constant index. Nothing is reused: the pool
        is eleven deep at the widest band the descriptor budget admits, so a
        liveness walk would buy nothing and a wrong reuse is a silent wrong
        answer. Raises :class:`VecEmitError` for a backward forward, or for more
        live values than the file holds.
        """
        wanted: set = set()
        for k, chain in enumerate(self.chains):
            for _, srcs in chain.ops:
                for s in srcs:
                    if not FWD <= s < KONST:
                        continue
                    if s - FWD >= k:
                        raise VecEmitError(
                            f"chain {k} forwards chain {s - FWD}, which has not "
                            f"run; a band hands results forward, never back"
                        )
                    wanted.add(s - FWD)
        pool = [
            r for r in range(REGISTERS) if r >= self.nin and r not in (OUT_REG, TMP_REG)
        ]
        if len(wanted) + len(self.consts) > len(pool):
            raise VecEmitError(
                f"{len(wanted)} forwarded results and {len(self.consts)} folded "
                f"constants against {len(pool)} free registers; split the band"
            )
        held = dict(zip(sorted(wanted), pool, strict=False))
        rest = pool[len(wanted) : len(wanted) + len(self.consts)]
        return held, dict(enumerate(rest))

    def _reg(self, src: int) -> int:
        """A band source index as the register that holds it."""
        if src >= KONST:
            return self.seeded[src - KONST]
        return self.held[src - FWD] if src >= FWD else src

    def _body(self, off: int) -> list[int]:
        """Every chain's ops for one step, and the stores they feed.

        The VRED scalar cycles S1..S3 although `reduce_row` broadcasts it
        immediately, so no two are ever live at once.
        """
        out: list[int] = []
        reds, stored = 0, 0
        for k, chain in enumerate(self.chains):
            dst = self.held.get(k, OUT_REG)
            fused = fuse_compares(chain.ops)
            for n, (kind, srcs) in enumerate(fused):
                last = n == len(fused) - 1
                out += lower_op(
                    kind,
                    [self._reg(s) for s in srcs],
                    dst if last else OUT_REG,
                    RED_SREGS[reds % len(RED_SREGS)],
                )
                reds += kind in REDUCE
            if chain.store:
                out.append(V.vst(dst, self.ad_st, off + stored * self.bw))
                stored += 1
        return out

    def static_descs(self) -> list[int]:
        """The L1 read and write windows, and the length of each transfer."""
        out = []
        for i in range(self.nin):
            out.append(V.desc_flit(self.ad_ld[i], 0, i * self.bw))
            out.append(V.desc_flit(self.ad_ld[i], 1, V.dim(1, self.step_words)))
        out.append(V.desc_flit(self.ad_st, 0, self.nin * self.bw))
        out.append(V.desc_flit(self.ad_st, 1, V.dim(1, self.step_words)))
        for i in range(self.nin):
            for n, (stride, bound) in enumerate(self._fill_dims(i)):
                out.append(V.desc_flit(self.ad_fill[i], n + 1, V.dim(stride, bound)))
        for g, (_, n) in enumerate(self.spans):
            out.append(
                V.desc_flit(self.ad_drain[g], 1, V.dim(V.WORD_BYTES, n * self.bw))
            )
        return out

    def _fill_dims(self, i: int) -> list:
        """Operand `i`'s DRAM walk, innermost first. One dim unless it spreads."""
        walk = self.walks[i]
        if walk is None:
            return [(V.WORD_BYTES, self.bw)]
        return walk.dims(self.batch)

    def _restore(self) -> list[int]:
        """Flits returning every dim a spread set to `vec_agu`'s reset state.

        A descriptor is CORE STATE and outlives the program that wrote it, so a
        band leaving a second dim behind changes the walk of the next kernel to
        reuse that descriptor -- which sets only the first.
        """
        out = []
        for i in range(self.nin):
            for n in range(1, len(self._fill_dims(i))):
                out.append(V.desc_flit(self.ad_fill[i], n + 1, V.dim(*DIM_UNUSED)))
        return out

    def flits(self, srcs, dsts, nelem: int) -> list[int]:
        """The whole program: image, descriptors, then one RUN per batch.

        Raises :class:`VecEmitError` unless one address arrives per filled
        operand and per drained result.
        """
        if len(srcs) != self.nin or len(dsts) != len(self.spans):
            raise VecEmitError(
                f"this band fills {self.nin} operands and drains "
                f"{len(self.spans)} regions; got {len(srcs)} and {len(dsts)}"
            )
        out = imem_flits(self.image) + self.static_descs()
        step = self.batch * 2
        for b in range(-(-nelem // self.batch)):
            for i in range(self.nin):
                walk = self.walks[i]
                at = b * step if walk is None else walk.at(self.batch, b) * 2
                out.append(V.desc_flit(self.ad_fill[i], 0, srcs[i] + at))
            for g in range(len(self.spans)):
                out.append(V.desc_flit(self.ad_drain[g], 0, dsts[g] + b * step))
            out.append(V.run_flit(0))
        return out + self._restore()
