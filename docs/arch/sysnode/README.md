---
title: The system node
summary: MAG, the memory mover and a control processor as one block — what makes a mesh an SoC rather than an accelerator with a host attached, and why the mover became an execution unit of the processor instead of a peer.
tags:
  - architecture
  - sysnode
  - memory
  - soc
---

# The system node

`src/kohakuaccel/sysnode/` — one per mesh, and the single point where a mesh
touches everything outside it.

**It is a system node, never a "node".** A **NoC endpoint** — an attachment
point on the on-chip network, where a compute unit hangs — is a node, and the
two are different things at different scales. Where this tree says `sysnode` it
means this block; where it says node it means an endpoint.

## The vocabulary, once

Every page under here assumes these, and no page assumes you already knew them.

| term | what it means here |
|---|---|
| **flit** | one unit of on-chip network traffic: a fixed-width word carrying a routing header and a payload. 288 bits in the reference build |
| **MAG** | the *memory access gateway* — the half of the node that serves memory requests, drives DRAM, and carries cross-mesh traffic |
| **mover** | the node's descriptor-driven copy engine. It reads memory and writes memory, walking strided N-dimensional descriptors, and never talks to a compute unit |
| **transform slot** | a socket on the mover's read-return path where a format conversion may be plugged in. The slot is the framework's; what fills it is a project's |
| **staging** | a URAM store inside the node with a reserved range in the address map. Reached by address, never by an instruction. Not a cache: no tags, no replacement, no coherence |
| **aperture** | a reserved region of the 40-bit address map, named by address bits `[35:32]` when bit `[39]` is set. Staging is aperture 0 |
| **granule** | 32 bytes — the unit an image loader writes and the width of the node's internal data word |
| **doorbell** | a counter one mesh increments in another to say "the data I pushed you is in memory". Not a flag: a count, so a reader polling slower than events arrive can tell how many it missed |
| **station** | the AXI-side building block outside the node — [axi](../axi.md) |
| **compute unit** | the datapath you design and attach to a NoC endpoint |

## Why this block exists: the SoC idea

The same silicon looks different from two directions, and the system node is
what makes both views true at once.

**From a CPU's side, an SoC is good because everything it needs is on the
chip** — memory, the interconnect, the peripherals — so a program is not
constantly negotiating with something across a bus it does not control.

**From an accelerator's side, an SoC is good because there is a CPU next to
it** — something that can run a loop, take a branch, hold state between kicks
and decide what happens next.

An accelerator mesh with a host on the far end of PCIe has neither. The system
node supplies the missing half **inside the mesh**: a processor whose two jobs
are *memory management and access* and *task dispatch*. That is the whole
justification, and the two jobs are worth taking separately.

**A control processor is structural; which processor it is, is a parameter.**
`sysnode.v` instantiates one unconditionally — there is no build without one —
and `CPU_RV64` picks between two complexes: the default RV32 one, and an RV64
one with supervisor privilege, Sv39 translation and a write-back L1, built to
host a runtime rather than a kernel. [control-processor](control-processor.md)
describes both, says which is the default, and states what each one connects.

## 1. A CPU for dispatch buys complex setups

Dispatch without a processor is a host writing 32-bit control registers across a
link measured in microseconds at best. That is enough to start one kernel. It is
not enough for the shapes real work actually has:

- **Graph execution.** A graph is nodes with dependencies: when this finishes,
  start those two; if that fault bit is set, stop. Every edge is a poll and a
  decision, and a handful of instructions on the card replaces a round trip per
  edge. Any accelerator whose work is a dependency graph wants this, whatever
  the nodes compute.
- **A recorded command program.** Vulkan's execution model is command buffers
  recorded once and submitted many times, pipelines compiled ahead of dispatch,
  and synchronisation split into fences, semaphores and barriers. That is a
  *program*, and it belongs on something that can run one.

Neither is a claim about a workload. The framework does not know what the
compute units compute, and this page describes the mechanism that makes either
shape expressible.

**Which processor is in the node decides how it is reached.** The default RV32
complex wears a compute-unit shell, so from outside it *is* a compute unit at
`(0,0)`: load, fire and observe are the ordinary sequence and a driver
enumerates it without knowing it is a processor. It costs no attach point —
`(0,0)` is a corner, which touches no router, so the coordinate is free in every
mesh by construction. It emits instructions to compute units and consumes their
completions.

The RV64 complex has **no shell**. It is loaded through an AXI-side window
instead, and it reaches the mesh through a **dispatch mailbox** in its control
region: software names a destination and two payload words, hardware builds the
`CU_INST` flit, and completions land in a 16-deep queue that raises the core's
external interrupt. It dispatches, but it is not enumerable — `(0,0)` answers no
`CU_CTRL` read in that configuration, and credit accounting is the program's
rather than the hardware's. What it connects and what it still leaves to
software is in
[control-processor](control-processor.md#what-the-rv64-configuration-connects).

## 2. A CPU for memory management makes the mover worth having

The memory mover walks N-dimensional strided descriptors with bound axes. On its
own it is a good engine with an awkward interface: somebody has to compose seven
register writes, in order, and know when it is finished.

**So the mover is not a peer with a doorbell — it is an execution unit of the
processor.** `mv.go` is a store to an address inside the processor's control
region, the descriptor is built by ordinary stores, and program order *is* the
queue. The consequences are the point:

| as a peer | as an execution unit |
|---|---|
| a command window somebody must drive | a store, decoded out of an address range |
| descriptors are host register writes | descriptors are what a program writes |
| ordering is a protocol | ordering is program order |
| the host's command window is the only way in | it is one of two, and the processor wins |

The host's window does not disappear — bring-up needs a path that works before
any program runs, and the case where the processor itself is the suspect is
exactly the case a path around it is worth most. What changes is that it stops
being the *architecture* and becomes a second entrance.

The same argument scales outward. **Several system nodes are much easier to
orchestrate when each has a processor**: cross-mesh work becomes a program on
each side plus a doorbell, rather than one host serialising every edge of the
graph across four meshes.

## What it owns

**The memory instruction set.** A compute unit does not design a way of *asking*
for memory; it inherits one. Read and write descriptors, entry geometry,
streaming runs, multi-destination delivery, and the mover's command set are all
defined here — [instruction-space](instruction-space.md).

That is the asking, and only the asking. **A compute unit's own memory system —
how many memories, how wide, how deep, at what read latency — is its author's
design and this system has no opinion on it.** Two units in the reference
project have operand memories of 928 and 256 bits; both are ordinary clients.

**The service behind those instructions.** Issuing the AXI bursts, streaming
responses back as flits that say where they belong, and reassembling write
bursts the mesh delivered out of order.

**The hub, and every attachment the node has.** A mesh has few attachments to
give away. Memory traffic, the control plane, the inter-mesh link and the
processor all need one, and giving each its own would cost four times the ports
for three consumers that are nearly idle. So **nothing inside the node owns a
port**: they are clients of `sn_hub`, which demultiplexes inbound by destination
and type and steers outbound by row — [edge-and-control](edge-and-control.md#the-hub).

**The control processor and its mover**, per §1 and §2.

## The problem the memory half solves

A compute unit should not contain a memory system. If it does, every unit
carries a copy of burst generation, 4 KB boundary handling and reassembly — and
each copy is a place to get it wrong.

The split is between **naming** memory and **serving** it. A unit names what it
wants ahead of time, because that is the assumption the whole framework rests
on: addresses are computable, not discovered by chasing pointers. Serving it is
here.

## The pages

| Page | What is in it |
|---|---|
| [abilities](abilities.md) | **the reference** — what a node can do as a standalone system, every register map a program needs, what does not cross the link, and the test behind each ability |
| [simd-model](simd-model.md) | **the design view** — processor as scalar, mover as its SIMD unit, slot as that unit's extension; where the slot sits, the register and fault contract, four worked transforms |
| [control-processor](control-processor.md) | the RV64 control complex: what it is, the address space it sees, Sv39, what it replaced and why, and what the whole node measures with it |
| [instruction-space](instruction-space.md) | the instruction set you inherit: who owns which bits of a flit, what a read, a write and a mover command can express |
| [memory-port](memory-port.md) | the port as the unit the machine grows by — intake, the read engine, write slots matched by source, and what a port costs |
| [transform-stage](transform-stage.md) | the transform slot: one bank **on the mover's read return**, reached only through descriptor mode 5, occupant selected by an id |
| [edge-and-control](edge-and-control.md) | staging, **the hub** and why nothing inside the node owns a port, the control agent, the host memory window |

If you are writing a compute unit, read [instruction-space](instruction-space.md)
first — most of what you were about to design is already there — then the
conventions in [memory-port](memory-port.md#conventions).

## Fixed protocol, addon, convention, or yours

| Thing | Category |
|---|---|
| memory request and response encoding, tags, acks | **fixed protocol** — [spec/memory-protocol](../../spec/memory-protocol.md) |
| the mover's command set and descriptor form | **fixed protocol** |
| the transform slot's position, selection and handshake | **fixed protocol** — [spec/transform-slot](../../spec/transform-slot.md) |
| control-agent register map and dispatch mechanism | **fixed protocol** — [spec/control-registers](../../spec/control-registers.md) |
| **what plugs into the transform slot** | **customizable addon** — a project supplies the occupant and the framework never names it. The reference instance's quantiser takes id 1; `src/templates/transform/` is an identity occupant with a bench, to build against |
| **staging inside the node** | **customizable addon** — whether, how much, with what behaviour |
| **DRAM-port beat packing** at the memory boundary | **customizable addon** — [axi](../axi.md) |
| **whether the control processor exists** | **not a parameter.** It is part of the node and there is no build without it |
| **which processor it is** | **customizable** — `CPU_RV64` selects the RV64 control complex; the default, `0`, is the RV32 one. The mover and the transform slot are the same in both, because they belong to the node |
| port count, coordinates, slot count, queue depths, primitives | **customizable** — `PORTS` and the rest, [spec/parameters](../../spec/parameters.md) |
| what the bytes mean: layout, tiling, tensor semantics | **yours** |
| your unit's own memories and how it stores what arrives | **yours**, entirely |

## What a compute-unit author must know

1. **You inherit a memory instruction set.** Read
   [spec/memory-protocol](../../spec/memory-protocol.md) before designing how
   your unit gets data.
2. **Name what you want ahead of time.** If your addresses are only knowable by
   following a pointer you have fetched, this system cannot serve you.
3. **Responses are self-describing. Do not build a cursor.**
4. **Write acks are fire-and-forget.** The slot count assumes you do not wait.
5. **Ask once for many consumers** — name extra destinations rather than issuing
   identical requests.
6. **Hold your credits yourself.** Issuing a request whose response you cannot
   absorb is how a fabric deadlocks.
7. **A fetch is never transformed.** Operands arrive in their final format; the
   mover converts before you ask — [transform-stage](transform-stage.md).

## What this system does not own

| Not owned | Who owns it |
|---|---|
| routing, links, arbitration between endpoints | [noc](../noc/) |
| the DRAM controller | vendor IP, via [axi](../axi.md) |
| clock crossing to memory, and width conversion to the memory's beat | [axi](../axi.md) |
| **what the transform computes** | the occupant's author; this system owns the slot |
| **whether and how lines are staged** | the addon's author |
| what the bytes mean — layout, tiling, tensor semantics | you, and your compiler |
| **your unit's memory system** | the compute unit's author, entirely |
| what an instruction does after dispatch delivers it | the compute unit |
| how many memory ports exist and where they attach | [ship](../ship/) |
| which die region the ports and their AXI masters land in | [physical](../physical/) |
| carrying traffic between meshes | the interlink, in [ship](../ship/) |

**The divisions on this page are of DESIGN, not of component.** MAG, the control
agent, the interlink and the processor are separate concerns and are described
separately, but the node is one module and none of them is separable from it:
MAG alone cannot start work without a host round trip, and the processor alone
cannot reach memory or another mesh. They are clients of one hub because
attachments are scarce, not because dispatch is a memory concern.

## What the node still cannot do

Stated here because the SoC framing above invites the question.

**A system node has no master port onto the station bus**, so its processor can
drive its own mesh and push into a peer's memory over the interlink, but it
cannot write a peer's control registers, retune a peer's clocks or reset a peer.
Those remain host operations. A processor per node makes multi-mesh
orchestration easier; it does not yet make one node a manager of the others.

**In the RV64 configuration it cannot be enumerated.** That complex answers no
`CU_CTRL` read at `(0,0)`, so a controller walking the mesh sees the coordinate
as empty, and there is no runtime way to tell which of the two configurations a
bitstream carries. It does dispatch; it just does not announce itself.

**In the RV64 configuration it publishes no ordering guarantee.** A compute
unit's completion means every write it made is visible, and that is a
dispatcher's only sequencing point. The guarantee comes from the shell; the RV64
complex has none and owes whoever waits on it an equivalent statement it has not
made. The default RV32 configuration inherits the shell's.

## What it costs

**Measured, out-of-context synthesis on `xcvu13p-fhgb2104-2L-e`, Vivado 2024.2,
at 3.333 ns, design state Synthesized, `PORTS=2`**, `sysnode` synthesised whole.
Nothing here is routed:

| configuration | script | LUT | FF | BRAM tiles | URAM | DSP | WNS |
|---|---|---|---|---|---|---|---|
| RV64 complex | `ooc_sysnode_rv64.tcl 2` | 32,859 | 46,436 | 57.5 | 65 | 47 | +0.039 |
| RV32 complex — the default | `ooc_sysnode.tcl` | 31,220 | 52,481 | 41.5 | 128 | 39 | +0.096 |

**Both configurations meet 300 MHz in out-of-context synthesis** — +0.039 ns and
+0.096 ns against a 3.333 ns request, with no failing endpoint in either. The
last cone to close in the RV64 node was in the mover, and registering the
command FIFO's room limit closed it.

**That is synthesis, not routing, and the caveat is not a formality.**
Synthesis slack is optimistic in this tree — one module lost 0.740 ns going to
routing, twenty times the RV64 margin here — and there is no routed result for
either row. "Meets 300 MHz in out-of-context synthesis" is the founded claim;
**neither row is closed timing, and no Fmax follows from either.**

The two runs also differ in where staging sits and in the processor's memory
sizes, and the RV32 row predates two changes to modules both configurations
share, so it is that configuration's last measurement rather than its current
cost. The breakdown and every caveat are in
[control-processor](control-processor.md#cost--measured). Per-port cost is in
[memory-port](memory-port.md#what-a-port-costs).

## Where today's source disagrees

- **The control agent is packaged with the router, and the interlink is packaged
  here** — [edge-and-control](edge-and-control.md#where-todays-source-disagrees).
- **The ship generator cannot select the RV64 complex** — `gen_mesh.py` emits no
  value for `CPU_RV64`, so every generated top takes the default RV32 branch.
  [control-processor](control-processor.md#where-todays-source-disagrees) has
  that and four more, including a doorbell path whose two ends decode different
  address ranges. What the RV64 branch does connect, and what it leaves to
  software, is
  [stated separately](control-processor.md#what-the-rv64-configuration-connects).

**Resolved, and kept because the shape of the fix is the useful part:** the
transform used to be welded into the slot — named directly in two framework
modules, its compression ratio hardcoded in the node's address arithmetic, its
selection bits named after one project's number format. The framework now names
exactly one module, `xform_bank`, geometry is declared by the occupant, and
`src/templates/transform/xform_bank.v` supplies an identity bank so a
framework-only build elaborates with no project source at all.
[integrate/addon-slots](../../integrate/addon-slots.md) has the extraction.

**Also resolved:** the mover's descriptor walker used to live in a project
package. It is at `src/kohakuaccel/sysnode/mover/mx_tdesc.v` now.
