"""Reading shape and stride out of tinygrad's lowered ast.

Up to 0.10 a kernel arrived with its ShapeTrackers attached and an operand's
strides were a field to read. 0.13 hands over a LOOP NEST instead: buffers are
`PARAM`, a load is `INDEX(param, expr)`, and `expr` is an affine polynomial over
`RANGE` nodes. The strides did not go away -- they became the coefficients.

So this module is one function, :func:`affine`, and the envelope reading around
it. Order the axes and every coefficient tuple it returns is the stride row the
matcher already knew how to read, which is why nothing downstream of here had to
learn a second vocabulary.
"""

from tinygrad.uop.ops import Ops


def envelope(ast):
    """`(store, loop ranges)` for a SINK of one stored loop nest, or None.

    0.13 wraps a kernel `SINK -> END -> STORE`, with the loop axes hanging off
    the END beside the store they close over.
    """
    if ast.op is not Ops.SINK or len(ast.src) != 1:
        return None
    end = ast.src[0]
    if end.op is not Ops.END or not end.src:
        return None
    store = end.src[0]
    if store.op is not Ops.STORE or len(store.src) != 2:
        return None
    ranges = end.src[1:]
    if any(r.op is not Ops.RANGE or extent(r) is None for r in ranges):
        return None
    return store, tuple(ranges)


def extent(rng) -> int | None:
    """How many steps a RANGE takes, or None when that is not a constant.

    A symbolic extent is the dynamic-shape machinery, which is the line this
    reader does not cross: a kernel is dispatched at a size known here.
    """
    if rng.op is not Ops.RANGE or len(rng.src) != 1:
        return None
    n = rng.src[0]
    return n.arg if n.op is Ops.CONST and type(n.arg) is int else None


def reduces(rng) -> bool:
    """Whether this RANGE is a reduced axis rather than a loop one.

    0.13 tags every range `(id, AxisType)`, so what a read is under is on the
    range itself -- no flag has to be threaded down the tree to find out.
    """
    kind = getattr(rng, "arg", None)
    return bool(kind) and getattr(kind[1], "name", "") == "REDUCE"


def affine(idx, axes: tuple) -> tuple | None:
    """`idx`'s coefficient on each of `axes` -- its stride row -- or None.

    None for any index that is not a zero-offset affine sum over `axes`: a
    divide, a modulo or a leftover constant is a reshape or a window this does
    not read, and guessing a stride wrong puts a wrong number in an address.
    """
    got = _terms(idx)
    if got is None or got.pop(None, 0) != 0:
        return None
    if any(rng not in axes for rng in got):
        return None
    return tuple(got.get(rng, 0) for rng in axes)


def _terms(u) -> dict | None:
    """`{range: coefficient}` for an affine `u`, with the constant under None."""
    if u.op is Ops.RANGE:
        return {u: 1}
    if u.op is Ops.CONST and type(u.arg) is int:
        return {None: u.arg}
    if u.op is Ops.ADD and len(u.src) == 2:
        left, right = (_terms(s) for s in u.src)
        if left is None or right is None:
            return None
        out = dict(left)
        for rng, coeff in right.items():
            out[rng] = out.get(rng, 0) + coeff
        return out
    if u.op is Ops.MUL and len(u.src) == 2:
        for held, const in (u.src, u.src[::-1]):
            if const.op is not Ops.CONST or type(const.arg) is not int:
                continue
            got = _terms(held)
            return None if got is None else {k: v * const.arg for k, v in got.items()}
    return None


def indexed(u, axes: tuple):
    """`(param index, strides)` for an INDEX straight off a PARAM, or None."""
    if u.op is not Ops.INDEX or len(u.src) != 2:
        return None
    param, idx = u.src
    if param.op is not Ops.PARAM:
        return None
    strides = affine(idx, axes)
    return None if strides is None else (param.arg, strides)


def held(param) -> int | None:
    """How many elements the buffer behind a PARAM holds."""
    return getattr(param.dtype, "size", None)


def order(store, ranges: tuple) -> tuple | None:
    """`ranges` in the result's own row-major order, or None if it is not one.

    Sorted by the stride each carries in the STORE's index, widest first, which
    is what makes the coefficient tuples downstream read as `[M][N]` rows rather
    than in whatever order the scheduler happened to nest the loops.
    """
    idx = store.src[0]
    if idx.op is not Ops.INDEX or len(idx.src) != 2:
        return None
    strides = affine(idx.src[1], ranges)
    if strides is None or 0 in strides:
        return None
    return tuple(r for _, r in sorted(zip(strides, ranges), key=_widest))


def _widest(pair):
    """Sort key putting the widest stride first, ties broken by axis id."""
    stride, rng = pair
    return -stride, rng.arg[0]


def reduced(u) -> tuple | None:
    """`(operator, value, reduced ranges)` for a REDUCE, or None.

    0.13 spells a reduction `REDUCE(value, *ranges)` with the operator in the
    arg, where 0.10 named axis NUMBERS against a shape. Naming the ranges is the
    better shape for us: the axis it folds is the one we are already reading
    strides against.
    """
    if u.op is not Ops.REDUCE or len(u.src) < 2 or not isinstance(u.arg, tuple):
        return None
    op = u.arg[0]
    ranges = u.src[1:]
    if any(r.op is not Ops.RANGE or extent(r) is None for r in ranges):
        return None
    return op, u.src[0], tuple(ranges)
