# What ISA the general cores should run

A recommendation with its reasoning, not a decision. The short version:
**RV32IM plus a custom extension**, because the encoding is the cheap part of a
CPU and the toolchain is the expensive part.

---

## 1. The question

The general cores ([`cores.md`](cores.md)) are nearly full CPUs -- integer plus
some floating point -- for work that is important and small: top-k routing,
sampling, index arithmetic, control flow, descriptors for the memory mover.

So: adopt an existing soft core, or design an ISA the way this project has
designed four already?

## 2. The strongest case for designing our own

It is better here than it would be almost anywhere else, and it deserves stating
before it is argued against.

This project has **already designed four instruction sets** --
[`../isa/`](../isa/) covers the cluster, the orchestrator, the agent and the
vector core. The encode and decode infrastructure exists: `codegen/encode.py`
carries a `FIELDS` bit map and `place`/`flit`/`decode`, and `hw/disasm.py`
reads them back. The marginal cost of a fifth encoding is genuinely lower here
than at a shop doing it for the first time, and the taste for it is
demonstrated.

## 3. Why it still loses

**Every one of those four ISAs is for a fixed-function dataflow engine with no
control flow and no compiler.** The vector ISA's only branch is `VLOOP`'s
counter (`isa/vector.md` §3.1). The cluster ISA is `FILL / GEMM / DRAIN`. None
of them has a register allocator, a calling convention, a stack frame, or a
single line of C behind it.

What transfers is the encoding. What makes a CPU expensive is everything else.

| | design our own | RV32IM |
|---|---|---|
| encoding, decoder | done it four times | free |
| assembler | write it | free |
| **C compiler** | **write a backend, or never use C** | free |
| debugger | write it | GDB |
| verification | ours to build | riscv-arch-test, riscv-formal |
| area, 4 cores | perhaps 30% less | on the order of 1-2k LUT each |

The area row is the one that gets argued about and the one that does not
matter. The binding constraint on this machine is routing headroom -- it is why
the vector core count is 16 rather than higher -- and four small cores are noise
against sixteen vector cores. **Every figure in that row needs a synthesis run
rather than an estimate**, which is how this repo already treats Fmax and area
(tasks #35, #39, #41).

## 4. The argument that actually decides it: these cores run POLICY

Look at what lands on them. Top-k, top-p, expert-choice routing, temperature,
beam bookkeeping, KV-cache index arithmetic.

That is not fixed mathematics. It is **the part of a model architecture that
changes constantly** -- top-k became top-p became expert-choice inside a couple
of years.

The clusters and the vector cores implement arithmetic that has been stable for
decades. The general cores implement decisions that change per model. Putting
the stable thing in fixed silicon and the changing thing on a core with a real C
toolchain is the entire reason to have these cores. If changing a routing policy
means rewriting assembly, the core discourages the exact flexibility it exists
to provide.

## 5. The verification argument, in this project's own terms

The failure this repo keeps meeting is the **plausible wrong answer**: a matmul
reading another matmul's output returning 1.26e+00 relative error with no
exception; a `(64, 8)` region unpacked by the wrong shape inventing a fault that
was not there; `E == lanes == 4` making a real bug invisible until the expert
count moved.

A hand-built CPU is a large surface for exactly that class -- forwarding,
load-use hazards, corner cases in the multiplier -- and none of it is what this
project is trying to build. An existing core arrives with a compliance suite and
formal harnesses. That is worth more here than the LUTs it costs.

## 6. Recommendation: RV32IM plus a custom extension

Not a compromise. This is how RISC-V is meant to be used -- `custom-0` and
`custom-1` opcode space is reserved for it.

**Base.** RV32IM, machine mode only. No MMU, no supervisor mode, no OS. Strip
the *system*, not the *instruction set*. `RV32E` (16 registers) if the register
file turns out to matter.

**Extension, where specialisation actually pays:**

- **E8M15 scalar floating point**, by attaching the ALU that already exists.
  `src/kohakutpu/vector/vec_alu.v` is 3 DSP at 324.8 MHz with a correctly
  rounded FMA (task #45) -- already timing-closed above the 300 MHz target. The
  real argument is not area: it is **one float format across the whole
  machine**. Given how much of this design's trouble has come from format and
  layout mismatches producing plausible wrong answers, not introducing a second
  float format is worth more than IEEE conformance.
- **Descriptor emit and fence** for [`memory-mover.md`](memory-mover.md). One
  instruction beats a sequence of MMIO stores, and removing the host round trip
  is the whole point of the MAG-side cores.
- Possibly a compare-exchange or select primitive, **if** top-k profiles hot.
  Not before.

The cost of the E8M15 choice, stated honestly: it is not binary32, so it cannot
be `RV32F`, the compiler will not emit it on its own, and it is reached through
intrinsics. The middle path is **soft-float for general C, custom instructions
for the small hot kernels** -- a softmax over a gate row deserves intrinsics,
`printf` does not.

## 7. Candidate base cores

Shortlist only; none of this is measured on this device.

| core | shape | note |
|---|---|---|
| **VexRiscv** | configurable, small, high Fmax on UltraScale+ | best area/IPC balance; the workload has real loops in it |
| PicoRV32 | smallest, very high Fmax | multi-cycle, low IPC -- fine for control, weak for a few-hundred-element top-k |
| Ibex | well verified | typically the lowest Fmax of the three |
| MicroBlaze V | AMD's own, Vivado-native | vendor-tied |
| CVA6, Rocket | -- | far too large |

VexRiscv is the one to synthesise first, because top-k and sampling are loops
rather than pure control.

## 8. Where designing our own would have won

Stated so the decision can be revisited if the premises change:

- if the core were a **pure descriptor sequencer** with no general computation.
  Ruled out by `cores.md` §4 -- these are meant to compute.
- if an **unusual execution model** were needed -- VLIW, dataflow, scalar SIMD.
  This is scalar control code.
- if we were instantiating **hundreds** and area dominated. There are four.

None hold today.

## 9. The risk on this path

Scope creep. RV32IM is small; the temptation is to keep adding -- compressed
instructions, atomics, supervisor mode, an MMU, an RTOS -- until there is a
general-purpose SoC bolted to a TPU, competing for the routing budget that
capped the vector cores at 16.

The discipline: **machine mode, no OS, one binary per core, loaded with the
device image.** If someone proposes an interrupt controller, that is a signal
work is being scheduled onto the wrong engine.

## 10. Open

- Synthesis numbers for the shortlist on xcvu13p: LUT, FF, DSP, Fmax.
- Whether the mover interface is a custom instruction or MMIO plus a fence.
  MMIO is simpler and lets the NoC-side cores drive it too; a custom instruction
  is lower latency. Start MMIO, measure, then decide.
- How a core is programmed and by whom -- part of the device image, or loaded
  per kick.
