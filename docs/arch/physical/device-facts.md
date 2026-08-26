---
title: Device facts, and how to establish them
summary: The part's real resources and hard limits — what an SLR is here, what a crossing costs, what a memory port is wide enough to hold — and the two facts that must be verified rather than inferred.
tags:
  - architecture
  - physical
---

# Device facts, and how to establish them

These are properties of the **part**, not measurements of any accelerator. They
are the facts a floorplan has to exist on top of, and they are given for the
part the reference instance targets, `xcvu13p-fhgb2104-2L-e`, as an example of
the *kind* of fact that has to be nailed down first.

The reference instance's own choices — which mesh on which die, which channel
serves it, what has been placed — live with the project, in
[projects/kohakutpu/ship](../../projects/kohakutpu/ship.md). What a *measured*
number means anywhere in this tree is [measurement](measurement.md).

## An SLR, in this machine's usage

A large FPGA of this class is not one piece of silicon. It is several dice
stacked side by side on an interposer, and each die is a **super logic region**
— an **SLR**. The vendor's term is used throughout these pages with no
extension: an SLR is one die, its fabric is ordinary fabric, and the only thing
that makes it special is that the connection to the next SLR is not ordinary
routing.

This part has **four SLRs in a line**, so **three boundaries**. That single fact
is why the framework has an [interlink](../ship/interlink.md) at all.

## What the part provides

| | |
|---|---|
| SLRs | four, in a line, identical in hard-block census |
| boundaries between them | three |
| asymmetries | the two end dies have one crossing face rather than two; one die is the **master**, carrying configuration, the JTAG/boundary-scan logic and the device-identity primitives |
| cross-die wires | **23,040 super-long lines (SLL) per boundary, shared between both directions** — it is a total, not a per-direction budget |
| crossing latency | one cycle, transmit register to receive register, plus whatever pipelining the frequency demands |
| memory channels | one DDR4 controller wired to each SLR — which is what makes an SLR-resident mesh able to reach its own memory without crossing |
| the memory controller | **soft**, not a hard block on this family. It costs fabric, and a DDR4 interface **cannot span SLRs** |
| high-bandwidth memory | none on this part. There is no fallback if the DDR4 channels are not enough |
| arithmetic block | DSP48E2, not DSP58 — so there is no native low-precision SIMD mode and a packing scheme has to build one |
| host bridge | anchored to one SLR by its transceiver placement |

The full per-SLR and whole-device census — LUT, flip-flop, block RAM, ultra RAM,
DSP, clock regions, Laguna sites — is in
[projects/kohakutpu/ship](../../projects/kohakutpu/ship.md#1-the-device),
because it is quoted there against what one instance actually consumed.

**One SLL figure is worth reading carefully.** 23,040 is the wire budget across
*one* boundary. It is not the device total and it is not per direction: a design
sending 12,000 wires one way has 11,040 left for everything coming back.

## Hard limits that are correctness rules

These are not performance advice. A design that violates one does not run
slower; it does not build, or it builds and is wrong.

**Cascades do not cross a boundary.** Carry chains, DSP cascades and block/ultra
RAM cascades propagate through dedicated silicon that stops at the die edge. A
datapath built on a cascade is therefore *by construction* a unit of placement,
and how you decompose a compute unit is a floorplan decision made at design
time.

**A crossing must be flop → SLL → flop with nothing in between.** The crossing
resource *is* a flip-flop, so the tool can only use one when the path is
register to register. A single combinational gate anywhere on the path — an AND
with a valid, a multiplexer on a ready — forfeits it, and the crossing degrades
into ordinary interconnect. This is why every protocol that spans a boundary in
this framework is credit-based: a `ready` travelling back across a boundary is
exactly the combinational crossing the arrangement exists to avoid.

**A memory interface cannot span a boundary.** Its I/O banks and its clocking
must be in one SLR, which pins it, and it consumes that SLR's budget.

## A block-RAM port is 72 bits, and nothing warns you

The widest a block-RAM port goes on this family is **72 bits**. An array wider
than that — 73 bits, 74, 928 — cannot be one memory, so synthesis must either
build it from several block RAMs or give up and build it from LUTs.

**When it gives up, it does so silently.** A `ram_style` attribute asking for
block RAM is *discarded without a warning*, and a structure that simulates
perfectly comes back from synthesis as thousands of LUTs. There is no message,
no critical warning, and nothing in the elaboration report. The only artefact
that shows it is the utilisation report, in the column nobody reads because
memory was never the constraint.

Two consequences worth designing against:

- **Width sets the primitive count; depth is often free.** A 928-bit array costs
  the same number of block RAMs at depth 512 as at depth 128, because
  `ceil(928/72)` decides it. Widening a memory is expensive and deepening one is
  frequently not.
- **Print the memory columns on every measurement run**, even when memory is
  nowhere near the budget. That is the only place the failure is visible. See
  [workflow/measure](../../workflow/measure.md#report-the-memory-columns-always).

The general form of the trap — that a memory primitive is *inferred* from a
`reg` array by a tool heuristic, and so is its read latency, which sets pipeline
depth — is why this framework instantiates memory primitives explicitly through
a named wrapper rather than writing an array and hoping. Pipeline depth is a
design decision, not a synthesis outcome.
[workflow/tooling-traps](../../workflow/tooling-traps.md#memory-primitives-are-named-never-inferred).

## What is actually scarce, in order

On this part, for this kind of design:

> **LUT ≫ BRAM / URAM ≫ FF.**

Flip-flops are the most abundant resource by a wide margin, which has one
practical consequence worth stating plainly: **pipelining to close timing is
nearly free.** Adding a register stage costs the resource the design has most of
and buys the thing it is short of.

**The bound on that licence.** It is permission to spend a few registers on a
path, not permission to duplicate a megabyte-scale array. Registers are cheap
per bit; a second copy of a memory is not, and it is not a flip-flop cost at
all.

A second reading habit belongs beside this one: a **CLB occupancy percentage is
routing and packing pressure, not a capacity ceiling**, and a design can sit near
90% CLB while using under 60% of its LUTs. Check the LUT figure before believing
that a die is full. [workflow/timing-closure](../../workflow/timing-closure.md#7-utilisation-what-is-actually-scarce)
owns that method.

## Two habits worth more than any number above

**Verify the mapping; do not infer it.** Which memory channel is on which SLR is
a board fact set by pinout, and it very plausibly is not in the order the
numbering suggests. On the reference board it is not. Establish it from
independent witnesses — an I/O bank to SLR query, the placed clock buffer's
coordinate in an implemented design, and the board's own pinout document — and
treat agreement between three as the evidence. A design built on the guessed
mapping crosses a boundary for its own memory, meets timing, works, and is
slower than it should be for a reason no report names.

**Distinguish "this design places nothing there" from "nothing can be placed
there."** An implemented design that leaves a region empty is a property of that
design, not of the part.

## Convention

**Establish the channel-to-SLR map from three independent witnesses.** *(Free.)*
An I/O bank query, a placed clock buffer's coordinate in an implemented design,
and the board's own pinout. Agreement between three is the evidence. The
numbering is not the mapping.

**Name the primitive; never infer a memory.** *(Free.)* Instantiate through a
wrapper with the memory type as a parameter, so that read latency is chosen
rather than discovered and a 73-bit array cannot quietly become LUTs.
