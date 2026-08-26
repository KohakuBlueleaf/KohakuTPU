---
title: RV32 PE
summary: The framework's small programmable compute unit — an RV32I core with the multiply half of M, attached to a mesh through the standard compute-unit port.
tags:
  - architecture
  - cpu
  - rv32
---

# RV32 PE

`src/kohakuaccel/pe/rv32/` — a small in-order RISC-V core packaged as an
ordinary **compute unit**: it hangs off one mesh router's local port, accepts
one instruction at a time, and reports a 32-bit word when it finishes. It is
one of this machine's [two processors](../README.md); the other is the
[RV64 system core](../rv64-sys/README.md), which is a runtime host rather than
a compute unit.

Two terms this page rests on, before anything else uses them:

- a **compute unit** is anything that attaches to one fabric port, takes
  instructions one at a time, names the memory it wants as an address and a
  length, and signals retirement. Nothing in the framework knows whether a unit
  multiplies, sorts or executes a program — [arch/README](../../README.md);
- a **kick** is the compute unit's start signal. The framework's dispatch
  instruction (`CU_INST`) arrives as a **flit** — the fabric's fixed-size
  packet — and for this unit it means *begin executing at this PC*. The unit
  retires when the program halts, and the **completion** (`CU_SIGNAL`) carries
  the halt word back to whoever kicked it.

So the shape of its life is *someone kicks me, I run to completion, I report a
word*. That is what makes it a compute unit rather than a host, and it is the
whole reason this machine has a second, differently shaped processor at all.

The directory layout is the architecture: `core/` is the pipeline, `mem/` the
two L1s, `noc/` the fabric attach, and `rv_pe.v` assembles them and holds
nothing else.

## Why the framework ships a processor at all

Every accelerator built on this framework needs something that decides *what to
do next* — sequencing kernels, walking descriptors, reacting to completions.
Hand-written state machines do that until the day the policy changes. A small
programmable core does it in software, and the framework's own communication
idiom — push a payload, then push a doorbell — gives many of them a way to
coordinate without locks and without cache coherence.

It is also a controller in the stronger sense: the RV32 PE can **dispatch
instructions to other compute units** and **read their completions back**,
through two store-only address regions and a control register file. A mesh can
therefore run a dependency graph without the host in the loop —
[programming](programming.md#dispatch-and-completions).

The design objectives, in order:

1. RV32I compatibility, so ordinary compilers work unmodified;
2. **low LUT**, so dozens of units on one device are realistic;
3. **high Fmax**, so scalar control is never the slow component in a machine
   whose datapaths are wide;
4. a memory frontend that is part of the core rather than a generic CPU bus
   with an adapter bolted on.

LUT and frequency outrank latency everywhere: the core spends flip-flops and
block RAM freely and prefers a pipeline stage over a bypass.

## The instruction set, in one line

**RV32I plus the multiply half of `M`.** `mul`, `mulh`, `mulhsu` and `mulhu`
execute on a shared 33 × 33 signed multiplier; `div`, `divu`, `rem` and `remu`
are decoded and **refused** — an illegal-instruction halt at the offending PC,
not a silently aliased result. There is no floating point, no CSR file, no
interrupts and no privilege. Each of those absences is a design decision with a
cost attached rather than an unfinished edge:
[architecture](architecture.md#what-this-core-deliberately-does-not-do).

## What it costs

**2,586 LUT, 3,844 FF, 9 BRAM and 4 DSP48 at 363.5 MHz**, against the 3.333 ns
request the unit is meant to be constrained at, with +0.582 ns of slack.
Out-of-context synthesis on `xcvu13p-fhgb2104-2L-e` under Vivado 2024.2,
`-flatten_hierarchy none`, produced by `scripts/tcl/ooc_rv_pe.tcl` in the
shipped configuration.

**Synthesis, not routed.** No Fmax anywhere in this repository is a
closed-timing figure, and synthesis slack is optimistic — a routed result will
be worse. [performance](performance.md) carries the full conditions, the
per-unit split, and what is *not* measured.

363.5 MHz is a ceiling rather than a response to the constraint: asking for
400 MHz instead yields the same 363.5 MHz, costs 86 LUT, and misses the
request. The four DSP48s are the multiplier. **Close to a fifth of the LUT
total is the compute-unit port** every unit on this fabric carries, processor
or not, so the marginal cost of *this unit being a processor* is a little over
2,100 LUT — [performance](performance.md#resources) has the per-unit split and
the run it comes from.

## What this PE deliberately does not do

- **It does not run an operating system.** No privilege modes, no CSR file, no
  interrupts, no MMU. A program is kicked, runs, and halts. If you need a core
  that boots once and runs forever, that is the
  [RV64 system core](../rv64-sys/README.md).
- **It does not divide.** `div` and `rem` fault. An iterative divider is a
  fixed structure bought for a rare instruction —
  [microarchitecture](microarchitecture.md#why-div-and-rem-are-a-different-answer).
- **It does not compute in floating point.** There is no float register file to
  name. The float arithmetic in this machine lives in the wide datapaths.
- **It does not cache anything another agent writes.** Coherence is removed by
  construction, not by omission — the only externally written memory is never
  cached, and the only cached memory is never externally written.
- **It does not own its own memory system's semantics beyond its windows.** The
  meaning of an address past its DRAM window belongs to
  [sysnode](../../sysnode/) and [ship](../../ship/).

## The pages

| Page | What is in it |
|---|---|
| [architecture](architecture.md) | the contract: the ISA as implemented, the memory model, ordering, halting, and the kick/completion protocol |
| [microarchitecture](microarchitecture.md) | how it is built: the pipeline, the multiplier, the arithmetic EX does **not** have and what each option would cost, the hazard rules, the predictor, the two L1s, the requestor |
| [programming](programming.md) | the programmer's guide: memory map, control words, the communication idioms as code, dispatch, boot, and running a program |
| [integration](integration.md) | instantiating one: parameters, the attach, the constraint to ask of it, extension seams, and the test suite |
| [performance](performance.md) | what it costs and achieves, with the condition on every figure |

Writing PE software: [programming](programming.md), standing on
[architecture](architecture.md). Choosing a configuration or floorplanning
units: [performance](performance.md), then [integration](integration.md).

## Where a wide datapath attaches

**`SIMD_EN` is a slot.** The framework names `khs_unit` and `khs_scalar_decode`
behind it; the parameter is 0 by default, so a framework-only build never
elaborates either and the names need not resolve. A project supplies them.
Unfilled, this core is exactly what the figure above measures.

The SIMT class is not a parameter on this core but a **rebuild on its shape** —
an ordinary RV32I opcode addressing a per-thread register file, lanes free to
disagree on both their path and their address. It shares the pipeline
structure, not the pipeline.

| | what it is | whose |
|---|---|---|
| [**simd**](../../../projects/kohakumpe/simd/) | a uniform-control wide datapath behind the same six pipeline boundaries, at `SIMD_EN` | [KohakuMPE](../../../projects/kohakumpe/README.md) |
| [**simt**](../../../projects/kohakumpe/simt/) | a per-thread rebuild with its own top | [KohakuMPE](../../../projects/kohakumpe/README.md) |

What either class costs, how many lanes it carries and what format it computes
in are that project's numbers, not this framework's — mesh populations and
float formats included. This page names the seam and stops;
[integration](integration.md#where-extensions-attach) specifies it.

Every PE class draws its non-standard instruction encodings from RISC-V's four
custom opcode majors, and there are no more than four.
[**opcode-map**](opcode-map.md) is the single authority on which class owns
which, and on where the room actually is. It is framework-level for the same
reason the allocation is finite: a table one class owns is not something
another class can check itself against.

## Fixed protocol, parameter, or yours

| Thing | Category |
|---|---|
| the compute-unit port, the flit, the `CU_CTRL` registers | **fixed protocol** — the fabric's, not this unit's: [spec](../../../spec/) |
| the address regions, control words, and window encoding | **fixed protocol** of this unit — [architecture](architecture.md) |
| push-and-doorbell ordering: program order is arrival order, per destination | **fixed protocol** of this unit — software depends on it |
| the halt model: no CSRs, no interrupts, halt word by cause | **fixed protocol** of this unit until an extension says otherwise |
| `BTB_ENTRIES`, `FWD_X`, `L1_LINES`, `REGFILE_PRIM`, window sizes, `WR_MAX` | **parameters** — the defaults are the shipped configuration: [integration](integration.md#parameters) |
| what a program computes | **yours** |

## What this PE does not own

| Concern | Whose |
|---|---|
| the flit, the link, the router, the port handshake | [noc](../../noc/) |
| descriptor encoding, write slots, response tagging | [sysnode](../../sysnode/) |
| where the PE lands on the die and at what clock | [physical](../../physical/) |
| the meaning of addresses beyond its own windows | [ship](../../ship/) and [sysnode](../../sysnode/) |
