# The memory mover

A layout and gather engine inside MAG. It moves bytes without computing on
them, which is most of what `limits.md` says this machine cannot do.

It goes in MAG because that is where the addresses are. `limits.md` §3.2 had
already reached the same place from the capability side: closing element-dynamic
indexing needs "an address generator that can take a vector of addresses, i.e. a
gather engine in MAG".

---

## 1. What it absorbs

Each of these is a real, current defect or workaround, not a hypothetical.

### 1.1 Order conversion between engines

A cluster DRAINS in sub-tile order; a FILL reads L1-entry order. Only a vector
store converts between them. The consequence, found this session: **a matmul
reading another matmul's output was silently wrong at every size** -- relative
error 1.26e+00 with no exception, because the bytes were right and the order was
not. The fix in the compiler is `lower()` inserting a relayout band: a vector
pass that multiplies by 1.0 to re-lay a tile.

With a mover that is a descriptor, and the vector pass disappears.

### 1.2 The host doing transposes

`(L, H*D)` is what a QKV projection emits. A head of it is `x[:, h*D:(h+1)*D]`,
an inner multi-column slice, which level 3 refuses -- so operands must arrive as
`(H, L, D)` and the transpose is the previous layer's job. That is the correct
call today and it is exactly the work a mover exists to do.

### 1.3 The ops that are refused for want of an address descriptor

`limits.md` §6: `concat` is "two writes into one region", `pad` is "a bound and
an offset on a zeroed region", a general `permute` "is a permutation of the
stride list", a strided window needs the AGU's four `(stride, bound)` pairs.
None of them need arithmetic. All of them need a unit that writes a region from
a descriptor.

### 1.4 The gather itself

`table[idx]`, `index_select`, `take_along_dim`, and the top-k routing that
`moe_dense` deliberately does not do. Today the workaround is
`one_hot(idx, V) @ table` -- a matmul that costs `T x V x C` multiply-accumulates
to move `T x C` values. Fine at 256 experts, catastrophic at a 32k vocabulary.

Scatter-add (`limits.md` §3.3) is the harder half and should be scoped
separately: read-modify-write ordering is a different problem from gather.

## 2. Why it is worth doing before the general cores

Because it is the only one of the two that helps the bottleneck.

Attention is vector bound -- 8 to 16 vector cores is about 2x throughput,
measured. Every item in §1.1 to §1.3 is currently paid **on the vector cores**:
the relayout band, the tile-order conversion, and `limits.md` §6.1's honest
suggestion to move data with a permutation matmul. Moving that into MAG is not
only a capability change, it hands the scarce engine back to arithmetic.

## 3. What it needs to be

Sketch, not a specification.

- **A descriptor, not an instruction stream.** Base, a stride list deep enough
  for the four `(stride, bound)` pairs the AGU already takes, an element width,
  and a destination. Enough to express: tile order to entry order, entry order
  to tile order, an outer-axis slice, a last-two-axis transpose, a strided
  window, and a `(L, H*D)` to `(H, L, D)` permute.
- **A vector of addresses as a second mode.** The gather case. An index stream
  plus a base and an element width. This is the expensive mode and it is what
  distinguishes the mover from a DMA engine.
- **Ordering against the compute engines.** A move that races the DRAIN feeding
  it is the same class of bug as §1.1 and would present the same way -- right
  bytes, wrong place. Whatever fences the clusters already use must cover it.

## 4. Where a moved tile lands, and the engine-to-engine path

The mover is memory to memory: it reads a region and writes a region. That
alone covers §1.1 to §1.4 and is the smaller, safer claim.

Separately, and worth doing, is a **direct engine-to-engine path** for
neighbours. Both directions correspond to a DRAM round trip the compiler makes
today purely to change layout:

- **`acu` to a neighbouring `vec`.** A folded epilogue drains FP16 to a region
  and then loads it straight back; `lower()`'s docstring already admits the fold
  "does not save the trip through DRAM". This would.
- **`vec` to a neighbouring `acu`.** Attention's `p @ v` stores `p` to DRAM in
  L1-entry order so a FILL can read it back, which is what `feeds_matmul`
  exists for, and it is the same trip the relayout band pays for back-to-back
  GEMM.

Either way this is a real amendment to
[`../compute/vector-core.md`](../compute/vector-core.md) §9, which currently
states that nothing in the machine moves a tile from a cluster to a vector core.

**It must be a nearest-neighbour fast path, not a general one.** In any mesh
some cluster is far from some vector core, so the DRAM path stays as the
fallback and level 2 chooses between them by placement. That keeps the ISA
addition small -- one hop, one direction each -- and it degrades to today's
behaviour rather than failing when the pair is distant.

The prerequisite is that the compiler learns locality at all. Right now `Target`
carries `clusters` and `vector_cores` as flat interchangeable counts and
`assign()` deals grid instances across nodes with no notion of distance, so
there is nothing to express "this vector band should run next to that cluster".

## 5. What it does NOT solve

- **Sort and top-k** stay on the host (`limits.md` §3.4). A mover moves; it does
  not compare. Routing decisions are thousands of operations per token against
  billions of MACs and a comparator network earns nothing.
- **Boolean mask selection** stays impossible in a static-shape IR -- the output
  length depends on the data (`limits.md` §3.1). The mover could execute a
  compaction whose length the host already computed, which is a different and
  much smaller claim.
- **Integer and floor arithmetic** (`limits.md` §4.1, §4.2) belong to the
  general cores, not here.
