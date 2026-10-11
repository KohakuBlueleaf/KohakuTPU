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


def lower_word(
    kind, ops: list, dst: int, tmp: int = TMP_REG, pr: int = PRED_REG
) -> list:
    """Instruction words for one op whose operands are ``(selector, register)``.

    A scalar or constant rides in the operand slot itself (`vec_lanes.v:333`
    reads S or K in any of the three); a commuting op written scalar-first
    swaps it into the second slot. Raises what :func:`lower_op` raises for an
    op this cannot place.
    """

    def put(slot, opnd):
        sel, reg = opnd
        return {slot: reg, "s" + slot[1]: sel}

    if kind in UNARY:
        return [V.alu(UNARY[kind], vd=dst, **put("va", ops[0]))]
    if kind in BINARY:
        a, b = ops
        if kind in COMMUTES and a[0] != V.SRC_V and b[0] == V.SRC_V:
            a, b = b, a
        op, slot = BINARY[kind]
        return [V.alu(op, vd=dst, **put("va", a), **put(slot, b))]
    if kind is OpKind.DIV:
        return [
            V.alu("VINV", vd=tmp, **put("va", ops[1])),
            V.alu("VMUL", vd=dst, **put("va", ops[0]), vb=tmp),
        ]
    if kind is OpKind.FMA:
        return [
            V.alu(
                "VFMA",
                vd=dst,
                **put("va", ops[0]),
                **put("vb", ops[1]),
                **put("vc", ops[2]),
            )
        ]
    if kind is OpKind.SELECT:
        return [
            V.alu(
                "VSEL",
                vd=dst,
                **put("va", ops[1]),
                **put("vb", ops[2]),
                **put("vc", ops[0]),
            )
        ]
    if isinstance(kind, Select):
        # vd 0 is a register only VLD writes, so the compare's WAW check never stalls.
        return [
            V.alu(
                COMPARE[kind.cmp], vd=0, **put("va", ops[0]), **put("vb", ops[1]), pr=pr
            ),
            V.alu("VMOV", vd=dst, **put("va", ops[2]), pr=pr, pm=1),
            V.alu("VMOV", vd=dst, **put("va", ops[3]), pr=pr, pm=2),
        ]
    return lower_op(kind, [reg for _, reg in ops], dst)


#: Ops whose operands commute, so a scalar written first moves to the second slot.
COMMUTES = {OpKind.MUL, OpKind.ADD, OpKind.MAX, OpKind.MIN}

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


class ResidentEpilogueKernel:
    """A chain over a tile the NoC delivered, and optionally ONE DRAM operand.

    A cluster's `DRAIN` that names this core writes `words` L1 words of FP16
    sub-tiles -- byte-identical to what a memory drain would have written -- so
    the epilogue reads L1 directly. Chunks run `group` at a time on disjoint
    registers, op-major, as a band's steps do, and the result is stored over
    the first tile.

    `operand` is a leaf index read per channel: a bias, laid out by
    `layout.ChannelBias` as one word per column group, filled once and re-read
    at stride 0.

    `buffers` regions, each holding every delivered tile, so a core runs one
    tile's epilogue while the next tile lands in the other region. Every L1
    access but the drain goes through `AD_L1`, whose base is the region; the
    drain's L1 word is an immediate, so the image differs per region in that
    one word.
    """

    #: Where slot 0's tile starts. Slot `r` starts `r * span_w` above it.
    PEER_WORD = 0
    #: A vector core carries eight: the L1 window, the drain, a bias's two.
    DESCRIPTORS = 8
    AD_DRAIN, AD_BFILL, AD_BREAD = 1, 2, 3

    @staticmethod
    def peer_word(words: int, slot: int = 0, buffer: int = 0, held: int = 1) -> int:
        """L1 word where slot `slot`'s delivered tile starts in region `buffer`,
        a region holding `held` tiles.

        The sender needs this before the epilogue exists, so it is arithmetic on
        `words` rather than a field of a built kernel.
        """
        span_w = -(-words // CHUNK_WORDS) * CHUNK_WORDS
        return (buffer * held + slot) * span_w

    def __init__(
        self,
        ops,
        resident,
        consts: dict,
        words: int,
        operand: int | None = None,
        gm: int = 0,
        gn: int = 0,
        buffers: int = 1,
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
        self.buffers = buffers
        self.region_w = len(held) * self.span_w
        self.side_word = buffers * self.region_w
        side = gn if operand is not None else 0
        require_l1(f"fused epilogue {words}w x{buffers}", self.side_word + side)
        if operand is not None and (not gn or not gm):
            raise VecEmitError("a per-channel operand needs the tile's gm/gn")

        div = any(kind is OpKind.DIV for kind, _ in self.ops)
        per = len(held) + (operand is not None) + 1 + div
        chunks = self.span_w // CHUNK_WORDS
        self.group = group = min(chunks, REGISTERS // per)
        regs = iter(range(REGISTERS))
        self.r_src = [
            {
                at: next(regs)
                for at in (*held, *([operand] if operand is not None else []))
            }
            for _ in range(group)
        ]
        self.r_run = [next(regs) for _ in range(group)]
        self.r_tmp = [next(regs) if div else TMP_REG for _ in range(group)]
        distinct = list(
            dict.fromkeys(float(v) for v in consts.values() if float(v) not in KREG)
        )
        # Parked at numbers only VLD writes: see `BandKernel._allocate`.
        loaded = group * (per - 1 - div)
        quiet = sorted(CONST_SREGS, key=lambda s: s >= loaded)
        self.sconst = dict(zip(distinct, quiet, strict=False))
        if len(distinct) > len(CONST_SREGS):
            raise VecEmitError(f"{len(distinct)} distinct constants in one epilogue")
        self.consts = {at: float(v) for at, v in consts.items()}

        body: list[int] = []
        if operand is not None:
            body += [V.vfill(self.AD_BFILL, self.side_word), V.vbar()]
        for c0 in range(0, chunks, group):
            steps = list(range(c0, min(c0 + group, chunks)))
            for g, c in enumerate(steps):
                off = c * CHUNK_WORDS
                for r, at in enumerate(held):
                    body.append(V.vld(self.r_src[g][at], AD_L1, r * self.span_w + off))
                if operand is not None:
                    # The walk restarts at zero each VLD, so the chunk's own phase
                    # into the `gn` words has to be the offset.
                    body.append(V.vld(self.r_src[g][operand], self.AD_BREAD, off % gn))
            for kind, srcs in self.ops:
                words = [
                    lower_word(
                        kind,
                        [self._operand(x, g) for x in srcs],
                        self.r_run[g],
                        self.r_tmp[g],
                    )
                    for g in range(len(steps))
                ]
                for w in range(len(words[0])):
                    body += [step[w] for step in words]
            for g, c in enumerate(steps):
                body.append(V.vst(self.r_run[g], AD_L1, c * CHUNK_WORDS))

        pre = [V.vseti(S_VL), V.VLMAX]
        for value, reg in self.sconst.items():
            pre += [V.vseti(reg), V.e8m15(value)]
        pre += [V.vsetvl(S_VL), V.vsetmode(V.FLAT)]
        self.images = [
            pre + body + [V.vdrain(self.AD_DRAIN, b * self.region_w), V.vhalt()]
            for b in range(buffers)
        ]
        self.image = self.images[0]

    def _operand(self, src: int, g: int) -> tuple[int, int]:
        """A chain source as ``(selector, register)`` for step `g` of a group."""
        if src == OUT_REG:
            return V.SRC_V, self.r_run[g]
        if src in self.consts:
            value = self.consts[src]
            if value in KREG:
                return V.SRC_K, KREG[value]
            return V.SRC_S, self.sconst[value]
        if src not in self.r_src[g]:
            raise VecEmitError(
                f"source {src} is neither a delivered tile nor a constant"
            )
        return V.SRC_V, self.r_src[g][src]

    def static_descs(self, buffer: int = 0) -> list[int]:
        """The L1 window over region `buffer`, the drain, and a bias's fill and
        stride-0 read, all dims."""
        out = [V.desc_flit(AD_L1, 0, buffer * self.region_w)]
        out += _dims(AD_L1, [(1, CHUNK_WORDS)])
        out += _dims(self.AD_DRAIN, [(V.WORD_BYTES, self.words)])
        if self.operand is None:
            return out
        # The `gm` dimension is at stride 0 and is NOT decoration: `_walk`
        # CLAMPS at its last index, so without it the read sticks at `gn - 1`.
        return (
            out
            + _dims(self.AD_BFILL, [(V.WORD_BYTES, self.gn)])
            + [V.desc_flit(self.AD_BREAD, 0, self.side_word)]
            + _dims(self.AD_BREAD, [(1, self.gn), (0, self.gm)])
        )

    def flits(self, dst: int, side: int = 0, buffer: int = 0) -> list[int]:
        """Image, descriptors and one RUN over region `buffer`. `side` is the
        per-channel operand's address, ignored when this epilogue reads none."""
        out = imem_flits(self.images[buffer]) + self.static_descs(buffer)
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


def _dims(ad: int, dims: list) -> list[int]:
    """DESC words setting all `AGU_DIMS` dims of `ad`, the unused ones reset."""
    full = list(dims) + [DIM_UNUSED] * (AGU_DIMS - len(dims))
    return [V.desc_flit(ad, n + 1, V.dim(s, b)) for n, (s, b) in enumerate(full)]


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


#: Scalar registers a folded constant may occupy: S0 is VL, S1..S3 take VRED
#: results. A constant there costs one VSETI and no vector register.
CONST_SREGS = tuple(range(4, 16))

#: The descriptor every VLD and VST addresses L1 through, at an absolute offset.
AD_L1 = 0

#: The image a band prefers to stay under, so other resident programs keep IMEM.
BAND_IMAGE = 320

#: Core cycles beyond the beats, MEASURED by state on vec_replay_tb (silu and
#: softmax, VC_STATE_PROF): an ALU word's S_EXEC, a VLD's and a VST's walk
#: overhead, `pipe_empty`'s 4*ALAT line after an ALU word, and a VRED's
#: S_RDRAIN + S_RWAIT (~140 at VL 128).
ALU_GAP, LD_GAP, ST_GAP, LANE_DRAIN, RED_WAIT = 2, 3, 2, 60, 132


class BandKernel:
    """Several chains as ONE vector program, the intermediates in registers.

    `vl` is the elements one STEP covers: VLMAX, or the ROW WIDTH when a chain
    reduces. A RUN is `halves` batches of `chunks` steps; half h+1 fills while
    half h computes. An ALU result lands 14 cycles after issue but `pipe_empty`
    stays low for the 4*ALAT metadata line (`vec_lanes.v:565`), so a VLD/VST
    after an ALU op waits ~60: steps run `group` at a time on disjoint
    registers, op-major, and one drain serves a group. Constants are S/K
    operands; one descriptor addresses L1 for every VLD/VST; a result with an
    input slot of its own is stored over it (`inplace`).

    Raises :class:`VecEmitError` for a band over the descriptors, L1 or
    registers, a forward of a chain that has not run, or a walk the batch cuts.
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
        halves: int = 1,
    ) -> None:
        self.chains, self.nin, self.chunks, self.vl = list(chains), nin, chunks, vl
        self.halves = halves
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
                f"a band reads {nin} operands and source {OUT_REG} is the running "
                f"result; the descriptor budget caps this well below it"
            )
        # Results sharing one DRAM region share one DRAIN, which is how the
        # hand-written flash step fits 3 operands and FOUR results in eight.
        self.spans = _spans(groups, self.nout)
        self.inplace = self.nout <= nin and all(n == 1 for _, n in self.spans)
        self.slots = nin if self.inplace else nin + self.nout
        per = nin + len(self.spans)
        need = 1 + halves * per
        if need > DESCRIPTORS:
            raise VecEmitError(
                f"a band of {len(self.chains)} chains reading {nin} operands and "
                f"writing {self.nout} in {len(self.spans)} regions over {halves} "
                f"halves needs {need} descriptors, have {DESCRIPTORS}"
            )
        require_l1(f"band x{chunks}x{halves}", halves * self.slots * self.bw)
        if self.bw > AGU_WALK:
            raise VecEmitError(f"a {self.bw}-word fill exceeds the {AGU_WALK} limit")

        self.ad_fill = [[1 + h * per + i for i in range(nin)] for h in range(halves)]
        self.ad_drain = [
            [1 + h * per + nin + g for g in range(len(self.spans))]
            for h in range(halves)
        ]
        self._allocate()

        pre = [V.vseti(S_VL), vl]
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
                f"words; a band unrolls every step, so take fewer chunks"
            )

    def _allocate(self) -> None:
        """Registers for `group` steps at once, and an S register per constant.

        Each step in a group holds its inputs, its running result, every chain
        a later one forwards, every stored result a later chain would overwrite,
        and a scratch for DIV. INPUTS TAKE THE LOW NUMBERS: only VLD writes them,
        and `vec_core` checks a pending write on the register NUMBER an operand
        field names even when it selects S or K -- so a constant parked at an
        input's number never stalls an op. Raises :class:`VecEmitError` for a
        backward forward, too many constants, or a step needing more registers
        than the file holds.
        """
        wanted: set = set()
        div = False
        for k, chain in enumerate(self.chains):
            for kind, srcs in chain.ops:
                div |= kind is OpKind.DIV
                for s in srcs:
                    if not FWD <= s < KONST:
                        continue
                    if s - FWD >= k:
                        raise VecEmitError(
                            f"chain {k} forwards chain {s - FWD}, which has not "
                            f"run; a band hands results forward, never back"
                        )
                    wanted.add(s - FWD)
        self.fused = [fuse_compares(c.ops) for c in self.chains]
        self.scalar = self._scalars()
        for k, at in self.scalar:
            if at == len(self.fused[k]) - 1:
                wanted.discard(k)
        last = len(self.chains) - 1
        keep = [
            k
            for k, c in enumerate(self.chains)
            if c.store and k not in wanted and k != last
        ]
        held = sorted(wanted)
        per = self.nin + 1 + len(held) + len(keep) + int(div)

        distinct = list(dict.fromkeys(v for v in self.consts if v not in KREG))
        if len(distinct) > len(CONST_SREGS):
            raise VecEmitError(
                f"{len(distinct)} distinct constants and {len(CONST_SREGS)} scalar "
                f"registers to hold them; split the band"
            )
        npool = len(RED_SREGS) + len(CONST_SREGS) - len(distinct)
        spare = int(
            any(
                kind in REDUCE and (k, at) not in self.scalar
                for k, ops in enumerate(self.fused)
                for at, (kind, _) in enumerate(ops)
            )
        )
        group = min(self.chunks, REGISTERS // per)
        if self.scalar:
            group = min(group, (npool - spare) // len(self.scalar))
        if group < 1:
            raise VecEmitError(
                f"one step holds {per} registers ({self.nin} inputs, "
                f"{len(held)} forwarded and {len(keep)} kept results) and "
                f"{len(self.scalar)} folded scalars; the files have {REGISTERS} "
                f"and {npool}; split the band"
            )
        self.group = group
        self.r_in = [[g * self.nin + i for i in range(self.nin)] for g in range(group)]
        free = iter(range(group * self.nin, REGISTERS))
        self.r_run = [next(free) for _ in range(group)]
        self.r_held = [{k: next(free) for k in held} for _ in range(group)]
        self.r_keep = [{k: next(free) for k in keep} for _ in range(group)]
        self.r_tmp = [next(free) if div else TMP_REG for _ in range(group)]

        def quiet(regs):
            return sorted(regs, key=lambda s: s >= group * self.nin)

        self.sconst = dict(zip(distinct, quiet(CONST_SREGS), strict=False))
        taken = set(self.sconst.values())
        pool = iter(quiet([*RED_SREGS, *(s for s in CONST_SREGS if s not in taken)]))
        self.s_fold = {
            key: [next(pool) for _ in range(group)] for key in sorted(self.scalar)
        }
        self.red_sregs = list(pool)

    def _scalars(self) -> set:
        """``(chain, op)`` of every fold whose readers all take a scalar operand.

        Such a fold's result stays in its S register and is read from there, so
        it pays no VBCAST (a 3-state walk per chunk, 24 cycles at VL 128) and
        holds no vector register. A reader is the chain's next op, or for a
        chain's last op every later op forwarding it; a store or another fold
        needs a vector register.
        """
        out = set()
        for k, ops in enumerate(self.fused):
            for at, (kind, _) in enumerate(ops):
                if kind not in REDUCE:
                    continue
                if at + 1 < len(ops):
                    readers = [ops[at + 1][0]]
                elif self.chains[k].store:
                    continue
                else:
                    readers = [
                        kd
                        for later in self.fused[k + 1 :]
                        for kd, srcs in later
                        if FWD + k in srcs
                    ]
                if readers and not any(r in REDUCE for r in readers):
                    out.add((k, at))
        return out

    def _operand(self, src: int, g: int, run_s=None) -> tuple[int, int]:
        """A band source index as ``(selector, register)`` for step `g` of a group.

        `run_s` is set when the running result is a fold still in S registers.
        """
        if src >= KONST:
            value = self.consts[src - KONST]
            if value in KREG:
                return V.SRC_K, KREG[value]
            return V.SRC_S, self.sconst[value]
        if src >= FWD:
            k = src - FWD
            key = (k, len(self.fused[k]) - 1)
            if key in self.s_fold:
                return V.SRC_S, self.s_fold[key][g]
            return V.SRC_V, self.r_held[g][k]
        if src == OUT_REG:
            if run_s is not None:
                return V.SRC_S, run_s[g]
            return V.SRC_V, self.r_run[g]
        return V.SRC_V, self.r_in[g][src]

    def _dst(self, k: int, g: int) -> int:
        """Where chain `k`'s result lands for step `g`."""
        return self.r_held[g].get(k, self.r_keep[g].get(k, self.r_run[g]))

    def _l1(self, h: int, slot: int) -> int:
        """L1 word where half `h`'s region `slot` starts."""
        return (h * self.slots + slot) * self.bw

    def _out(self, h: int, t: int) -> int:
        """L1 word where half `h`'s `t`-th stored result starts."""
        return self._l1(h, t if self.inplace else self.nin + t)

    def _fills(self, h: int) -> list[int]:
        return [V.vfill(self.ad_fill[h][i], self._l1(h, i)) for i in range(self.nin)]

    def _drains(self, h: int) -> list[int]:
        return [
            V.vdrain(self.ad_drain[h][g], self._out(h, at))
            for g, (at, _) in enumerate(self.spans)
        ]

    def _half(self, h: int) -> list[int]:
        """Half `h`'s steps, `group` at a time: loads, ops op-major, then stores."""
        out: list[int] = []
        for s0 in range(0, self.chunks, self.group):
            steps = list(range(s0, min(s0 + self.group, self.chunks)))
            n = len(steps)
            for g, s in enumerate(steps):
                off = s * self.step_words
                for i in range(self.nin):
                    out.append(V.vld(self.r_in[g][i], AD_L1, self._l1(h, i) + off))
            for k, fused in enumerate(self.fused):
                run_s = None
                for at, (kind, srcs) in enumerate(fused):
                    last = at == len(fused) - 1
                    ops = [[self._operand(x, g, run_s) for x in srcs] for g in range(n)]
                    run_s = self.s_fold.get((k, at))
                    if run_s is not None:
                        out += self._fold(kind, ops, None, run_s)
                        continue
                    dsts = [
                        self._dst(k, g) if last else self.r_run[g] for g in range(n)
                    ]
                    if kind in REDUCE:
                        out += self._fold(kind, ops, dsts)
                    else:
                        out += self._interleave(kind, ops, dsts)
            stored = 0
            for k, chain in enumerate(self.chains):
                if not chain.store:
                    continue
                for g, s in enumerate(steps):
                    at = self._out(h, stored) + s * self.step_words
                    out.append(V.vst(self._dst(k, g), AD_L1, at))
                stored += 1
        return out

    def _interleave(self, kind, ops: list, dsts: list) -> list[int]:
        """One op over a group's steps, word-major, so no word waits on its step's last.

        A fused compare-and-select keeps its predicate live across the words, so
        it interleaves four steps at a time, one predicate register each.
        """
        width = 4 if isinstance(kind, Select) else len(ops)
        out: list[int] = []
        for at in range(0, len(ops), width):
            words = [
                lower_word(kind, ops[g], dsts[g], self.r_tmp[g], pr=g - at)
                for g in range(at, min(at + width, len(ops)))
            ]
            for w in range(len(words[0])):
                out += [step[w] for step in words]
        return out

    def _fold(self, kind, ops: list, dsts: list, keep=None) -> list[int]:
        """A row reduction over a group's steps: one TREE window per batch of scalars.

        The mode switch either side is part of the idiom (a TREE-mode word that is
        not a VRED faults F_OPCODE), so the steps share it rather than paying it each.
        With `keep` the results stay in those S registers and nothing is broadcast.
        """
        if keep is not None:
            out = [V.vsetmode(V.TREE)]
            for g, opnd in enumerate(ops):
                sel, reg = opnd[0]
                if sel != V.SRC_V:
                    raise VecEmitError("a row reduction folds a vector register")
                out.append(V.vred(keep[g], reg, REDUCE[kind], vb=reg))
            return out + [V.vsetmode(V.FLAT)]
        pool = self.red_sregs
        out: list[int] = []
        for at in range(0, len(ops), len(pool)):
            part = range(at, min(at + len(pool), len(ops)))
            out.append(V.vsetmode(V.TREE))
            for j, g in enumerate(part):
                sel, reg = ops[g][0]
                if sel != V.SRC_V:
                    raise VecEmitError("a row reduction folds a vector register")
                out.append(V.vred(pool[j], reg, REDUCE[kind], vb=reg))
            out.append(V.vsetmode(V.FLAT))
            out += [V.vbcast(dsts[g], pool[j]) for j, g in enumerate(part)]
        return out

    def cycles(self, fill_word: float, fill_lat: float) -> float:
        """Core cycles one RUN takes, priced word by word over the image.

        `fill_word` and `fill_lat` are the memory side's per-word streaming and
        first-word latency; only a RUN's first fill waits on them, the rest land
        while a half computes.
        """
        beats = -(-self.vl // V.LANES)
        busy, total, first = False, 0.0, True
        imm = False
        for w in self.image:
            if imm:
                imm = False
                continue
            op = w >> 27
            if op <= V.OPS["VRSQRT"]:
                total += beats + ALU_GAP
                busy = True
                continue
            if busy and op in (V.OPS["VLD"], V.OPS["VST"], V.OPS["VSETMODE"]):
                total += LANE_DRAIN
            busy = False
            if op == V.OPS["VLD"]:
                total += beats + LD_GAP
            elif op == V.OPS["VST"]:
                total += beats + ST_GAP
            elif op == V.OPS["VRED"]:
                total += beats + RED_WAIT
            elif op == V.OPS["VBCAST"]:
                total += 3 * beats
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
        return total

    def static_descs(self) -> list[int]:
        """The L1 window, and every fill's and drain's walk, ALL FOUR dims each.

        A descriptor is core state another program may have widened, so every
        dim this band relies on is written; `imem.rewrite` drops the ones the
        core already holds.
        """
        out = [V.desc_flit(AD_L1, 0, 0)]
        out += _dims(AD_L1, [(1, self.step_words)])
        for h in range(self.halves):
            for i in range(self.nin):
                out += _dims(self.ad_fill[h][i], self._fill_dims(i))
            for g, (_, n) in enumerate(self.spans):
                out += _dims(self.ad_drain[h][g], [(V.WORD_BYTES, n * self.bw)])
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
        for h in range(self.halves):
            for i in range(self.nin):
                for n in range(1, len(self._fill_dims(i))):
                    out.append(
                        V.desc_flit(self.ad_fill[h][i], n + 1, V.dim(*DIM_UNUSED))
                    )
        return out

    def flits(self, srcs, dsts, nelem: int) -> list[int]:
        """The whole program: image, descriptors, then one RUN per `halves` batches.

        Raises :class:`VecEmitError` unless one address arrives per filled
        operand and per drained result, and the batches fill whole RUNs.
        """
        if len(srcs) != self.nin or len(dsts) != len(self.spans):
            raise VecEmitError(
                f"this band fills {self.nin} operands and drains "
                f"{len(self.spans)} regions; got {len(srcs)} and {len(dsts)}"
            )
        nb = -(-nelem // self.batch)
        if nb % self.halves:
            raise VecEmitError(
                f"{nb} batches do not make whole RUNs of {self.halves}; a RUN "
                f"stores every half whatever `nelem` says"
            )
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
