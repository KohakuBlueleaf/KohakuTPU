---
title: The system node
summary: MAG, the memory mover and a control processor as one block — what makes a mesh an SoC rather than an accelerator with a host attached, and why the mover became an execution unit of the CPU instead of a peer.
tags:
  - architecture
  - sysnode
  - memory
  - soc
---

# The system node

`src/kohakuaccel/sysnode/` — one per mesh, and the single point where a mesh
touches everything outside it.

**It is a system node, never a "node".** A NoC endpoint is a node, every compute
unit sits on one, and the two are different things at different scales. Where
this tree says `sysnode` it means this block; where it says node it means an
endpoint.

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

The processor is a compute unit on the mesh at its own coordinate, so load, fire
and observe are the ordinary sequence — a driver enumerates it without knowing
it is a processor. See [control-processor](control-processor.md).

## 2. A CPU for memory management makes the mover worth having

The memory mover walks N-dimensional strided descriptors with bound axes. On its
own it is a good engine with an awkward interface: somebody has to compose seven
register writes, in order, and know when it is finished.

**So the mover is not a peer with a doorbell — it is an execution unit of the
CPU.** `mv.go` is a store to an address, the descriptor is ordinary program
data, and program order *is* the queue. The consequences are the point:

| as a peer | as an execution unit |
|---|---|
| a command window somebody must drive | a store, decoded off the L1 request path |
| descriptors are host register writes | descriptors are data a program builds |
| ordering is a protocol | ordering is program order |
| the aux command window is architecture | the window disappears |

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

**The edge complex.** A mesh has few attachments to give away. Memory traffic,
the control plane and the inter-mesh link all need one, and giving each its own
would cost three times the ports for two consumers that are nearly idle. Instead
they share: inbound flits demultiplexed by type, outbound steered by row.

**The control processor and the mover**, per §1 and §2.

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
| [simd-model](simd-model.md) | **the design view** — processor as scalar, mover as its SIMD unit, slot as that unit's extension; where the slot sits, the register and fault contract, four worked transforms |
| [control-processor](control-processor.md) | the RV32 core: why it is here, how it addresses memory, the mover as an execution unit, and what it measured |
| [instruction-space](instruction-space.md) | the instruction set you inherit: who owns which bits of a flit, what a read, a write and a mover command can express |
| [memory-port](memory-port.md) | the port as the unit the machine grows by — intake, the read engine, write slots matched by source, and what a port costs |
| [transform-stage](transform-stage.md) | the transform slot: one bank **on the mover's read return**, reached only through descriptor mode 5, occupant selected by an id |
| [edge-and-control](edge-and-control.md) | staging, the share layer, the control agent, the host memory window |

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
| whether the control processor is generated | **customizable** — `CTRL_PE`; 3,220 LUT and zero slack at two ports |
| port count, coordinates, slot count, queue depths, primitives | **customizable** — [spec/parameters](../../spec/parameters.md) |
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

One boundary is drawn more finely than the module is, and it is a claim rather
than a description: **the control agent is a separate system the node hosts.**
It shares the memory ports because attachments are scarce, not because dispatch
is a memory concern.

## What the node still cannot do

Stated here because the SoC framing above invites the question. **A system node
has no master port onto the station bus**, so its processor can drive its own
mesh and push into a peer's memory over the interlink, but it cannot write a
peer's control registers, retune a peer's clocks or reset a peer. Those remain
host operations. A processor per node makes multi-mesh orchestration easier; it
does not yet make one node a manager of the others.

## Where today's source disagrees

- **The control agent is packaged with the router, and the interlink is packaged
  here** — [edge-and-control](edge-and-control.md#where-todays-source-disagrees).

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
