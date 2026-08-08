# The three IR levels

Design. `src/ktpu/ir/`.

Each level answers one question and refuses the other two.

| level | answers | must not contain |
|---|---|---|
| **graph** | *what* the user meant | tiles, grids, addresses, engines |
| **schedule** | *how* the machine will do it | opcodes, flit layout, word addresses |
| **target** | *which bytes* | any decision at all |

The boundary is enforceable and should be enforced: `verify.py` checks each
level for the things the level below is allowed to know. A tile appearing in a
graph-level op is a bug, not a shortcut.

---

## 1. The type system — `ir/dtype.py`

This is the part nothing external can model, so it is the part that is written
first and tested hardest.

| dtype | bits | where it lives | notes |
|---|---|---|---|
| `FP32` | 32 | host, DRAM | E8 M23 |
| `FP16` | 16 | host, DRAM, NoC | E5 M10, **the only output format today** |
| `E8M15` | 24 | vector core | S1 E8 M15, no subnormals |
| `ACC24` | 24 | matmul accumulator | S1 E7 M16, `BIAS=63` |
| `MXFP7` | 7 + scale | matmul operands, NoC | int7 with a shared `E5M3` scale per 32 |
| `INT8/16/32` | | indices, masks | not arithmetic |

Three facts the type system has to encode, because passes will reason about
them and getting them wrong is silent:

- **`FP16 → E8M15` is exact** and **`E8M15 → FP32` is exact**. E8 contains E5
  and E8 *is* FP32's exponent, so neither conversion can overflow. That is why
  the vector core needs no saturation logic
  ([`../compute/vector-core.md`](../compute/vector-core.md) §1.1).
- **`ACC24 → E8M15` is range-lossless**, costing one rounding of the bottom
  mantissa bit. E7's range is strictly inside E8's. This is the split-K
  epilogue's whole basis (§11 of the same doc).
- **`* → FP16` can saturate at 65,504**, and today it does so silently
  (`mx_fpacc.v:582`). Any pass that inserts an `FP16` store is inserting a
  potential range failure and should be able to say so.

A `DType` therefore carries `(exp_bits, man_bits, bias, has_subnormals)` and can
answer `contains_range_of(other)` and `is_exact_from(other)` — so a conversion
pass proves its own safety rather than a comment claiming it.

### 1.1 Mixing formats is normal, and rejecting it would be wrong

There is no promotion rule here, because there is no promotion *question*: **the
format an op computes in is the engine's, not a function of its operands.**

| | operands | computes in | because |
|---|---|---|---|
| matmul | `FP16 × MXFP7` | int7 + E5M3 block scale | weights pre-quantised once, activations quantised online in `mx_quant` — the legacy driver's `preq=(bool, bool)` is exactly this |
| vector | `FP32 + FP16` | `E8M15` | E8 contains E5 **and** E8 *is* FP32's exponent, so both convert on the load path with no range logic at all |

Both are ordinary. The second one is the reason E8 was chosen in the first place
([`../compute/vector-core.md`](../compute/vector-core.md) §1.1), so a rule
rejecting it would contradict the datapath.

So level 1 asks only **"what must this value be able to hold?"** — `converge()`,
which is a range question first and a precision question second, and which never
invents a format. Level 2 knows the engine and therefore what is actually
computed in. A float mixed with an integer is still an error: `INT` exists for
indices and masks, not arithmetic.

### 1.2 Where a conversion physically happens — and why there is no cast instruction

Three places, and **none of them is a separate pass over the data**:

```
   host upload        FP32 -> FP16      the host is writing the bytes anyway
   read path          FP16 -> MXFP7     mx_quant, already in MAG
   vector store       E8M15 -> FP16/FP32/ACC24/INT   a field in VST
```

That covers every case, which is why the answer to "should there be an explicit
cast instruction, or a pre-cast pass?" is **neither**:

- **FP32 into a matmul** — the operand is quantised to **int7** with a shared
  scale regardless of what it arrived as. Feeding FP32 to that is precision
  theatre: everything below ~7 bits plus a block scale is discarded before the
  first multiply. A tensor destined for a matmul should be stored FP16 by
  whatever produced it — the host on upload, or the vector core's `VST.FP16` if
  it was computed on-device. If FP32 operands are wanted anyway, the honest
  route is teaching `mx_quant` an FP32 mode: the block-peak reduction works
  unchanged (FP32 is sign-magnitude with the exponent above the mantissa, so the
  peak is still a plain unsigned max) and the cost is halved fetch bandwidth,
  8 elements per beat instead of 16. Not free, but not a new mechanism.
- **Vector core output** — the cast is already a **field on the store**
  ([`../isa/vector.md`](../isa/vector.md) §2), so `cast → store` folds into one
  instruction at level 2 and costs nothing. Casting to `ACC24` is what makes the
  split-K epilogue work.
- A standalone `cast` op still exists at graph level, because a user may want
  one and because the folding pass needs something to fold. If it survives to
  level 3 unfolded, it is a real pass over the data and the cost model will say
  so.

**The one range limit worth writing down:** `MXFP7`'s scale is `E5M3`, which
spans FP16's ~30 binades. A block whose peak lies outside FP16's range clamps —
so quantising FP32 is bounded by FP16's range, not FP32's. In practice that
costs nothing, since data outside FP16's range could not have been an FP16
tensor either, but it is the reason `MXFP7.value` is `FP16` and not `FP32`.

---

## 2. Level 1 — graph IR

Value semantics. A `Tensor` is `(shape, dtype, layout)` and an `Op` consumes and
produces tensors. No mutation, no aliasing, no machine.

```python
Tensor(shape=(256, 1024), dtype=FP16)

class Op:
    kind: OpKind
    inputs: tuple[Value, ...]
    attrs: dict          # axis, epsilon, the matmul's transposes, ...
    out: Value
```

### 2.1 The op set

Small on purpose. Anything expressible as a composition of these is a
composition, not an op.

| group | ops |
|---|---|
| **elementwise 1** | `neg abs recip rsqrt exp2 log2 sqrt relu` |
| **elementwise 2** | `add sub mul div max min` |
| **elementwise 2** | `cmplt cmpgt cmpeq` |
| **elementwise 3** | `fma select` — operand order is **`(cond, on_true, on_false)`** |
| **reduce** | `sum max min sumsq` over an axis set |
| **contract** | `matmul` — the only macro-op |
| **view** | `reshape permute expand slice pad` |
| **convert** | `cast` |

All three comparisons exist because the vector ISA has all three as single
opcodes ([`../isa/vector.md`](../isa/vector.md) §3). With `cmplt` alone a
frontend spends four ops on `a == b` to say what the hardware does in one.

`slice` takes per-axis `[begin, end)` and **has no step** — a strided slice is a
gather, and gather with computed indices is what this machine deliberately does
not have (§8 of [`../compute/vector-core.md`](../compute/vector-core.md)).
Refusing it here keeps that promise where it can still be explained. `pad` takes
per-axis `(lo, hi)` and a fill value; zero is the useful default because a zero
contributes nothing to a dot product, which is what lets a GEMM pad to a whole
tile and get the same answer.

There is no `broadcast` op: `expand` is it. A frontend that wants
`broadcast_to` builds it from `reshape` then `expand`.

**A mask is a value, not a predicate register.** `cmplt` produces a tensor and
`select` consumes one. The vector ISA has a separate predicate file — `VCMPLT`
writes `P[pr]` and `VSEL` reads it — so **level 2 is where a mask becomes a
predicate**, and that materialisation is a lowering step rather than something
level 1 knows about. The two representations are deliberately different and the
seam between them is named here so it is not discovered later.

Two deliberate absences.

**No `softmax`, no `layernorm`, no `gelu`.** They are compositions, and a
frontend that emits them as single ops hides the fusion opportunity that is this
machine's entire performance story. If a pattern is worth recognising, that is a
*pass* over the composition, not a node.

**Views are ops, not metadata.** `permute` produces a value. A view op is
expected to fold into the consumer's address generator at level 2 and cost
nothing, but making it a value means "did this materialise?" is a question with
an answer.

### 2.2 What graph level is not allowed to know

Not the cluster count, not `TILES=512`, not L1 entry counts, not `STAGE_FLITS`,
not whether a vector core exists. A graph is valid against a *family* of
machines; the target appears at level 2.

---

## 3. Level 2 — schedule IR

Where the compiler actually lives. A schedule is a **grid of work items over an
engine**, plus where the data sits.

```python
class Band:                 # one unit of dispatched work
    engine: Engine          # MATMUL | VECTOR
    grid: tuple[int, ...]   # (m_tiles, n_tiles, k_split) for matmul
    tile: Tile              # the shape ONE instance computes
    ops: list[SchedOp]      # what it does, in order
    residency: dict         # what stays in L1 across the grid
```

### 3.1 The grid is 3D, and this is the fix

Today's driver splits **N and only N** (`kernel.py:284`), so `M=256 K=1024
N=256` at 8 clusters gets a 32-column band, which drives the tile search to
`gm=64 gn=8 nk=1`, which makes `m_tiles=1` and 32 K-chunks — one cluster live at
a time, roughly 10% of peak. The full arithmetic is in
`.plan/measurements/dispatch-n-only.md`.

The schedule level makes that a printable object:

```
   grid  = (ceil(M/BM), ceil(N/BN), SK)
   instance (mo, no, ko)  computes  C[mo, no] += A[mo, ko] @ B[ko, no]
```

- `SK == 1` — each instance owns its output tile outright and writes it.
- `SK > 1` — instances sharing `(mo, no)` produce **partials**, which must be
  reduced. That reduction is a second `Band` on the vector core, and it is why
  `ACC24` is in the type system: the partials never round-trip through FP16
  ([`../compute/vector-core.md`](../compute/vector-core.md) §11).

A pass that picks `SK > 1` without emitting the reduction band is invalid, and
`verify.py` should say so rather than producing a program that silently sums
nothing.

### 3.2 Memory space is explicit

`DRAM | L1 | REG`. Every level-2 value names one. The existing driver's L1
residency decisions — B resident across the `m` loop, A double-buffered — are
real and measured (a quarter of all memory traffic, and 22.3% of the machine
respectively). They become attributes on a `Band` rather than `if` statements in
a code generator.

### 3.3 Engine assignment

Two engines, and the rule is nearly trivial: `matmul` goes to the cluster,
everything else to the vector core. What is *not* trivial, and is the pass worth
writing well:

- **fusion** — how many elementwise ops share one pass over the data. The
  machine is memory-bound below 2 ops per element and compute-bound above it
  ([`../compute/vector-bringup.md`](../compute/vector-bringup.md) §2.1), so this
  pass is worth more than any other.
- **epilogue folding** — an elementwise chain immediately after a `matmul`
  should run on the partials while they are still on the mesh, not after a
  DRAM round trip.

### 3.4 Cost is attached here

Every `Band` can be costed without lowering it: `max(compute, memory)` per
instance, times grid size, plus round overheads. That is what makes the
schedule level reviewable — two schedules for the same graph can be compared
before either becomes bytes.

---

## 4. Level 3 — target IR

Bytes, and no decisions. One `Program` per engine kind:

```python
MatmulProgram  →  FILL / GEMM / DRAIN flits      (isa/cluster.md)
VectorProgram  →  32-bit instruction words        (isa/vector.md)
ControlProgram →  host writes and polls           (isa/orchestrator.md)
```

Plus the operand image: what goes where in DRAM, in the layout the packer
writes and the address generator walks. **These two are one decision and must be
one object** — the existing driver keeps them in step by hand, and the comment
at `bench.py:459` explains that a mismatch "overlaps two operands and the answer
is quietly wrong".

Level 3 is also where the disassembler lives, because the only trustworthy check
that codegen is right is reading back what was emitted.

---

## 5. Verification

Per level, and cheap enough to run always:

- **graph** — shapes and dtypes agree; no cycles; every value used once or
  explicitly copied.
- **schedule** — the grid covers the output exactly once, no gaps and no
  overlaps; every `SK > 1` has its reduction band; memory spaces are consistent;
  every tile fits the target's residency limits.
- **target** — addresses land inside their operand's region; flit counts fit
  `STAGE_FLITS`; instruction fields fit their widths.

**The coverage check at schedule level is the one that matters.** "Every output
element is produced exactly once" is the invariant the N-only split violated in
spirit — it produced every element once, but on one cluster at a time — and it
is the invariant a 3D grid can violate outright by miscomputing a K-split. It is
also checkable without hardware, which makes it the cheapest real test in the
system.

---

## 6. What is deliberately not in the IR

- **No autotuning.** Tile choice is an analytic decision against a stated cost
  model. When measurement disagrees with the model, the model is what gets
  fixed — a search that finds a good tile teaches nothing about why.
- **No control flow at graph level.** The DSL rejects runtime-dependent
  branching ([`dsl.md`](dsl.md)); what survives tracing is straight-line.

### 6.1 No dynamic shapes *in the IR* — which is not the same as no dynamic shapes

**Every shape in the IR is a concrete integer.** Not a placeholder, not a symbol,
not a bound. A pass that has to reason about `?` cannot pick a tile, cannot
compute a grid, cannot check that the grid covers the output exactly once, and
cannot cost itself — so admitting symbolic extents would cost the IR every
property §5 depends on.

**Dynamic shape belongs above the IR, and is fully expected there.** The DSL and
tinygrad both specialise: they see the real shape at call time, and they
*generate or select* a graph for it. Which means:

```
   caller with a runtime shape
        │   specialise: bucket, pad, or trace fresh
        ▼
   a graph whose every extent is an integer  ──▶  the IR, as specified
```

Three strategies, all upper-level and none needing IR support: **cache by
shape** (trace once per distinct shape — right for a handful of shapes),
**bucket** (round up to the next of a fixed set and mask the tail — right for
sequence lengths), and **pad** (to a tile multiple, which the machine requires
anyway since a zero contributes nothing to a dot product).

So the constraint reads: *the IR is a specialised program; specialisation is
somebody else's job.* That is a division of labour, not a limitation, and it is
the same one every ahead-of-time tensor compiler eventually converges on.
