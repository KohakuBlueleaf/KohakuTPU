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


def _attend(q, k, v, scale, block, lo, hi, tri=None):
    """Online softmax over key blocks `[lo, hi)`, returning `(rows, D)`.

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
        if m is None:
            m = mj
            p = exp2((s - m) * LOG2_E)
        else:
            m2 = maximum(m, mj)
            corr = exp2((m - m2) * LOG2_E)
            p = exp2((s - m2) * LOG2_E)
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

    batch = q.shape[0] if rank == 4 else 1
    heads = q.shape[-3] if rank >= 3 else 1
    kv_heads = k.shape[-3] if rank >= 3 else 1
    if heads % kv_heads:
        raise ValueError(f"{kv_heads} kv heads does not divide {heads} query heads")
    group = heads // kv_heads
    scale = chan**-0.5 if scale is None else scale

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
