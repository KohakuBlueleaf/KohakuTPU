"""GroupNorm, and GroupNorm fused with SiLU -- the SDXL UNet's normalisation.

A group is a ROW here: the caller reshapes `(N, C, H, W)` to `(N*G, C/G*H*W)`,
so normalising a group is normalising a row and the leading axes are a batch.
That puts the group size under `VRED`'s vector length -- see :func:`group_norm`.

TWO PASSES, NOT `E[x^2] - E[x]^2`. The lane carries 16 significand bits and that
identity cancels: at a group of 8192 with a mean 64 sigma off zero it returns the
variance 21.5% low, against 2.1e-05 for the form below. Measured, in this lane's
own arithmetic.
"""

from kohakuaccel.lang import dims, units
from kohakutpu.hw import vector as V
from kohakutpu.lang import kernel
from kohakutpu.ops.activation import sigmoid

from kohakutpu import lang as L

G, NPG = dims("G, NPG")
R, W = dims("R, W")

#: One vector pass, and so the row `spread` and `fold` work in.
VLMAX = V.VLMAX


@kernel
def group_norm_staged(
    x=L.In(..., G, NPG),
    w=L.In(..., G, NPG),
    b=L.In(..., G, NPG),
    y=L.Out(..., G, NPG),
    *,
    eps=1e-5,
):
    """``(x - mean) * rstd * w + b`` over each row, a pass at a time.

    SUPERSEDED by :func:`kohakutpu.kernels.group_norm_fused`, which ships as
    `group_norm`: eight stages against one, 1254 flits against 416 at 64x64, at
    lower error. Kept as the staged reference the fused form is graded against.

    `w` and `b` arrive at full shape, not per channel: an elementwise pass reads
    operands of one length. `NPG` is the group size and is a reduction row, so it
    is at most `VLMAX` and a multiple of 16; `vec_core` raises F_REDVL otherwise.
    """
    total, mu, dev = L.temp(G, NPG), L.temp(G, NPG), L.temp(G, NPG)
    sq, var, inv, scaled = (L.temp(G, NPG) for _ in range(4))

    total <<= L.row_sum(x)
    mu <<= total / NPG
    dev <<= x - mu
    # Divided BEFORE squaring, as in rmsnorm: a group of 128 at RMS 30 sums to
    # 115200 and fp16 stops at 65504, after which rsqrt returns zero.
    sq <<= (dev / NPG) * dev
    var <<= L.row_sum(sq)
    inv <<= L.rsqrt(var + eps)
    scaled <<= dev * inv
    y <<= scaled * w + b


@kernel
def group_norm_silu(
    x=L.In(..., G, NPG),
    w=L.In(..., G, NPG),
    b=L.In(..., G, NPG),
    y=L.Out(..., G, NPG),
    *,
    eps=1e-5,
):
    """``silu(group_norm(x, w, b))`` as ONE kernel.

    The pair every UNet block uses, and the normalised tile never leaves the
    card between them. Two passes rather than one because `h * sigmoid(h)` reads
    `h` twice, and a vector chain carries one running result.
    """
    total, mu, dev = L.temp(G, NPG), L.temp(G, NPG), L.temp(G, NPG)
    sq, var, inv, scaled = (L.temp(G, NPG) for _ in range(4))
    h = L.temp(G, NPG)

    total <<= L.row_sum(x)
    mu <<= total / NPG
    dev <<= x - mu
    sq <<= (dev / NPG) * dev
    var <<= L.row_sum(sq)
    inv <<= L.rsqrt(var + eps)
    scaled <<= dev * inv
    h <<= scaled * w + b
    y <<= h * sigmoid(h)


def fold(held, rows: int, width: int, part: int, join=None):
    """Join `rows` rows of `width` down to one, pairwise. Returns the `(1, W)` buffer.

    A CROSS-ROW reduction, which `VRED` cannot do -- it runs along a row. Adding
    a buffer to itself offset by half its rows is the same tree and costs
    `log2(rows)` passes. Exact and order-free, so the shape of the tree is free.
    An odd count carries its last row into the next level rather than dropping it.

    `join` combines two rows and defaults to addition; :func:`kohakutpu.lang.maximum`
    gives the cross-row maximum, which is the same tree over a different monoid.
    """
    join = join or _add
    tails, n = [], rows
    while n > 1:
        if n % 2:
            # Lifted into a buffer of its OWN length: a pass whose operands
            # reach different distances is refused, and rightly.
            tail = L.temp(1, width)
            with units(1) as e:
                tail[e] <<= held.rows_from(n - 1)[e] * 1.0
            tails.append(tail)
            n -= 1
        half = n // 2
        nxt = L.temp(half, width)
        with units(nxt.parts(part)) as e:
            nxt[e] <<= join(held.rows_from(0)[e], held.rows_from(half)[e])
        held, n = nxt, half
    for tail in tails:
        nxt = L.temp(1, width)
        with units(1) as e:
            nxt[e] <<= join(held[e], tail[e])
        held = nxt
    return held


def _add(a, b):
    """The default `join`, kept a function so the emitted chain is unchanged."""
    return a + b


def spread(one, rows: int, width: int, part: int):
    """Copy a `(1, W)` buffer into every row of a `(rows, W)` one. Returns it.

    The read side of `fold`, and the thing a reduction cannot do: `VRED`
    broadcasts along the row it reduced and nothing spreads a value across rows.
    Doubling, so `log2(rows)` levels, each writing a whole number of `VLMAX`
    passes -- which is what makes the writes land on exactly their own rows.
    """
    held, have = one, 1
    while have < rows:
        take = min(have, rows - have)
        nxt = L.temp(have + take, width)
        # Gridded over the SOURCE: one instance covers one `part`, and what a
        # copy has to cover is what there is to copy.
        with units(held.parts(part)) as e:
            nxt.rows_from(0)[e] <<= held[e]
        with units(held.parts(part)) as e:
            nxt.rows_from(have)[e] <<= held[e]
        held, have = nxt, have + take
    return held


@kernel
def group_stats(
    x=L.In(..., R, W),
    mean=L.Out(1, W),
    var=L.Out(1, W),
    *,
    rows=8,
    width=128,
    part=8192,
):
    """The mean and variance of ONE group of `rows * width` elements.

    Lifts the 128-element ceiling `group_norm` has: a group arrives as `rows` of
    `width`, `row_sum` reduces within a row and :func:`fold` across them. `rows`
    must be a power of two and `width` at most VLMAX; both are compile-time, so
    they are knobs and the port shape has to agree with them.

    Both results are `(1, W)` with the value in every lane. Two passes over the
    whole group: :func:`fold` reduces to the mean, :func:`spread` carries it back
    to full shape, and the deviations are folded in turn -- so no identity stands
    in for the mean and the error does not grow with it. At 163,840 elements,
    SDXL's largest group: mean 2.4e-04, variance 7.7e-04 at a mean 8 sigma off
    zero, against 7.5e-02 for the form that avoids the spread.
    """
    if width != VLMAX:
        raise ValueError(
            f"group_stats needs width == {VLMAX}: `spread` writes one row block "
            f"per pass and a pass is {VLMAX} elements, so a narrower row would "
            f"be written over by the block after it. Got {width}"
        )
    xs, dev, sq, qs = (L.temp(rows, width) for _ in range(4))

    # EVERY partial is scaled before it is folded. A group of 163,840 sums to
    # far past fp16's 65,504, and so does the constant `rows * width` itself.
    xs <<= x / rows
    total = fold(xs, rows, width, part)

    summed, mu = L.temp(1, width), L.temp(1, width)
    summed <<= L.row_sum(total)
    mu <<= summed / width
    mean <<= mu * 1.0

    # TWO PASSES over the whole group, which is what `spread` buys: the mean
    # comes back at full shape, so no identity has to stand in for it.
    dev <<= x - spread(mu, rows, width, part)
    sq <<= (dev / width) * dev
    qs <<= L.row_sum(sq)
    within = fold(qs, rows, width, part)
    var <<= within / rows
