---
title: Memory agent
summary: Descriptors in, DRAM traffic out, operand streams back — the memory half of the instruction set, and the two slots you plug into.
tags:
  - architecture
  - mas
  - memory
---

# Memory agent

`src/kohakumas/` — MAG, the Memory Access Gateway. The single point where one
mesh touches everything outside it.

## What it owns

Three things, and the third is the one people miss.

**The memory instruction set.** A compute unit does not design a way of *asking*
for memory; it inherits one. Read and write descriptors, entry geometry,
streaming runs, multi-destination delivery, and the memory mover's own command
set are all defined here. See [instruction-space](instruction-space.md).

That is the asking, and only the asking. **A compute unit's own memory system —
how many memories, how wide, how deep, at what read latency — is its author's
design and this system has no opinion on it.** Two units in the reference
project have operand memories of 928 and 256 bits and different memory counts;
both are ordinary clients here.

**The service behind those instructions.** Issuing the AXI bursts, running the
declared transform, streaming responses back as flits that say where they
belong, and reassembling write bursts the mesh delivered out of order.

**The edge complex.** A mesh has a small number of attachments to give away.
Memory traffic, the control plane and the inter-mesh link all need one, and
giving each its own would cost three times the ports for two consumers that are
nearly idle. Instead they share: inbound flits are demultiplexed by type,
outbound flits are steered by destination row. One coordinate, three consumers,
told apart by what the flit is rather than by where it went.

## The problem it solves

A compute unit should not contain a memory system. If it does, every unit
carries a copy of burst generation, 4 KB boundary handling, reassembly, and
whatever format conversion the machine uses — and each copy is a place to get it
wrong.

The split this system draws is between **naming** memory and **serving** it. A
unit names what it wants ahead of time, because that is the assumption the whole
framework rests on: addresses are computable, not discovered by chasing
pointers. Serving it — turning that name into bus traffic and getting the result
back in a form the unit can consume without bookkeeping — is here.

## The pages

| Page | What is in it |
|---|---|
| [instruction-space](instruction-space.md) | the instruction set you inherit: who owns which bits of a flit, what a read, a write and a mover command can express, and what that constrains in your compiler |
| [memory-port](memory-port.md) | the port as the unit the machine grows by — intake, the read engine and its self-describing responses, write slots matched by source, and what a port costs |
| [transform-stage](transform-stage.md) | the first addon slot: where a format conversion sits, how it is selected, what is fixed about it, and what the reference project plugs in |
| [edge-and-control](edge-and-control.md) | the second addon slot (staging), the share layer, the control agent, the host memory window and the mover |

If you are writing a compute unit, read [instruction-space](instruction-space.md)
first — most of what you were about to design is already there — then the
conventions in [memory-port](memory-port.md#conventions), which are the ones
this system forces on you.

## Fixed protocol, addon, convention, or yours

| Thing | Category |
|---|---|
| memory request and response encoding, tags, acks | **fixed protocol** — [spec/memory-protocol](../../spec/memory-protocol.md) |
| the mover's command set and descriptor form | **fixed protocol** |
| the transform stage's position, selection and handshake — the **slot** | **fixed protocol** |
| control-agent register map and dispatch mechanism | **fixed protocol** — [spec/control-registers](../../spec/control-registers.md) |
| **what plugs into the transform slot** | **customizable addon**. KohakuTPU's quantiser is one such plug-in |
| **staging of fetched lines inside the memory agent** | **customizable addon** — whether, how much, and with what behaviour |
| **DRAM-port beat packing** at the memory boundary | **customizable addon** — see [axi](../axi.md) |
| port count, port coordinates, slot count, queue depths, storage primitives | **customizable** — sized for correctness first; [spec/parameters](../../spec/parameters.md) |
| the conventions in [memory-port](memory-port.md#conventions) and [transform-stage](transform-stage.md#convention) | **convention** — four are forced by this system's design, three are free |
| what the bytes mean: layout, tiling, tensor semantics | **yours** |
| your unit's own memories and how it stores what arrives | **yours**, entirely |

## What a compute-unit author must know

1. **You inherit a memory instruction set.** Read
   [spec/memory-protocol](../../spec/memory-protocol.md) before designing
   anything about how your unit gets data. Most of what you were about to invent
   is there.
2. **Name what you want ahead of time.** Address, geometry, count. If your
   addresses are only knowable by following a pointer you have fetched, this
   system cannot serve you and neither can the framework.
3. **Responses are self-describing. Do not build a cursor.** Bin each response
   by its tag and word index.
4. **Write acks are fire-and-forget.** Do not wait for one. The slot count is
   sized on the assumption that you do not.
5. **Ask once for many consumers.** If several units want the same bytes, name
   the others as extra destinations rather than issuing several identical
   requests.
6. **Hold your credits yourself.** The memory agent does not track how many
   responses you can absorb. Issuing a request whose response you cannot take is
   how a fabric deadlocks.
7. **If your arithmetic wants a different in-memory format, write a transform —
   do not change your unit.** That is what
   [the slot](transform-stage.md) is for.

## What this system does not own

| Not owned | Who owns it |
|---|---|
| routing, links, arbitration between endpoints | [noc](../noc/) |
| the DRAM controller | vendor IP, reached through [axi](../axi.md) |
| clock crossing between the mesh and memory, and width conversion to the memory's beat | [axi](../axi.md) |
| **what the transform computes** | the addon's author. This system owns the slot |
| **whether and how fetched lines are staged** | the addon's author, likewise |
| what the bytes mean — layout, tiling, tensor semantics | you, and your compiler |
| **your unit's memory system** — count, width, depth, read latency, banking | the compute unit's author, entirely |
| what an instruction does after dispatch delivers it | the compute unit |
| how many memory ports exist and where they attach | [ship](../ship/) |
| which die region the ports and their AXI masters land in | [physical](../physical/) |
| carrying traffic between meshes | the interlink, described in [ship](../ship/). This system hosts its endpoint; it does not define its protocol |

One boundary here is drawn more finely than the module currently is, and it is a
deliberate claim rather than a description of the file: **the control agent is a
separate system that MAG hosts.** It shares the memory ports because attachments
are scarce, not because dispatch is a memory concern. Everything about it —
staging, credits, the status mirror, the mailbox — would be unchanged if the
memory ports were replaced wholesale.

## Where today's source disagrees

Each divergence is written up on the page it concerns:

- **The transform addon is welded into the slot** —
  [transform-stage](transform-stage.md#where-todays-source-disagrees).
- **`mm_mover.v` reaches into a project package for `mx_tdesc.v`** —
  [instruction-space](instruction-space.md#where-todays-source-disagrees).
- **The control agent is packaged with the router, and the interlink is packaged
  here** — [edge-and-control](edge-and-control.md#where-todays-source-disagrees),
  which also covers `mag_dram_port.v` not being instantiated by `mag.v`.
