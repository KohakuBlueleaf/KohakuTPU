# Using a general core: the kernel side and the codegen side

[`isa.md`](isa.md) recommends RV32IM and stops there. This is the missing half:
**how a kernel reaches one, and what the compiler has to emit.**

The whole difficulty is in one sentence. Every other engine in this machine is
**straight-line and statically scheduled** -- the cluster ISA is `FILL / GEMM /
DRAIN`, and the vector core's only branch is `VLOOP`'s counter (`isa.md` §3).
A general core is the first thing here with real control flow, so its duration
and its results are unknown when the schedule is built.

Everything below follows from deciding how much of that dynamism is allowed to
escape into the schedule.

---

## 1. Three tiers of coupling. Build them in order.

| tier | what is dynamic | what it costs | status |
|---|---|---|---|
| **1** | the **data** | nothing -- the machine model already supports it | build this |
| **2** | the **extent** of a dispatch | a scalar-parameterised dispatch count | later, high value |
| **3** | the **control flow** of the program | a branch in the control program | probably never |

### Tier 1 -- dynamic data, static schedule

The general core is a **consumer on the NoC, dispatched exactly like a GEMM**.
It reads a region, writes a region, signals completion. The shapes around it are
fixed, so level 2 and level 3 are unchanged. This is the same move the vector
core just made: a seventh consumer, not a seventh layer.

This tier already covers the work `cores.md` §4 lists: top-k over a gate,
`argsort`, sampling, index arithmetic, an `arange`, a counter-based PRNG.

**The rule that makes it work, and it is not negotiable: the output shape must
be a compile-time constant.**

```
  top_k(probs, k=2)               -> (T, 2)     static      OK
  argsort(scores)                 -> (T,)       static      OK
  "the tokens routed to expert e" -> (n_e, C)   data        NOT OK
```

The third is why capacity factor exists. The core writes a **fixed** `(E, C)`
index buffer plus a validity mask, and the grouped GEMM runs a fixed shape with
some rows masked. That is the standard answer and it is the one that fits a
statically scheduled machine.

### Tier 2 -- dynamic extent

The step after. The core returns a **scalar** in its completion signal, and a
dispatch takes its count from that scalar instead of from an immediate.

This is deliberately **not** a branch. The control program stays straight-line;
only the *extent* of a dispatch becomes dynamic. That is a far smaller change
than control flow, and it buys the one thing capacity padding cannot: **skipping
an expert that received no tokens**, and stopping a beam search that converged.

### Tier 3 -- the core drives dispatch

Full inversion: the general core runs the control program and dispatches to
clusters. Stated only so the boundary is explicit. It collides with the existing
orchestrator and agent ISAs, and nothing yet needs it.

---

## 2. Sub-kernels: a kernel carries its own general-core program

A kernel is compiled for several engines at once. The parts that land on
clusters and vector cores become dispatches; the part that lands on a general
core becomes a **sub-kernel** -- a program generated with the kernel, loaded
into the allocated core, and run there.

**The compiler still never emits RISC-V.** It emits **C**, and stock
`riscv32-unknown-elf-gcc` emits the machine code. That is what keeps
[`isa.md`](isa.md)'s reasoning intact: RV32IM was chosen for a free toolchain,
and writing a backend would throw away the thing that was bought. Generating
the *input* to that toolchain costs nothing of the sort.

```
  level 1    idx = route(gate, k=2)      a @gcore body; static output shape
  level 2    a band on Engine.GEN, placed on a core
  build      restricted Python -> C -> gcc -> blob, cached by content hash
  stage      blob written to the core's instruction memory     ONCE
  level 3    GRUN  arg block: in/out descriptors, scalars, cycle budget
```

### 2.1 Why generated, and not a library of entry points

The obvious alternative is a fixed set of precompiled routines selected by ID.
It is worse in this project's own terms: **an entry-ID table is a contract
between the device image and the compiler that nothing checks**, which is the
same shape as every plausible-wrong-answer failure this repo has met. A
generated sub-kernel has one source.

Generation also specialises -- a known `k`, a known row count, a folded stride
-- where a library routine must stay general.

The library does not disappear; it degenerates into **sub-kernels that happen
to ship precompiled**, hit by the same content-hash cache. One mechanism, not
two.

### 2.2 Residency is what makes it affordable

Staging a program over the NoC costs one flit per instruction. Paying that per
invocation would be absurd; paying it once is nothing.

**Load once per kernel instantiation, `GRUN` per invocation.** This is exactly
how `vec_cu` already works -- IMEM writes stage a program, `RUN` executes it,
and `vec_cu_tb` runs two different kernels from one staging pass. The case that
matters is autoregressive decode, where `cores.md` §1 is paying a host round
trip *every step*; per step this sends one dispatch, not a program.

The cost this introduces: **which sub-kernel is resident on which core becomes
a scheduling decision.** Four cores and finite instruction memory means a
working set, and level 2 models neither residency nor locality today. It is the
same shape as L1 double-buffering, and it is the part most likely to be got
wrong first.

### 2.3 The source form, and why the simulator comes free

A sub-kernel is written as **restricted Python inside the DSL** -- bounded
loops, static shapes, no pointers, no recursion, no allocation, no library
calls except intrinsics. The restriction set is simply what the emitter
accepts, and it is also exactly what is safe on a core with no OS.

Then the golden model costs nothing: **`interp/mesh.py` calls the Python
function.** The C is derived from the same AST, so there is no second
implementation to keep in step -- the failure mode of a hand-written routine
paired with a hand-written reference. A test runs both and compares.

When a sub-kernel profiles hot it reaches the custom E8M15 instructions through
intrinsics, which `isa.md` §6 already draws the line for: intrinsics for the
small hot kernels, soft-float for everything else.

### 2.4 What generation costs, stated plainly

- **A RISC-V toolchain becomes a compile-time dependency.** Content-hash the
  generated C and cache the blob; ship the common sub-kernels precompiled so
  the toolchain is only needed when a kernel introduces a new one.
- **A runaway loop hangs the machine** rather than returning a wrong answer.
  The dispatch carries a **cycle budget**; exceeding it traps and reports a
  fault through the path the CU framework already has.
- **Generated code is not reviewed code.** The restriction set is the only
  thing standing between a kernel and a core that writes outside its regions,
  so the emitter must be the place that enforces §2.6, not a convention.

### 2.5 The ABI

Fixed, because the blob and the dispatch are produced by different passes and
nothing at run time checks that they agree.

- Arguments arrive in an **argument block**, not a calling convention: an entry
  offset, up to N scalar args, a cycle budget, and input/output **descriptors**
  in the same `base + 4 x (stride, bound)` form the mover and the vector AGU
  already use.
- A sub-kernel may touch **only** its declared regions. The compiler owns
  allocation; a core that writes outside them corrupts a tensor silently, and
  the emitter is what has to prevent it.
- Completion carries a scalar. Tier 1 ignores it; Tier 2 is exactly this field.

### 2.6 The correctness rules

These are where the plausible-wrong-answer failures live, so they are rules
rather than guidance.

**Linear layout only.** A general core reads memory with a CPU's addressing, not
a cluster's. It must never see tile order or entry order. Any tensor crossing a
`GEN` band gets a **relayout band** -- the mechanism `lower()` already inserts
for matmul -> matmul. This project has been bitten repeatedly by right bytes in
the wrong order; a scalar core reading a drained tile is the same failure with a
new engine attached.

**FP16 / FP32 in memory, and nothing else.** Same discipline as everywhere. If
the custom E8M15 ALU is attached, E8M15 stays internal to the core exactly as it
stays internal to the vector lane. `MEMORY_DTYPES` already forbids the
alternative and a test enforces it.

**No data cache, or write-through with a fence before the completion signal.** A
consumer reading a stale index buffer is a wrong answer with no exception, which
is the exact class this repo keeps meeting. `isa.md` §10 leaves MMIO-plus-fence
open; whichever way that goes, the fence must be **before** the signal, not
after.

**The Python reference is the golden model.** `interp/mesh.py` executes each
routine as Python, and a test runs the C against it. That is how the two halves
stay in sync, and it is the practice already used for `vec_tables.py`, which is
both the generator and the bit-exact model.

**Timing is measured, not derived.** The compiler cannot know how long a
data-dependent routine runs. Carry a measured per-routine constant and report it
as a **bracket**, the way the timing model already reports throughput and cycles.

---

## 3. What this costs in the repo

Concretely, against what exists today:

| | |
|---|---|
| `ir/sched.py` | an `Engine.GEN` |
| level 1 | a node kind carrying a `@gcore` body and a static output shape |
| `passes/lower.py` | insert a relayout band on every edge into or out of a `GEN` band |
| `codegen/cu.py` | allocate its regions **linear** |
| **a new emitter** | restricted-Python AST -> C, plus a content-hash blob cache |
| level 3 | `GSTAGE` (blob -> instruction memory) and `GRUN` (arg block) |
| level 2 | which sub-kernel is resident on which core |
| `interp/mesh.py` | execute it by calling the Python body directly |
| timing | a measured constant per sub-kernel, reported as a bracket |

Only two of those are genuinely new: **the emitter** and **residency in level
2**. The rest is the same shape as adding the vector core, and `vec_cu` already
demonstrates the staging-then-run transport end to end.

---

## 4. The interleaving problem, which is the real risk

The concern that prompted this document: **a kernel may interleave vector and
general work, and it cannot all be expressed as one C program.**

It cannot, and it should not try. But then every hop is a dispatch and a
completion signal, and `cores.md` §4 budgets this work at "thousands of
operations per token". If a routine is 200 instructions and the round trip is
comparable, the overhead is the workload.

Three answers, in order of preference:

1. **Batch by layer, not by token.** Route a whole layer's gate matrix in one
   `GRUN`. This removes the problem for MoE, sampling and index generation --
   which is most of the list. Note that staging is already off this path: the
   sub-kernel is resident, so a step costs a dispatch, not a program.
2. **Overlap.** A `GEN` band that no in-flight band depends on can run beside
   the clusters. Nothing in level 2 models this today, which is the same gap
   `README.md` §3 names for locality.
3. **A persistent loop with lightweight synchronisation**, only if 1 and 2
   measure badly. The core runs a program mirroring the kernel's structure and
   blocks on flags instead of being dispatched per task. This is strictly more
   powerful and strictly more dangerous: it is no longer a passive consumer, and
   a missed wake-up is a hang rather than a wrong answer.

**This ordering is a guess and should be replaced by a measurement.** The number
that decides it is the dispatch-to-completion latency for a trivial routine, and
nothing in the repo can produce it yet.

---

## 5. Bare metal, briefly

Asked directly, so answered directly. There is no RTOS and no libc.

- a `crt0.S` of roughly thirty lines: set `sp`, zero `.bss`, call `main`, trap
- a linker script naming instruction memory and data memory
- `-march=rv32im -mabi=ilp32 -nostdlib -ffreestanding -O2`

Two things live on a core, and the split matters. A **resident stub** ships in
the device image: reset vector, the wait-for-`GRUN` loop, the fault path and the
intrinsics. **Sub-kernels are staged into instruction memory over the NoC** and
change per kernel. The stub is reviewed once; the sub-kernels are generated.

`isa.md` §9 names the risk and it applies here: the temptation is to grow this
into an SoC. **Machine mode, no interrupts, one binary per core.** If something
wants an interrupt controller, work is being scheduled onto the wrong engine.

---

## 6. Open

- Dispatch-to-completion latency for a trivial routine. Decides §4 entirely.
- **Instruction memory size, and therefore the working set.** This is the number
  that says whether residency is a non-issue or a scheduling problem. Nothing
  else in §2.2 can be settled without it.
- The restricted-Python subset the emitter accepts. It is the enforcement point
  for §2.6, so it has to be defined before anything is generated, not after.
- Whether Tier 2's scalar-parameterised dispatch belongs in the orchestrator ISA
  or the agent's.
- Whether the NoC-side cores need a memory port of their own (`cores.md` §6), or
  whether reaching a tensor across the mesh is acceptable at this volume.
- Which sub-kernels ship precompiled. Top-k, `argsort`, Philox and `arange` are
  the obvious four; nothing else is justified yet.
