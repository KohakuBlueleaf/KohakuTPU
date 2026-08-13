---
title: KohakuAccel
summary: A framework for building FPGA accelerators around a compute unit you design.
tags:
  - overview
  - architecture
---

# KohakuAccel

A framework for building FPGA accelerators.

You write a compute unit. KohakuAccel is everything else: DRAM and its
controllers, a memory agent that turns descriptors into transfers, an on-chip
network that carries instructions to your units and results back, host
interface, floorplanning across SLRs, clock domains, and the build and
measurement flow that gets it to close timing.

## What kind of framework

The word "framework" is load-bearing, so be precise about which one:

| | is a framework for | serves people who |
|---|---|---|
| Vitis HLS | turning C into RTL | do **not** want to write RTL |
| IP catalogues | assembling vendor blocks | want someone else's datapath |
| soft-processor overlays | running software on fabric | want a CPU on an FPGA |
| **KohakuAccel** | **building an accelerator around a datapath you designed** | **want to write the interesting RTL and nothing else** |

If you want to avoid writing RTL, use HLS. KohakuAccel assumes the datapath is
the part you care about, and that writing a DDR controller, a DMA engine, an
on-chip network, a dispatch mechanism and a driver for the fifth time is not.

## Four kinds of thing

"You supply this, we supply that" is too coarse to build against. Everything in
KohakuAccel is one of four kinds, and every page in this tree says which:

| | what it is | can you change it |
|---|---|---|
| **fixed protocol** | flit format, compute-unit port handshake, memory request and response encoding, credit and retry, cross-mesh encapsulation | **No.** Change it and you are not on the framework any more. |
| **customizable addon** | ships working, *built* to be swapped: the in-MAG transform stage, in-MAG staging, the NoC-endpoint L2 adapter, DRAM-port packing | **Yes** — that is what the slot is for. The default is a starting point, not a decision. |
| **convention** | how to design a thing well, with worked examples: L1 fill and response tagging, unit-to-unit messaging, how to spend your instruction bits | **Your call.** Some are forced in practice because MAG hands you data in a shape; the rest are advice. Each one says which. |
| **yours** | the datapath, the memory structure, what the instructions mean, pipeline depth | **Entirely.** |

A convention is not a specification and not a default implementation. It is
"here is how we did it, here is why, here is what breaks if you deviate."
Mistaking a convention for a contract wastes effort obeying a suggestion;
mistaking a contract for a convention produces traffic that routes plausibly
and means something else. Every page says which it is talking about.

## How much is actually yours

More than the picture suggests. The two compute units in the reference
instance share the port and nothing else:

| | matrix cluster | vector core |
|---|---|---|
| L1 width | 928 bit, split A operand / B operand | 256 bit |
| L1 count | 4 core + 1 accumulator tile | 1, plus a separate instruction memory |
| read latency | 1 for L1, 2 for the accumulator | parameterised |
| register file | none | three mirrored RAMs for three read ports |

Same project, same mesh, same port. 928 bits against 256; five memories
against two. There is no framework-mandated L1, because there could not be
one.

What *is* fully defined is **how you receive and send**. That is the trade:
you design the whole unit, and you never have to work out how to connect it.
The port is given, the protocol across it is given, and the conventions and
worked examples show what a well-behaved unit looks like on the wire.

Beyond the unit, the framework carries DDR4 controllers, AXI fabric and host
DMA; the memory agent that turns descriptors into streamed operands; dispatch
from host to unit and completion back; SLR floorplanning, clock domains and
runtime frequency control; and the out-of-context measurement flow, timing
closure practice and bringup path that get it onto real silicon.

## What is actually on the die

    host (PCIe)
      |
    XDMA  --------------------------------.
      |                                    |
    AXI fabric (kohakuaxi) ----------------+---- JTAG-AXI (debug)
      |                |                   |
    DDR4 x N        control              instruction dispatch
      |                                     |
    +--------------------------------------------------------+
    |  MAG (kohakumas)   memory agent, one per mesh           |
    |    descriptors in -> DRAM traffic -> streamed responses |
    |    plus two addon slots: transform, staging            |
    +--------------------------------------------------------+
      |
    +--------------------------------------------------------+
    |  mesh (kohakunoc)                                       |
    |                                                         |
    |    router --- router        each router carries local   |
    |      |          |           ports; endpoints hang off   |
    |    router --- router        them                        |
    |      |                                                  |
    |    [ L2 adapter ]    <- addon, optional                 |
    |    [ compute unit ]  <- YOURS, inside and out           |
    +--------------------------------------------------------+
      |
    interlink -> other meshes, other SLRs

A **ship** is one complete assembly of the above, floorplanned for a specific
device. A device image may hold several meshes, one per SLR, joined by the
interlink.

The compute unit is the only block you have to write. The addon slots are
places you *may* write, with something working already in them.

## Does your workload fit

The framework assumes a shape. It fits when:

- Work decomposes into units that stream operands in, compute, and stream
  results out.
- A unit's working set fits in on-chip memory for the duration of a step.
- Addresses are known ahead of time — expressible as descriptors, not
  discovered by following pointers.
- Units are independent within a step; they synchronise between steps, not
  inside one.

It does not fit when you need pointer chasing or data-dependent addressing,
tight low-latency coupling *between* units (write one larger unit instead),
cache coherence between units, or kernels small enough that dispatch dominates
the work.

Saying no here is cheaper than finding out after floorplanning.

## The tree

**[arch/](arch/)** — what exists and how it maps to real circuit. Start with
[arch/README](arch/README.md) for the macro view, then the system that concerns
you: [noc](arch/noc/), [mas](arch/mas/), [ship](arch/ship/) for assembly,
[physical](arch/physical/) for floorplan and clocking, and
[axi](arch/axi.md) for the boundary to everything outside.

Each system's README states what it owns, which of the four kinds its parts
are, what it does *not* own and which neighbour takes over, and where today's
source disagrees with the decomposition. `axi` is a single page because that
boundary is closed; the others carry pages beneath them.

**[integrate/](integrate/)** — the surface you build against. Which of the four
kinds each thing is, how to write a compute unit, the conventions and the
examples behind them, how to spend your instruction bits, how to fill an addon
slot, how to choose a mesh, how the software stack plugs in.

**[spec/](spec/)** — normative contracts, and only those. Signals, flit fields,
encodings, parameters. A unit that satisfies these works; one that does not,
does not. Anything you are free to ignore is a convention and lives in
[integrate/](integrate/), not here.

**[workflow/](workflow/)** — the practice: build, measure out of context, close
timing, simulate, bring up, debug. Hardware has no `pip install`; this is the
part that is genuinely laborious and where the framework saves the most time.

**[projects/](projects/)** — accelerators built on the framework, at their own
level. [KohakuTPU](projects/kohakutpu/) is the reference instance: an MXFP7
tensor accelerator that exercises every part of the framework.

**[notes/](notes/)** — design rationale and open research. Why decisions went
the way they did, and what is still undecided.

## Numbers

Measurements live with the project that produced them, never in framework docs.
Any Fmax, LUT count or utilisation figure describes **one accelerator on one
part** — for the reference instance, `xcvu13p-fhgb2104-2L-e`. Those numbers are
evidence the framework closes on real silicon. They are not specifications of
it, and a framework doc that quotes them as if they were is wrong.

## Source layout

    src/
      kohakuaxi/   AXI utilities: fabric, bridges, address decode
      kohakumas/   Memory agent: descriptors, DRAM ports, dispatch
      kohakunoc/   Mesh: router, links, flit protocol, compute-unit port
      common/      Shared primitives: FIFOs, named memory wrappers
      kohakutpu/   KohakuTPU's compute units — a project, not the framework
      synth_top/   Ship assemblies, per device

    compiler/          tensors, kernels, schedules, machine code
    driver/            transports, dispatch, completion
    docs/              this tree
    ref/               cloned reference frameworks, git-ignored

## House rule

If a page says "comprehensive", "powerful", or "seamless", it is out of date.
Say what it does, what it costs, and where it stops.
