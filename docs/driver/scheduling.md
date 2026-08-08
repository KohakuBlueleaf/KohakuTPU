# The auto-scheduler: what it does, what it costs, what you see

Every decision `lower()` makes between a level-1 graph and a level-2 schedule,
what each one is worth, and how to read it in `examples/00_pipeline.py`.

The running example is `gelu(x @ w + bias)` at `256 x 1024 x 256` on
`VU13P_8CU`. Every number here is `ktpu.codegen.cost_of` counting the emitted
instruction stream, not an estimate.

---

## 1. The passes, in order

### 1.1 Engine assignment

`engine_for(kind)`. `matmul` goes to a cluster, everything elementwise or
reducing goes to a vector core, and **views go nowhere**: a reshape, permute or
expand folds into the consumer's address descriptor and moves no data
(`vector-core.md` §10). A slice becomes an element offset, a single-column
slice becomes a stride.

Anything neither engine can do — gather with computed indices, sort, top-k —
gets `Engine.HOST`, which is legal and *visible*. A kernel that quietly assumed
hardware support is then caught here rather than on the machine.

### 1.2 Tile choice

`choose_tile(m, k, n, target)` searches power-of-two `(gm, gn)` sub-tile counts
with `gm*gn <= t.tiles`, taking

```
  nk        = min(l1_a // (banks*gm), l1_b // gn, k // kblock)
  intensity = 2*gm*gn / (gm+gn)            arithmetic intensity
  useful    = m*k*n / (padded m*k*n)       the fraction that is real work
  score     = intensity * useful
```

Ties go to larger `nk`, then to the squarer tile. **The whole problem is scored,
never a per-cluster slice** — that was the defect in
`.plan/measurements/dispatch-n-only.md`, where a narrow band changed the *tile*
and turned a 2x shortfall into a 7x one.

At `256 x 1024 x 256` this picks `gm=16, gn=32, nk=4` → a `64 x 128` output
block with `k=128` per sweep.

### 1.3 Shared-operand blocking

`shared_a_blocking()`. An operand is packed into L1-entry order **for a tile**,
blocked by `(tile.m, tile.k)`. A region holds one layout, so two matmuls sharing
an A operand cannot block it differently — MoE's `x` feeds the gate and every
expert. The pass takes the smallest blocking in each group, which is always
safe: a smaller tile means more grid instances and still covers the output.

Without it, whichever matmul packed last wins and the other reads the right
bytes in the wrong places. `Program.packing` now refuses a conflicting overwrite,
so if this pass is ever wrong it is a `ValueError` and not a wrong answer.

### 1.4 Grid

`grid_for(m, n, choice)` is `ceil(m/tile.m) x ceil(n/tile.n) x sk`, checked by
`Grid.covers`: every output element produced exactly once. A gap leaves the
output's previous contents in place; an overlap races. Overshoot below one tile
is padding and is fine — a zero contributes nothing to a dot product.

`4 x 2 = 8 instances` here, one per cluster.

### 1.5 Fusion into bands

Maximal runs of elementwise ops become one VECTOR band, because a pass doing one
op per element is memory-bound and one doing several is not (`vector-core.md`
§7.2: FLAT is 10.7 results/cycle against 16 ALUs; D2 already saturates).

Two rules cut a run:

- **a band is shape-uniform.** Flash attention's running max is `(T, 1)` and its
  scores are `(T, C)`; they cannot share a pass because the trip count differs.
- **a reduction ends its band**, since its output shape is not its input's. Its
  shape key is its INPUT size, not its output.

### 1.6 Epilogue folding

An elementwise run reading only the preceding matmul becomes a band that
`consumes` it — same tiles, same order, loaded as ACC24. `epilogue_grid` derives
the grid from the producer's tiles, splitting a tile by ROWS while that leaves
fewer instances than there are vector cores, and `correspondence()` checks it.

**It does not save the trip through DRAM.** Nothing in the machine moves a tile
from a cluster to a vector core (`vector-core.md` §9), and ACC24 is the wider
format. §3 is what it does buy.

### 1.7 Operand binding

`codegen/operands.plan()` resolves every operand to something the ISA can name:
the chain wire, a vector register, a scalar register, or a load. Constants
become `VSETI`. The whole chain runs per VL chunk — **`VMODE` bounds how many ops
CHAIN, not how long a value lives** — so intermediates stay in registers.

---

## 2. What it looks like in simulation

`python examples/00_pipeline.py`, stages 2 and 4:

```
band b0  grid(4, 2, sk=1) = 8 instances
  m_ per instance  C[64,128] += A[64,128] @ B[128,128]   x 8 K-chunks of 128
     whole problem  256 x 1024 x 256   accumulates in acc24
band b1  grid(4, 2, sk=1) = 8 instances  consumes=b0
  v_ per instance  16 ops on 64x128 = 8192 elements   reads acc24 resident from b0
```

The grids match instance for instance: `correspondence(b0, b1) == (1, 1)`. That
is the check that was missing when a flat `grid(64, 1)` over `1024 x 1` passed
for a partner of eight `64 x 128` tiles — the element totals agreed and instance
*j* named no tile.

```
instructions          424   (drain 8  fill 128  gemm 64  valu 128
                             vld 16  vloop 8  vseti 48  vsetmd 8  vsetvl 8  vst 8)
device image      344,192 words = 1,344 KiB
dram read         835,584 words
dram write         81,920 words
useful flops   135,266,304
overhead            0.027 dram bytes per flop
vector body           152 instructions inside the loop
```

`valu 128` is 16 ops on 8 cores, and `vld 16 / vst 8` is two loads and one store
per core. **Nothing but VALU scales with chain depth**, which is the invariant
`tests/ktpu/unit/test_cost.py` locks.

---

## 3. The RAM, justified

Folded against unfolded, same kernel, same instruction count:

| | folded | unfolded | delta |
|---|---|---|---|
| device image | 344,192 w | 327,808 w | **+16,384 w = +64 KiB** |
| dram read | 835,584 w | 819,200 w | +16,384 w |
| dram write | 81,920 w | 65,536 w | +16,384 w |
| instructions | 424 | 424 | — |
| overhead | 0.027 B/flop | 0.026 B/flop | +4% |

Every one of those deltas is the same 16,384 words, and it is **exactly one
thing**: the matmul's output staged as ACC24 instead of FP16.

```
  folded     v3   49,152 w   b0 produces %3, acc24
  unfolded   v3   32,768 w   b0 produces %3, fp16
```

24 bits against 16 is 1.5x, and `49,152 = 32,768 * 1.5`. Nothing else moved.

**What the 64 KiB buys.** The intermediate is rounded and clamped ONCE, on the
vector core's final store, instead of twice. At FP16's ceiling of 65,504 the
first clamp is not a rounding but a loss, and it is silent. Measured, from
`examples/04_run_it_and_check_it.py` on a kernel whose matmul output is 204,800:

```
  folded  (acc24)     max abs err 5.00e-02   relative 2.44e-04
  not folded (fp16)   max abs err 1.39e+02   relative 6.80e-01   clamped 4096
```

4,096 of 4,096 elements clamped, and the answer comes back as 65.5 where the
true value is 204.8. **5% more DRAM for a result that is not silently wrong** is
the trade, and it is the same argument as `vector-core.md` §11.

It is also a *choice*: `lower(graph, target, fold_epilogue=False)` takes the
smaller image and the FP16 intermediate.

### 3.1 What is NOT in that 64 KiB

An earlier version of this scheduler spilled every VMODE group boundary to a
`tmp` region in DRAM:

```
  tmp   245,760 words     42% of the whole image
```

That was a mistake, not a trade — `VMODE` bounds chaining, not lifetime, so
those values belonged in registers. It also cost 328 vector instructions against
224. Both are gone, and `test_cost.py` asserts no scratch region exists.

The remaining honest gap: the bias gets its own 128-word region rather than
being expanded to a full `256 x 256` operand, which is 128 words instead of
32,768. That is the address generator doing its job.

---

## 4. What the auto path still cannot do

It tiles, fuses, places and orders. It does not change the MATHS — so it never
derives flash attention's online softmax from `softmax(q @ k.T) @ v`, because
that is a different expression with different intermediates.

The fix is not a smarter scheduler but a **rewrite catalogue**: tiled softmax →
online form, variance → Welford, log-sum-exp → shifted. Each entry is finite and
testable. Until then a kernel that needs one is written by hand — which is what
`examples/05_attention.py` is for.

Everything else should be optimal, and a gap that is not one of those rewrites
is a bug in this document's passes, not a reason to hand-write.
