"""Row-wise kernels for rows wider than one reduction pass.

`VRED` folds at most VLMAX lanes, so `softmax`, `layernorm` and `rmsnorm` refuse
a row past 128 -- and a DiT's model dim is 1024. The way round is the hierarchy
`groupnorm.group_stats` is built on: the row arrives as `rows` sub-rows of
`width`, `row_sum` reduces WITHIN one and :func:`fold_flat` ACROSS them.

EVERY ROW AT ONCE, so `(L, D)` arrives as `(L*D/width, width)` and one grid
instance covers `part` elements of it rather than one row. The fold is shifted
adds, which are translation-invariant; the SPREAD is not, and is a periodic READ
-- `Buffer.per_group`, a stride-0 AGU dimension and no pass at all.
"""

from kohakuaccel.lang import dims, units
from kohakutpu.lang import kernel
from kohakutpu.ops import LOG2E

from kohakutpu import lang as L

R, W = dims("R, W")

#: One reduction pass. Re-exported here because every caller of these kernels
#: needs it to compute `rows`.
VLMAX = L.VLMAX

#: Elements one vector instance handles, and a whole number of GROUPS: a spread
#: resumes at group starts, and `_legal` refuses a `part` that is not.
PART = 8192


def split(cols: int, width: int = VLMAX) -> int:
    """Sub-rows a `cols`-wide row splits into. The `rows=` every kernel here takes.

    Raises :class:`ValueError` when `width` is not VLMAX, when it does not divide
    `cols` -- a sub-row is one whole reduction pass and a partial one would be
    written over by the sub-row behind it -- or when the split is not a power of
    two, which the fold's halving needs.
    """
    _legal(1, width)
    if cols % width:
        raise ValueError(
            f"a {cols}-wide row does not split into {width}-wide sub-rows; the "
            f"tail would be a partial pass. Pad the row to a multiple of {width}"
        )
    return cols // width


def _legal(rows: int, width: int, part: int | None = None) -> None:
    """Raise :class:`ValueError` unless the split is one this fold can walk.

    `width` must be VLMAX, or a pass writes one whole sub-row block over a
    narrower row; `rows` must be a power of two, since every fold level halves
    what is left; and `part` must be whole groups, or an instance starts part
    way through one and the spread reads every later group misaligned.
    """
    if width != VLMAX:
        raise ValueError(
            f"a sub-row must be a whole pass, which is {VLMAX} elements: a pass "
            f"writes one sub-row block, so a narrower one would be written over "
            f"by the block after it. Got width={width}"
        )
    if rows < 1 or rows & (rows - 1):
        raise ValueError(
            f"a group is a power of two sub-rows: every fold level pairs the "
            f"halves of what is left, and an odd count has no partner. Got "
            f"rows={rows}"
        )
    if part is not None and part % (rows * width):
        raise ValueError(
            f"part={part} is not whole {rows * width}-element groups, so an "
            f"instance would start part way through one and the spread would "
            f"read every group after it at the wrong offset"
        )


def _add(a, b):
    """The default `join`, kept a function so the emitted chain is unchanged."""
    return a + b


def fold_flat(held, rows: int, width: int, part: int, join=None):
    """Join every group's `rows` sub-rows down to one, ALL GROUPS AT ONCE.

    Returns the buffer holding each group's total at its OWN group start, sub-row
    `g * rows`, which is where :meth:`kohakutpu.lang.Buffer.per_group` reads it.
    `join` combines two sub-rows and defaults to addition; `L.maximum` gives the
    cross-row maximum, the same tree over a different monoid.

    EVERY LEVEL'S TEMP IS EXACTLY WHAT ITS PASS WRITES. The pass is clamped to
    the shorter of the two shifted operands, so a full-length temp would keep a
    tail nothing wrote -- zero on the model and arbitrary on the card, and one
    Inf there reaches a lane. Sized this way there is no such tail to read.
    """
    join = join or _add
    n = rows
    while n > 1:
        half = n // 2
        nxt = L.temp(held.rows - half, width)
        with units(nxt.parts(part)) as e:
            nxt[e] <<= join(held.rows_from(0)[e], held.rows_from(half)[e])
        held, n = nxt, half
    return held


def group_mean(v, rows: int, width: int, part: int):
    """The mean of every `(rows, width)` group of `v`, at each group's start.

    TWO SCALINGS, never one: `v` is divided by `rows` before the cross-row fold
    and the row sum by `width` after it, so no partial reaches fp16's 65,504.
    A group of 163,840 sums far past it, and so does `rows * width` itself.
    """
    scaled, summed = L.temp(v.rows, width), L.temp(v.rows, width)
    scaled <<= v / rows
    summed <<= L.row_sum(scaled)
    total = fold_flat(summed, rows, width, part)

    out = L.temp(total.rows, width)
    out <<= total / width
    return out


def group_msq(v, rows: int, width: int, part: int):
    """The mean of `v * v` over every group, at each group's start.

    Divided BEFORE squaring, as `rmsnorm` is: a group of 128 at RMS 30 sums to
    115200 and fp16 stops at 65504, after which `rsqrt` returns zero.
    """
    sq, qs = L.temp(v.rows, width), L.temp(v.rows, width)
    sq <<= (v / width) * v
    qs <<= L.row_sum(sq)
    within = fold_flat(qs, rows, width, part)

    out = L.temp(within.rows, width)
    out <<= within / rows
    return out


def group_max(v, rows: int, width: int, part: int):
    """The largest element of every group, at each group's start.

    The same tree as :func:`group_mean` over a different monoid: `maximum` across
    the sub-rows, then `row_max` along the one that is left. Nothing is scaled --
    a maximum cannot overflow what its own operands fit in.
    """
    top = fold_flat(v, rows, width, part, join=L.maximum)
    out = L.temp(top.rows, width)
    out <<= L.row_max(top)
    return out


@kernel
def layernorm_wide(
    x=L.In(..., R, W),
    w=L.In(..., R, W),
    b=L.In(..., R, W),
    y=L.Out(..., R, W),
    *,
    eps=1e-5,
    rows=8,
    width=VLMAX,
    part=PART,
):
    """``(x - mean) * rsqrt(var + eps) * w + b`` over rows of `rows * width`.

    Every row at once; `(L, D)` may be passed as it is, with `rows = D/width`
    from :func:`split`.

    TWO PASSES over the row, not `E[x^2] - E[x]^2`: that identity returns the
    variance 25% low at a mean 64 sigma off zero on this lane's 16-bit
    significand, and a post-projection activation is never centred.

    Raises :class:`ValueError` for a split this fold cannot walk.
    """
    _legal(rows, width, part)
    v, wv, bv, out = [t.as_rows(width) for t in (x, w, b, y)]
    mu = group_mean(v, rows, width, part)

    dev = L.temp(v.rows, width)
    dev <<= v - mu.per_group(rows)

    msq = group_msq(dev, rows, width, part)
    inv = L.temp(msq.rows, width)
    inv <<= L.rsqrt(msq + eps)

    scaled = L.temp(v.rows, width)
    scaled <<= dev * inv.per_group(rows)
    out <<= scaled * wv + bv


@kernel
def rmsnorm_wide(
    x=L.In(..., R, W),
    w=L.In(..., R, W),
    y=L.Out(..., R, W),
    *,
    eps=1e-5,
    rows=8,
    width=VLMAX,
    part=PART,
):
    """``x * rsqrt(mean(x^2) + eps) * w`` over rows of `rows * width`.

    Every row at once, as :func:`layernorm_wide` takes them. `w` arrives at full
    shape, not per channel: an elementwise pass reads operands of one length.
    Raises :class:`ValueError` for a split this fold cannot walk.
    """
    _legal(rows, width, part)
    v, wv, out = [t.as_rows(width) for t in (x, w, y)]
    msq = group_msq(v, rows, width, part)
    inv = L.temp(msq.rows, width)
    inv <<= L.rsqrt(msq + eps)
    out <<= v * inv.per_group(rows) * wv


@kernel
def softmax_wide(
    x=L.In(..., R, W),
    y=L.Out(..., R, W),
    *,
    rows=8,
    width=VLMAX,
    part=PART,
):
    """Softmax over rows of `rows * width`, every row at once.

    The maximum needs the same hierarchy the sum does -- `row_max` reaches one
    sub-row and the group is `rows` of them -- so both are folded across the
    sub-rows before either is used.

    `(L, D)` may be passed as it is -- `as_rows` is the identity on a tensor
    already shaped `(R, width)`. Raises :class:`ValueError` for a bad split.
    """
    _legal(rows, width, part)
    v, out = x.as_rows(width), y.as_rows(width)
    hi = group_max(v, rows, width, part)

    ex = L.temp(v.rows, width)
    ex <<= L.exp2((v - hi.per_group(rows)) * LOG2E)

    # The MEAN comes back from the fold, so the sum is one multiply on the
    # folded buffer rather than a pass over the row.
    mean = group_mean(ex, rows, width, part)
    total = L.temp(mean.rows, width)
    total <<= mean * float(rows * width)
    out <<= ex / total.per_group(rows)
