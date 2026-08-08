# What this machine cannot do

An inventory of the gaps between MAG + matmul cluster + vector core and the op
set a real model needs, written against torch / tinygrad names so it can be
checked rather than argued about.

The point is the input to a later decision — whether a scalar core with its own
memory unit is worth building — so every entry says which of three things it is:

| | |
|---|---|
| **HW** | no silicon path. New hardware or the host does it. |
| **SW** | the silicon could, the compiler does not yet. A bug list, not a design question. |
| **WORKAROUND** | expressible with what exists, at a stated cost. |

Sources: [`isa/vector.md`](isa/vector.md) §3 and §3.1, [`isa/cluster.md`](isa/cluster.md),
[`compute/vector-core.md`](compute/vector-core.md) §12, and the 33 `OpKind`s in
`ktpu.ir.graph`. Where the compiler already refuses something, the refusal is
quoted — those are the honest boundaries because they are enforced.

---

## 1. The axis that actually matters

Almost every "we cannot do X" splits on one question: **is the index a scalar the
host already knows between kicks, or is it per-element and decided inside the
kernel?**

```
   SCALAR-DYNAMIC          the value varies per STEP, not per element
                           KV-cache append offset, sequence length, layer index,
                           loop trip count, which expert bank this call uses
   -> today: the host computes it and writes a descriptor field before the kick.
      Capability: PRESENT.  Cost: one host round trip per step.

   ELEMENT-DYNAMIC         the address depends on DATA, per element
                           embedding lookup, top-k routing, sort, x[mask],
                           scatter-add, take_along_dim
   -> today: impossible on-device at any cost.
      Capability: ABSENT.  This is the real gap.
```

That split is the whole decision. A scalar core buys latency on the first
class — it removes a host round trip. It buys **nothing** on the second, which
needs an address generator that can take a *vector* of addresses, i.e. a gather
engine in MAG. They are different projects and should not be argued as one.

---

## 2. Present and adequate — so the gaps are read against something

Elementwise float: `add sub mul div neg abs max min`, and the compares
`cmplt cmpgt cmpeq` feeding `select`. Transcendentals `exp2 log2 recip rsqrt
sqrt` are **one pass each**, which is the unusual part — on a GPU they are
quarter-rate. Composed from those: `exp log pow sigmoid tanh silu gelu softmax
layernorm rmsnorm`.

`pow(x, y) = exp2(y * log2(x))` is exact-cost, not a fallback.

Reductions `sum rmax rmin sumsq` over a row. Matmul in MXFP7 with an ACC24
accumulator. Views: `reshape`, `expand`, outer-axis `slice`, a transpose of the
last two axes, and a single-column slice as a stride.

**Masking is not a gap.** `where(mask, a, b)` is `VSEL` with a predicate
register, and an additive `-inf` attention mask is an ordinary `add`. What is
missing is mask *selection* — §3.1.

---

## 3. Element-dynamic indexing — the real hole

### 3.1 Boolean mask selection

```python
x[mask]                       # torch, tinygrad
torch.masked_select(x, mask)
torch.nonzero(mask)
```

**HW, and worse: not expressible in a static-shape IR at all.** The output
length depends on the data, and `docs/driver/ir.md` fixes every extent at trace
time. This is not "the hardware lacks an instruction" — it is a different
execution model.

*Workaround:* do the arithmetic on all of it and multiply by the mask
(`where(mask, x, 0)`), which is what every dense implementation does anyway.
Only *compaction* is lost, and compaction is a memory-traffic optimisation, not
a semantic one. Real cost: none for correctness, everything for sparsity.

Compaction proper belongs on the host, and then the compacted tensor is uploaded
as an ordinary dense operand.

### 3.2 Gather and embedding

```python
table[idx]                    # nn.Embedding, index_select, take_along_dim
```

**WORKAROUND, at a bad but bounded cost.** `one_hot` *is* expressible —
`cmpeq(expand(idx), arange)` with `arange` uploaded as a constant — and then

```
   gather(table, idx)  ==  one_hot(idx, V) @ table
```

which is a matmul the cluster runs at full rate. The cost is `T x V x C`
multiply-accumulates to move `T x C` values. At vocabulary 32k that is
catastrophic; at 256 experts or 512 buckets it is genuinely fine.

So embedding lookup is **not** the argument for a gather engine — token
embedding is one layer and the host can do it. Per-layer gathers are.

### 3.3 Scatter and scatter-add

```python
out.scatter_add_(0, idx, src)      # MoE combine, sparse accumulation
```

**HW.** The one-hot trick inverts (`one_hot(idx).T @ src`) but the transpose of a
`T x V` one-hot is itself the problem. Host.

### 3.4 Sort, top-k, order statistics

```python
torch.topk, torch.sort, torch.median, torch.argsort
```

**HW.** No comparator network, no data-dependent movement. This is why
`moe_dense` evaluates *every* expert and weights by the gate: top-k routing is a
gather with computed indices, so the DSL's docstring says route on the host and
call the kernel on grouped tokens.

`argmax`/`argmin` as an **index** is a partial exception — §5.2.

---

## 4. Integer, bitwise, and randomness

### 4.1 Integer arithmetic

**HW for real integer ops** — `isa/vector.md` §3.1: "no integer arithmetic
beyond dtype conversion, no bitwise ops".

**WORKAROUND for small integers.** E8M15 carries 16 explicit mantissa bits, so
every integer up to `2^16` is exactly representable and `add/sub/mul/compare` on
them are exact in float. Counters, small indices and one-hot arithmetic are
fine. It breaks at `2^17`, silently, by rounding.

### 4.2 `floor`, `ceil`, `round`, `mod`, `sign`

**HW, and this is the one gap I would not have predicted.** There is no rounding
op in the 33 `OpKind`s and none in the vector ISA's 32 opcodes. Without `floor`
there is no integer division and no `mod`, which blocks:

- positional index arithmetic (`pos % period`)
- any quantisation that computes a bucket index on-device
- `arange` generated on-device rather than uploaded

Everything above is currently solved by **uploading a table the host computed**,
which is genuinely standard practice — but it is an upload per shape, and it is
the cheapest gap on this page to close: `floor` on E8M15 is an exponent compare
and a mantissa mask, and the normaliser already exists.

### 4.3 Random numbers

```python
torch.rand_like(x), F.dropout
```

**HW.** No PRNG, and no integer/bitwise ops to build one from. Inference does
not care. **Training does** — dropout needs a fresh noise tensor per step, which
means a host upload of the same size as the activations, every step.

If training is ever a target, a counter-based PRNG (Philox/Threefry) is a few
hundred LUTs — but it needs §4.1's integer ops first, which is the real
dependency.

---

## 5. Reductions and scans

### 5.1 Prefix scan

```python
torch.cumsum, torch.cumprod
```

**WORKAROUND, and a cheap one.** `cumsum(x)` along a row of length `n` is
`x @ L` with `L` lower-triangular ones. The cluster runs it at full rate, and at
`n = 128` a `128 x 128` triangular matmul is nothing. `cumprod` is
`exp2(cumsum(log2(x)))`, valid for positive inputs only.

This is the example worth remembering: **a matmul by a structured constant
replaces a scan.** The same trick gives transposes (§6.1) and one-hot gathers
(§3.2), and it is the general shape of "somehow work around it" on this machine.

### 5.2 `argmax` as an index

**SW, not HW.** The vector ISA's `VRED` kind field already lists
`SUM MAX MIN SUMSQ DOT ARGMAX ANY ALL`; level 1 only has `sum rmax rmin sumsq`.
So `ARGMAX`, `ANY` and `ALL` are silicon that the compiler cannot reach.

Even without it: `argmax(x) = sum(where(x == max(x), arange, 0))`, exact when
the maximum is unique and wrong (it sums the indices) when it is not.

### 5.3 Reductions wider than one pass

**SW.** `VLMAX` is 128, and `lower()` now refuses a reduction over more than
that:

> `sumsq reduces 256 elements and VLMAX is 128; a row wider than one pass needs
> the TREE to carry a partial across passes, which is not emitted yet`

The hardware has 16 rotating accumulators for exactly this
([`compute/vector-core.md`](compute/vector-core.md) §7.3). It is unemitted code,
not missing silicon — but until it lands, `rmsnorm` over a 256-wide row does not
compile, and that is an ordinary model shape.

### 5.4 Multi-axis reduction

**SW.** `reduce` takes an `axes` tuple at level 1, but `lower()` shapes a
reduction band from a single contracted extent. Reduce one axis at a time.

---

## 6. Memory layout

### 6.1 Transpose and permutation

**Partly present, partly WORKAROUND.** A transpose of the last two axes on a
matmul's B operand is free — it is absorbed by the packer, which is how
`q @ k.T` works. Anything else, `through_views()` refuses:

> `%12 permutes more than the last two axes; that needs a stride list level 3
> does not carry`

*Workaround:* `P @ A` with `P` a permutation matrix — `n^3` work to move `n^2`
data, which is only sane for small `n`.

*The right fix is SW, not HW*: `vector-core.md` §10 says a permute is a
permutation of the stride list and the AGU already takes four `(stride, bound)`
pairs. Level 3 carries an offset and a single stride today; carrying the full
list is a compiler change.

### 6.2 `concat` and `pad`

**SW.** Both are `OpKind`s at level 1, neither is reachable from the DSL, and
both are refused below it. `pad` is a bound and an offset on a zeroed region;
`concat` is two writes into one region. Neither needs hardware.

This is the most-felt gap on the page, because it is what forces a kernel to
return several values instead of one. Causal attention carries softmax state per
query block, so each block is a separate result; joining them into one `(T, C)`
tensor is exactly a row-wise `concat`. Today `attention(..., causal=True)` returns a tuple
and the caller reads the blocks in order.

Two things soften it. A **row** concat of per-block results is disjoint writes
into one region, so it is a level-3 addressing change, not a data movement. And
a **column** concat that feeds a matmul is not needed at all -- see s6.4.

### 6.3 Non-contiguous slices

**SW.** Outer-axis slices and single columns lower today; a general strided
window does not. Same AGU argument as §6.1.

### 6.4 Two layout rules attention had to obey

Neither is a gap, but both decide kernel shape before any arithmetic does, and
both were found by writing multi-head attention rather than by reading the ISA.

**Heads get their own axis.** `(H, L, D)` and `(B, H, L, D)` lower: a head is
then `x[h]` or `x[b][h]`, an outer-axis index, which is a plain offset. That is
also what torch produces after the usual `view(B, L, H, D).transpose(1, 2)`, so
nothing unusual is being asked for.

What does NOT lower is the **un-transposed `(L, H*D)`** a QKV projection emits:
a head of it is `x[:, h*D:(h+1)*D]`, the inner multi-column slice §6.3 refuses.
So the transpose has to happen before the kernel — it is the previous layer's
job, and doing it on-device is the memory-movement gap this page already tracks
(§6.1, and the MMU question in §9).

A rank 3 or 4 operand is packed **slab by slab**: `x[h]` is an offset of whole
`(L, D)` slabs, so packing the flattened tensor would interleave heads inside
one tile. `Mesh.put` does this; it is the same class of obligation as the two
below.

**A column concat that feeds a matmul is free.** `concat_h(o_h) @ wo` equals
`sum_h o_h @ wo[h*C:(h+1)*C]`, a block matmul identity. Fusing the output
projection removes the only concat multi-head attention would need, exactly
rather than approximately, and turns a missing op into an outer-axis slice of
the weight. Worth checking for before asking for `concat`: a concat consumed by
a matmul is usually a sum of sliced matmuls.

**A constant read against a drained tile is stored in drain order.** A DRAIN
writes sub-tile order; a vector core reading an operand element for element
against it reads sequentially, and nothing between memory and the ALU can
permute. So the host must write that operand the way the drain did --
`Program.packing` records the obligation, the same way it records B's
transpose. Getting it wrong reads as ~9e-01, not as noise.

### 6.6 Independent block sizes and back-to-back GEMM

A kernel is entitled to one block size per axis: GEMM wants `BLOCK_M`,
`BLOCK_N`, `BLOCK_K`; attention wants a query block, a key block and a head
dim. Requiring two of them to be equal is a compiler defect, never a design, and
everything in this section is a **bug**, not a limitation. The one sanctioned
limitation nearby is a block wider than the dimension it covers, which is a
nonsense configuration; it never arises in any case below.

**Plain GEMM is correct at every size measured** -- 64x1024x64, 256x1024x256,
512x512x512 (grid 8x4), and with a folded epilogue, all ~6e-04. Large K and
multi-tile outputs are not a problem in themselves.

**A matmul may now read another matmul's output.** It used to be silently wrong
at every size, including equal contraction widths: rel 1.26e+00 with no
exception, because a DRAIN writes sub-tile order, a FILL reads L1-entry order,
and only a vector store converts. `lower()` now inserts a relayout band -- a
multiply by 1.0, the same trick s6.1 describes for moving data -- whenever a
matmul takes its A operand straight from another. Nothing else in the repo
exercised this, which is why it survived so long.

**Head dim and block are independent.** `Band.shape` gives each band its own
working shape so `_bcast` stops consulting a tiling belonging to another matmul,
and `Program.tilings` gives each region its own geometry so `result`,
`_entry_at` and the gather reads unpack with the tile the value was actually
drained with. `prog.tiling` is only a default now.

Working, pinned by `test_block_sizes_are_independent`:

| `L` | `DH` | `BLOCK` | | |
|---|---|---|---|---|
| 128 | 64 | 64 | dense + causal | equal, the old case |
| 64 | 32 | 32 | dense + causal | equal, smaller |
| 128 | 32 | 64 | dense | head dim below the block |
| 128 | 64 | 32 | dense | head dim above the block |
| 128 | 128 | 64 | dense | head dim wider than the block |
| 256 | 128 | 64 | dense | and over several output tiles |

**Five configurations still fail**, each `xfail(strict=True)` so the gate reports
the day one starts passing:

| `L` | `DH` | `BLOCK` | | |
|---|---|---|---|---|
| 256 | 32 | 64 | dense | head dim below block, m > 1 tile |
| 256 | 64 | 128 | dense | same |
| 128 | 32 | 64 | causal | causal with `DH != BLOCK` |
| 128 | 64 | 32 | causal | same |
| 128 | 128 | 64 | causal | same |

The decisive observation: `L=256 DH=64 BLOCK=128` fails while
`L=256 DH=128 BLOCK=64` passes, with the **same two matmul shapes and the same
two tiles**, only in the opposite order. So no shape here is unsupported --
something still keys off which tile ends up as the destination.

For the dense pair the failing instruction is known exactly: an entry-layout
row-gather `VST` whose row is wider than the contraction chunk, so it spans two
k-blocks while the store path addresses one. Applying that mapping alone removes
the exception but returns a wrong number, because `row = coff // tn + chunk` is
not the global row once the destination spans several tiles -- `coff` is
relative to the band instance. Fix the row first, then the k-block mapping.
Task #62 carries the instruction dump and the order to do it in. The causal
class is undiagnosed and plausibly shares the same defect.

### 6.5 In-place update at a computed offset (KV cache)

**PRESENT, via the scalar path.** Appending to a KV cache at step `t` writes at
a base the host knows before the kick — a descriptor field, not a gather. §1's
first class.

What it costs today is a host round trip per decode step. That is the strongest
argument on this page for a scalar core, and it is a *latency* argument, not a
capability one.

---

## 7. Control flow and shape

**Data-dependent branching: HW.** `VLOOP`'s counter is the only branch, and the
DSL rejects a data-dependent `if` at trace time with the source line rather than
silently picking a side. Early exit, beam search and while-until-converged are
host loops.

**Dynamic shapes: by design, above the IR.** Every extent is concrete at trace
time; a variable sequence length is specialised or padded by the layer above.
`docs/driver/ir.md` §6.1.

---

## 8. Numerics

| | |
|---|---|
| no subnormals | deliberate — `vector-core.md` §1.3, and free rather than a compromise |
| FP16 store saturates at 65504, silently | **task #49**, open. Epilogue folding does NOT mitigate it: the accumulator converts to FP16 on EMIT (`mx_acu_fp.v` stage 6) and a DRAIN writes FP16 (isa/cluster.md §5), so the clamp happens either way |
| FP32 compute | designed (`vector-core.md` §5, the extended mode) and **not built** |
| `sin`/`cos` | absent. RoPE uses a host-computed table, which is what everyone does anyway |

---

## 9. What I would put on the shortlist

Ordered by capability gained per unit of work, not by how interesting it is.

1. **The SW list, first, because it is free silicon.** §5.3 wide reductions,
   §6.2 row `concat`, §6.1 full stride lists, §5.2 exposing `ARGMAX/ANY/ALL`.
   Every one is hardware that already exists and a compiler that cannot reach
   it. Nothing on the HW list should be argued before these land, because they
   change what the HW list even contains.

   Causal, multi-head and grouped-query attention needed **no new instruction**
   — see [`examples/05_attention.py`](../examples/05_attention.py),
   which runs all three at level 3. That is evidence for doing the SW list
   first: three kernels that looked like they wanted hardware wanted slicing
   and one elementwise constant. What they did expose was two compiler
   defects, now fixed and pinned — a multi-result graph kept only its last
   output, and a constant read against a drained tile was stored row-major
   (§6.4). Both read as wrong numbers, not as errors.

2. **`floor` and friends** (§4.2). One instruction on the existing normaliser,
   and it unblocks on-device index arithmetic, `mod`, and eventually a PRNG.

3. **A scalar unit that can write a descriptor between kicks** (s6.5, s1). Buys
   autoregressive decode without a host round trip per step. This is the "CPU
   core" in its cheapest form: it does not need to be fast, it needs to be
   *there*, next to the dispatch agent, holding a few registers and computing
   addresses. It buys nothing for §3.

4. **A gather engine in MAG** (§3.2, §3.3). The only thing that closes
   element-dynamic indexing, and much the most expensive: a vector of addresses
   through the memory path, with all the coalescing and ordering that implies.
   Worth it only if per-layer gathers — not token embedding — turn out to
   matter.

5. **Integer and bitwise ops** (§4.1). Small, and mostly a prerequisite for the
   PRNG rather than a goal.

**Sort and top-k stay on the host.** §3.4 is the one place where "the host does
it" is the right permanent answer rather than a placeholder: routing decisions
are thousands of operations per token against billions of MACs, and a comparator
network earns nothing.

---

## 10. How to check this list

The refusals are enforced, so they can be exercised. `scripts/py/ir_server.py`
compiles an arbitrary kernel and shows exactly where it stops:

```python
def kernel(x, g):
    return D.rmsnorm(x, g)

INPUTS = {"x": (64, 256), "g": (256,)}     # 256 > VLMAX -> §5.3's message
```

Anything in this document marked **SW** should produce a clear `ScheduleError`
naming the limit. If it produces a wrong number instead, that is a worse bug
than the missing feature.
