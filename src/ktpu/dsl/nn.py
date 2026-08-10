"""Composed kernels: attention and MoE.

Both are written in ordinary Python over tracers. The loops are compile-time --
the block, head and expert counts are plain ints -- so they unroll, and the
state carried across iterations is tracers threaded through the loop. Every
option on `attention` is a compile-time `if`, so a configuration that is off
costs nothing: it is not in the graph.
"""

from ktpu.dsl.library import silu, softmax
from ktpu.dsl.ops import exp2, maximum
from ktpu.dsl.tracer import Tracer

LOG2_E = 1.4426950408889634


def _pair(v, what: str) -> tuple[int, int]:
    if isinstance(v, int):
        return (v, v)
    a, b = v
    return (int(a), int(b))


def _band(x: Tracer, axis: int, lo: int, hi: int) -> Tracer:
    key = [slice(None)] * x.rank
    key[axis] = slice(lo, hi)
    return x[tuple(key)]


def _pick(x: Tracer, axis: int, start: int, count: int, step: int) -> Tracer:
    """`count` elements from `start` along `axis`, every `step`-th one.

    A strided slice is a gather and the tracer refuses it, so a stride is
    expressed as reshape-and-drop instead: split the axis into `(count, step)`
    and keep column 0. That is a view, and the skipped elements are never read.
    """
    if step == 1:
        return _band(x, axis, start, start + count)
    v = _band(x, axis, start, start + count * step)
    head, tail = list(v.shape[:axis]), list(v.shape[axis + 1 :])
    v = v.reshape(*head, count, step, *tail)
    return v[(slice(None),) * (axis + 1) + (0,)]


def conv2d(x: Tracer, w: Tracer, stride=1, padding=0) -> Tracer:
    """`(N, H, W, C)` against `(KH, KW, C, F)` -> `(N, Ho, Wo, F)`.

    Read as KH*KW matmuls, NOT as an im2col. For one filter tap the input is a
    plain OFFSET VIEW, so the contraction is `(N*Ho*Wo, C) @ (C, F)` and the
    taps accumulate into one result. Nothing is materialised and no data moves,
    which is `docs/compute/tensor-isa.md` §3.2 -- "a convolution is a matmul
    with a more interesting descriptor" -- with the descriptor carried by the
    view the slice already is.

    An im2col would expand the operand by KH*KW in both footprint and
    bandwidth; this expands nothing and costs KH*KW accumulating matmuls, which
    is the work the convolution was always going to do.
    """
    if x.rank != 4:
        raise ValueError(f"conv2d expects (N, H, W, C), got {x.shape}")
    if w.rank != 4:
        raise ValueError(f"conv2d expects a (KH, KW, C, F) filter, got {w.shape}")
    n, h, wd, c = x.shape
    kh, kw, cw, f = w.shape
    if c != cw:
        raise ValueError(
            f"conv2d channel mismatch: input has {c}, filter expects {cw} "
            f"({x.shape} against {w.shape})"
        )
    sh, sw = _pair(stride, "stride")
    ph, pw = _pair(padding, "padding")
    ho = (h + 2 * ph - kh) // sh + 1
    wo = (wd + 2 * pw - kw) // sw + 1
    if ho <= 0 or wo <= 0:
        raise ValueError(
            f"conv2d output would be {ho}x{wo}: {x.shape} with a {kh}x{kw} "
            f"filter, stride {(sh, sw)}, padding {(ph, pw)}"
        )

    # `_pick` reshapes a whole (count, step) block, so the tail has to exist.
    # ho*sh >= h + 2*ph - kh + 1 by construction, so hp >= h + 2*ph and the
    # extra never underruns the requested padding.
    hp = (kh - 1) + ho * sh
    wp = (kw - 1) + wo * sw
    xp = x.pad(((0, 0), (ph, hp - h - ph), (pw, wp - wd - pw), (0, 0)))

    acc = None
    for ky in range(kh):
        for kx in range(kw):
            v = _pick(xp, 1, ky, ho, sh)
            v = _pick(v, 2, kx, wo, sw)
            part = v.reshape(n * ho * wo, c) @ w[ky, kx]
            acc = part if acc is None else acc + part
    return acc.reshape(n, ho, wo, f)


def conv1d(x: Tracer, w: Tracer, stride=1, padding=0) -> Tracer:
    """`(N, L, C)` against `(K, C, F)` -> `(N, Lo, F)`.

    The 2D case with a unit height, so there is one implementation to be wrong
    in rather than two.
    """
    if x.rank != 3:
        raise ValueError(f"conv1d expects (N, L, C), got {x.shape}")
    if w.rank != 3:
        raise ValueError(f"conv1d expects a (K, C, F) filter, got {w.shape}")
    n, ell, c = x.shape
    k, cw, f = w.shape
    s = stride if isinstance(stride, int) else int(stride[0])
    p = padding if isinstance(padding, int) else int(padding[0])
    out = conv2d(
        x.reshape(n, 1, ell, c),
        w.reshape(1, k, cw, f),
        stride=(1, s),
        padding=(0, p),
    )
    return out.reshape(n, out.shape[2], f)


def _head(x: Tracer, rank: int, b: int, h: int) -> Tracer:
    """The `(L, D)` slab for batch `b`, head `h`, from a rank 2/3/4 operand.

    Both index steps are OUTER axes, so each is a plain offset rather than a
    stride list -- which is why `(H, L, D)` and `(B, H, L, D)` lower and the
    un-transposed `(L, H*D)` does not (docs/limits.md s6.3).
    """
    match rank:
        case 2:
            return x
        case 3:
            return x[h]
        case _:
            return x[b][h]


#: `(x + BIG) - BIG` rounds to an integer while the significand is 16 bits or
#: fewer; widen it and the sum lands where ulp < 1 and `_ceil` stops rounding.
BIG = 32768.0


def _ceil(x: Tracer) -> Tracer:
    """An integer no smaller than `x`, in three elementwise ops.

    The level-1 op set has no `ceil` -- the hardware computes `floor` inside
    `exp2` as a bit slice but never exposes it -- so this is the magic-number
    round: adding `BIG` pushes every fraction below the ulp, and subtracting it
    again leaves the integer. The `+ 1` first makes the result at least `x`
    rather than merely near it, which is what `_attend` needs and rounding to
    nearest does not give.
    """
    return (x + 1.0 + BIG) - BIG


def _attend(q, k, v, scale, block, lo, hi, tri=None, pow2_corr=False):
    """Online softmax over key blocks `[lo, hi)`, returning `(rows, D)`.

    `scale` arrives ALREADY IN LOG2 SPACE, so each `exp2` takes its operand
    directly; `exp2((s - m) * log2 e)` would cost a vector pass per exp2, and
    `passes/scalefold.py` removes it from kernels not written this way.

    With `pow2_corr`, the running max is rounded UP to an integer so every
    `corr` is exactly a power of two and the accumulator could apply it as an
    exponent bias rather than a vector pass -- the `row_rescale` precondition.
    Up rather than to nearest keeps `s - m` non-positive, so `exp2` cannot
    overflow. OFF, because it does not pay: `_ceil` is narrow-span work between
    a wide reduction and its wide consumer, so it splits `[mul, rmax, sub,
    exp2, sum]` into three bands and sends `s` back through L1. Measured on
    attention full+causal it costs +170 instructions and +572 vector cycles,
    and `row_rescale` returns less than that even once built.

    `tri` is a 0/1 constant applied to `p` on the LAST block only, which is how
    causal handles the one block straddling the diagonal. Zeroing AFTER the
    exponential is exact: softmax is shift invariant, so a row max taken over
    the whole block rather than only its visible part just makes `p` smaller.
    A row of the diagonal block always keeps its diagonal element, so the
    denominator is never zero.
    """
    m = ell = acc = corr = None
    for j in range(lo, hi):
        s = (q @ k[j * block : (j + 1) * block].transpose()) * scale
        mj = s.max(axis=-1, keepdim=True)
        if pow2_corr:
            mj = _ceil(mj)
        if m is None:
            m = mj
            p = exp2(s - m)
        else:
            m2 = maximum(m, mj)
            corr = exp2(m - m2)
            p = exp2(s - m2)
            m = m2
        if tri is not None and j == hi - 1:
            p = p * tri
        vj = v[j * block : (j + 1) * block]
        if acc is None:
            ell, acc = p.sum(axis=-1, keepdim=True), p @ vj
        else:
            ell = ell * corr + p.sum(axis=-1, keepdim=True)
            acc = acc * corr + p @ vj
    return acc / ell


def attention(
    q: Tracer,
    k: Tracer,
    v: Tracer,
    scale: float | None = None,
    block: int = 64,
    causal: bool = False,
    tri: Tracer | None = None,
    wo: Tracer | None = None,
    pow2_corr: bool = False,
):
    """Flash attention: single or multi head, optionally GQA, optionally causal.

    `q`, `k`, `v` share a rank: `(L, D)`, `(H, L, D)` or `(B, H, L, D)` -- heads
    on their own axis, as torch leaves them after `.transpose(1, 2)`; `(L, H*D)`
    does not lower (docs/limits.md s6.3). Fewer heads in `k`/`v` is GQA: query
    head `h` reads kv head `h // (H // H_kv)`. `causal` needs `tri`, a
    `(block, block)` lower triangular 0/1 constant, and reads only key blocks
    `0..i` for query block `i`. `wo`, `(H*D, Dm)`, fuses the output projection.

    Returns one tensor, or a tuple ordered batch, query block, head when joining
    would need `concat` (s6.2): `wo` collapses the head axis, causal splits by
    query block. Raises ValueError on a bad or mismatched rank, an indivisible
    block, a kv count not dividing the heads, or `causal` without `tri`.

    `pow2_corr` rounds the running max up so every rescale is a power of two,
    which `row_rescale` needs and which currently costs more than it saves --
    see `_attend`.
    """
    rank = len(q.shape)
    if rank not in (2, 3, 4):
        raise ValueError(f"attention takes rank 2, 3 or 4 operands, got {rank}")
    if len(k.shape) != rank or len(v.shape) != rank:
        raise ValueError(
            f"q is rank {rank} but k is {len(k.shape)} and v is {len(v.shape)}; "
            "all three must agree"
        )
    tlen, chan = q.shape[-2], q.shape[-1]
    klen = k.shape[-2]
    if klen % block or tlen % block:
        raise ValueError(f"block {block} does not divide q {tlen} / k {klen}")
    if causal and tri is None:
        raise ValueError("causal attention needs tri, a (block, block) 0/1 constant")
    if causal and tlen != klen:
        raise ValueError(
            f"causal with {tlen} query rows against {klen} key rows: query "
            "block i is aligned to key block i, so a shorter query would "
            "attend a prefix that is not its own. Slice the keys to match, or "
            "pass an offset-aware tri per block"
        )

    batch = q.shape[0] if rank == 4 else 1
    heads = q.shape[-3] if rank >= 3 else 1
    kv_heads = k.shape[-3] if rank >= 3 else 1
    if heads % kv_heads:
        raise ValueError(f"{kv_heads} kv heads does not divide {heads} query heads")
    group = heads // kv_heads
    # Into log2 space once, here: `_attend` then exponentiates without a
    # per-exp2 multiply, and the row max it rounds is already a base-2 one.
    scale = (chan**-0.5 if scale is None else scale) * LOG2_E

    pieces = []
    for b in range(batch):
        for i in range(tlen // block if causal else 1):
            joined, per_head = None, []
            for h in range(heads):
                qh = _head(q, rank, b, h)
                out = _attend(
                    qh[i * block : (i + 1) * block] if causal else qh,
                    _head(k, rank, b, h // group),
                    _head(v, rank, b, h // group),
                    scale,
                    block,
                    0,
                    i + 1 if causal else klen // block,
                    tri if causal else None,
                    pow2_corr,
                )
                if wo is None:
                    per_head.append(out)
                else:
                    y = out @ wo[h * chan : (h + 1) * chan]
                    joined = y if joined is None else joined + y
            pieces += per_head if wo is None else [joined]
    return pieces[0] if len(pieces) == 1 else tuple(pieces)


def moe_dense(x: Tracer, wg: Tracer, w1: list, w2: list) -> Tracer:
    """Mixture of experts with a softmax gate over every expert.

    `x` is `(T, C)`, `wg` is `(C, E)`, and `w1`/`w2` are lists of `E` weights
    shaped `(C, H)` and `(H, C)`. Returns `(T, C)`.

    Every expert is evaluated and weighted by its gate, so the routing is
    compile-time and the whole thing unrolls into `E` independent FFNs plus a
    weighted sum. Top-k sparsity is deliberately NOT here: choosing experts per
    token is a gather with computed indices, which this machine does not do --
    route and permute on the host, then call this on the grouped tokens.
    """
    if len(w1) != len(w2):
        raise ValueError(f"{len(w1)} w1 against {len(w2)} w2")
    gate = softmax(x @ wg)
    out = None
    for e, (a, b) in enumerate(zip(w1, w2, strict=True)):
        y = silu(x @ a) @ b
        weighted = y * gate[:, e : e + 1]
        out = weighted if out is None else out + weighted
    if out is None:
        raise ValueError("moe_dense needs at least one expert")
    return out


def moe_grouped(x: Tracer, w1: list, w2: list, counts: list) -> Tracer:
    """MoE over tokens already grouped by expert on the host.

    `x` is `(T, C)` with the first `counts[0]` rows belonging to expert 0, the
    next `counts[1]` to expert 1, and so on; `sum(counts)` must be `T`. `w1`
    and `w2` are per-expert weights. Returns `(T, C)`.

    This is the sparse form the machine can actually run: the routing decision
    happened before the kernel, so what is left is a sequence of independent
    GEMMs with compile-time shapes.

    Raises ValueError if `counts` does not sum to `T`.
    """
    total = sum(counts)
    if total != x.shape[0]:
        raise ValueError(f"counts sum to {total}, x has {x.shape[0]} rows")

    parts = []
    row = 0
    for a, b, n in zip(w1, w2, counts, strict=True):
        if n:
            parts.append(silu(x[row : row + n] @ a) @ b)
        row += n
    if not parts:
        raise ValueError("moe_grouped needs at least one non-empty group")
    return parts[0] if len(parts) == 1 else parts[0].concat(*parts[1:], axis=0)
