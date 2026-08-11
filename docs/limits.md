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

**CLOSED.** `VLMAX` is 128 and a GroupNorm row is 640, which used to be a
`ScheduleError`. `passes/reduce.py` splits the reduction instead: reshape the
row to `(5, 128)`, reduce the inner axis, reduce the five partials. The reshape
is a view, the second stage is a 128th of the first, and the partial lives in a
value rather than in the TREE. `sumsq` folds its chunk totals with a plain
`sum`, since it has already squared.

`chunk_width` takes the largest DIVISOR that fits, so 640 splits 5 × 128 and
320 splits 4 × 80 — an uneven tail would need a mask the tree has not got, and a
span that is prime above 128 still does not compile.

The 16 rotating accumulators ([`compute/vector-core.md`](compute/vector-core.md)
§7.3) would make this one pass instead of two; they are still unemitted, and it
is now a performance question rather than a compile failure.

### 5.4 Multi-axis reduction

**SW.** `reduce` takes an `axes` tuple at level 1, but `lower()` shapes a
reduction band from a single contracted extent. Reduce one axis at a time.

---

## 6. Memory layout

### 6.1 Transpose and permutation

**Partly present, partly WORKAROUND.** A transpose of the last two axes on a
matmul's B operand is free — it is absorbed by the packer, which is how
`q @ k.T` works.

A permute that only moves an axis a later slice collapses is also free now:
`through_views()` replays the whole chain as a stride list and asks whether the
answer is an offset, a run and one stride. `reshape(L, H, dh).permute(1, 0, 2)[h]`
is, so the torch head split lowers. What is left over needs three levels —
`(H, L, dh)` with every head live at once is `H` runs of `dh`, `L` times — and
that is refused:

> `%3 walks [(4, 3), (6, 12), (3, 1)] of %0: three levels, and vec_agu carries
> an offset, a run and one stride`

*Workaround:* `P @ A` with `P` a permutation matrix — `n^3` work to move `n^2`
data, which is only sane for small `n`.

*The rest is SW, not HW*: the AGU takes four `(stride, bound)` pairs and
`mx_tdesc` six dimensions; level 3 carries one run and one stride. Carrying the
whole list is a compiler change. Where the operand is HOST-PACKED the compiler
already walks the full list, which is how a stride-2 conv tap is gathered.

### 6.2 `concat` and `pad`

**SW.** Both are `OpKind`s at level 1 and both are refused below it. `pad` is a
bound and an offset on a zeroed region; `concat` is two writes into one region.
Neither needs hardware.

`pad` has one case that works: a zero border around a GRAPH INPUT that a matmul
reads through a window is folded into the host's gather, so a padded `conv2d`
compiles and its taps read zeros outside the image. A pad of anything a band
computed does not, because the border exists only in the host's copy:

> `the A operand %37 is a window of a PADDED %33, which a band computes; the
> border exists only in the host's copy, so the value has to come back and go
> out padded`

That is `mx_tdesc`'s `valid` bit, which the RTL has and level 3's FILL does not
carry.

This is the most-felt gap on the page, because it is what forces a kernel to
return several values instead of one. Causal attention carries softmax state per
query block, so each block is a separate result; joining them into one `(T, C)`
tensor is exactly a row-wise `concat`. Today `attention(..., causal=True)` returns a tuple
and the caller reads the blocks in order.

Two things soften it. A **row** concat of per-block results is disjoint writes
into one region, so it is a level-3 addressing change, not a data movement. And
a **column** concat that feeds a matmul is not needed at all -- see s6.4.

### 6.3 Non-contiguous slices

**Mostly CLOSED.** An inner-axis band lowers: `p[:, :h]` of a `2h`-wide
projection is `h` contiguous elements every `2h`, one run and one stride, which
is what a GEGLU chunk and a conv filter tap both are. Two limits remain, and
both are enforced rather than silently wrong:

- A window over a value a CLUSTER drained must land on tile boundaries — a
  drained tile is sub-tile order, so half a tile is not a run of addresses:
  > `band b1 reads a 64-wide window of %2 at offset 64, but %2 is drained in
  > 64x128 tiles; a window that splits a tile is not a run of addresses`
- Three levels are refused on an engine and gathered on the host (§6.1).

A window on a MATMUL operand becomes a region of its own, listed in
`Program.windows`, and the host cuts it out at pack time. That is not a
compromise: the operand was going to be packed anyway, so the gather is free at
run time. `scratch/sdxl-fwd/conv_card.py` runs the nine descriptors a traced
`conv2d` produces on the FPGA and scores p50 2.5e-3 against a direct
convolution in fp64.

### 6.4 Two layout rules attention had to obey

Neither is a gap, but both decide kernel shape before any arithmetic does, and
both were found by writing multi-head attention rather than by reading the ISA.

**Heads get their own axis.** `(H, L, D)` and `(B, H, L, D)` lower: a head is
then `x[h]` or `x[b][h]`, an outer-axis index, which is a plain offset. That is
also what torch produces after the usual `view(B, L, H, D).transpose(1, 2)`, so
nothing unusual is being asked for.

The **un-transposed `(L, H*D)`** a QKV projection emits also lowers now: a head
of it is `x[:, h*D:(h+1)*D]`, `D` contiguous elements every `H*D`, which §6.3
resolves. Better still, `nn.project_heads` slices the WEIGHT instead --
`x @ w[:, h*dh:(h+1)*dh]` -- so the window is on an operand the host packs and
each head's result comes back contiguous, needing no relayout at all.

What still does not work is feeding attention from Q/K/V a BAND computed. A
GEMM's B operand has to be in L1-entry order and only the host packer produces
that; the A side has a relayout band and B has none, and at `bflip=0` B would
also need a transpose no engine here does:

> `the B operand %47 comes from band b3, and only a HOST-PACKED operand is in
> L1-entry order. Bring it back and pass it in, or give the value a relayout
> band`

So a transformer block compiles as two kernels with the projection's result
going out and back, which is what the card path does today. A B-side relayout
band is the missing compiler piece.

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

### 6.7 A vector kernel's L1 footprint has a bad band

**MEASURED, UNEXPLAINED, GUARDED.** A vector kernel whose buffers occupy 352 to
480 of `vec_core`'s 512 L1 words returns **wrong data in its output buffer** and
reports success. 320 words and below is clean, and so is exactly 512.

Reproduced on two unrelated kernels, which is what rules out a bug in either:

| kernel | footprints measured clean | footprints measured wrong |
|---|---|---|
| `group_norm_kernel` | 256, 288, 320, **512** | 352, 384, 416, 448, 480 |
| `MapKernel("affine")` | 256, 288, 320, **512** | 352, 384, 416, 448, 480 |

The corruption is 16 L1 words wide in GroupNorm and a longer run in `MapKernel`,
and its position moves with the footprint, so it is not a fixed address. A first
model — "a VFILL landing at L1 word F also writes 16 spurious words at F+240" —
explained every GroupNorm case and was then **falsified** by `MapKernel`, so the
mechanism is still open. `scratch/sdxl-fwd/gn_sweep.py` and `l1_spur.py`
reproduce it in about a minute each.

`veckernels.require_l1` refuses the band, so the failure is now an exception at
build time rather than a wrong number. What it costs: at SDXL's C=320 and 32
groups a GroupNorm group is `10*hw` elements, so **the spatial extent is capped
at hw <= 128** — 8x16 works, 12x16 does not.

### 6.8 FIXED 2026-08-11 — the driver never emitted the L1 bank bits

**Root cause and fix, both in the driver. No bitstream involved.**

`eoff`/`aoff`/`boff` are EIGHT bits, so L1 entry 256 wraps to 0. The second
256-entry bank is reachable only through the bank bit — the ninth bit — and
`ktpu.hw.matmul._flit` emitted none of the three:

```
[114] abank   GEMM: which 256-entry half of L1 A this sweep reads
[113] bbank   GEMM: ... and of L1 B
[112] fbank   FILL: which half this fill writes
```

`mx_cluster_cu.v` implements all of it — `i_abank`/`i_bbank`/`i_fbank` at
lines 245-247, registered at 714, and `pl_ent = {fbank_r, rtag}` at 627. The
hardware was complete; the driver addressed half of L1 and wrote the other
half's chunk on top of it.

`kernel.plan`'s `a_bank`/`b_bank` return an ABSOLUTE entry index — `(i %
banks_a) * entries_per_chunk_a` — which is 256 for the second chunk whenever a
chunk is 256 entries. Truncated to eight bits that is 0, so **chunk 1's fill
landed on chunk 0 while the sweep was still reading it**, and both GEMMs read
`B@0`. `kernel.split` now divides that index into `(bank, offset)` and both are
emitted; it raises past bank 1, which is all the ISA has.

**The rule this predicted, confirmed on every row: broken iff a chunk is 256
entries AND there is more than one chunk.** `gn=16` was necessary but never
causal — it is what lets `nk` reach 16 so `gn*nk` reaches 256; `gn=32` forces
`nk=8` and a 128-entry chunk, where both live chunks fit in bank 0 at different
offsets, which is how this survived so long.

Card measurements, same six shapes before and after:

| shape | before, p50 / over 10% | after, p50 / over 10% |
|---|---|---|
| 64x576x64 | 1.652e-01 / 2778 of 4096 | **2.182e-05 / 0** |
| 64x640x64 | 1.699e-01 / 2847 of 4096 | **2.531e-05 / 0** |
| 64x1024x64 | 1.650e-01 / 2814 of 4096 | **2.489e-05 / 0** |
| 64x1280x64 | 3.016e-05 / 443 of 4096, max 0.73 | **2.483e-05 / 0** |
| 77x2048x64 | 6.36e-05 / 1197 of 4928, max 1.08 | **2.433e-05 / 0** |
| 128x640x64 | 1.43e-01 / 5191 of 8192 | **2.361e-05 / 0** |

Regression check, two shapes that always passed: 64x1024x96 2.770e-05 and
64x1536x128 2.473e-05, both 0 over 10%.

**Why no simulator caught it:** `bench.py` hardcodes `L1_A_ENTRIES=128,
L1_B_ENTRIES=256`, so a bank is 64 entries there and a chunk can never reach
256. Every sim run was a different machine from the card, and a sim PASS on this
shape means nothing.

**It also recovers half of L1.** `HANDOFF-anchor-largek` filed "the driver emits
no bank bits, so half of L1 is unreachable" as lost performance. It was a
correctness bug, and both are now closed.

**End to end on the card**, the BasicTransformerBlock run both ways:

| projections | before | after |
|---|---|---|
| per head, N=64 (the broken shape) | 1.21e-02 | **1.07e-03** |
| full width, N=640 (avoiding it) | 1.07e-03 | **1.07e-03** |

The two now agree digit for digit, at the format's own cost of 1.06e-03 — so the
workaround is no longer needed and full-width projection is a pure traffic win
(122 matmuls to 68) rather than a correctness requirement.

#### The compiler path had it worse: a capacity bug in a truncation bug's clothes

`ktpu.codegen` carried the same defect and one more underneath it. `encode.FIELDS`
had no `abank`/`bbank`/`fbank` at all, so that path structurally could not emit
them; and `cu.py` laid B out as `boff = ch * b_ent`, one chunk after another with
no wrap-around, while A alternated with `(ch % 2) * a_ent`.

Adding a guard that refuses an entry index past bank 1 failed **25 tests** on
`L1 entry 512 is in bank 2`. So `ch * b_ent` did not merely truncate at 256 — it
addressed past even TWO banks, meaning the compiler path was relying on the 8-bit
field wrapping for shapes whose B does not fit L1 at all. **A capacity bug
wearing a truncation bug's clothes**, silent, with the tests passing over it.

Fixed by mirroring the legacy planner's rule: B is resident only when
`chunks * b_ent` actually fits two banks, and otherwise alternates banks per
chunk and is refilled. The cost is B residency on shapes that never really had
it — more B traffic, correct answers.

Nothing was wrong on hardware, because `fromdsl.flits` refuses anything but
`fill, fill, gemm, drain` and `codegen.encode` has no vector opcodes, so that
path cannot stage these shapes on the card today. It was a landmine for whoever
turned it on.

Everything below is the original investigation, kept because the method is the
reusable part: card scored against `mxfp7.model_matmul` rather than fp64, so the
format's cost and the machine's error stay separate.

### 6.8.1 How it presented: a 64-wide output with a large K is silently wrong

**MEASURED 2026-08-11 on ship_3x2, card against the machine's OWN MXFP7 model.**
A GEMM with `N = 64` and a padded `K` above 512 returns wrong data in some
elements and reports success. `N >= 96` is clean at every `K` measured, including
2048, so this is selected by N, not by K alone.

| shape | padded | card vs mxfp7 model p50 | max | elements over 10% |
|---|---|---|---|---|
| 64x128x64 | 64x128x64 | 2.36e-05 | 2.69e-04 | 0 of 4096 |
| 64x256x64 | 64x256x64 | 2.25e-05 | 3.24e-04 | 0 of 4096 |
| 64x384x64 | 64x384x64 | 2.47e-05 | 2.98e-04 | 0 of 4096 |
| 64x512x64 | 64x512x64 | 2.48e-05 | 2.68e-04 | 0 of 4096 |
| **64x640x64** | 64x**1024**x64 | **1.61e-01** | 9.51e-01 | **2776 of 4096** |
| **64x1024x64** | 64x1024x64 | **1.72e-01** | 1.05e+00 | **2874 of 4096** |
| **128x640x64** | 128x768x64 | **1.43e-01** | 8.51e-01 | **5191 of 8192** |
| **77x2048x64** | 128x2048x64 | 6.36e-05 | **1.08e+00** | **1197 of 4928** |
| 64x640x96 | 64x768x128 | 2.71e-05 | 4.09e-04 | 0 of 6144 |
| 64x640x128 | 64x768x128 | 2.65e-05 | 4.06e-04 | 0 of 8192 |
| 64x640x640 | 64x768x640 | 2.04e-05 | 3.66e-04 | 0 of 40960 |
| 77x2048x640 | 128x2048x640 | 2.36e-05 | 4.46e-04 | 0 of 49280 |

`77x2048x64` is the one to remember: **p50 6.36e-05 and a max of 1.08.** A median
that looks perfect while a quarter of the elements are wrong is the same
signature as the `nk >= 9` bug in HANDOFF-anchor-largek, and a spot check of the
median would have passed it.

Reproduced with `run_fpga.py --m 64 --k 640 --n 64` (its own generated operands,
verdict FAIL, detach 215.9) and independently through `chain.gemm` with real
checkpoint weights, so it is neither the harness nor the operand content.

### ROOT CAUSE: the host fills both B chunks into the same L1 entries

**This is a HOST-SIDE PROGRAM-CONSTRUCTION BUG, not RTL and not timing. It needs
no bitstream.** The control program the host issues for `64x640x64` says it
outright — `disasm.listing` over `prog.setup` + `prog.cmds`:

```
FILL B  255 entries from 0x800000 -> L1 0      <- B chunk 0
FILL B    1 entries from 0x80ff00 -> L1 255
FILL B  255 entries from 0x810000 -> L1 0      <- B chunk 1 OVERWRITES chunk 0
FILL B    1 entries from 0x81ff00 -> L1 255
FILL A  255 entries from 0x0      -> L1 0
FILL A    1 entries from 0xff00   -> L1 255
GEMM 16x16 over 16 K blocks, A@0 B@0  load
FILL A  255 entries from 0x10000  -> L1 0
FILL A    1 entries from 0x1ff00  -> L1 255
GEMM 16x16 over 16 K blocks, A@0 B@0  accumulate  emit -> 0x1000000
DRAIN 256 sub-tiles
```

**Both B chunks are filled before either GEMM runs, into the same L1 range.** The
second destroys the first, and both GEMMs then read `B@0`, so every K-chunk is
multiplied by the LAST chunk of B. The A fills ARE correctly interleaved — chunk,
GEMM, chunk, GEMM — so this is specific to the B side.

`64x640x96`, which passes, interleaves B correctly and moves A's offset:

```
FILL A 128 -> L1 0  ; FILL B -> L1 0 ; GEMM A@0   B@0 load
FILL A 128 -> L1 128; FILL B -> L1 0 ; GEMM A@128 B@0 accumulate
FILL A 128 -> L1 0  ; FILL B -> L1 0 ; GEMM A@0   B@0 accumulate emit
```

**Confirmed predictively on the card.** If the model is right the output must be
`sum_i A_i @ B_last`, and it is, at MXFP7-model accuracy — against every
partial-accumulation alternative, which are all four orders of magnitude worse:

| candidate for the card's wrong output | p50 | over 10% |
|---|---|---|
| the CORRECT `sum_i A_i @ B_i` | 1.477e-01 | 2667 of 4096 |
| partial sum, chunk 0 alone | 2.212e-01 | 3155 of 4096 |
| partial sum, chunk 1 alone | 1.670e-01 | 2784 of 4096 |
| **`sum_i A_i @ B_last`** | **2.646e-05** | 158 of 4096 |
| `sum_i A_i @ B_first` | 1.876e-01 | 2960 of 4096 |

So it is NOT a partial K accumulation. 158 elements (3.9%) still exceed 10%
against the prediction and are **unexplained** — the median is an exact hit, the
tail is not, and no mechanism is claimed for it.

Two further measurements pin it down:

* **Deterministic.** Three runs of `64x640x64` gave a bit-identical output,
  `sha256 62ccc139...`. Not marginal timing.
* **The card runs it on ONE cluster** (`cu=1`, `rnd=1`), so this is not
  multi-cluster distribution either.

Why `gn=16` and why two chunks: at `gn=16 nk=16` a B chunk is exactly 256
entries — one whole L1 bank — so two chunks need two banks, and
HANDOFF-anchor-largek already records that **the driver emits no bank bits at
all, so every chunk lives in bank 0**. One chunk has nothing to overwrite, and
`gn=32` interleaves its fills so an overwrite is harmless.

The `nk >= 9` fix capped the B chunk to one L1 bank; it did not make the second
chunk land anywhere else.

**The discriminator is `gn=16` with MORE THAN ONE K-CHUNK.** Measured directly by
holding `N=64` (so `gn` stays 16) and walking `K` across the boundary, with the
tile and the chunk count printed per row and the answer scored against the MXFP7
model. Two earlier readings of this were wrong — it is not a tile-selection
boundary, and the threshold is not 4 chunks:

| shape | padded K | tile | chunks | clusters | | |
|---|---|---|---|---|---|---|
| 64x384x64 | 384 | gm=16 gn=16 nk=12 | **1** | 1 | 2.36e-05 | PASS |
| 64x512x64 | 512 | gm=16 gn=16 nk=16 | **1** | 1 | 2.60e-05 | PASS |
| 64x576x64 | 1024 | gm=16 gn=16 nk=16 | **2** | 1 | 1.65e-01 | **2778 of 4096 wrong** |
| 64x640x64 | 1024 | gm=16 gn=16 nk=16 | **2** | 1 | 1.70e-01 | **2847 of 4096 wrong** |
| 64x1024x64 | 1024 | gm=16 gn=16 nk=16 | **2** | 1 | 1.65e-01 | **2814 of 4096 wrong** |
| 64x1280x64 | 1536 | gm=16 gn=16 nk=16 | **3** | 1 | 3.02e-05 | **443 of 4096 wrong**, max 0.73 |
| 64x1024x96 | 1024 | gm=16 gn=32 nk=8 | 4 | 1 | 2.35e-05 | PASS |
| 64x1024x128 | 1024 | gm=16 gn=32 nk=8 | 4 | 1 | 2.35e-05 | PASS |
| 64x1024x320 | 1024 | gm=16 gn=32 nk=8 | 4 | 3 | 2.47e-05 | PASS |
| 64x1536x128 | 1536 | gm=16 gn=32 nk=8 | 6 | 1 | 2.55e-05 | PASS |

Three facts fall out, and the third is the one to design a fix against:

* **One chunk is clean; two is catastrophic.** The failure switches on at the
  first multi-chunk case, so it is the cross-chunk accumulate, not depth.
* **`gn=16` is necessary, not merely correlated.** `gn=32` is clean at 4 AND 6
  chunks. So this is not multi-chunk accumulation in general.
* **Three chunks is LESS wrong than two** — 443 elements against 2814, with a
  median that looks perfect. Non-monotonic in the chunk count, which no simple
  "loses the low bits" story explains.

**The card runs `N=64` on ONE cluster**, so single-cluster execution is not a
difference between the card and xsim.

`bench.tile_for` — the path `chain.Shape` and therefore the card actually use —
picks `nk=16` here, not the `nk=4` that calling `choose_tile` directly reports.
Reproduce against `gm=16 gn=16 nk=16` at K=1024, two chunks.

**What it cost, and why it hit SDXL squarely.** The attention head dim is exactly
64, so slicing the WEIGHT per head -- which §6.4 recommends, because it returns
each head contiguous -- landed precisely on the broken shape for every Q/K/V
projection at C=640 and C=1280.

**The workaround is no longer needed.** Projecting full width (`N = C`) and
cutting the heads out on the host was the fix before the bank bits were emitted;
per-head N=64 now scores the same 1.07e-03 end to end. Keep full-width projection
anyway, but for its own reason: it takes the block's projections from 30 GEMMs to
6, and the head cut is free because the host already unpacks that drain.

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

   **The area arithmetic says the same thing.** ONE vector core is ~33,000 LUT.
   A new instruction costing ~3,000 LUT lands in all six for ~18,000 — **half a
   core's worth of area for a capability every core gains.** Adding cores only
   wins when the budget genuinely holds two more (24+6 → 24+8); below that,
   instructions beat cores on area every time. And the schedule, not the
   silicon, is what is short: softmax is 15 instructions and 3.5 dispatch flits
   per row, measured at S=64 (227 flits for 64 rows), while `VRED …EXPSUM`,
   `VFMA` and `VLOOP` are all in the RTL and the lowering emits none of them —
   15 → 11 instructions and 64 dispatches → 1, with no hardware change.

   **Two of these belong in the cluster rather than the vector core at all.**
   `mx_acu_fp`'s `OP_ADD` is `tile[addr] += chain` with no matmul, so a residual
   add and the time-embedding broadcast add are ACU commands. Both run on vec
   today, and vector is the critical resource in attention — measured 3x
   matmul's cost on a 20-head, Lq 1024, Lkv 256 block. Spend the vector core
   only where the ACU cannot reach.

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
def kernel(x):
    return x.reshape(128, 4, 64).permute(1, 0, 2) * 2.0

INPUTS = {"x": (128, 256)}                 # three levels -> §6.1's message
```

Anything in this document marked **SW** should produce a clear `ScheduleError`
naming the limit. If it produces a wrong number instead, that is a worse bug
than the missing feature.
