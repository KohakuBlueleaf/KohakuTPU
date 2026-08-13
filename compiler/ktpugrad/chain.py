"""Reading a tinygrad UOp tree back out as ONE vector chain.

The fallback :mod:`ktpugrad.match` leaves behind: where that recognises a tree a
hand-written kernel already computes, this translates an ARBITRARY elementwise
tree, and a reduction along the last axis, onto `kohakutpu.lang.vector`'s
`Value` algebra.

Not a lowering pass. `chains_of` already turns an expression TREE into chains
with the lifted subexpressions forwarded in registers, and `BandKernel` already
runs those chains as ONE program, so the work here is choosing our operators for
theirs and REFUSING the rest.
"""

from dataclasses import dataclass

from kohakutpu.hw import vector as V
from kohakutpu.hw.ops import OpKind
from kohakutpu.isa.vecemit import DESCRIPTORS, IMEM_WORDS, OUT_REG, REGISTERS
from kohakutpu.lang.backend import MAX_CHUNKS
from tinygrad.dtype import dtypes
from tinygrad.uop.ops import Ops

from ktpugrad import uops as U
from ktpugrad.errors import ChainRefused

# The matcher's own load reader, imported rather than restated: a second copy of
# what a stride row means would drift from this one in silence.
from ktpugrad.match import (
    _const,
    _load,
    _relu_of,
    _rowmajor,
    _unwrap,
    summarise,
)

#: Their operator to ours. Every one of these is a single ALU word, or two for
#: a divide, and none needs an operand form outside `SRC_V`.
ARITH = {
    Ops.ADD: OpKind.ADD,
    Ops.SUB: OpKind.SUB,
    Ops.MUL: OpKind.MUL,
    Ops.FDIV: OpKind.DIV,
    Ops.NEG: OpKind.NEG,
    Ops.RECIPROCAL: OpKind.RECIP,
    Ops.EXP2: OpKind.EXP2,
    Ops.LOG2: OpKind.LOG2,
}

#: Their REDUCE operator to our row reduction. `Ops.MUL` -- `x.prod()` --
#: has no entry because this machine's L1 tree has no product mode.
FOLD = {
    Ops.ADD: OpKind.SUM,
    Ops.MAX: OpKind.RMAX,
}

#: Arity of each, so a UOp carrying a different one is refused rather than
#: silently read with its first two sources.
ARITY = {
    OpKind.NEG: 1,
    OpKind.RECIP: 1,
    OpKind.EXP2: 1,
    OpKind.LOG2: 1,
    OpKind.ADD: 2,
    OpKind.SUB: 2,
    OpKind.MUL: 2,
    OpKind.DIV: 2,
}

#: Instruction words each op costs. A divide is `VINV` then `VMUL`, a fold is
#: `reduce_row`'s VSETMODE/VRED/VSETMODE/VBCAST; the rest are one ALU word.
WORDS = {OpKind.DIV: 2, OpKind.SUM: 4, OpKind.RMAX: 4}

#: DRAM operands one chain may fill. Two descriptors each, plus one shared store
#: and one drain.
MAX_OPERANDS = (DESCRIPTORS - 2) // 2

#: Leaves one chain may name: source :data:`OUT_REG` is the running result, so
#: operands and distinct constants together stop one below it.
MAX_LEAVES = OUT_REG

#: A band's preamble and tail at the widest legal shape: length, three words per
#: seeded constant, mode, a fill each, VBAR, then one drain and VHALT.
OVERHEAD = 4 + 3 * (MAX_LEAVES - MAX_OPERANDS) + MAX_OPERANDS + 1 + 2

#: Instruction words one chain may hold. A band UNROLLS every step, so 62 unary
#: ops built a 520-word image -- measured, against `IMEM_WORDS`' 512.
MAX_WORDS = (IMEM_WORDS - OVERHEAD) // MAX_CHUNKS - (MAX_OPERANDS + 1)

#: Values a band holds in registers at once: one per lifted subexpression and
#: one per seeded constant.
MAX_HELD = REGISTERS - MAX_OPERANDS - 2


@dataclass(frozen=True)
class Source:
    """One loaded buffer: which schedule-item buffer, and the shape it is held at.

    `held` is the buffer's OWN shape, before the broadcast to the result's. An
    elementwise pass reads operands of one length, so a shorter one is spread on
    the host, exactly as a `linear_add` bias is.
    """

    index: int
    held: tuple

    @property
    def elems(self) -> int:
        """Elements the buffer itself holds."""
        n = 1
        for x in self.held:
            n *= x
        return n


@dataclass(frozen=True)
class Load:
    """A chain leaf: operand `at` of the recipe's `operands`."""

    at: int


@dataclass(frozen=True)
class Scalar:
    """A chain leaf: one folded constant."""

    value: float


@dataclass(frozen=True)
class Op:
    """One operation over leaves and other operations."""

    kind: OpKind
    args: tuple


@dataclass(frozen=True)
class Recipe:
    """One generated chain: what it reads, computes and writes.

    `out` and each `Source.index` index the schedule item's buffers, which is
    what `si_lowerer` passes through `ProgramSpec.globals`. `root` is the
    expression in our own operators, hashable so a kernel can carry it as a knob
    and trace once per distinct expression.

    `shape` is what the vector program WALKS and `stored` what tinygrad's result
    buffer holds. They differ only for a fold: `VRED` broadcasts back across the
    row, so the kernel writes `[rows][cols]` where the buffer wants `[rows][1]`.
    """

    out: int
    operands: tuple
    root: object
    shape: tuple
    stored: tuple

    @property
    def elems(self) -> int:
        """Elements the walked shape holds."""
        n = 1
        for x in self.shape:
            n *= x
        return n

    @property
    def folds(self) -> bool:
        """Whether this chain reduces a row, so the band steps one row a step."""
        return self.stored != self.shape

    @property
    def row(self) -> tuple:
        """``(rows, cols)`` a fold walks. Meaningless unless :attr:`folds`."""
        return self.shape

    @property
    def nodes(self) -> list:
        """Every distinct operation in the expression, first-encounter order.

        By VALUE, since `chains_of` lifts a repeated subexpression into one
        chain the others read from a register -- so a node reached twice costs
        one register and runs once.
        """
        return _nodes(self.root, [])

    @property
    def words(self) -> int:
        """Instruction words the chain's body will spend per step."""
        return sum(WORDS.get(n.kind, 1) for n in self.nodes)

    @property
    def held(self) -> int:
        """Values the band will keep in registers: the lifted ones and the constants."""
        return len(_shared(self.root)) + len(_scalars(self.root))

    def __repr__(self) -> str:
        reads = ", ".join(
            f"{s.index}:{'x'.join(map(str, s.held))}" for s in self.operands
        )
        walks = "x".join(map(str, self.shape))
        if self.folds:
            walks += f" folded to {'x'.join(map(str, self.stored))}"
        return f"Recipe({walks}, {len(self.nodes)} ops, reads {reads} -> {self.out})"


def read_chain(ast) -> Recipe:
    """`ast` as one generated chain, elementwise or folding a row.

    Raises :class:`ChainRefused` for anything else, naming the UOp kinds it
    saw: an op with no operator of ours, a view this does not read, a dtype
    this machine does not carry, a reduction along an axis this machine cannot
    fold, or an expression past one of the ceilings a vector core has.
    """
    store, loop, out = _result(ast)
    stored = _extents(loop)
    operands: list = []
    unknown: list = []
    folded: list = []
    root = _node(ast, store.src[1], loop, operands, unknown, folded)
    if unknown:
        kinds = ", ".join(sorted(set(unknown)))
        _refuse(ast, f"it computes {kinds}, which no operator here spells")
    made = Recipe(
        out,
        tuple(operands),
        root,
        _extents(folded[0]) if folded else stored,
        stored,
    )
    _within(ast, made)
    return made


def refusal(ast) -> str:
    """Why this ast is not a generated chain, for a caller that raises its own."""
    try:
        read_chain(ast)
    except ChainRefused as why:
        return str(why)
    return ""


# ------------------------------------------------------------------ the shape
def _result(ast):
    """`(store, loop axes, out buffer)` for an elementwise SINK, or a refusal.

    The store must walk its loop nest row-major, which is what separates an
    elementwise kernel from a contraction: a contraction reads an axis the store
    does not.
    """
    got = U.envelope(ast)
    if got is None:
        _refuse(ast, "it is not one SINK of one stored loop nest")
    store, ranges = got
    loop = U.order(store, ranges)
    if loop is None:
        _refuse(ast, "its result is not one plain row-major walk of the loop nest")
    dest = U.indexed(store.src[0], loop)
    if dest is None:
        _refuse(ast, "it stores somewhere other than a buffer")
    if dest[1] != _rowmajor(_extents(loop)):
        _refuse(ast, "its result is not one plain row-major walk of the loop nest")
    value = store.src[1]
    if value.dtype is not dtypes.half:
        _refuse(ast, f"it computes {value.dtype}, and this machine carries only fp16")
    return store, loop, dest[0]


def _extents(axes: tuple) -> tuple:
    """The shape a tuple of loop ranges walks."""
    return tuple(U.extent(r) for r in axes)


# ------------------------------------------------------------------- the tree
def _node(ast, u, axes: tuple, operands: list, unknown: list, folded: list):
    """One UOp as a leaf or an operation of ours, collecting operands as it goes.

    `axes` are the loop ranges in scope here, and a `REDUCE` adds its own: below
    one the operands are the whole row, above one they are the folded column.
    `folded` collects the row axes so the caller knows what the band steps.

    Appends to `unknown` and keeps walking for an op with no operator here, so
    the refusal names every one rather than the first. Raises
    :class:`ChainRefused` for an op whose arity is not the one we lower, for a
    load this does not read, and for a constant that is not a number.
    """
    u = _unwrap(u)
    if u.op is Ops.INDEX:
        return Load(_source(ast, u, axes, operands))
    if u.op is Ops.CONST:
        if not isinstance(u.arg, (int, float)) or isinstance(u.arg, bool):
            _refuse(ast, f"a constant of {u.arg!r}, which is not a number")
        return Scalar(float(u.arg))
    if u.op is Ops.REDUCE:
        return _folding(ast, u, axes, operands, unknown, folded)
    got = _selected(ast, u, axes, operands, unknown, folded)
    if got is not None:
        return got
    if u.op is Ops.MAX:
        return _maximum(ast, u, axes, operands, unknown, folded)
    if u.op is Ops.SQRT:
        return _sqrt(ast, u, axes, operands, unknown, folded)
    kind = ARITH.get(u.op)
    if kind is None:
        unknown.append(u.op.name)
        for s in u.src:
            # A structural refusal from under a REDUCE reported the operand
            # shape as the cause and buried the reduce.
            try:
                _node(ast, s, axes, operands, unknown, folded)
            except ChainRefused:
                pass
        return Scalar(0.0)
    if len(u.src) != ARITY[kind]:
        _refuse(ast, f"{u.op.name} over {len(u.src)} sources, not {ARITY[kind]}")
    return Op(
        kind, tuple(_node(ast, s, axes, operands, unknown, folded) for s in u.src)
    )


def _folding(ast, u, axes: tuple, operands: list, unknown: list, folded: list):
    """`u` as one row reduction of ours, or a refusal naming what it folds.

    `VRED` folds exactly the lanes VL names, so the ONLY reduction this spells
    is one whole trailing row of a rank-2 walk. Every other shape is refused
    by name rather than assimilated: a fold over the wrong extent returns a
    number with no fault and no warning. Raises :class:`ChainRefused`.
    """
    got = U.reduced(u)
    if got is None:
        _refuse(ast, "a reduction whose axes are not plain loop ranges")
    op, value, over = got
    inner = (*axes, *over)
    if len(over) != 1:
        _refuse(ast, f"a reduction over {len(over)} axes; a row fold takes one")
    if len(inner) != 2:
        _refuse(
            ast,
            f"a reduction over a rank-{len(inner)} walk of {_extents(inner)}; the "
            f"only fold spelled here is one row of a plain [rows][cols] buffer, "
            f"and a rank-{len(inner)} one is either a CONTRACTION the matcher "
            f"declined or a batched fold whose row count is ambiguous",
        )
    kind = FOLD.get(op)
    if kind is None:
        _refuse(
            ast,
            f"a {op.name} reduction; the L1 tree folds "
            f"{', '.join(sorted(o.name for o in FOLD))} and nothing else",
        )
    if folded and folded[0] != inner:
        _refuse(
            ast,
            f"reductions over both {_extents(folded[0])} and {_extents(inner)}; "
            f"one program steps one row shape, so it could fold only one of them "
            f"correctly",
        )
    if not folded:
        folded.append(inner)
    return Op(kind, (_node(ast, value, inner, operands, unknown, folded),))


def _selected(ast, u, axes: tuple, operands: list, unknown: list, folded: list):
    """`u` as relu, abs, rsqrt or a min/max select, in our own arithmetic.

    tinygrad spells `relu` `WHERE(CMPLT(0, x), x, 0)`, `abs`
    `x * WHERE(CMPNE(x, 0), WHERE(CMPLT(x, 0), -1, 1), 0)` and `rsqrt`
    `RECIP(SQRT(x))`; ours are `(x + |x|) * 0.5`, a single `VABS` and a single
    `VRSQRT`. Returns None for anything else -- a compare-and-select in general
    needs predication no kernel here has run, and reading one as arithmetic
    would compute a different function and report success.
    """
    held = _rsqrt_of(u)
    if held is not None:
        return Op(OpKind.RSQRT, (_node(ast, held, axes, operands, unknown, folded),))
    held = _abs_of(u)
    if held is not None:
        return Op(OpKind.ABS, (_node(ast, held, axes, operands, unknown, folded),))
    held = _relu_of(u)
    if held is not None:
        inner = _node(ast, held, axes, operands, unknown, folded)
        clamp = Op(OpKind.ADD, (inner, Op(OpKind.ABS, (inner,))))
        return Op(OpKind.MUL, (clamp, Scalar(0.5)))
    got = _minmax_of(u)
    if got is None:
        return None
    wider, a, b = got
    args = [_node(ast, s, axes, operands, unknown, folded) for s in (a, b)]
    return _extremum(wider, *args)


def _minmax_of(u):
    """`(is a maximum, a, b)` for a min/max written as a select, or None.

    `x.clip(lo, hi)` is two of these nested. `WHERE(CMPLT(a, b), b, a)` takes
    the larger and `WHERE(CMPLT(a, b), a, b)` the smaller -- the ONLY two
    select shapes read here, because every other one is a real predicate.
    """
    if u.op is not Ops.WHERE or len(u.src) != 3:
        return None
    cond, first, second = u.src
    if cond.op is not Ops.CMPLT or len(cond.src) != 2:
        return None
    a, b = cond.src
    if first is b and second is a:
        return True, a, b
    if first is a and second is b:
        return False, a, b
    return None


def _extremum(wider: bool, a, b):
    """`max(a, b)` or `min(a, b)` as `(a + b +/- |a - b|) * 0.5`.

    Exact on fp16, and it is what :func:`_maximum` already does for their
    `MAX` -- the same identity whichever way they spelled it.
    """
    gap = Op(OpKind.ABS, (Op(OpKind.SUB, (a, b)),))
    total = Op(OpKind.ADD, (a, b))
    joined = Op(OpKind.ADD, (total, gap)) if wider else Op(OpKind.SUB, (total, gap))
    return Op(OpKind.MUL, (joined, Scalar(0.5)))


def _sqrt(ast, u, axes: tuple, operands: list, unknown: list, folded: list):
    """`SQRT(x)` as ``1/(1/sqrt(x))`` -- their spelling, our arithmetic.

    This ALU has no square root, so a root is two words: `L.sqrt_approx`'s pair
    of seeds. `L.sqrt_newton` refines it at six, and is NOT what a bare
    `x.sqrt()` gets -- 0.09 ulp of 1.556 for four more words, measured in
    `scripts/py/sqrt_paths.py`. Rewritten HERE rather than at build time so the
    leaf, word and register counts below are the ones that will run.

    `RECIP(SQRT(x))` never reaches this: :func:`_rsqrt_of` folds that pair onto
    one exact `VRSQRT` first, which is both cheaper and more accurate. Raises
    :class:`ChainRefused` for an arity we do not lower.
    """
    if len(u.src) != 1:
        _refuse(ast, f"SQRT over {len(u.src)} sources, not 1")
    inner = _node(ast, u.src[0], axes, operands, unknown, folded)
    return Op(OpKind.RECIP, (Op(OpKind.RSQRT, (inner,)),))


def _rsqrt_of(u):
    """The node `u` takes the reciprocal square root of, or None.

    Read as the PAIR, never as `SQRT` alone: `OpKind.RSQRT` is ONE `VRSQRT` and
    the seed's own error, where `1/sqrt(x)` built out of :func:`_sqrt` would be
    two words and two table errors. Matched before the generic walk, so this
    stays the cheaper reading.
    """
    if u.op is not Ops.RECIPROCAL or len(u.src) != 1:
        return None
    root = _unwrap(u.src[0])
    if root.op is not Ops.SQRT or len(root.src) != 1:
        return None
    return root.src[0]


def _abs_of(u):
    """The node `u` takes the magnitude of, or None.

    tinygrad's `abs` is `x * sign(x)` with the sign as two nested selects, so
    the outer MUL has to be read here rather than as a plain product.
    """
    if u.op is not Ops.MUL or len(u.src) != 2:
        return None
    for held, sign in (u.src, u.src[::-1]):
        if _is_sign(sign, held):
            return held
    return None


def _is_sign(u, held) -> bool:
    """Whether `u` is ``sign(held)`` as tinygrad's two nested selects.

    `CMPNE` is read either way round since it is symmetric; `CMPLT` is not, and
    only `CMPLT(held, 0)` is accepted.
    """
    if u.op is not Ops.WHERE or len(u.src) != 3:
        return False
    nonzero, pick, zero = u.src
    if not _const(zero, 0.0) or nonzero.op is not Ops.CMPNE:
        return False
    if not any(
        a is held and _const(b, 0.0) for a, b in (nonzero.src, nonzero.src[::-1])
    ):
        return False
    if pick.op is not Ops.WHERE or len(pick.src) != 3:
        return False
    below, under, over = pick.src
    if below.op is not Ops.CMPLT or len(below.src) != 2:
        return False
    if below.src[0] is not held or not _const(below.src[1], 0.0):
        return False
    return _const(under, -1.0) and _const(over, 1.0)


def _maximum(ast, u, axes: tuple, operands: list, unknown: list, folded: list):
    """`MAX(a, b)` as ``(|a - b| + a + b) * 0.5`` -- their spelling, our arithmetic.

    MAX's own operand slot has never been demonstrated by a kernel that has run,
    and the identity is exact on fp16. Rewritten HERE rather than at build time
    so the leaf, word and register counts below are the ones that will run.
    """
    if len(u.src) != 2:
        _refuse(ast, f"MAX over {len(u.src)} sources, not 2")
    a, b = (_node(ast, s, axes, operands, unknown, folded) for s in u.src)
    gap = Op(OpKind.ABS, (Op(OpKind.SUB, (a, b)),))
    return Op(OpKind.MUL, (Op(OpKind.ADD, (Op(OpKind.ADD, (gap, a)), b)), Scalar(0.5)))


def _source(ast, u, axes: tuple, operands: list) -> int:
    """Which operand this read is, appending it on first encounter.

    The load must cover the whole result, either at its own shape or broadcast
    from a shorter one -- a zero stride is an axis the buffer does not hold.
    Raises :class:`ChainRefused` for any other stride row.
    """
    got = _load(u, axes)
    if got is None:
        _refuse(ast, "a load that is not one plain fp16 read of a buffer")
    index, at, strides = got
    if axes and U.reduces(axes[-1]) and strides[-1] not in (0, 1):
        # UNDER A FOLD the innermost axis is the one VRED walks, so a stride
        # other than one there is a fold DOWN A COLUMN however the ranges nest.
        _refuse(
            ast,
            f"an operand strided {strides[-1]} along the axis being folded; this "
            f"machine folds ALONG a row, so only the last axis is a fold",
        )
    held = tuple(1 if s == 0 else n for n, s in zip(at, strides))
    if strides != _rowmajor(held):
        _refuse(
            ast, f"a load with strides {strides}, which is not a broadcast of {held}"
        )
    made = Source(index, held)
    if made not in operands:
        operands.append(made)
    return operands.index(made)


# ------------------------------------------------------------------ the walls
def _within(ast, made: Recipe) -> None:
    """Refuse a recipe past a ceiling a vector core has, naming which one.

    Checked HERE rather than left to the emitter: a band builds its image at
    dispatch, and the refusal it raises there names the operand count whatever
    the real cause was. Raises :class:`ChainRefused`.
    """
    if made.folds:
        _foldable(ast, made.row[-1])
    leaves = len(made.operands) + len(_scalars(made.root))
    for got, cap, why in (
        (
            len(made.operands),
            MAX_OPERANDS,
            "operands, and a core has eight descriptors",
        ),
        (leaves, MAX_LEAVES, f"leaves, and source {OUT_REG} is the running result"),
        (
            made.words,
            MAX_WORDS,
            (
                f"instruction words, and a band unrolls every step into a "
                f"{IMEM_WORDS}-word image"
            ),
        ),
        (made.held, MAX_HELD, "held values, and the register file is smaller"),
    ):
        if got > cap:
            _refuse(ast, f"it needs {got} {why}; at most {cap} fit")
    if not made.operands:
        _refuse(ast, "it reads no buffer at all, so there is nothing to walk")


def _foldable(ast, cols: int) -> None:
    """Refuse a row `VRED` cannot fold, naming where the caller can go instead.

    The escape hatch is NOT the way round either of these -- MEASURED: at 256 and
    at 1024, `ktpugrad.softmax` and every shipped row-wise kernel raise the same
    refusal this does, because `RowReduceKernel` carries the identical check.
    Saying so is the point; sending a reader to a door that is also shut costs
    them the trip. Raises :class:`ChainRefused`.
    """
    if cols % V.LANES:
        _refuse(
            ast,
            f"a {cols}-wide row: VRED folds whole lane groups, so the reduced "
            f"axis has to be a multiple of {V.LANES}. Pad it to one -- the "
            f"shipped kernels carry the same check, so `ktpugrad.softmax` and "
            f"the norms refuse this too rather than being the way round it",
        )
    if cols > V.VLMAX:
        _refuse(
            ast,
            f"a {cols}-wide row: VRED folds exactly the lanes VL names and "
            f"VLMAX is {V.VLMAX}. No SINGLE pass folds a wider row, and neither "
            f"this generator nor `ktpugrad.softmax` reshapes, so the escape "
            f"hatch is still not the way round it. What IS: `K.softmax_wide`, "
            f"`K.layernorm_wide` and `K.rmsnorm_wide` fold hierarchically, one "
            f"every row at once -- hand them the row split as "
            f"`(-1, {V.VLMAX})` with `rows=K.split({cols})`. "
            f"Or reduce over an axis of {V.VLMAX} or less",
        )


def _refuse(ast, why: str) -> None:
    """Raise :class:`ChainRefused` for `why`, naming every op the ast holds."""
    raise ChainRefused(
        f"the elementwise chain generator refuses this scheduled kernel because "
        f"{why}; it holds {summarise(ast)}"
    )


def _nodes(expr, out: list) -> list:
    """Every distinct operation in `expr`, a repeat counted once."""
    if not isinstance(expr, Op) or expr in out:
        return out
    out.append(expr)
    for arg in expr.args:
        _nodes(arg, out)
    return out


def _shared(expr) -> list:
    """Operations `expr` reaches more than once, which `chains_of` will lift."""
    return [node for node, times in _reached(expr, []) if times > 1]


def _reached(expr, out: list) -> list:
    """``[node, times reached]`` for every operation, a repeat not walked again."""
    if not isinstance(expr, Op):
        return out
    for seen in out:
        if seen[0] == expr:
            seen[1] += 1
            return out
    out.append([expr, 1])
    for arg in expr.args:
        _reached(arg, out)
    return out


def _scalars(expr, out: list | None = None) -> list:
    """Every distinct constant in `expr`, which the band seeds into a register."""
    out = [] if out is None else out
    if isinstance(expr, Scalar):
        if expr not in out:
            out.append(expr)
    elif isinstance(expr, Op):
        for arg in expr.args:
            _scalars(arg, out)
    return out
