---
title: The software stack
summary: Three IR levels with every interesting decision at level 2 — tile choice discounted by padding, a 3D grid, fusion, and the round-cutting that a machine without hardware loops forces on its compiler.
tags:
  - kohakutpu
  - compiler
  - software
---

# The software stack

> **Kind: Yours throughout.** The three IR levels, the tile-choice discount, the
> grid and the round-cutting are this project's own compiler, and nothing in the
> framework requires any of them. The only Fixed protocol it touches is the
> instruction encoding it emits
> ([spec/instruction-encoding](../../spec/instruction-encoding.md)); every level
> above that is a design this project chose.

What turns a tensor program into flits, uploads them, kicks the machine and reads
the answer back. `compiler/` and `driver/`, pure Python with numpy for the
numeric model and nothing else in the core.

**There is one compilation path**: `kohakutpu.lang` down to `kohakutpu.isa`,
emitting cluster and vector programs alike. A second pipeline once existed and is
retired; nothing in the tree reaches it. **§7 is the current status.** §1–§3 are
the design reasoning behind the levels, which holds even where a module name in
them has since moved.

---

## 1. Six levels, and why there is an IR at all

Each level says what it **decides** and what it is **not allowed to know**:

| | Level | Decides | Vocabulary | Must not know |
|---|---|---|---|---|
| **L5** | Tensor | *what* to compute | arrays, ops, dtypes | tiles, units, addresses, layout |
| **L4** | Kernel (DSL) | *how one unit's work is shaped* | grid, sweep, region, accumulator | addresses, coordinates, encodings |
| **L3** | Graph IR | *what buffers exist, who reads them* | values, ops, regions, lifetimes | byte order, placement, timing |
| **L2** | Schedule IR | *where things live and run* | bands, placement, **layout, addresses** | bit positions, flit format |
| **L1** | Unit program | *the instruction stream per unit* | ordered typed instructions | bit positions, routing |
| **L0** | Bytes | *encoding and dispatch* | payload words, kicks, awaits | what any of it means |

Read the "must not know" column as the actual specification. The rule that makes
the stack real:

> **Only adjacent levels may appear in one piece of code.**

A file written at L5 may mention L4 ideas. It may not mention L2 ideas. When a
byte address, a node coordinate or a flit appears in an L5 file, that is not a
shortcut — it is a **missing layer**, and someone below failed to decide
something and pushed the decision upward.

Three boundaries carry all the weight, and every past usability complaint is one
of them being crossed:

- **Memory is L2-and-below.** Allocation, byte addresses, packing, uploads,
  readback. An L4 or L5 file that names an address has had L2's job handed to it.
- **Dispatch is L0.** Artifacts, kicks, awaits, credits, node coordinates,
  staging slots. An L4 file that builds an `Artifact` is doing driver work.
- **Layout is L2, chosen by L4's tiling.** The kernel's `gm`/`nk` *determine* the
  byte order an operand needs, but the kernel never packs anything. L2 asks the
  compiled kernel what layout each port wants and materialises it.

**The one sanctioned escape is device control.** Exactly as `torch.cuda` is the
only sub-L5 thing a PyTorch program touches, an application may call
`dev.sync()`, `dev.empty_cache()` and `dev.stats()`. Those are *control*, not
*data* — they name no address, no layout and no unit. Anything else that seems to
need crossing a level is a missing API.

**The reason for an IR is a specific defect.** Work was once split on N and only
N, so a `256 x 1024 x 256` problem at eight clusters got a 32-column band, which
drove the tile search to a shape with one m-tile and 32 K-chunks — one cluster
live at a time. That was **not findable by reading the code**, because the
decision was distributed across three files. A scheduling decision you cannot
print is a scheduling decision you cannot review; the schedule level exists to
make it a value you can hold.

### 1.1 Why not MLIR, Triton or tinygrad's IR as the middle

Recorded because it is the decision most likely to be revisited, and it should be
revisited on evidence.

**Nothing upstream can model this machine.** Not as a complaint — as a fact about
how specific it is. The dtypes (`int7 + E5M3`, `E8M15`, the accumulator float)
exist in no external type system. The matmul cluster is a **macro-op**, not a loop
nest. Tile choice is arithmetic intensity discounted by padding against L1 entry
counts and a sub-tile budget. Rounds are a host round trip bounded by a staging
window.

In MLIR, Triton and tinygrad alike, **the matmul cluster is an escape hatch** — a
custom op that pattern matching targets. So adopting any of them buys the vector
path and the fusion layer, and never the matmul path. Worth knowing before paying
for it. What is kept open is that the IR is serialisable and its level-1 op set
stays close enough to `linalg`/`vector` that a bridge would be a translation
rather than a redesign.

### 1.2 The type system is the part written first

| dtype | bits | where it lives |
|---|---|---|
| `FP32` | 32 | host, DRAM |
| `FP16` | 16 | host, DRAM, mesh — **the only output format today** |
| `E8M15` | 24 | vector core |
| accumulator float | `ACC_MW + 8` | matmul accumulator, **internal — never a memory word** |
| `MXFP7` | 7 + scale | matmul operands, mesh |
| `INT8/16/32` | | indices and masks, not arithmetic |

Three facts the type system has to *encode* rather than comment on, because
passes reason about them and getting them wrong is silent:

- **`FP16 → E8M15` is exact and `E8M15 → FP32` is exact.** E8 contains E5 and E8
  *is* FP32's exponent, so neither can overflow — which is why the vector core
  needs no saturation logic.
- **Accumulator float → E8M15 is range-lossless**, costing one rounding of the
  bottom mantissa bit. That is the split-K epilogue's whole basis.
- **Anything → FP16 can saturate at 65,504**, and today it does so silently. A
  pass that inserts an FP16 store is inserting a potential range failure and
  should be able to say so.

So a dtype carries `(exp_bits, man_bits, bias, has_subnormals)` and can answer
`contains_range_of` and `is_exact_from` — a conversion pass proves its own safety
rather than a comment claiming it.

**There is no promotion rule, because there is no promotion question.** The format
an op computes in is the *engine's*, not a function of its operands: a matmul
computes in MXFP7 whatever arrived, and a vector op computes in E8M15 whether it
was handed FP32 or FP16. Mixing is normal and rejecting it would contradict the
datapath. Level 1 therefore asks only "what must this value be able to hold?" —
a range question first and a precision question second — and level 2, which knows
the engine, knows what is actually computed in.

**And there is no cast instruction**, because every conversion is already
somewhere: FP32→FP16 on host upload, FP16→MXFP7 in a mover pass through the
transform slot, and E8M15→FP16/FP32 as a *field on the vector store*. The middle
one used to be on the fetch and is not — which changes when it is paid, once per
tensor rather than once per read, and makes it a pass the compiler schedules
rather than a flag it sets. A standalone cast
survives at graph level because a user may want one and the folding pass needs
something to fold; if it reaches level 3 unfolded it is a real pass over the data
and the cost model says so.

### 1.3 No dynamic shapes in the IR, which is not the same as no dynamic shapes

**Every shape in the IR is a concrete integer.** A pass that has to reason about
`?` cannot pick a tile, cannot compute a grid, cannot check that the grid covers
the output exactly once, and cannot cost itself — so symbolic extents would cost
the IR every property its verification depends on.

Dynamic shape belongs *above* the IR and is fully expected there: the frontend
sees the real shape at call time and specialises, by caching per shape, by
bucketing to a fixed set and masking the tail, or by padding to a tile multiple —
which this machine requires anyway, since a zero contributes nothing to a dot
product. The IR is a specialised program; specialisation is somebody else's job.

---

## 2. Level 2: every interesting decision

A schedule is a **grid of work items over an engine**, plus where the data sits.
A `Band` carries its engine, its grid, the tile one instance computes, its ops in
order, and what stays resident in L1 across the grid.

### 2.1 Tile choice, ranked by intensity discounted by padding

The machine holds one tile of the problem at a time, and the constraints are the
hardware's own silent-wrap limits ([isa.md](isa.md) §4.6):

```
   gm * gn     <= TILES                   resident output sub-tiles
   gm * nk     <= bank_a                  A's L1 entries, in ONE bank
   gn * nk     <= bank_b                  B's likewise
   bank_x      =  min(l1_x // banks, L1_OFF_SPAN)      L1_OFF_SPAN = 256
```

Dividing by the bank count is what keeps the array working through a fill — the
sweep for chunk *i* is still reading while chunk *i+1* lands. `L1_OFF_SPAN` is a
**separate ceiling an 8-bit offset field imposes whatever L1 grows to**, so it is
a named constant rather than folded into the capacity. It had to become one: for
a session only the A term divided, B was sized against the whole of L1, and every
plan past a certain chunk size wrapped an offset onto entry 0 and multiplied
another K block's operand ([results.md](results.md) §9.2).

Candidates are then ranked:

```python
score = 2 * gm * gn / (gm + gn)          # MACs per operand byte
score *= (m*n*k) / (padded_m * padded_n * padded_k)
key = (round(score * 4096), nk, -abs(gm - gn))
```

**Intensity first**, because it is the quantity every other cost divides into: it
decides how many bytes the fetch path must move per unit of compute, and no
amount of scheduling changes it. Then K per fill, since operands are re-read every
pass and only the output stays put. Then squareness, to break ties toward the
smaller padding bill.

> **Residency is the constraint, not the objective.** Maximising `gm*gn` — which
> is what this used to do — is a different thing: 32x1 and 8x4 both hold 32
> sub-tiles, and their intensities are 1.94 and 5.33.

Two guards, and both exist because the largest tile is not the best tile:

- **Powers of two only.** Ranked on intensity alone the best shape at 512
  sub-tiles is 22x23 — 22.5 MACs/byte against 16x32's 21.3 — whose output block is
  88x92, so every dimension of every problem pads up to a multiple of 88 or 92 and
  a 1024-cube pays 11% before any efficiency is counted. The intensity difference
  is 5%; the padding difference is not.
- **The padding discount itself.** At a 64x128 block a 300x300 GEMM pads to
  320x384 and does 36.5% more arithmetic than the problem contains. On shapes that
  already fit, the discount is 1 and the ranking reduces to plain intensity, which
  is why the answers for well-shaped problems do not move.

### 2.2 The grid is 3D

```
   grid  = (ceil(M/BM), ceil(N/BN), SK)
   instance (mo, no, ko)  computes  C[mo, no] += A[mo, ko] @ B[ko, no]
```

`SK == 1` means each instance owns its output tile outright and writes it.
`SK > 1` means instances sharing `(mo, no)` produce **partials**, which must be
reduced — and a pass that picks `SK > 1` without emitting the reduction band is
invalid rather than merely slow.

**"Every output element is produced exactly once" is the check that matters.** A
gap leaves the output's previous contents in place and an overlap races; overshoot
below one tile is padding and is fine. It is the invariant the N-only split
violated in spirit — it did produce every element once, on one cluster at a
time — and it is checkable without hardware, which makes it the cheapest real test
in the system.

### 2.3 Fusion, which is worth more than any other pass

Maximal runs of elementwise ops become one vector band. The reason is the
bandwidth arithmetic in [vector-core.md](vector-core.md) §5: a pass doing one op
per element is memory-bound and one doing two is compute-bound, and the crossover
sits exactly where the depth-2 chain mode does. **So the codegen's single most
important job is not instruction selection, it is fusion.**

Two rules cut a run, and both are about shape rather than arithmetic: a band is
shape-uniform, because instances of different trip counts cannot share a pass;
and a reduction ends its band, since its output shape is not its input's — its
shape key is its *input* size.

**Epilogue folding** makes an elementwise run that reads only the preceding
matmul into a band that consumes it: same tiles, same order, same FP16. Measured,
it moves **nothing** — same device image, same DRAM traffic, same instruction
count — because both forms stage the matmul output as FP16, which is the only
thing a cluster can write. What it actually buys is the grid: the epilogue's
instances derive from the producer's tiles, so the elementwise pass walks the
matmul's output in the matmul's own order and no re-tiling pass is needed. Worth
having, free, and **a scheduling win rather than a numerical one**.

> That section used to claim the opposite — that folding staged accumulator-width
> values and bought "one rounding instead of two". The driver really did allocate
> a 1.5x region and emit an accumulator dtype on the drain, and none of it
> corresponded to hardware. **Accumulator width is the resident tile's format,
> never a memory word.**
>
> And folding does not buy range either. A matmul output above 65,504 is clamped
> on emit, folded or not; one example kernel's true output of 204,800 comes back
> as 65.5 with every element clamped, and nothing in the scheduler mitigates it.

### 2.4 Memory space and residency are explicit

Every level-2 value names `DRAM`, `L1` or `REG`. The residency decisions that
matter are the ones the hardware measured: **B resident across the m loop** (a
quarter of all memory traffic at the 256-cube) and **A double-buffered** (22.3% of
the machine's time). They are attributes on a band rather than `if` statements in
a code generator.

There is a corresponding pass most people would not predict. An operand is packed
into L1-entry order **for a tile**, and a region holds one layout, so two matmuls
sharing an A operand cannot block it differently — a mixture-of-experts input
feeds the gate and every expert. The pass takes the smallest blocking in each
group, which is always safe, because a smaller tile means more grid instances and
still covers the output. Without it, whichever matmul packed last wins and the
other reads the right bytes in the wrong places; the program object now refuses a
conflicting overwrite, so a mistake here is an exception rather than a wrong
answer.

### 2.5 Layout is part of the kernel, not a property of the tensor

A pass needs the L1 entries for one `(output tile, K chunk)`. In the natural
group-major order those are `gt` separate runs, so a pass would need `gt` fill
instructions instead of one, or the hardware would need a strided fetch.

Reordering memory removes the problem instead of paying for it:

```
   (group, lane, block, k)  ->  (tile, chunk, group, block, lane, k)
```

which is exactly the order a pass consumes, so a pass's entries become one
contiguous run and a `FILL` is a single instruction. Each entry still appears
exactly once; only the order changes.

The layout contract is `[lanes][K]` row-major FP16 with `lanes % 4 == 0` and
`K % 32 == 0`, where a lane is a row of A or a **column** of B — B is stored
transposed. Two consequences worth knowing: `A @ B.T` is what a standard linear
layer already computes and its weight is `[N][K]`, so **weights upload verbatim**;
and `C[M][N]` row-major is exactly the shape the next layer wants as its A
operand.

The packing is done as an array transpose rather than a Python loop, because a
512x512 operand is 16k entries and the loop version takes minutes rather than
milliseconds. **The operand image and the instruction stream are one decision and
must be one object** — kept in step by hand, a mismatch overlaps two operands and
the answer is quietly wrong.

---

## 3. Rounds: what a machine without hardware loops costs its compiler

A large GEMM has more passes than the machine can hold at once, so passes are cut
into **rounds** — each a self-contained upload, load and `GO`. The card never
needs the whole program, and nothing about the result depends on where the cuts
fall.

A round is bounded by **three** limits, and checking only one is how a program
silently overruns the resource it was supposed to fit:

| limit | bound by |
|---|---|
| staging window | the agent's staging flit capacity |
| command RAM | the orchestrator's command count |
| passes per round | dispatch credit |

**The credit bound is the least obvious.** Each program permanently consumes one
credit, because its last instruction retires as a *batch* completion and only
instruction completions refill ([isa.md](isa.md) §8). So a round of `P` programs
seeded once with `C` credits needs `C > P`, and `C` is itself capped by the
compute unit's instruction FIFO depth. Being cut here costs an extra round; being
wrong here stops the machine with nothing executed and no error.

Credit is seeded **once per round, not once per kick**. Re-seeding per kick makes
the `C > P` arithmetic hold trivially and is wrong, because credit is also the
bound that keeps instructions in flight below the target's FIFO depth — `P` kicks
would admit `P·C` instructions against a FIFO of 32.

The command cost of a pass is **not a constant**, because a kick only writes the
dispatch registers that changed since the last one. So the cut asks the program
builder rather than assuming a figure, and the same builder is used both to
measure a candidate round and to build the real one, so the two can never
disagree about what a round costs.

**Clusters are interleaved before cutting.** Rounds are cut from the pass list in
order, so emitting one cluster's passes and then the next would fill whole rounds
with a single cluster's work and leave the others idle — N clusters taking N times
as long as one, which is exactly what kick-all-then-wait-once exists to avoid. The
list is round-robined across clusters first, with the ragged tail appended.

### 3.1 The shadow, and the two registers it must not cover

The program object remembers what it has written to each register and skips a
write that would restate the current value. **This is where the command RAM is
actually won**: dispatching a pass writes four registers, but across a round only
the destination and base really move, so dropping the repeats roughly triples the
passes a round can carry. On a two-cluster GEMM it is 55 commands down to 15, and
the gap widens with every cluster.

It is valid **only for registers the hardware reads and never modifies.** Two
kinds do not qualify and both fail silently: one where the write itself is the
event (the kick — shadowing it drops every dispatch after the first), and one the
hardware **consumes** (the credit — the shadow stops matching the real value the
moment the machine runs).

The test is not "does the driver want the same value again". It is **"does the
register still hold what the driver last wrote"**.

The shadow starts empty, so a program never assumes a value it did not set itself.
That is what makes rounds independent: no round depends on register state another
round left behind, so a round can be re-run or reordered without changing the
result.

### 3.2 Setup and program are separate

Instruction flits go straight into the staging RAM as host writes; only control
goes into the command RAM. A program is **not a recording of every AXI write** —
putting flits in the command RAM makes the host ship each one twice and makes the
program grow with the *problem* rather than with its *control flow*.

---

## 4. Level 3, and the two encoders

Level 3 is bytes and no decisions: one program per engine kind, plus the operand
image. It is also where the disassembler lives, because the only trustworthy check
that codegen is right is reading back what was emitted.

**There were two encoders and only one of them had ever run on the card.** That
gap is closed: `kohakutpu.isa` emits both kinds, and the hand-packed encoder that
did run is kept as the witness it is checked against.

| | `kohakutpu.isa` | `kohakutpu.hw` |
|---|---|---|
| cluster ops | yes, and runs | yes — the witness |
| vector ops | **yes, and runs** | yes — the witness |
| input | bands from `kohakutpu.lang` | hand-built assembler programs |
| field-width checks | yes | yes |

What made the vector side hard is worth keeping, because it explains the shape of
`vecemit` rather than a missing table entry: a vector instruction is not a flit
(§7.1 of [isa.md](isa.md)), so one instruction expands into a variable number of
envelope flits plus instruction-memory allocation plus an entry; and the envelope
opcode space is shared between node types, so the encoder cannot be node-agnostic
and a mistake is silently executed rather than rejected.

**Nothing here is silently wrong on hardware**, because the emitter refuses what
it cannot handle and names the knob when it does.

> Two paths drift independently, and that is why there is now one. The bank fields
> ([isa.md](isa.md) §4.6) were absent from the old compiler path entirely, and its B
> offset did not merely truncate at 256 — it addressed past two banks. Adding the
> fields and a range check immediately failed 25 tests: the path had been relying
> on 8-bit wrap for shapes that do not fit L1 at all. **A capacity bug wearing a
> truncation bug's clothes, completely silent.**

---

## 5. Frontends

Three, and the important thing is that **they enter at different levels**.

```
   tinygrad / any graph frontend ---> LEVEL 1 --auto-schedule--+
                                                              |
   own DSL (Triton-shaped)       ----------------------------->+--> LEVEL 2 --> 3
                                      writes the schedule itself
```

**Auto-scheduling exists for the graph path and only for it.** A scheduler tiles,
fuses and reorders; it does not invent algorithms. Flash attention's online
softmax carries a running max and a running denominator and rescales the
accumulator between blocks — state the naive graph never contains. No amount of
fusion produces it, because it is an *algorithmic* rewrite. So a DSL that emits
level 1 is useless: everything it writes goes through the scheduler that cannot
get there. **The DSL must write level 2 directly.**

### 5.1 tinygrad: a device, with the GPU-shaped passes declared away

The decision is to implement a tinygrad `Device` — allocator, renderer, compiler,
program — so tinygrad's whole pipeline runs unmodified and a user runs tinygrad
the way they already do. No fork, no patched scheduler.

This was rejected once on the grounds that "the grid and tile would be
tinygrad's", and that was wrong: **the optimizer does not assume, it asks.** Every
GPU-shaped transform is gated on a capability the renderer declares, so the
optimizations that would hurt are not something to work around — they are
something to switch off truthfully.

| declared | ours | what it disables |
|---|---|---|
| `has_local` | `False` | the whole threadgroup family |
| `has_shared` | `False` | shared-memory staging and its reductions |
| `supports_float4` | `False` | vec4 packing; the natural width is a 128-element vector, not four |
| `global_max`/`local_max` | `None` | launch-geometry clamping — this machine has bands over cores, not a grid launch |

**None of this is a lie told to make the optimizer behave.** The machine genuinely
has no threadgroups and no shared memory — L1 is an explicitly filled per-core
scratchpad. Saying so is describing the device.

What is turned *on* is the cluster as a declared tensor core, which is the
sanctioned way to say "this shape has a machine instruction" and is how the
cluster becomes reachable without touching the scheduler. Get it wrong in the safe
direction: an unmatched contraction falls through to multiply-plus-sum on the
vector core, which is correct and slow, whereas **a false match is wrong and
fast** — so the declaration must describe the cluster's real shape and nothing
wider.

The mapping is closer than expected, because both projects made some of the same
choices — notably **base-2 `exp2`/`log2` primitives**, chosen here because range
reduction becomes a bit slice ([vector-core.md](vector-core.md) §4.1) and there
for the reason every backend does. Dtypes are clean at the seam: tinygrad buffers
are FP16/FP32 and the internal formats never cross it.

The part that is genuinely work is neither of those. tinygrad linearises to a
scalar loop nest with explicit index arithmetic, and the vector core wants a
hardware loop over vector-length chunks plus an address descriptor. Turning one
into the other is a pattern match on the loop nest, and the fallback if the match
is incomplete is not a wrong answer — an unmatched nest becomes a host op, which
is visible in the schedule.

### 5.2 What the auto path must achieve

**Within 2x of a hand-written kernel on anything fusable, with the gap always
explained by a named missing transform.** "Auto-scheduled, so slow" is not an
acceptable answer — it is a statement that the scheduler has not been written yet.

The gap today, in order of cost:

1. **Nothing folds the epilogue onto the matmul.** A GEMM drains to DRAM and a
   vector band reads it straight back — a full round trip per output element for
   work the accumulator could have finished in place. This is the biggest item and
   nothing implements it.
2. **The compiler does not know an operation has another mathematical form.** This
   is the only real wall, and it is narrower than "auto-scheduling is slow". Every
   *individual* operation should be optimal, and so should every tiling, fusion and
   ordering — those are scheduling questions. What a scheduler cannot do is change
   the maths. The fix is **a rewrite catalogue, not a better scheduler**: tiled
   softmax to the online recurrence, variance to Welford, log-sum-exp to the
   shifted form. Each entry is finite and testable, and each one moves the wall.
3. **Fusion stops at runs of elementwise ops**, so a reduction ends a band and a
   normalisation is two passes where one would do.
4. **No reuse across bands** — every vector band starts from DRAM.

Items 1, 3 and 4 are ordinary compiler work with known answers and account for
most of the gap; none of them is a property of auto-scheduling. Item 2 applies to
a short list of kernels, and hitting it is what tells you to write that one by
hand — where **a hand-written kernel is also the specification for the rewrite
rule that should eventually replace it.**

The bridge that makes both paths worth having is **kernel substitution**: a
hand-written kernel registered against a graph pattern, so a model auto-scheduled
everywhere else gets the hand-written attention where it matters.

---

## 6. Checking a schedule without hardware

`kohakutpu.sim` executes a kernel directly in Python. It answers two questions
the RTL simulator answers expensively and one it cannot answer at all:

| question | with the simulator | with the interpreter |
|---|---|---|
| does the program compute the right numbers? | a full RTL run | seconds |
| where do the cycles go? | a full RTL run | the cost model, per band |
| is this schedule better than that one? | **cannot ask** for an unbuilt unit | compare two schedules |

Two levels, and keeping them separate is the point. **Functional** execution
applies each engine's real arithmetic — MXFP7 quantisation, accumulator-width
accumulation, E8M15 in the vector core — so precision is modelled rather than
idealised, which is what makes a kernel checkable before RTL exists. **Timed**
execution moves flits through a mesh model, charging a cost model per band; it is
not cycle-accurate and does not pretend to be, and it answers "which band is the
limiter".

**The RTL simulator stays the authority. The interpreter is what makes it rare**:
a functional disagreement is a compiler bug, and only a *timing* disagreement
needs the RTL.

Two rules go with that, and they are the difference between a model and a claim:
a modelled rate is **never reported as measured**, and the axis says so; and the
model is checked against the bit-exact software reference the hardware bench
already uses, so the two cannot drift.

**No autotuning.** Tile choice is an analytic decision against a stated cost
model, and when measurement disagrees with the model, the model is what gets
fixed — a search that finds a good tile teaches nothing about why. A measured
search is worth wiring later as a *second opinion* on the cost model, not as a
replacement for it.

---

## 7. What runs today

| | |
|---|---|
| the kernel language | `kohakutpu.lang` — one path, tracing a Python function into bands |
| cluster ops | encoded by `kohakutpu.isa`, checked byte-for-byte against the hand-packed encoder that has run on the card |
| vector ops | **emitted**, by `kohakutpu.isa.vecemit` — every fused kernel is a vector program |
| multi-pass and fused epilogues | supported; `Kernel.relax` stages a fusion the compiler cannot run, so the author never picks |
| the simulator | `kohakutpu.sim`, three levels from "just compute it" to a mesh model; still not the authority |
| the DSL | traces by letting value objects flow through an ordinary Python function; runtime-dependent control flow is rejected rather than unrolled |
| tinygrad | `ktpugrad`, on tinygrad 0.13 — matmul, epilogues and elementwise chains lower and run |
