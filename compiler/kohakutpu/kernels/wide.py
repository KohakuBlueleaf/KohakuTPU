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
from kohakutpu.ops.activation import sigmoid

from kohakutpu import lang as L

R, W = dims("R, W")

#: One reduction pass. Re-exported here because every caller of these kernels
#: needs it to compute `rows`.
VLMAX = L.VLMAX

#: Elements one vector instance handles, and a whole number of GROUPS: a spread
#: resumes at group starts, and `_legal` refuses a `part` that is not.
PART = 8192


def part_for(group: int, want: int = PART) -> int:
    """The largest whole number of `group`-element groups at most `want`.

    The `part=` a caller must pass alongside `rows=`: a group of 1280 does not
    divide :data:`PART`, and `_legal` refuses a part that cuts one. Returns
    `want` exactly where `group` divides it, so a shape that already ran keeps
    the program it ran; one whole group where the group is wider than `want`.

    A kernel cannot do this itself -- an extent is a symbol while tracing, and
    the knob is read off `Compiled.knobs` rather than off the trace.
    """
    return max(1, want // group) * group


def split(cols: int, width: int = VLMAX) -> int:
    """Sub-rows a `cols`-wide row splits into. The `rows=` every kernel here takes.

    Raises :class:`ValueError` when `width` is not VLMAX, or when it does not
    divide `cols` -- a sub-row is one whole reduction pass and a partial one
    would be written over by the sub-row behind it.

    The count NEED NOT be a power of two: :func:`fold_flat` splits an odd count
    and joins the halves, for passes rather than reach. SDXL's widths are 640
    and 1280, which are 5 and 10 sub-rows, and neither is a power of two.
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
    narrower row; and `part` must be whole groups, or an instance starts part
    way through one and the spread reads every later group misaligned.

    `rows` is NOT required to be a power of two. It was, while every fold level
    halved what was left and an odd count had no partner; :func:`fold_flat` now
    splits an odd count instead of dropping the sub-row that has none.
    """
    if width != VLMAX:
        raise ValueError(
            f"a sub-row must be a whole pass, which is {VLMAX} elements: a pass "
            f"writes one sub-row block, so a narrower one would be written over "
            f"by the block after it. Got width={width}"
        )
    if rows < 1:
        raise ValueError(f"a group is at least one sub-row. Got rows={rows}")
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

    EVERY LEVEL'S TEMP IS EXACTLY WHAT ITS PASS WRITES: a full-length one keeps
    a tail nothing wrote, arbitrary on the card, and one Inf there reaches a lane.

    The result is `held.rows - (rows - 1)` sub-rows however `rows` factorises,
    which is forced from both sides: one shorter and the last group's start falls
    off the end, one longer and some level read past its own operand. So a
    non-power-of-two split costs PASSES, never reach -- see :func:`_fold_span`.
    """
    out = held.rows - (rows - 1)
    return _fold_span(held, 0, rows, out, held.rows, width, part, join or _add)


def _fold_span(src, off, count, out, rows, width: int, part: int, join):
    """`count` sub-rows of `src` from `off`, joined into sub-row 0 of `out` rows.

    An EVEN count halves, the shifted add this fold has always been. An ODD one
    splits at the power of two below it and joins two partial buffers, both of
    exactly `out` rows -- a pass reads ONE length, so nothing else is available.

    `count == 1` is a VIEW and no pass; the invariant
    `rows - off - count + 1 == out` makes its reach exactly `out` rows, so the
    join above it agrees. A spread cannot serve here at all: it reaches whole
    groups, `m * rows`, and `out` never is one.
    """
    if count == 1:
        return src.rows_from(off) if off else src
    if not count % 2:
        half = count // 2
        left = rows - off - half
        nxt = L.temp(out if count == 2 else left, width)
        with units(nxt.parts(part)) as e:
            nxt[e] <<= join(src.rows_from(off)[e], src.rows_from(off + half)[e])
        if count == 2:
            return nxt
        return _fold_span(nxt, 0, half, out, left, width, part, join)
    top = 1 << (count.bit_length() - 1)
    big = _fold_span(src, off, top, out, rows, width, part, join)
    rest = _fold_span(src, off + top, count - top, out, rows, width, part, join)
    joined = L.temp(out, width)
    with units(joined.parts(part)) as e:
        joined[e] <<= join(big[e], rest[e])
    return joined


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
    out <<= _normed(v, rows, width, part, eps) * wv + bv


def _normed(v, rows: int, width: int, part: int, eps: float):
    """``(x - mean) * rsqrt(var + eps)`` over groups of `rows * width`.

    The arithmetic :func:`layernorm_wide` and :func:`group_norm_wide` share, and
    the reason they are one implementation: a GroupNorm is a LayerNorm whose
    `rows` span the group rather than the channel axis.
    """
    mu = group_mean(v, rows, width, part)
    dev = L.temp(v.rows, width)
    dev <<= v - mu.per_group(rows)

    msq = group_msq(dev, rows, width, part)
    inv = L.temp(msq.rows, width)
    inv <<= L.rsqrt(msq + eps)

    scaled = L.temp(v.rows, width)
    scaled <<= dev * inv.per_group(rows)
    return scaled


@kernel
def group_norm_wide(
    x=L.In(..., R, W),
    w=L.In(..., R, W),
    b=L.In(..., R, W),
    y=L.Out(..., R, W),
    *,
    eps=1e-5,
    rows=320,
    width=VLMAX,
    part=PART,
):
    """``(x - mu) * rstd * w + b`` over groups of `rows * width`, EVERY GROUP AT ONCE.

    `(N, C, H, W)` arrives reshaped to `(N*G, C/G * H * W)`, so a group is a row
    and `rows` is that row's sub-rows. SDXL's three group sizes are 163,840,
    81,920 and 40,960 elements, which are 1280, 640 and 320 sub-rows.

    `w` and `b` arrive at FULL shape, not per channel: a per-channel gain over a
    plane would be a spread taking ONE element of every `H*W`, and a spread's
    sub-row is whole 16-element words.

    Raises :class:`ValueError` for a split this fold cannot walk.
    """
    _legal(rows, width, part)
    v, wv, bv, out = [t.as_rows(width) for t in (x, w, b, y)]
    out <<= _normed(v, rows, width, part, eps) * wv + bv


@kernel
def group_norm_silu_wide(
    x=L.In(..., R, W),
    w=L.In(..., R, W),
    b=L.In(..., R, W),
    y=L.Out(..., R, W),
    *,
    eps=1e-5,
    rows=320,
    width=VLMAX,
    part=PART,
):
    """``silu(group_norm(x, w, b))`` as ONE kernel, at SDXL's group sizes.

    THE pair every UNet and VAE resnet issues -- `norm -> act -> conv`, so it is
    the norm and the activation that touch, not the conv and the activation.
    Two passes rather than one because `h * sigmoid(h)` reads `h` twice and a
    vector chain carries one running result.
    """
    _legal(rows, width, part)
    v, wv, bv, out = [t.as_rows(width) for t in (x, w, b, y)]
    h = L.temp(v.rows, width)
    h <<= _normed(v, rows, width, part, eps) * wv + bv
    out <<= h * sigmoid(h)


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
