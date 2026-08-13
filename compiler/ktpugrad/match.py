"""Reading a library call back out of one tinygrad kernel's ast.

The scheduler has already fused by the time this runs, and it hands over one
loop nest over the buffers it reads. This recognises the nests that a kernel in
:mod:`kohakutpu.kernels` computes and REFUSES everything else.

It is a matcher over a closed list, not a lowering pass. An unrecognised stride
returns None rather than being assimilated, because a stride guessed wrong is a
wrong answer in an address field and nothing downstream can tell.

Shape and stride come from :mod:`ktpugrad.uops`, which reads them off the loop
ranges; ordered `(M, N, K)` they are the rows this matched when a kernel still
arrived with its ShapeTrackers attached.
"""

from dataclasses import dataclass

from tinygrad.dtype import dtypes
from tinygrad.uop.ops import Ops

from ktpugrad import uops as U

#: `kohakutpu.kernels.activation`'s constants, restated so that reading an ast
#: needs no import of the kernel library.
LOG2E = 1.4426950408889634
SQRT2PI = 0.7978845608028654
GELU_CUBE = 0.044715

#: GELU's whole tanh expansion, folded: `2 sqrt(2/pi) log2e`. tinygrad emits
#: this one constant where 0.10 emitted the three separately.
GELU_EXP = 2 * SQRT2PI * LOG2E

#: Where a contraction's axes are once the scheduler has laid the kernel out:
#: `(M, N, K)`, reduced over the last.
AXES = 3


@dataclass(frozen=True)
class Operand:
    """One loaded matrix: which buffer it is, and which way round."""

    index: int
    #: `MK`/`NK` mean the buffer is already the row-major order the kernel takes;
    #: `KM`/`KN` mean the host has to transpose it first.
    order: str


@dataclass(frozen=True)
class Addend:
    """A bias added to the contraction: which buffer, and how it spreads.

    `MN` is a whole result-shaped buffer -- a residual. `N` is one row spread
    down the rows and `M` one column spread across them, which is what a torch
    Linear's bias and a per-row shift schedule to.
    """

    index: int
    order: str

    def held(self, m: int, n: int) -> tuple:
        """This buffer's own shape, before it is spread to the result's."""
        return {"MN": (m, n), "N": (1, n), "M": (m, 1)}[self.order]


@dataclass(frozen=True)
class Copy:
    """A whole-buffer copy of `elems` fp16 elements, `src` to `out`.

    Not a library kernel. `Tensor.numpy` casts and calls `contiguous` before it
    reads, so an unrealised tensor schedules a plain buffer-to-buffer copy on
    the device and the frontend does not work at all without it.
    """

    out: int
    src: int
    elems: int


@dataclass(frozen=True)
class Match:
    """One library kernel and the buffers to run it on.

    `out`, `a`, `b`, `g` and `c` index the schedule item's buffers, which is
    what `si_lowerer` passes through `ProgramSpec.globals`. `c` is the bias or
    the residual and `g` the gate it is scaled by; `scale` is a folded constant
    rather than a buffer. Only a kernel that takes one has each.
    """

    kernel: str
    out: int
    a: Operand
    b: Operand
    m: int
    n: int
    k: int
    c: Addend | None = None
    g: Addend | None = None
    scale: float | None = None

    @property
    def a_shape(self) -> tuple:
        """`a`'s buffer as it is stored, before any transpose."""
        return (self.m, self.k) if self.a.order == "MK" else (self.k, self.m)

    @property
    def b_shape(self) -> tuple:
        """`b`'s buffer as it is stored, before any transpose."""
        return (self.n, self.k) if self.b.order == "NK" else (self.k, self.n)

    @property
    def addends(self) -> list:
        """The spread operands, in the order the kernel's ports take them."""
        return [x for x in (self.g, self.c) if x is not None]

    @property
    def buffers(self) -> list:
        """Every buffer this kernel touches, result first."""
        return [
            self.out,
            self.a.index,
            self.b.index,
            *(x.index for x in self.addends),
        ]

    def __repr__(self) -> str:
        extra = "".join(
            f", {n}={x.index}:{x.order}"
            for n, x in (("g", self.g), ("c", self.c))
            if x is not None
        )
        if self.scale is not None:
            extra += f", s={self.scale}"
        return (
            f"Match({self.kernel} {self.m}x{self.k}x{self.n}, "
            f"a={self.a.index}:{self.a.order}, b={self.b.index}:{self.b.order}"
            f"{extra} -> {self.out})"
        )


def match_ast(ast) -> Match | Copy | None:
    """What `ast` computes, or None when nothing here recognises it."""
    got = U.envelope(ast)
    if got is None:
        return None
    store, ranges = got
    loop = U.order(store, ranges)
    if loop is None:
        return None
    plain = _plain_copy(store, loop)
    if plain is not None:
        return plain
    if len(loop) != AXES - 1:
        return None
    epilogue = _epilogue(store.src[1])
    if epilogue is None:
        return None
    kernel, acc, bias, gate, scale = epilogue
    got = _contraction(_unwrap(acc), loop)
    if got is None:
        return None
    axes, shape, a, b = got
    where = _destination(store, axes, shape)
    if where is None:
        return None
    rows, cols = shape[:2]
    spread = [None if u is None else _addend(u, axes, rows, cols) for u in (bias, gate)]
    if any(u is not None and x is None for u, x in zip((bias, gate), spread)):
        return None
    return Match(kernel, where, a, b, *shape, c=spread[0], g=spread[1], scale=scale)


def summarise(ast) -> str:
    """The ops `ast` holds, for a refusal that says what was actually seen."""
    return ", ".join(sorted({u.op.name for u in ast.toposort()}))


# ------------------------------------------------------------------ the shape
def _rowmajor(shape) -> tuple:
    """Row-major strides for `shape`, with 0 wherever the extent is one."""
    out, step = [], 1
    for n in reversed(shape):
        out.append(0 if n == 1 else step)
        step *= n
    return tuple(reversed(out))


def _plain_copy(store, loop: tuple) -> Copy | None:
    """`store` as a whole-buffer fp16 copy, or None.

    Both sides must walk the loop nest row-major, so what is recognised is a
    memcpy and nothing that also moves elements about.
    """
    shape = tuple(U.extent(r) for r in loop)
    out = U.indexed(store.src[0], loop)
    src = U.indexed(store.src[1], loop)
    if out is None or src is None or store.src[1].dtype is not dtypes.half:
        return None
    if out[1] != _rowmajor(shape) or src[1] != out[1]:
        return None
    elems = 1
    for n in shape:
        elems *= n
    return Copy(out[0], src[0], elems)


def _destination(store, axes: tuple, shape: tuple):
    """The buffer index a STORE of an `[M][N]` result writes, or None."""
    got = U.indexed(store.src[0], axes)
    if got is None or got[1] != (shape[1], 1, 0):
        return None
    return got[0]


def _a_order(shape, strides) -> str | None:
    """Which way round an A operand's buffer is: `MK`, `KM`, or None.

    A is broadcast over N, so its stride there must be zero; the other two say
    whether the buffer is `[M][K]` or `[K][M]`.
    """
    m, _, k = shape
    if strides[1] != 0:
        return None
    return {(k, 1): "MK", (1, m): "KM"}.get((strides[0], strides[2]))


def _b_order(shape, strides) -> str | None:
    """Which way round a B operand's buffer is: `NK`, `KN`, or None.

    `NK` is the library's own convention -- `b` stored `[N][K]`, as a torch
    Linear weight already is -- and it is what `x @ w.T` produces. Plain
    `x @ w` gives `KN` and costs a host transpose.
    """
    _, n, k = shape
    if strides[0] != 0:
        return None
    return {(k, 1): "NK", (1, n): "KN"}.get((strides[1], strides[2]))


# ------------------------------------------------------------------- the tree
def _unwrap(u):
    """`u` with the reduce's widening CASTs peeled off.

    Only fp16 and fp32 are stepped through: a cast between anything else is a
    dtype this machine does not carry, and stepping over it would silently
    compute in the wrong one.
    """
    while u.op is Ops.CAST and {u.dtype, u.src[0].dtype} <= {dtypes.half, dtypes.float}:
        u = u.src[0]
    return u


def _load(u, axes: tuple):
    """`(buffer index, shape, strides)` for an fp16 read of a buffer, or None."""
    if u.dtype is not dtypes.half:
        return None
    got = U.indexed(u, axes)
    if got is None:
        return None
    return got[0], tuple(U.extent(r) for r in axes), got[1]


def _contraction(red, loop: tuple):
    """`(axes, shape, a, b)` for a REDUCE that is a matmul, or None.

    The reduction must be a sum over ONE axis of a product of two reads -- which
    is what `x @ w` lowers to and what a GEMM is.
    """
    got = U.reduced(red)
    if got is None:
        return None
    op, value, folded = got
    if op is not Ops.ADD or len(folded) != 1:
        return None
    axes = (*loop, *folded)
    mul = _unwrap(value)
    if mul.op is not Ops.MUL or len(mul.src) != 2:
        return None
    loads = [_load(_unwrap(x), axes) for x in mul.src]
    if any(x is None for x in loads):
        return None
    # A MUL is commutative and the scheduler does not promise an order, so which
    # operand is A is read off the strides rather than off the position.
    for left, right in (loads, loads[::-1]):
        shape = left[1]
        a_order, b_order = _a_order(shape, left[2]), _b_order(shape, right[2])
        if a_order and b_order:
            return axes, shape, Operand(left[0], a_order), Operand(right[0], b_order)
    return None


#: `(activation, whether a bias is fused in)` -> the library kernel. Written out
#: rather than composed: a name that does not exist is a dispatch, not an error.
KERNELS = {
    (None, False): "matmul",
    (None, True): "linear_add",
    ("silu", False): "linear_silu",
    ("silu", True): "linear_add_silu",
    ("relu", False): "linear_relu",
    ("relu", True): "linear_add_relu",
    ("gelu", False): "linear_gelu",
    ("gelu", True): "linear_add_gelu",
}


def _epilogue(value):
    """`(kernel, accumulator, bias, gate, scale)`, or None.

    Every kernel here is one contraction and one epilogue over it. The
    activation is peeled first, since its operand is the bias ADD a bare
    `linear_add` would otherwise match, and what is left is the contraction with
    at most one of a bias, a scalar or a gated residual on it.
    """
    act, core = _activation(value)
    acc, bias = _biased(core)
    if acc is not None:
        name = KERNELS.get((act, bias is not None))
        return None if name is None else (name, acc, bias, None, None)
    if act is not None:
        return None
    got = _scaled(core)
    if got is not None:
        return "linear_scale", got[0], None, None, got[1]
    got = _gated(core)
    if got is not None:
        return "linear_gate_add", got[0], got[2], got[1], None
    return None


def _activation(value) -> tuple:
    """`(name, the node the activation is over)`, or `(None, value)`.

    The three a `linear_*` kernel holds a fused form of. Each is matched by the
    exact tree tinygrad writes, not by a family of shapes: a clamp read as a
    sigmoid would compute a different function and report success.
    """
    if value.op is Ops.MUL and len(value.src) == 2:
        for held, other in (value.src, value.src[::-1]):
            if _is_sigmoid(other, held):
                return "silu", held
    for name, read in (("relu", _relu_of), ("gelu", _gelu_of)):
        got = read(value)
        if got is not None:
            return name, got
    return None, value


def _biased(core) -> tuple:
    """`(accumulator, bias LOAD)` for `acc + b`, or `(core, None)` for a bare one.

    Returns `(None, None)` for anything that is neither, which is what makes an
    unrecognised epilogue a refusal.
    """
    if _unwrap(core).op is Ops.REDUCE:
        return core, None
    if core.op is Ops.ADD and len(core.src) == 2:
        for acc, bias in (core.src, core.src[::-1]):
            if _unwrap(acc).op is Ops.REDUCE and bias.op is Ops.INDEX:
                return acc, bias
    return None, None


def _scaled(core) -> tuple | None:
    """`(accumulator, the scalar it is multiplied by)` for `acc * s`, or None.

    Attention's `1/sqrt(d)`, which the scheduler folds into the score kernel.
    """
    if core.op is not Ops.MUL or len(core.src) != 2:
        return None
    for acc, const in (core.src, core.src[::-1]):
        if _unwrap(acc).op is Ops.REDUCE and _scalar(const) is not None:
            return acc, _scalar(const)
    return None


def _gated(core) -> tuple | None:
    """`(accumulator, gate LOAD, residual LOAD)` for `r + g * acc`, or None.

    An adaLN-Zero block writes this twice: the projection is scaled by a
    per-channel gate before it rejoins the residual stream.
    """
    if core.op is not Ops.ADD or len(core.src) != 2:
        return None
    for res, scaled in (core.src, core.src[::-1]):
        if res.op is not Ops.INDEX or scaled.op is not Ops.MUL:
            continue
        if len(scaled.src) != 2:
            continue
        for gate, acc in (scaled.src, scaled.src[::-1]):
            if gate.op is Ops.INDEX and _unwrap(acc).op is Ops.REDUCE:
                return acc, gate, res
    return None


def _relu_of(value):
    """The node `value` clamps at zero, or None.

    tinygrad spells relu `WHERE(CMPLT(0, x), x, 0)` and `maximum(0)` as
    `MAX(x, 0)`; the library computes `(x + |x|) * 0.5`, which is what the
    vector core has demonstrated. Matching THEIR spelling onto OUR arithmetic
    is the whole of the difference -- adding a compare-and-select to the DSL
    would mean lowering predication no kernel on this machine has ever run.
    """
    if value.op is Ops.MAX and len(value.src) == 2:
        for held, zero in (value.src, value.src[::-1]):
            if _const(zero, 0.0):
                return held
        return None
    if value.op is not Ops.WHERE or len(value.src) != 3:
        return None
    cond, held, other = value.src
    if cond.op is not Ops.CMPLT or len(cond.src) != 2:
        return None
    below, above = cond.src
    if not _const(below, 0.0) or above is not held or not _const(other, 0.0):
        return None
    return held


def _addend(load, axes: tuple, m: int, n: int) -> Addend | None:
    """`load` as a bias over an `[M][N]` result, or None.

    Three stride rows and no others: the whole result, one row spread down, one
    column spread across. A bias does not read the reduced axis, so its stride
    there must be zero.
    """
    got = _load(load, axes)
    if got is None:
        return None
    index, _, strides = got
    order = {(n, 1, 0): "MN", (0, 1, 0): "N", (1, 0, 0): "M"}.get(strides)
    return None if order is None else Addend(index, order)


def _is_sigmoid(u, acc) -> bool:
    """Whether `u` is ``recip(exp2(acc * -log2e) + 1)`` over the SAME `acc`.

    Character for character `kohakutpu.kernels.activation.sigmoid`. The identity
    check on `acc` is what makes this a fused epilogue rather than a product of
    two unrelated things.
    """
    if u.op is not Ops.RECIPROCAL or u.src[0].op is not Ops.ADD:
        return False
    for one, power in (u.src[0].src, u.src[0].src[::-1]):
        if not _const(one, 1.0) or power.op is not Ops.EXP2:
            continue
        scale = power.src[0]
        if scale.op is not Ops.MUL or len(scale.src) != 2:
            continue
        for head, tail in (scale.src, scale.src[::-1]):
            if head is acc and _const(tail, -LOG2E):
                return True
    return False


def _gelu_of(value):
    """The node `value` applies tinygrad's tanh GELU to, or None.

    0.13 folds `0.5 x (1 + tanh(sqrt(2/pi) (x + 0.044715 x^3)))` all the way
    down to `x * sigmoid(2 sqrt(2/pi) (...))` -- character for character
    `kernels.activation.gelu_tanh`, so the whole expansion is one constant.
    """
    if value.op is not Ops.MUL or len(value.src) != 2:
        return None
    for held, sigmoid in (value.src, value.src[::-1]):
        if sigmoid.op is not Ops.RECIPROCAL or sigmoid.src[0].op is not Ops.ADD:
            continue
        power = _beside(sigmoid.src[0], Ops.ADD, 1.0)
        if power is None or power.op is not Ops.EXP2:
            continue
        poly = _beside(power.src[0], Ops.MUL, -GELU_EXP)
        if poly is not None and _is_cubic(poly, held):
            return held
    return None


def _is_cubic(u, held) -> bool:
    """Whether `u` is ``x + 0.044715 x^3`` over the SAME `held`."""
    if u.op is not Ops.ADD or len(u.src) != 2:
        return False
    for base, term in (u.src, u.src[::-1]):
        cube = _beside(term, Ops.MUL, GELU_CUBE)
        if base is not held or cube is None or cube.op is not Ops.MUL:
            continue
        if len(cube.src) != 2:
            continue
        for square, one in (cube.src, cube.src[::-1]):
            if one is not held or square.op is not Ops.MUL:
                continue
            if len(square.src) == 2 and all(s is held for s in square.src):
                return True
    return False


def _beside(u, op, want: float):
    """The other operand of a binary `op` whose one side is the constant `want`.

    Returns None when `u` is not that op, or when neither side is `want`. A
    scheduler promises no operand order, so every constant is read this way.
    """
    if u.op is not op or len(u.src) != 2:
        return None
    for held, const in (u.src, u.src[::-1]):
        if _const(const, want):
            return held
    return None


def _scalar(u) -> float | None:
    """`u` as a plain numeric constant, or None.

    `bool` is excluded though it is an `int`: a predicate read as 1.0 would fold
    a compare into arithmetic and report success.
    """
    if u.op is not Ops.CONST or not isinstance(u.arg, (int, float)):
        return None
    return None if isinstance(u.arg, bool) else float(u.arg)


def _const(u, want: float) -> bool:
    """Whether `u` is the constant `want`."""
    got = _scalar(u)
    return got is not None and abs(got - want) < 1e-6
