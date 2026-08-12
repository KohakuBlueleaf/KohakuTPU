---
title: Architecture
summary: What is on the die, which system owns what, and how work flows from host to compute unit and back.
tags:
  - architecture
  - overview
---

# Architecture

This is the macro view. It says what exists in a KohakuAccel machine, which
system owns each piece, and what a unit of work does from the moment a host
asks for it to the moment the answer is back in DRAM.

Read this page first. Then read the system that concerns you: [noc](noc/) if
you are writing a compute unit, [mas](mas/) if you are deciding how it gets its
operands, [axi](axi.md) if you are attaching something from outside,
[ship](ship/) if you are assembling a device image, [physical](physical/) if
you are deciding where it all goes on the die.

One thing on this page is yours to write. Everything else is the framework.

## What is on the die

```
                                 host
                                   |
              PCIe / DMA           |          JTAG-AXI (bring-up)
                       \           |           /
                        \          |          /
             +---------------------------------------------+
             |          AXI surface        arch/axi        |
             |   address decode | clock crossing | width   |
             |   conversion | N-to-1 concentration         |
             +--+----------------+---------------------+---+
                |                |                     |
          memory window   control window        DRAM boundary
                |                |                     |
             +--v----------------v--+            +-----v-----+
             |     edge complex     |            |   DDR4    |
             |       arch/mas       |<---------->|controller |
             |                      |   AXI      | (vendor)  |
             |  memory ports        |            +-----------+
             |  control agent       |
             |  interlink endpoint  |------------> other meshes
             +----------+-----------+
                        |
             N attachments to the fabric
                        |
             +----------v----------------------------------+
             |             fabric        arch/noc          |
             |                                             |
             |     R --- R --- R      one flit per cycle   |
             |     |     |     |      per link             |
             |     R --- R --- R      XY dimension-order   |
             |     |     |     |      busy/valid + retry   |
             |     R --- R --- R                           |
             |                                             |
             |  every endpoint attaches through the same    |
             |  compute-unit port: noc_cu_base             |
             +----------+----------------------------------+
                        |
             +-------------------------------+
             |         compute unit          |  <-- YOURS, all of it:
             |  datapath | its own memories  |      datapath, memory
             |  its instruction semantics    |      system, pipeline,
             +-------------------------------+      ISA
```

A **ship** is one complete instance of that picture, elaborated for a specific
mesh shape and device region. A device image may hold several ships, one per
die region, joined at their edge complexes by the interlink.

## The five systems

Each system is defined by what it owns, not by which directory it currently
lives in. The framework is legible exactly to the extent that these boundaries
hold.

| System | Owns | Stops at |
|---|---|---|
| [**noc**](noc/) | the flit, the link, the router, the mesh coordinate space, and the port every endpoint attaches through | the meaning of what a flit carries |
| [**mas**](mas/) | the memory half of the instruction set, the ports that serve it, the transform stage on each memory path, and the multiplexing of every non-compute consumer onto the fabric's edge | the DRAM controller, what the transform computes, and what the bytes mean |
| [**axi**](axi.md) | the boundary to everything that is not the framework: host, DRAM, debug. Arbitration, clock crossing, width conversion, burst legality | anything that speaks flits |
| [**ship**](ship/) | assembly: turning a mesh picture into a module, and joining several meshes into one image | placement of what it assembled |
| [**physical**](physical/) | die regions, pblocks, clock domains, what may and may not cross a boundary | logic. It constrains; it does not compute |

Two of those names are historical. `noc` and `mas` are what the source calls
them and what the specs call them; read them as **fabric** and **memory agent**
if that helps.

## What the framework actually removes

Not the design work. The **connection** problem.

Writing a compute unit for an FPGA accelerator has always come with a second,
larger job attached: work out how this thing reaches memory, how instructions
get to it, how results get back, how it does not deadlock the machine, and how a
host ever sees any of it. That job is the same every time, it is where the
subtle failures live, and it has nothing to do with what you were trying to
build.

KohakuAccel does that job once. **You still write a whole compute unit** — the
datapath, its memory system, its pipeline, its instruction semantics. What you
never have to work out is how it connects.

The port is given. The protocol is given. Worked examples and stated conventions
show what a well-behaved unit looks like. Everything inside the unit is yours.

## Four kinds of thing

Parts of this framework have very different standing, and treating them alike is
the fastest way to either fight it or be surprised by it. Every system page ends
with a table sorting its parts into these four.

| | What it is | Can you change it |
|---|---|---|
| **Fixed protocol** | flit format, the port handshake, memory request and response encoding, credit and retry, cross-mesh encapsulation | **No.** Change it and you are off the framework |
| **Customizable addon** | ships working and is *designed* to be swapped or extended: the transform stage inside the memory agent, memory-agent staging of fetched lines, the endpoint-side L2 adapter, DRAM-port beat packing | **Yes** — that is what the slot exists for |
| **Convention** | how to design a thing well, backed by worked examples: L1 fill and response tagging, unit-to-unit messaging, how to spend your instruction bits | Follow or don't. Some are **forced by the memory agent's design**; the rest are genuinely free, and each page says which |
| **Yours** | the datapath, the memory structure, instruction semantics, pipeline depth, mesh shape and unit population, the compiler back end, the driver's device model | **Entirely** |

The normative form of row one is [spec/](../spec/README.md). Row two is where
the reference project plugs in its own pieces — KohakuTPU's numeric-format
quantiser sits in the memory agent's transform slot, and nothing about that slot
is specific to it.

Row three is the row most easily mistaken for one of the others. A convention is
not a specification and not a default implementation. It is *here is how we did
it, here is why, here is what breaks if you deviate.*

## You inherit a way of asking, not a memory system

An instruction flit's bits have three owners, and only the last is yours:

```
    [ routing header ]  [ memory request encoding ]  [ your payload ]
      arch/noc            arch/mas                     you
```

The machine already knows how to *say* fetch this region, in these entries,
transformed this way, delivered to these nodes; how to say write this back; and
how to say rearrange one region of memory into another. You do not design a
request format, and your compiler's back end schedules memory instructions it
did not have to define.

**This is about encoding and transport only.** It says nothing about what your
unit does with the data once it arrives — how many memories it has, how wide
they are, what their read latency is, how they are banked. That is your design,
and the framework has no opinion on it.

## What a compute unit is

The framework's whole shape follows from what it assumes a compute unit to be,
and the assumption is deliberately thin. A compute unit is anything that:

- attaches to one fabric port and accepts instructions one at a time;
- names the memory it wants ahead of time, as an address and a length;
- signals retirement, and can be asked its capabilities and its counters.

It is not assumed to be arithmetic. Nothing in `noc_cu_base`, `mag_mem_port` or
the router knows whether the datapath multiplies, sorts, or hashes.

**A compute unit includes its own memory system, and no two need resemble each
other.** Two units in the reference project share nothing internally:

| | matmul cluster | vector core |
|---|---|---|
| operand memory | two RAMs, **928 bits** wide — one per operand | one RAM, **256 bits** wide |
| read latency | 1 | 1, or 2 when built on the deeper primitive |
| other memories | a separate accumulator tile RAM at read latency **2** | a 32-bit instruction memory in distributed LUTRAM, and a register file of three mirrored RAMs to synthesise three read ports |

*(`mx_cluster_mgr.v`, `mx_acu_fp.v`, `vec_core.v`, `vec_regfile.v` — structural
facts about KohakuTPU's units, not framework fixtures.)*

Different widths, different counts, different read latencies, different storage
primitives, in one project. **L1 shape is not a framework fixture and nothing in
this tree documents it as one.** What is fully defined is how you receive and
send.

`src/kohakunoc/noc_cu_null.v` attaches to the fabric with no compute and no
memory at all, so the cost of *being connected* can be measured separately from
the cost of computing. It is a measurement instrument and not a starting
template — [noc](noc/) says why.

What the framework does assume about your workload, and what it costs you when
the assumption is wrong, is in
[docs/README](../README.md#does-your-workload-fit). What you have to write is
[integrate/compute-unit](../integrate/compute-unit.md); the normative
obligations are [spec/compute-unit-port](../spec/compute-unit-port.md).

## How work flows

One step of work, end to end. Nothing here is specific to what the compute unit
computes.

**1. The host places operands in DRAM.** It writes through the AXI surface into
the edge complex's memory window. The window is an AXI slave with its own
master behind it, so a long upload is one burst on the host side and whatever
the memory wants on the other. If the machine declares a transform on the
inbound path, it runs here, once per byte written, rather than once per read.

**2. The host stages a program and kicks it.** Instructions are written into a
staging RAM inside the control agent, again as ordinary AXI writes. The host
then names a destination coordinate and writes `PROG_KICK`. The agent reads the
staging RAM, stamps each instruction's routing header — destination from
`PROG_DST`, source with its own coordinates — and pushes the flit into the
fabric.

The agent stalls on **credit**, never on the network. A credit is one
instruction the target's queue can still hold. This is not throughput
smoothing: backpressuring an instruction into the mesh blocks whatever is
behind it, which may be the completion that would have freed the resource the
instruction is waiting for. Stalling locally is safe; stalling the network is
not.

**3. The instruction arrives at a compute unit.** It lands in that unit's
instruction FIFO inside `noc_cu_base`, which hands the datapath one instruction
at a time on an `inst_flit` / `inst_valid` / `inst_ready` handshake. The
framework remembers who sent it, so the unit never needs to be told where its
controller is.

**4. The compute unit asks for operands.** It emits a `MEM_RD_REQ` naming a
byte address, a length, and — if it wants a run of consecutive entries — a
count. The flit is routed to whichever memory port serves its row.

**5. The memory port fetches and streams back.** It issues AXI reads, runs the
fetch-path transform if the request asked for one — that stage is yours to
supply, see [mas](mas/) — and emits response flits each of which says where it
belongs: the requester's own transaction tag plus
this entry's position in the run, plus the word index within the entry. The
receiver needs no cursor, and arrival order stops being load-bearing. A request
may name extra destinations, so one fetch and one transform can serve several
consumers.

**6. The compute unit computes, then writes results.** A write is a descriptor
flit followed by data flits. The memory port matches data to descriptor by
source coordinate rather than by arrival order, because the mesh may put another
node's flit between them. The write ack is fire-and-forget: the unit does not
wait for it.

**7. The unit retires the instruction.** It raises `exec_done`, and
`noc_cu_base` queues a `CU_SIGNAL` back to whoever sent the instruction.
Completions are queued rather than held in a register, because a datapath can
retire faster than a congested link drains and one register would let each
completion overwrite the last — silently losing the credits they carry.

**8. The host sees it.** The agent does not queue completion signals for the
host to read; it summarises them into a per-node status mirror and a global
count. A host that never reads a mailbox therefore cannot wedge the control
plane, which is exactly what a queued-and-undrained mailbox would do.

## The three protocols, and where each stops

The framework is three protocols with hard edges between them. Most confusion
about "where does this belong" resolves by asking which protocol the thing
speaks.

| Protocol | Between | Unit | Flow control |
|---|---|---|---|
| **AXI4** | outside world and the framework | burst | `VALID`/`READY`, per channel |
| **flit** | endpoints on one fabric | 288-bit flit | `busy`/`valid` **with retry**, plus end-to-end credits |
| **descriptor** | a compute unit and memory | one entry, or a run of them | credits held by the requester |

The flit link handshake is not AXI's and the difference is not cosmetic. A
sender asserts `valid` and holds both `valid` and `data` until a cycle in which
`busy` is low; a receiver accepts if and only if `valid && !busy`. Both halves
are required. A sender that gives up loses a flit; a receiver that accepts
unconditionally duplicates one. Either failure is silent and lands several
modules away from its cause.

## Coordinates

One numbering runs through every system, so it is worth stating once.

A fabric position is `(x, y)` in a `2^POS_WIDTH` square. Routers occupy an
inner rectangle bounded by `GRID_LO` on both axes and `GRID_X_HI` / `GRID_Y_HI`
per axis. Endpoints sit either on a router's local port or just outside the
router rectangle, on the edge ring, where they are reached by the coordinate
clamp described in [noc](noc/).

When a coordinate has to be packed into one field — `PROG_DST`, the status
mirror index, a request's extra-destination list — it is `{y, x}` with `y` in
the high half. That packing is the same everywhere, and mismatching it is the
kind of error that presents as traffic arriving at a plausible wrong node.

## The rule that keeps this honest

Every page under `arch/` states what its system does **not** own and which
neighbour takes over. That section is not politeness. A framework is usable
exactly to the degree that a reader can predict where a given concern lives,
and the only way to make that predictable is to write the negative space down.

Where the current source disagrees with the decomposition described here, the
page says so. The documents lead; the tree follows.

## No numbers here

Framework pages carry no Fmax, LUT, FF, BRAM or utilisation figures. Those
describe one accelerator on one part. Where a number is unavoidable to explain
a decision it is marked as an example from the KohakuTPU reference instance on
`xcvu13p-fhgb2104-2L-e`, and it lives with that project in
[projects/kohakutpu/results](../projects/kohakutpu/results.md), not here.

Device facts — how many die regions a part has, how many hard memory
controllers, what a cascade may not cross — are not measurements and do appear,
on [physical](physical/).
