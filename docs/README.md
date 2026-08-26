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
KohakuAccel is one of four kinds:

| | what it is | can you change it |
|---|---|---|
| **fixed protocol** | flit format, compute-unit port handshake, memory request and response encoding, credit and retry, cross-mesh encapsulation | **No.** Change it and you are not on the framework any more. |
| **customizable addon** | ships working, *built* to be swapped: the in-MAG transform stage, in-MAG staging, the NoC-endpoint L2 adapter, DRAM-port packing | **Yes** — that is what the slot is for. The default is a starting point, not a decision. |
| **convention** | how to design a thing well, with worked examples: L1 fill and response tagging, unit-to-unit messaging, how to spend your instruction bits | **Your call.** Some are forced in practice because the system node hands you data in a shape; the rest are advice. Each one says which. |
| **yours** | the datapath, the memory structure, what the instructions mean, pipeline depth | **Entirely.** |

A convention is not a specification and not a default implementation. It is
"here is how we did it, here is why, here is what breaks if you deviate."
Mistaking a convention for a contract wastes effort obeying a suggestion;
mistaking a contract for a convention produces traffic that routes plausibly
and means something else.

### Where a build-time parameter falls

Parameters are not a fifth kind, and the split is worth stating because it is
easy to get backwards:

> **A framework parameter's value is yours. Its range, its meaning and what it
> costs are fixed protocol.**

Setting `L1_LINES` to 128 is your call. What `L1_LINES` *means*, which values are
legal, and what happens at the edges is not — and a parameter whose out-of-range
value fails silently rather than loudly is a defect in the part, not a choice you
made. Where a parameter has a value the shipped design uses, the page says so and
says why, because a default is evidence about the design and not merely a
starting point.

**The word "framework" in that rule is load-bearing.** A parameter on something
*you* built is yours entirely — value, range and meaning alike — because you also
wrote the specification it answers to. The rule divides a knob you turn from a
contract someone else keeps; it does not apply where you are both parties.

### Where the labels appear

The scheme classifies **parts of the machine**, so it is applied where parts are
described, not uniformly across the tree:

| | |
|---|---|
| **[arch/](arch/)** | every *system* page ends with a table sorting its parts into the four |
| **[spec/](spec/)** | every page is normative. It uses three of the four — a specification never describes something that is yours |
| **[integrate/](integrate/)** | every page opens by naming its kind, because this is the section you build against |
| **[projects/](projects/)** | a project page labels what was **forced** versus what was chosen; see [projects/](projects/) for why that matters more here than anywhere |
| **[workflow/](workflow/)** | **outside the scheme.** These pages describe practice, not parts. "How to close timing" is neither a protocol nor an addon |
| **[notes/](notes/)** | **outside the scheme**, and not normative at all |

A page describing one component in depth inherits its section's classification
rather than repeating it.

## How much is actually yours

More than the picture suggests. The two compute units in the reference
instance share the port and nothing else:

| | matrix cluster | vector core |
|---|---|---|
| L1 width | 928 bit, one memory per operand | 256 bit |
| L1 count | 2, each two banks by address arithmetic | 1, plus a 32-bit instruction memory |
| read latency | 1 for L1, 2 for the accumulator tile | 1, or 2 if L1 is built in URAM |
| register file | none — the accumulator tile is the only other state | three mirrored RAMs, one per read port |
| memories in total | 3 | 5 |

Same project, same mesh, same port. 928 bits against 256, and the machine with
the wider L1 is the one with fewer memories. There is no framework-mandated L1,
because there could not be one.

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
    |  system node       ONE component, one per mesh          |
    |    MAG          memory access, cross-mesh, the agent    |
    |    control PE   a processor fused to MAG: dispatch,     |
    |                 supervision, the mover and its          |
    |                 transform slot                          |
    |    hub          N attachments; nothing inside owns one  |
    |    descriptors in -> DRAM traffic -> streamed responses |
    |    the PE answers at (0,0) -- a corner, so it costs no  |
    |    attach point. Neither half ships without the other.  |
    +--------------------------------------------------------+
      |
    +--------------------------------------------------------+
    |  mesh (kohakuaccel/noc)                                 |
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

### Two processors, and why

The framework carries two RISC-V cores, because two jobs in the picture above
have different lifecycles and a single core cannot have both shapes:

| | lifecycle | where it sits |
|---|---|---|
| **RV32 PE** | kicked, runs to completion, reports one word | a mesh endpoint, behind the compute-unit port |
| **RV64 system core** | boots once and runs until the image is torn down | no compute-unit shell; fused directly to MAG |

A batch unit can afford to block on the network; the unit that *arbitrates* the
network cannot. That difference, not instruction width, is why there are two.

**The node always has a control processor** — it is structural, and there is no
build without one. **Which** processor is a parameter: `CPU_RV64` selects the
RV64 complex and **defaults to 0**, so a bare build carries the RV32 one. The
RV64 configuration's fabric-facing paths are not all connected yet;
[arch/cpu/](arch/cpu/) covers both cores, the parameter, and what each
configuration does and does not yet do.

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
you: [noc](arch/noc/), [sysnode](arch/sysnode/), [cpu](arch/cpu/) for the two
processors, [ship](arch/ship/) for assembly, [physical](arch/physical/) for
floorplan and clocking, and [axi](arch/axi.md) for the boundary to everything
outside.

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

**[glossary.md](glossary.md)** — every project-specific term in one alphabetical
page: what it is, where it sits, which of the four kinds it is, and which page
covers it properly. Start here when a word on any page is unfamiliar.

## Numbers

Measurements live with the project that produced them, never in framework docs.
Any Fmax, LUT count or utilisation figure describes **one accelerator on one
part** — for the reference instance, `xcvu13p-fhgb2104-2L-e`. Those numbers are
evidence the framework closes on real silicon. They are not specifications of
it, and a framework doc that quotes them as if they were is wrong.

## Source layout

    src/kohakuaccel/     THE FRAMEWORK
      noc/               mesh: router, links, flit protocol, unit port,
                         orchestrator, CU base, L2 adapter
      sysnode/           THE system node, one component: MAG, the control
                         processor with the mover and its transform slot,
                         the interlink, and the hub that owns every port
        cpu/             the control processor as MAG sees it, in both
                         RV32 and RV64 form
      pe/rv32/           the RV32 CPU PE; SIMD_EN names an extension it
                         does not own
      pe/rv64-sys/       the RV64 core, in two configurations: a mesh
                         compute unit, and the shell-less core that fuses
                         to MAG. core/ holds the pipeline, ALU, L1, MMU
                         and node-port arbiter
      axi/               station bus, links, AXI plumbing
      common/            shared primitives: FIFOs, named memory wrappers
      verif/             bench-only models: axi_ram, port checkers
    src/templates/       worked examples with benches: CU, transform occupant,
                         endpoint adapter
    src/examples/saxpy/  the example project, RTL half
    src/kohakutpu/       a project: matmul, vector, transform occupants, and
                         the tops generated for it
    src/kohakumpe/       a project: the SIMT PE, and the SIMD extension
                         that fills the framework's SIMD_EN slot
    src/reference/       reference and proof-of-concept copies; nothing ships
    src/attic/           dead

    compiler/          tensors, kernels, schedules, machine code
    driver/            kohakuaccel (framework) and kohakutpu (project)
    scripts/py/        check.py, xsim.py, gen_mesh.py, the linters
    docs/              this tree
    docs-web/          the same material as a site
    ref/               cloned reference frameworks, git-ignored

**The split above is measured, not asserted.** `scripts/py/deps.py` reads every
instantiation under `src/kohakuaccel/` and fails the run on one whose module is
defined only under a project; it is in the standard check suite. The framework
tree builds with no project source on the path.

What it permits is a **slot**: a module the framework *names* behind a parameter
that is 0 by default, so a framework-only build never elaborates one and the
name need not resolve. There are three — `xform_bank` at MAG's transform stage,
and `khs_unit`/`khs_scalar_decode` at the CPU PE's `SIMD_EN`. Each is listed in
`deps.py` with its reason. A slot is the only shape in which the framework may
mention something it does not own.

## House rule

If a page says "comprehensive", "powerful", or "seamless", it is out of date.
Say what it does, what it costs, and where it stops.
