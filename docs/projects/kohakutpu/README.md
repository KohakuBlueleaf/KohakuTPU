---
title: KohakuTPU
summary: An MXFP7 tensor accelerator on xcvu13p-fhgb2104-2L-e — the framework's reference instance, and the worked example every framework interface was shaped against.
tags:
  - kohakutpu
  - overview
---

# KohakuTPU

A tensor accelerator built on KohakuAccel. Two compute units — a matmul cluster
and a vector core — a number format designed around a DSP48E2, an instruction set
spent on that datapath, and a compiler that plans against the machine's own
capacities.

**Device: `xcvu13p-fhgb2104-2L-e`, Vivado 2024.2.** Every measurement in these
pages was taken on that part with that tool, and they describe *this*
accelerator rather than the framework. Almost all of them are **out-of-context
synthesis**, which makes every frequency an upper bound and no frequency a
closed-timing result; [results.md](results.md) §1 states the provenance rule
these pages are written to, and every figure is expected to name the run behind
it.

KohakuTPU is the framework's worked example. It is the reason the framework's
interfaces are the shape they are, it is where a second project looks to see how
the first one solved something, and it is the evidence that a compute unit
written against the port contract can be built, measured and put on a device.

---

## 0. The words these pages use

The reader is assumed to know digital logic, computer architecture and FPGAs in
general, and to know nothing at all about this machine. Every term below is a
KohakuAccel or KohakuTPU term, not an industry one, and it means only what is
written here.

| term | what it is |
|---|---|
| **compute unit** | the block a project writes. It attaches to the network at one port and is otherwise entirely the project's. KohakuTPU has two kinds: a **matmul cluster** and a **vector core**. |
| **mesh** | the on-chip network the compute units hang off — a grid of routers, plus one system node. |
| **node** | a coordinate on the mesh. A router occupies one; a compute unit attaches at one. |
| **system node** | the one component that owns a mesh's memory: the **memory agent** (MAG), a control processor, the **mover**, and the hub every port passes through. |
| **flit** | the unit the mesh moves: a header plus a 256-bit payload. An instruction is a flit; an operand word is a flit. |
| **ship** | one complete device image — meshes, system nodes, host interface — floorplanned for a named part. KohakuTPU's is four meshes, one per SLR. |
| **interlink** | the registered, credit-flow link joining one mesh's memory agent to the next one's, across an SLR boundary. |
| **kick** | the host write that starts a staged program running. Nothing runs until one arrives. |
| **completion** | the signal a compute unit returns when an instruction retires. It is also what refills dispatch credit, so completions are flow control and not only notification. |
| **mover** | the descriptor-driven engine inside the system node that walks memory and copies it, without a compute unit's involvement. |
| **transform slot** | a place on the memory agent's path where a project may insert a datapath that rewrites data as it streams past. KohakuTPU's MXFP7 quantiser is what occupies it here. |
| **staging** | the 2 MB store inside each mesh's memory agent, reachable by address rather than by instruction. Built; see [results.md](results.md) and [relayout.md](relayout.md) for what is and is not wired to it. |
| **granule** | 32 bytes — the smallest unit any data path on this machine moves. A 4x4 FP16 sub-tile is exactly one. |
| **relayout** | changing a buffer's byte order. This machine's orders are not interchangeable, and [relayout.md](relayout.md) is what one costs. |
| **station** | one node of the AXI fabric outside the meshes — see [kohakuaxi/](../kohakuaxi/README.md). Distinct from a mesh node; the two networks do not share a vocabulary. |

The four categories every page labels its subjects with — **fixed protocol**,
**customizable addon**, **convention**, **yours** — are defined in
[docs/README.md](../../README.md) and applied to KohakuTPU in §3.1 below.

---

## 1. What it computes

`C = A · B` in **MXFP7**: a 7-bit signed significand with an E5M3 scale shared by
a block of 32, multiplied inside DSP48E2s and reduced as exact integers, with
floating point reached once per 32 MACs. Operands and results in memory are FP16;
the internal format is never visible to software.

| | |
|---|---|
| element format | int7 significand + E5M3 scale per 32 along K |
| tensor CU | 4x8x4 — 64 DSP48E2, 128 MAC/cycle |
| cluster | 4 tensor CUs + 1 accumulator — 4x32x4, **512 MAC/cycle** |
| accumulate | `S1 E7 M14` (FP22), one add per 32 multiplies |
| supported shape | `M = 4a`, `N = 4b`, `K = 32c` |
| vector core | 16 lanes of E8M15, three DSPs each, four transcendentals at full rate |

The whole machine is **AMP FP16-MXFP7**, so the throughput unit is FLOPS rather
than IOPS: the integer datapath is an implementation of a floating-point multiply
whose exponent has been factored out of the block.

## 2. What it demonstrates

**That a compute unit can be almost all datapath.** Every MAC is 0 LUT, 0 FF, 1
DSP — the multiply and the whole K=32 reduction happen inside the DSPs. Against
the FP8 design it replaced, the same 128 MACs cost 1,188 LUT instead of 12,731,
and essentially all of the difference is accumulation leaving the fabric
([results.md](results.md) §7).

**That the framework's port contract is enough to feed one.** A cluster attaches
with a single mesh port. It works because the resident output tile creates enough
operand reuse to bring the demand under one word per cycle — which is an
arithmetic property of the datapath, not a concession the framework made
([accumulator.md](accumulator.md) §1).

**That the layering pays for itself.** One `GEMM` flit becomes 256 accumulator
commands; one `FILL` flit becomes 128 response flits; four flits become a whole
matmul ([isa.md](isa.md) §8).

**That a dataflow machine's ceiling is a property of its schedule, not of its
buses.** The machine went from 6.8% of its own datapath peak to 87.6% **without
widening a single bus** — every gain came from what was requested and when, not
from more wire ([results.md](results.md) §8). An arithmetic ceiling derived from
operand traffic is only as sound as its assumption that each byte is fetched
once, and that assumption is the schedule's to keep.

### 2.1 Two units, one port — and nothing else in common

This is the project's strongest single piece of evidence about the framework, and
it is an argument about *flexibility* rather than about fit. KohakuTPU contains
two compute units. They are the same shape at exactly one place: the mesh port.

| | matmul cluster | vector core |
|---|---|---|
| operand memory width | **928 bits** | **256 bits** |
| operand memories | **two** (`u_l1a`, `u_l1b` — A and B are separate RAMs) | **one** flat scratchpad |
| memories in the unit | **five** L1-class RAMs: two per manager plus each node's own accumulator tile | operand L1, an instruction memory in distributed LUTRAM, and a register file mirrored three times to synthesise three read ports |
| read latency | 1 on L1, **2** on the accumulator tile | 1, and the walk derives from the primitive rather than assuming it |
| what an instruction is | a macro-op — one flit becomes hundreds of internal commands | a program — words loaded into instruction memory, then entered |
| element format | int7 with a shared block scale | E8M15 |

**Both plug into the identical mesh port.** The framework fixed *how a unit
receives work and returns results* and nothing else; everything behind that
boundary diverged completely, down to the number of memories, their widths, their
primitives and their latencies.

So none of the structure on these pages should be read as "the way a compute unit
is built". A 928-bit L1 with a separate A and B RAM is what a DSP cascade eating
eight operand words per cycle needs; a 256-bit flat scratchpad is what a 16-lane
SIMD core needs. **They are two answers, not one pattern**, and the fact that both
answers reached the mesh through the same six signals is what the framework is
claiming.

## 3. Which framework features it exercises

| framework | how KohakuTPU uses it |
|---|---|
| [compute-unit port](../../spec/compute-unit-port.md) | two unit types on the same contract — a cluster and a vector core, one with a macro-op and one with a program |
| [instruction payload](../../spec/instruction-encoding.md) | three cluster opcodes in a 256-bit payload; 32-bit vector words, eight per payload, inside a load-and-run envelope |
| [flit format](../../spec/flit-format.md) | operand words sized so 32 int7 elements plus 4 scales fill the payload exactly |
| [memory protocol](../../spec/memory-protocol.md) | streaming descriptors, out-of-order tagged responses, burst writes, and a per-request quantise flag |
| [memory agent](../../arch/sysnode/README.md) | KohakuTPU's own quantiser occupies the framework's transform slot at id 1, reachable only by the memory mover |
| [mesh](../../arch/noc/README.md) | unit-to-unit bulk transfer, used for peer accumulation at full accumulator width |
| [ship assembly](../../arch/ship/README.md) | four independent meshes, one per SLR, joined by the interlink |
| [measurement flow](../../workflow/measure.md) | every figure in [results.md](results.md) |

### 3.1 Which category is which

The tree distinguishes four kinds of thing, and a project page is only useful if
it says which kind each of its subjects is. For KohakuTPU:

| category | meaning | what falls here |
|---|---|---|
| **fixed protocol** | cannot be changed by a project | the mesh port's six signals and its retry flow control; the flit header; how an instruction arrives and a completion returns; the memory request/response protocol |
| **customizable addon** | ships working, meant to be swapped | **the MXFP7 quantiser** — KohakuTPU's number format plugged into the memory agent's transform stage ([number-format.md](number-format.md) §5); staging or an L2 in the same agent, if it is ever built ([notes/cache/](../../notes/cache/README.md)) |
| **convention** | how to design a thing — some forced by the agent's design, some free | operands stored tile-major so a fill is one instruction; K swept outermost inside a sweep and innermost across chunks; rounds cut against three limits at once; naming a memory primitive rather than inferring it |
| **yours** | the project's own, top to bottom | **almost everything else on these pages**: the number format, the DSP packing, the cascade, the accumulator and its tile, the vector ALU, both instruction sets, the compiler, the mesh populations |

**Most of KohakuTPU is in the last row, and saying so is what makes the framework
claim credible.** A framework that had dictated the datapath would not have needed
a project to prove anything.

## 4. Status

| | |
|---|---|
| matmul datapath | **built and verified** against both a behavioural model and a real DSP48E2 |
| accumulator | **built**, FP22, resident tile, peer transfer reachable |
| cluster as an endpoint | **built**, one mesh port, and meets a 310 MHz target in out-of-context synthesis with slack ([results.md](results.md) §2). Not placed at any cluster count |
| quantiser | **built**, as the transform slot's occupant at id 1; a fetch is never transformed |
| vector ALU | **built and measured** — FMA within one ulp (correctly rounded outside one stated subtractive corner), faithful seeds |
| vector core around it | **built**, and its instruction set partly so |
| driver and hand-built encoders | **run on the card** |
| compiler path | one path, `kohakutpu.lang` to `kohakutpu.isa`; cluster **and** vector ops emit |
| tinygrad frontend | **built** on 0.13 — matmul, epilogues and elementwise chains lower and run |
| tensor-descriptor ISA | designed, walker built and validated, **not wired in** |
| chain bypass, `FWD` | **not built** |
| split-K epilogue on a vector core | **designed, not built** |
| place-and-route on a populated die | **not done** for any cluster-count configuration |

Two open defects are recorded rather than hidden: **silent FP16 saturation** on
the way out of the accumulator ([accumulator.md](accumulator.md) §7), and **an L1
footprint band that returns wrong data**, currently guarded rather than fixed
([results.md](results.md) §9.1).

## 5. How to read the rest

The order below is the order the decisions were forced, and each page assumes the
one before it.

1. **[number-format.md](number-format.md)** — MXFP7. The format sets the operand
   width, which sets the packing, which sets the cascade depth, which sets the
   block size. Start here or nothing else will look motivated.
2. **[matmul.md](matmul.md)** — the tensor core: two int7 MACs per DSP sharing an
   activation through the pre-adder, the packing offset, the guard-bit budget, and
   the cascade that reduces K=32 without touching the fabric.
3. **[accumulator.md](accumulator.md)** — the resident output tile, why its size
   decides the port count, the reuse contract, and where floating point starts.
4. **[vector-core.md](vector-core.md)** — the second unit: E8M15 chosen so an FMA
   fits one DSP exactly, and four base-2 seeds at full rate.
5. **[isa.md](isa.md)** — one worked example of spending the framework's
   instruction payload bits, at three scales.
6. **[compiler.md](compiler.md)** — the software stack: six levels and what each
   is forbidden to know, tile choice discounted by padding, and the
   round-cutting a machine without hardware loops forces on its compiler.
7. **[ship.md](ship.md)** — the device, and why the machine is four meshes.
8. **[multi-mesh.md](multi-mesh.md)** — writing kernels across those four: what an
   address means, which splits the silicon takes, and the one it refuses.
9. **[results.md](results.md)** — every measured number, with its conditions.

Then the pages about writing against it, in no particular order:

- **[writing-kernels.md](writing-kernels.md)** — how much of the schedule to say,
  the one rule about stages, and why a tiling is a view rather than a checkpoint.
- **[fused-epilogue.md](fused-epilogue.md)** — the drain that lands in a vector
  core's L1 instead of DRAM: the encoding, the sequencing, and the band it fits.
- **[memory.md](memory.md)** — the two granules that bind every span, why the
  drained byte order is the fast one, and what a model-sized placement still needs.
- **[conv2d.md](conv2d.md)** — 3x3 convolution as an implicit GEMM, the branch that
  runs on today's bitstream, and why the materialised fallback is not viable.
- **[sdxl-requirements.md](sdxl-requirements.md)** — a modern network used as a
  probe: every layer SDXL issues, whether the op exists, the kernels the gaps
  need, and the measured relayout bill that is the actual blocker.
- **[relayout.md](relayout.md)** — that bill, paid on the card: the 32-byte
  granularity wall a `Tile` order runs into, the 4x4 granule transpose that
  closes it, the MAG L2 as an allocatable tier, and the relayout counts before
  and after.
- **[tinygrad.md](tinygrad.md)** — the optional tensor frontend, what it switches
  off, and the ops where it is worse than calling the library.
- **[hardware-wants.md](hardware-wants.md)** — ten asks the compiler and the
  kernels ran into, each naming the level it was established at.

And two about the device image rather than the datapath:

- **[v6-plan.md](v6-plan.md)** — replacing the AXI tree outside the meshes with
  a station line: what it recovered, which die the recovery landed on, and why
  that was not the die that needed it. The fabric itself is
  [kohakuaxi/](../kohakuaxi/README.md).
- **[xdma-channels.md](xdma-channels.md)** — the host DMA block is 17.7% of one
  SLR; what its channel count costs, and what one unmade change would return.

If you are here to see whether the framework would suit a different datapath,
read [integrate/](../../integrate/README.md) instead; these pages are specific on
purpose.

Forward-looking work that has not been decided lives in
[notes/](../../notes/README.md) — chiefly the staging and cache design space,
which is where the next structural decision about this machine will be made.
