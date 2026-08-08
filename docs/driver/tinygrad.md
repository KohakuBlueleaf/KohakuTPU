# Wiring tinygrad in

Design. `src/ktpu/frontend/tinygrad_bridge.py`, not built.

---

## 1. The corrected layering

The DSL and tinygrad enter at **different levels**, and conflating them was a
design error worth recording.

```
   tinygrad / any graph frontend ──> LEVEL 1  ──auto-schedule──┐
                                     tensors                   │
                                                               ├──> LEVEL 2 ──> LEVEL 3
   our DSL (Triton-shaped)      ─────────────────────────────> │    schedule     program
                                     writes the schedule itself
```

**Auto-scheduling exists for the graph path and only for it.** A scheduler
tiles, fuses and reorders; it does not invent algorithms. Flash attention's
online softmax carries a running max and a running denominator and rescales the
accumulator between blocks — state the naive `softmax(q @ k.T) @ v` graph never
contains. No amount of fusion produces it, because it is an *algorithmic*
rewrite and not a scheduling one.

So a DSL that emits level 1 is useless: everything it writes goes through the
scheduler that cannot get there. **The DSL must write level 2 directly.**
tinygrad, which really is a tensor graph, enters at level 1 where it belongs.

That is the split. This document is the level-1 half.

---

## 2. What we want from tinygrad, and what we do not

**Want:** a real frontend. Models that already run, autodiff, a broad op set,
dtype and shape handling, and tensor-level fusion — all of it pure Python with
no build step.

**Careful with: its kernel optimizer.** tinygrad's `Kernel` applies upcast /
local / group transforms shaped by GPUs: threadgroups, warps, shared memory.
None of those exist here. What does exist is a residency equation — L1 entries,
512 output sub-tiles, `STAGE_FLITS` rounds — already implemented and tested in
`passes/tile.py`.

The thing worth knowing before choosing an attachment point: **the optimizer
does not assume, it asks.** Every GPU-shaped transform is gated on a capability
the *Renderer* declares. So the optimizations that would hurt are not something
to work around — they are something to switch off truthfully.

---

## 3. How to attach: a Device, with the GPU-shaped passes declared away

**The decision: implement a tinygrad Device.** Provide `Allocator`, `Renderer`,
`Compiler` and `Program`. tinygrad's whole pipeline runs, unmodified, and a user
runs tinygrad the way they already do — which is the entire point. No fork, no
patched scheduler, no bespoke entry point.

> An earlier revision of this document rejected the Device route on the grounds
> that "the grid and tile would be tinygrad's". That was wrong, and it is the
> conclusion this section corrects. The transforms it feared are conditional on
> `Renderer` flags; a device that says it has no local memory is never handed a
> threadgroup tiling. Declaring is not a workaround.

### 3.1 What we turn off, by declaring it

| Renderer field | ours | what it disables |
|---|---|---|
| `has_local` | `False` | the whole LOCAL / threadgroup family: `OptOps.LOCAL`, `GROUP`, `GROUPTOP` |
| `has_shared` | `False` | shared-memory staging and the reductions that stage through it |
| `supports_float4` | `False` | vec4 packing; our natural width is `VL = 128` lanes, not four |
| `global_max` / `local_max` | `None` | launch-geometry clamping — we have bands over 8 cores, not a grid launch |

None of this is a lie told to the optimizer to make it behave. **The machine
genuinely has no threadgroups and no shared memory** — L1 is an explicitly
filled per-core scratchpad ([`../compute/vector-core.md`](../compute/vector-core.md)
§9), not a cache and not shared. Saying so is describing the device.

What survives is what we want: elementwise fusion into one kernel, which maps
onto one of our bands, and "one kernel, one output buffer", which maps onto one
of our regions ([`scheduling.md`](scheduling.md) §1.5).

### 3.2 What we turn on: the cluster as a tensor core

`Renderer.tensor_cores` is the sanctioned way to say "this shape has a machine
instruction". A `TensorCore` declares dims, input and output dtypes, and the
reduce and upcast axes; tinygrad then matches the matmul pattern and emits the
TC op instead of a loop nest.

That is exactly `FILL / GEMM / DRAIN`, and it is how the cluster becomes
reachable **without touching the scheduler**. §5 is why this is still the hard
part in practice.

### 3.3 What BEAM becomes

`BEAM` searches `OptOps` by *measured* runtime. Once the GPU-shaped ops are
declared away, what is left for it to search over is close to what our own
`choose_tile` searches — and `ktpu.codegen.cost_of` already counts a schedule's
DRAM traffic and instruction mix from the emitted stream.

So BEAM is not a threat to the residency equation; pointed at that cost model it
is a second opinion on it, with the advantage of being empirical. Worth wiring
after the basics work, not before.

### 3.4 The part that is genuinely work

tinygrad linearises to a scalar loop nest with explicit index arithmetic. Our
vector core wants a `VLOOP` over `VL` chunks plus an address descriptor
([`../isa/vector.md`](../isa/vector.md) §5). Turning one into the other is a
pattern match on the loop nest, and it is where the effort in this bridge
actually goes — not in fighting the optimizer.

The fallback if that match is ever incomplete is not a wrong answer: an
unmatched nest becomes `Engine.HOST`, which is visible in the schedule.

---

## 4. The mapping

Closer than expected, because tinygrad and this machine made some of the same
choices.

| tinygrad | ours | note |
|---|---|---|
| `ADD SUB MUL` | `ADD SUB MUL` | direct |
| `RECIP` | `RECIP` | both keep reciprocal primitive, not divide |
| `SQRT` | `SQRT` | |
| **`EXP2` `LOG2`** | **`EXP2` `LOG2`** | both chose **base 2** primitives; `exp`/`log` are the composed forms in each |
| `MAX` | `MAX` | |
| `CMPLT CMPNE` | `CMPLT CMPEQ` | ours has all three compares |
| `WHERE` | `SELECT` | |
| `REDUCE_AXIS(SUM/MAX)` | `SUM RMAX` | |
| `RESHAPE PERMUTE EXPAND PAD SHRINK` | `RESHAPE PERMUTE EXPAND PAD SLICE` | `SHRINK` is our `SLICE` |
| `CAST BITCAST` | `CAST` | bitcast has no use here |
| *(no matmul op)* | `MATMUL` | §5 |

The base-2 agreement is the pleasant one: we chose `exp2`/`log2` because range
reduction becomes a bit slice ([`../compute/vector-core.md`](../compute/vector-core.md)
§4.1), and tinygrad chose them for the same reason every backend does. No
conversion loss at the boundary.

**Dtypes are clean at the seam.** tinygrad buffers are FP16/FP32, which is
exactly `HOST_DTYPES`. `E8M15`, `ACC24` and `MXFP7` are internal and never cross
it — nothing to translate.

---

## 5. Matmul is the one hard part

tinygrad has no matmul primitive: `a @ b` is a broadcasted `MUL` followed by
`REDUCE_AXIS(SUM)` over the contracted axis. Our `MATMUL` is a macro-op the
cluster executes whole.

So the cluster must be reached by **pattern-matching `MUL + REDUCE_SUM` back
into `MATMUL`** — recognising the shapes and strides that mean "contraction over
one axis". This is the escape hatch every accelerator backend needs, and §3.2 is
the mechanism: declare a `TensorCore` and tinygrad performs the match itself,
the same way it does for GPU tensor cores. We supply the shape and the dtypes;
we do not supply the matcher.

Get it wrong in the safe direction: an unmatched contraction falls through to
`MUL` + `SUM` on the vector core, which is correct and slow. A false match is
wrong and fast. So the `TensorCore` declaration must describe the cluster's real
shape — `(gm*lanes) x (gn*lanes)` output over `nk*kblock` of K — and nothing
wider, because a TC that claims more than the hardware does is a false match
tinygrad will happily make.

---

## 6. Build order

1. **A `Renderer` that declares the machine honestly** — §3.1's flags — and
   renders an elementwise kernel only. Nothing else can be checked until the
   GPU-shaped passes are off, because their output is not expressible.
2. `Graph` from what the renderer receives, elementwise and reductions only.
   Check against the reference simulator versus tinygrad's own CPU backend.
3. The `TensorCore` declaration for the cluster, §3.2 and §5.
4. Views: tinygrad's ShapeTracker is more general than our five view ops, so
   some movement will need materialising. Find out how much before deciding
   whether to widen the view set. `through_views` already rejects what it
   cannot express rather than folding it away wrongly.
5. `Allocator` + `Program` so `Tensor(...).to("KTPU").realize()` works end to
   end, calling our `codegen` and then `ktpu.interp.mesh`.
6. Point `BEAM` at `cost_of`, §3.3.

Steps 1-2 are most of the value: an elementwise kernel that runs through the
real tinygrad pipeline proves the capability declaration is right, which is the
whole premise of taking the Device route.

**Pin the version.** tinygrad's internals move quickly; the bridge is one file
so a re-port is bounded, and that is the trade for a pure-Python frontend with
no build step.

---

## 7. What the auto path must achieve

**The target is within 2x of a hand-written kernel on anything fusable, and the
gap must always be explained by a named missing transform.** "Auto-scheduled, so
slow" is not an acceptable answer — it is a statement that the scheduler has not
been written yet.

| path | covers | target |
|---|---|---|
| tinygrad → L1 → auto-schedule → L2 | any model, no hand work | **within ~2x** |
| DSL → L2 directly | the kernels that matter | the ceiling |

The hand path should win on the handful of kernels where an *algorithmic*
rewrite beats any schedule — attention, chiefly. Everywhere else the auto path
should be close enough that hand-writing is not worth the effort, and where it
is not, the reason below is the work item.

### The gap today, in order of how much it costs

1. **Nothing folds the epilogue onto the matmul.** A GEMM drains to DRAM and a
   vector band reads it straight back. That is a full round trip per output
   element for work the accumulator could have finished in place. **This is
   ours to fix and it is the biggest item** — [`ir.md`](ir.md) §3.3 names it and
   nothing implements it.
2. **The compiler does not know the operation has another mathematical form.**
   This is the only real limit, and it is narrower than "auto-scheduling is
   slow". Every *individual* operation should be optimal in the auto path, and
   so should every tiling, fusion and ordering of operations — those are
   scheduling questions and a scheduler can answer them.

   What it cannot do is **change the maths**. `softmax(s)` and the online form —
   a running max, a running denominator, a rescaled accumulator — are equal as
   functions and structurally unrelated as programs. No reordering of the naive
   graph produces the recurrence, because the running state is not in the graph
   to be reordered. Attention through the graph path therefore materialises the
   whole `T x T` score matrix, which is simply what the graph says to compute.

   **The fix is a rewrite catalogue, not a better scheduler.** If the compiler
   is *told* the identity — tiled softmax becomes the online recurrence, `var`
   becomes Welford, `log(sum(exp))` becomes the shifted form — then the auto
   path can apply it, and the wall moves. That is what "smart enough" means
   here: the limit is how many algebraic identities the compiler has been given,
   and each one is a finite, testable addition.

   The DSL is then for the kernel whose rewrite is not in the catalogue yet —
   and a kernel written by hand is also the specification for the rewrite rule
   that should eventually replace it.
3. **Fusion stops at runs of elementwise ops.** A reduction ends a band, so
   `rmsnorm` is two passes over the data where one would do. Fixable: the
   reduction and its consumer can share a residency.
4. **No reuse across bands.** Every vector band starts from DRAM; nothing keeps
   a value in L1 between them.

Items 1, 3 and 4 are ordinary compiler work with known answers and they account
for most of the gap; none of them is a property of auto-scheduling, they are
passes that have not been written. Item 2 is the only real wall, it applies to a
short list of kernels rather than to the general case, and hitting it is what
tells you to write that one by hand.

### The bridge that makes both worth having

**Kernel substitution**: a hand-written DSL kernel registered against a graph
pattern, so a model auto-scheduled everywhere else gets the hand-written flash
attention where it matters. That is not a nicety — it is what makes the two
paths one system, and it should be designed as soon as the DSL writes level 2.

---

## 8. Open

- **Where to tap the graph.** Before scheduling gives us the cleanest tensor
  view; after gives us fusion decisions we might otherwise repeat. Wants a look
  at what a `ScheduleItem` actually carries at the pinned version.
- **Whether to expose our DSL kernels to tinygrad** as custom ops, so a model
  can use a hand-written flash attention while the rest is auto-scheduled. That
  is the arrangement that makes both paths worth having, and it should be
  designed once the DSL writes level 2.
