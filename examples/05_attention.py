"""One attention kernel: multi-head, optional GQA, optional causal.

    python examples/05_attention.py

`00_pipeline.py` shows what the compiler decides for a straight-line kernel.
This one is about the case a compiler CANNOT do for you: the `L x L` score
matrix is never built, because you walk K and V in blocks carrying a running
maximum and denominator, rescaling each time the maximum moves. No scheduler
derives that from `softmax(q @ k.T) @ v` -- it is a different expression.

Everything past that is a compile-time flag on the SAME kernel. Multi-head is
a loop over an outer axis, GQA is one line inside it, causal is a narrower
inner range. None of them needed a new instruction.
"""

import numpy as np

import ktpu.dsl as D
from ktpu.codegen import codegen, cost_of
from ktpu.dsl.ops import exp2, maximum
from ktpu.interp.mesh import Mesh
from ktpu.ir import FP16
from ktpu.passes import lower
from ktpu.target import VU13P_8CU as TARGET

L, DH, BLOCK = 128, 64, 64
LOG2_E = 1.4426950408889634
TRI = np.tril(np.ones((BLOCK, BLOCK)))


def head(x, rank, b, h):
    """The (L, D) slab for batch b, head h. Both steps are OUTER axes, so each
    is a plain offset -- which is why (H, L, D) lowers and (L, H*D) does not."""
    match rank:
        case 2:
            return x
        case 3:
            return x[h]
        case _:
            return x[b][h]


def online_softmax(q, k, v, scale, lo, hi, tri=None):
    """Attention over key blocks [lo, hi), never forming the full score matrix."""
    m = ell = acc = corr = None
    for j in range(lo, hi):  # a PYTHON loop: it unrolls at trace time
        s = (q @ k[j * BLOCK : (j + 1) * BLOCK].transpose()) * scale
        mj = s.max(axis=-1, keepdim=True)
        if m is None:
            m = mj  # the first block seeds the state, so no -inf is needed
            p = exp2((s - m) * LOG2_E)
        else:
            m2 = maximum(m, mj)
            corr = exp2((m - m2) * LOG2_E)  # what the old sum is worth now
            p = exp2((s - m2) * LOG2_E)
            m = m2
        if tri is not None and j == hi - 1:
            p = p * tri  # the diagonal block, and only it
        vj = v[j * BLOCK : (j + 1) * BLOCK]
        if acc is None:
            ell, acc = p.sum(axis=-1, keepdim=True), p @ vj
        else:
            ell = ell * corr + p.sum(axis=-1, keepdim=True)
            acc = acc * corr + p @ vj
    return acc / ell


def attention(q, k, v, causal=False, tri=None, wo=None):
    """(L,D), (H,L,D) or (B,H,L,D) in. GQA when k carries fewer heads than q."""
    rank = len(q.shape)
    batch = q.shape[0] if rank == 4 else 1
    heads = q.shape[-3] if rank >= 3 else 1
    group = heads // (k.shape[-3] if rank >= 3 else 1)
    chan, scale = q.shape[-1], q.shape[-1] ** -0.5

    pieces = []
    for b in range(batch):
        for i in range(L // BLOCK if causal else 1):
            joined, per_head = None, []
            for h in range(heads):
                qh = head(q, rank, b, h)
                out = online_softmax(
                    qh[i * BLOCK : (i + 1) * BLOCK] if causal else qh,
                    head(k, rank, b, h // group),
                    head(v, rank, b, h // group),
                    scale,
                    0,
                    i + 1 if causal else L // BLOCK,
                    tri if causal else None,
                )
                if wo is None:
                    per_head.append(out)
                else:
                    y = out @ wo[h * chan : (h + 1) * chan]
                    joined = y if joined is None else joined + y
            pieces += per_head if wo is None else [joined]
    return pieces[0] if len(pieces) == 1 else tuple(pieces)


def rule(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def spec(*shape):
    return D.TensorSpec(shape, FP16)


def compile_and_run(graph, arrays, m, k, n):
    sched = lower(graph, TARGET)
    prog = codegen(sched, m, k, n, TARGET)
    mesh = Mesh(prog, TARGET)
    mesh.upload(sched.bands[0], arrays[0], arrays[1])
    names = [graph.producer(v).attrs["name"] for v in graph.inputs]
    for name, arr in list(zip(names, arrays, strict=True))[2:]:
        mesh.bind(name, arr)
    mesh.run()
    outs = [mesh.result(f"v{v.vid}") for v in graph.outputs]
    return outs, cost_of(prog, sched, TARGET), sched


def reference(q, k, v, is_causal=False):
    s = (q @ k.T) * q.shape[-1] ** -0.5
    if is_causal:
        rows = np.arange(q.shape[0])[:, None]
        s = np.where(np.arange(k.shape[0])[None, :] <= rows, s, -np.inf)
    s = s - s.max(axis=-1, keepdims=True)
    p = np.exp(s)
    return (p / p.sum(axis=-1, keepdims=True)) @ v


rng = np.random.default_rng(0)
q1, k1, v1 = (rng.standard_normal((L, DH)) * 0.3 for _ in range(3))

# ---------------------------------------------------------------------------
rule("1.  the line no auto-scheduler writes")
print("""
  The kernel is at the top of this file -- the code that actually runs below,
  not a paraphrase of it.

  `corr` is the line. It exists because the maximum you subtracted for block 0
  is not the maximum after block 1, so everything accumulated so far has to be
  rescaled. Get it wrong and the answer is still finite, still plausible, and
  wrong -- which is why the checks below compare against a reference rather
  than eyeballing the numbers.

  The `for` loops are ordinary Python. They run at TRACE time, so the block and
  head counts are compile-time constants and the graph comes out fully
  unrolled. Nothing data-dependent is allowed to branch -- try it and the
  tracer raises with the source line.""")

graph = D.trace(lambda a, b, c: attention(a, b, c), *[spec(L, DH)] * 3)
widest = max(op.out.numel for op in graph.ops)
outs, dense_cost, sched = compile_and_run(graph, (q1, k1, v1), L, DH, DH)
want = reference(q1, k1, v1)
print(f"""
  {len(graph.ops)} ops, {L // BLOCK} blocks, {len(sched.bands)} bands.

  The claim worth checking mechanically:  no value is ever {L} x {L}.
    widest value      {widest:,} elements
    L x L would be    {L * L:,}
    {"OK -- the score matrix is never materialised" if widest < L * L else "FAIL"}

    relative error    {np.abs(outs[0] - want).max() / np.abs(want).max():.2e}""")

# ---------------------------------------------------------------------------
rule("2.  causal: the saving is work not done")
print("""
  Query block i attends keys 0..i. Three kinds of (query block, key block):

      j <  i     fully visible   -- run it, no mask at all
      j == i     straddles       -- run it, zero the upper triangle
      j >  i     fully hidden    -- NEVER FORMED

  That last line is the point, and it is the structure Triton's
  `_attn_fwd_inner` uses: an off-band stage with no masking, then one on-band
  stage for the diagonal. Masking a full L x L score matrix would compute every
  block and throw half away.

  The diagonal mask is applied AFTER the exponential, which is exact: softmax
  is shift invariant, so a row max over the whole block rather than its visible
  part only makes `p` smaller, and every row keeps its diagonal element so no
  denominator is zero. Triton computes the mask from `offs_m >= offs_n`; we
  upload it, because there is no `arange` and no index compare on this machine
  (docs/limits.md s4.2).""")

cg = D.trace(
    lambda a, b, c, tri: attention(a, b, c, causal=True, tri=tri),
    *[spec(L, DH)] * 3,
    spec(BLOCK, BLOCK),
)
couts, causal_cost, _ = compile_and_run(cg, (q1, k1, v1, TRI), BLOCK, DH, DH)
got = np.concatenate(couts)
cwant = reference(q1, k1, v1, is_causal=True)
nb = L // BLOCK
print(f"""
    relative error      {np.abs(got - cwant).max() / np.abs(cwant).max():.2e}
    dense flops         {dense_cost.flops:,}
    causal flops        {causal_cost.flops:,}   ({causal_cost.flops / dense_cost.flops:.0%})
    predicted           nb(nb+1)/2 of nb^2 = {(nb + 1) / (2 * nb):.0%}, tending to 50%

  Causal returns ONE RESULT PER QUERY BLOCK. Softmax state is per row, so each
  block carries its own running max and denominator; joining them into one
  (L, D) tensor is a row `concat`, which does not lower (docs/limits.md s6.2).""")

# ---------------------------------------------------------------------------
rule("3.  multi-head and GQA: the same kernel, two flags")
print("""
  Operands are (H, L, D) or (B, H, L, D) -- heads on their OWN axis, which is
  what torch produces after `view(B, L, H, D).transpose(1, 2)`. A head is then
  an outer-axis index, a plain offset.

  The un-transposed (L, H*D) that a QKV projection emits does NOT lower: a head
  of it is an inner multi-column slice (docs/limits.md s6.3). Producing the
  transposed form is the previous layer's job.

  GQA is one line:  kv = h // (heads // kv_heads).

  `wo` fuses the output projection, using

      concat_h(o_h) @ wo  ==  sum_h  o_h @ wo[h*D:(h+1)*D]

  a block matmul identity -- exactly equal, not an approximation. It removes
  the head concat entirely, which is why passing `wo` turns a per-head tuple
  back into a single tensor.""")

H = 4
qh = rng.standard_normal((H, L, DH)) * 0.3
wo = rng.standard_normal((H * DH, DH)) * 0.3
costs = {}
for kv in (H, 2):
    kh = rng.standard_normal((kv, L, DH)) * 0.3
    vh = rng.standard_normal((kv, L, DH)) * 0.3
    g = D.trace(
        lambda a, b, c, w: attention(a, b, c, wo=w),
        spec(H, L, DH),
        spec(kv, L, DH),
        spec(kv, L, DH),
        spec(H * DH, DH),
    )
    o, cost, _ = compile_and_run(g, (qh, kh, vh, wo), L, DH, DH)
    grp = H // kv
    ref = sum(
        reference(qh[h], kh[h // grp], vh[h // grp]) @ wo[h * DH : (h + 1) * DH]
        for h in range(H)
    )
    costs[kv] = (cost, np.abs(o[0] - ref).max() / np.abs(ref).max(), len(g.outputs))

(mc, mr, mo), (gc, gr, go) = costs[H], costs[2]
print(f"""
    kv heads            {H}                {2}
    relative error      {mr:.2e}         {gr:.2e}
    graph outputs       {mo}                {go}
    flops               {mc.flops:,}      {gc.flops:,}
    image               {mc.image_words:,} w      {gc.image_words:,} w
    saved                                 {mc.image_words - gc.image_words:,} words

  The flop counts are IDENTICAL and that is correct. Every query head still
  runs its own attention; what GQA removes is K and V storage and the traffic
  to read them. On a machine whose overhead metric is dram bytes per flop, that
  is the number GQA is meant to move -- and for decode, where the KV cache is
  the working set, it is the one that matters.""")

# ---------------------------------------------------------------------------
rule("4.  what none of this needed")
print("""
  No new instruction. Causal, multi-head and GQA are all expressible with the
  op set that already existed, because each is a slicing decision plus one
  elementwise constant. Every option is a compile-time `if`, so a plain
  single-head call carries none of the ops the other paths would add.

  What they DID need, and what the tests now pin:

    * every graph output gets a region. A multi-result kernel used to keep
      only the last band's output; the others were allocated nothing and the
      failure read as a missing region rather than a wrong number.
    * a constant read element for element against a drained tile is STORED in
      sub-tile order. The read is sequential and nothing between memory and
      the ALU can permute, so the host must write it the way the DRAIN did.
      Getting this wrong reads as 8.9e-01.
    * a rank 3 or 4 operand is packed slab by slab, because `x[h]` is an
      offset of whole slabs and packing the flattened thing would interleave
      heads inside one tile.

  A layout defect reads about 1e-1; E8M15 rounding reads about 1e-3. Two orders
  of magnitude is how you tell them apart, and it is why every check here
  compares against a float64 reference that shares no code with the kernel.""")
