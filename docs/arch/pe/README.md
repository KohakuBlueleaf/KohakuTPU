---
title: Controller PE
summary: A small RV32I processor whose memory interface was designed around KohakuAccel from the start — the controller for any accelerator built on this framework.
tags:
  - architecture
  - pe
  - rv32
---

# Controller PE

`src/kohakuaccel/pe/rv32/` — an RV32I processing element that attaches to the
fabric exactly like every other compute unit: one local port, one instruction
FIFO, the four `CU_CTRL` registers. A driver enumerates it without knowing it
is a processor. The only thing different is what it does with an instruction:
a `CU_INST` is a **kick**, and the unit retires when the program halts.

The layout is the architecture: `core/` is the pipeline, `mem/` the two L1s,
`noc/` the fabric attach, and `rv_pe.v` assembles them and holds nothing else.

## The problem it solves

Every accelerator built on this framework needs something that decides *what
to do next* — sequencing kernels, walking descriptors, reacting to
completions. Hand-written state machines do it until the day the policy
changes, and then they do it wrong. A small programmable core does it in
software, and the framework's own communication idiom — push and doorbell —
gives many of them a way to coordinate without locks and without coherence.

The design objectives, in order: RV32I compatibility so ordinary compilers
work; **very low LUT**, so dozens of PEs on one device are realistic; **high
Fmax**, so scalar control is never the slow component; and a memory frontend
that is part of the core rather than a generic CPU bus with an adapter bolted
on. LUT and frequency outrank latency everywhere — the core spends FF and
BRAM freely and prefers a pipeline stage over a bypass. One number locates
it: **2,491 LUT and 5 BRAM at 410 MHz** on `xcvu13p`, a quarter of which is
the port logic every compute unit carries anyway —
[performance](performance.md).

It is framework, not project. KohakuMPE later adds SIMD and DSP on top of
this base; [integration](integration.md#where-extensions-attach) names the
seams and nothing anticipates them further than that.

## The pages

| Page | What is in it |
|---|---|
| [architecture](architecture.md) | the contract: the ISA as implemented, the memory model, ordering, halting, and the unit's kick/completion protocol |
| [microarchitecture](microarchitecture.md) | how it is built: the pipeline, the hazard rules, the predictor, the two L1s, the requestor — and why each has its shape |
| [programming](programming.md) | the programmer's guide: memory map, control words, the communication idioms as code, boot, and running a program |
| [integration](integration.md) | instantiating one: parameters, the attach, the constraint to ask of it, extension seams, and the test suite |
| [performance](performance.md) | what it costs and achieves: frequency, resources, instruction and memory timing, communication, multi-core scaling |

If you are writing PE software, [programming](programming.md) is the page,
and [architecture](architecture.md) is what it stands on. If you are choosing
a configuration or floorplanning PEs, [performance](performance.md) then
[integration](integration.md).

## Fixed protocol, parameter, or yours

| Thing | Category |
|---|---|
| the compute-unit port, the flit, the control registers | **fixed protocol** — the fabric's, not this unit's: [spec](../../spec/) |
| the address regions, control words, and window encoding | **fixed protocol** of this unit — [architecture](architecture.md) |
| push-and-doorbell ordering: program order is arrival order, per destination | **fixed protocol** of this unit — software depends on it |
| the halt model: no CSRs, no interrupts, halt word by cause | **fixed protocol** of this unit until an extension says otherwise |
| `BTB_ENTRIES`, `FWD_X`, `L1_LINES`, `REGFILE_PRIM`, window sizes, `WR_MAX` | **parameters** — the defaults are the shipped configuration: [integration](integration.md#parameters) |
| what a program computes | **yours** |

## What this PE does not own

| Concern | Whose |
|---|---|
| the flit, the link, the router, the port handshake | [noc](../noc/) |
| descriptor encoding, write slots, response tagging | [mas](../mas/) |
| where the PE lands on the die and at what clock | [physical](../physical/) |
| the meaning of addresses beyond its own windows | [ship](../ship/) and [mas](../mas/) |
