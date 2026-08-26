---
title: Glossary
summary: Every project-specific term in one place — what it is, where it sits, and which page covers it properly.
tags:
  - overview
  - glossary
---

# Glossary

Terms used across this tree that do not mean what they might mean elsewhere, or
that name something specific to this machine. Each entry says what the thing is,
where it sits, and which page to read for the real account. Nothing here is
normative: where an entry and a [spec/](spec/README.md) page disagree, the spec
is right and this page is the bug.

**Read the four kinds first.** Every part of this framework is one of **fixed
protocol**, **customizable addon**, **convention** or **yours**, and entries
below are tagged with which. [README](README.md) defines them; the four also
have their own entries here.

---

### addon slot

A place the framework deliberately leaves open: it names a module, gives that
name a default occupant that works, and never looks inside. The shipped slots are
the [transform slot](#transform-slot) on the mover's read-return path, staging
inside the system node, the [L2 adapter](#l2-adapter) at a mesh endpoint, and
beat packing at the DRAM boundary. A slot is the only shape in which the
framework may mention something it does not own.
→ [integrate/addon-slots](integrate/addon-slots.md).
*(The slot is **fixed protocol**; what fills it is a **customizable addon**.)*

### aperture

The high bit of the address that selects something other than DRAM. With it set,
a further field names *which* non-DRAM region is meant; the on-chip
[staging](#staging) store is the one that exists today. An address to an
unimplemented aperture faults, which is unlike an ordinary address past the end
of memory.
→ [address-map](address-map.md). *(**Fixed protocol**.)*

### buf_id

The field in a unit-to-unit transfer naming *which* buffer of the destination
unit the data is for. It is what lets the network carry a payload addressed as
*(which buffer, where in it, how much)* without knowing what a buffer index means
— an abstraction that holds equally for a unit with two operand buffers and one
with four. It is a **framework namespace**, not a free field: some indices are
reserved and a unit does not pick its own numbering.
→ [spec/flit-format](spec/flit-format.md). *(**Fixed protocol**.)*

### completion

The signal flit a compute unit emits when an instruction retires. It is what
returns a dispatch [credit](#credit) to whoever issued the instruction, which is
why emitting one is not optional — a unit that computes correctly and never
signals will stall its own dispatcher. Completions are summarised by the
[control agent](#control-agent) into a per-node status word and a running count
rather than being queued.
→ [spec/control-registers](spec/control-registers.md). *(**Fixed protocol**.)*

### compute unit

The block you design and attach to a mesh [endpoint](#node) — the datapath, its
memories, its pipeline and what its instructions mean. It is the only part of a
machine you have to write; everything around it is the framework. Two compute
units on the same mesh need agree on nothing but the port they attach through.
→ [integrate/compute-unit](integrate/compute-unit.md) for how to write one,
[spec/compute-unit-port](spec/compute-unit-port.md) for the contract.
*(The unit is **yours**; the port it attaches through is **fixed protocol**.)*

### control agent

The host's reach into a mesh: an AXI slave on one side and a mesh endpoint on the
other, living inside the [system node](#system-node). It offers a raw flit
mailbox that can inject anything including a malformed flit, instruction dispatch
from a staged program, and a status mirror of completions. The RTL module is
named for orchestration rather than for control, which is a naming drift rather
than a second thing.
→ [arch/sysnode](arch/sysnode/README.md). *(**Fixed protocol**.)*

### convention

One of the four kinds. A convention is how the shipped parts happen to agree —
"here is how we did it, here is why, here is what breaks if you deviate" — and
you may deviate. It is neither a specification nor a default implementation, and
each one in this tree says whether it is *forced* (practically unavoidable
because the machine hands you data in a shape) or *free*. Mistaking a convention
for a contract wastes effort obeying a suggestion; mistaking a contract for a
convention produces traffic that routes plausibly and means something else.
→ [README](README.md), [integrate/what-you-own](integrate/what-you-own.md).

### credit

The end-to-end flow control that keeps the fabric from deadlocking. Hop-by-hop
backpressure stops a buffer overflowing; credit stops a *protocol* deadlock,
where a node's input is full of requests and it cannot inject the response that
would drain them. The rule is that nobody issues a request whose response they
cannot absorb, and nobody dispatches an instruction the target's queue cannot
hold — so credits live at the endpoints and the router contains no counter at
all.
→ [arch/noc](arch/noc/README.md). *(**Fixed protocol** that credit is held; how
many you hold is **yours**.)*

### customizable addon

One of the four kinds. Something that ships working and is *built* to be swapped
rather than merely tolerated being swapped — see [addon slot](#addon-slot). The
default occupant is a starting point, not a decision.
→ [README](README.md).

### descriptor

A description of an address sequence — base, count, layout — rather than a single
address. It is the reason one flit produces a whole burst instead of one request
per word, and it is why this framework needs addresses that are computable ahead
of time rather than discovered by following a pointer. The [mover](#mover) walks
richer ones: strided, multi-dimensional and bounded, so an element outside the
tensor is defined rather than undefined.
→ [spec/memory-protocol](spec/memory-protocol.md),
[arch/sysnode](arch/sysnode/README.md). *(**Fixed protocol**.)*

### doorbell

A write whose *arrival* is the signal, rather than its value. The
[interlink](#interlink) uses one to tell another mesh that data pushed to it has
landed, and it is a count rather than a sticky flag so a reader polling more
slowly than events arrive can tell how many it missed. Its contract is that
completion means *landed*: an inbound doorbell waits for the writes ahead of it to
be acknowledged by memory before it counts, so a consumer released by a doorbell
is released by data in memory and not data in a queue.
→ [arch/ship/interlink](arch/ship/interlink.md). *(**Fixed protocol**.)*

### endpoint

See [node](#node).

### fixed protocol

One of the four kinds. Something you must obey: the flit format, the compute-unit
port handshake, memory request and response encoding, credit and retry,
cross-mesh encapsulation. Change one and you are not on this framework any more.
→ [README](README.md), and [spec/](spec/README.md) for the normative statements.

### flit

The unit of everything on the on-chip network: one fixed-width word carrying a
routing header and a payload, moved in one cycle. Nothing on the network is
smaller and nothing spans two cycles. The router reads the destination
coordinates and nothing else — every other field is opaque to it, which is what
keeps the router small and lets the message set change without touching routing
logic. The width is a build-time parameter, not a constant.
→ [arch/noc/flits-and-links](arch/noc/flits-and-links.md) for the architecture,
[spec/flit-format](spec/flit-format.md) for the bit-exact layout.
*(**Fixed protocol**.)*

### granule

The common unit of data size across the machine: the flit payload, the mover's
internal word and the memory agent's internal beat are all one granule, on
purpose. Offsets in a unit-to-unit transfer count granules rather than bytes.
A write narrower than one granule at an endpoint that does not honour byte
strobes takes the rest of that granule with it, which is a bring-up hazard rather
than an architectural feature.
→ [integrate/README](integrate/README.md),
[workflow/bringup](workflow/bringup.md). *(**Fixed protocol**.)*

### hub

The demultiplexer inside the [system node](#system-node) that lets everything in
the node share the node's mesh attachments. **The system node has attachments;
nothing inside it does** — the memory engines, the control agent, the interlink
and the control processor are all clients of the hub. It classifies inbound flits
by destination and type and steers outbound ones by mesh row, with separate
arbiters per client so a stalled one cannot hold up the others.
→ [arch/sysnode/edge-and-control](arch/sysnode/edge-and-control.md).
*(**Fixed protocol**.)*

### interlink

The second routing layer, joining several meshes in one device image at their
edges. It does not inherit the fabric's deadlock proof, so it earns its own the
same way — dimension-order routing, but on *mesh* coordinates. It carries three
things: memory writes bound for another mesh's memory, fabric flits marked for
another mesh, and doorbells. Cross-mesh traffic is write-only; a memory *read*
naming another mesh is not forwarded.
→ [arch/ship/interlink](arch/ship/interlink.md). *(**Fixed protocol**.)*

### kick

The host write that launches a staged program at a node — one kick, one
destination. The registers that set up a kick have a mandated write order,
because several of them share a flit and a write to one arrives at the others;
a driver that elides unchanged writes must not elide these.
→ [spec/control-registers](spec/control-registers.md). *(**Fixed protocol**.)*

### L2 adapter

An addon that sits between a router's local link and an [endpoint](#node),
presenting the same signals on both faces. Because both faces are identical, the
default is a straight wire and a staging or caching version drops into the same
place with nothing else changing. It is one of the answers to the caching
question this framework leaves open rather than settles.
→ [arch/noc](arch/noc/README.md), [notes/cache](notes/cache/README.md).
*(**Customizable addon**.)*

### logic levels

The count of LUT and carry stages a signal passes through between two registers,
reported per timing path. It is the diagnostic that says what *kind* of timing
failure you have — a deep path is a logic problem and pipelining fixes it; a
shallow path that still fails is a distance problem and placement fixes it. It is
a screen rather than a verdict, because a level is not a fixed delay.
→ [workflow/timing-closure](workflow/timing-closure.md).

### MAG

The *memory access gateway*: the half of the [system node](#system-node) that
serves memory requests, drives DRAM and carries cross-mesh traffic. It turns
descriptors into memory traffic and answers every memory request on its mesh, and
it has no mesh attachment of its own — it reaches the mesh through the
[hub](#hub) like every other client of the node. A compute unit never contains a
memory system; it names what it wants and MAG serves it.
→ [arch/sysnode](arch/sysnode/README.md). *(**Fixed protocol**.)*

### map

The plain-text picture of a mesh: a grid of tokens, one per position, saying what
hangs off each port of each router. A generator reads it and emits a synthesisable
top. It is deliberately readable as a picture, unknown tokens are rejected by name
with a message saying what to write instead, and a position that cannot exist must
be explicitly empty so a mis-shaped map is caught rather than shifted.
→ [workflow/build](workflow/build.md),
[integrate/mesh-topology](integrate/mesh-topology.md). *(**Convention**.)*

### mesh

One grid of routers, the endpoints hanging off them, and exactly one
[system node](#system-node) with its own DRAM behind it. A device image may hold
several, joined by the [interlink](#interlink). A mesh is one clock domain by
construction, and the fabric ends at the mesh edge — a router never knows another
mesh exists.
→ [arch/noc](arch/noc/README.md),
[integrate/mesh-topology](integrate/mesh-topology.md).

### mover

The descriptor-driven copy engine inside the [system node](#system-node): it
reads memory and writes memory, walking strided multi-dimensional descriptors,
and it never talks to a compute unit. It has no mesh attachment. It is not a peer
with a command queue but an **execution unit of the node's control processor** —
a descriptor is built by ordinary stores and program order is the queue — which
is what makes a transpose or a gather a descriptor rather than missing hardware.
→ [arch/sysnode/simd-model](arch/sysnode/simd-model.md). *(**Fixed protocol**.)*

### NMU / NSU

The two shims on the [station bus](#station--station-bus): a master joins through
an NMU, a slave is driven by an NSU. They are where width, clock domain and
protocol differences are resolved — once per port, never pairwise between ports,
which is what makes per-port cost independent of how many other ports exist.
→ [projects/kohakuaxi/station-bus](projects/kohakuaxi/station-bus.md).

### NoC

The on-chip network — the routers, the links between them and the port a compute
unit attaches through. In this tree "the fabric" and "the NoC" are the same
thing, and both mean the inside of a [mesh](#mesh); the AXI plumbing outside a
mesh is the [station bus](#station--station-bus) and is a different system.
→ [arch/noc](arch/noc/README.md).

### node

An attachment point on a mesh, at a coordinate — a NoC endpoint. Your compute
unit is one. **A node is never the [system node](#system-node)**: the two are
different things at different scales, and this tree keeps the words apart
deliberately.
→ [arch/noc](arch/noc/README.md).

### OOC — out-of-context

Synthesising one module alone against a part, constrained with a clock you
invent, rather than as part of a whole device build. It is the framework's
central measurement practice because it costs minutes where an implementation run
costs hours, and it answers exactly one question: is this block's logic depth
compatible with the frequency I want. It says nothing about placement, routing or
the assembled machine, and an out-of-context frequency is never a closed-timing
figure.
→ [workflow/measure](workflow/measure.md),
[arch/physical/measurement](arch/physical/measurement.md).

### PE — processing element

A compute unit that fetches and executes instructions, rather than one driven by
a fixed dispatch word. The term carries two senses in this tree and context
separates them: the **control PE** is the processor inside the system node that
handles dispatch and memory management, while a **SIMD PE** or **SIMT PE** is one
of the programmable compute units built as a project on this framework.
→ [arch/cpu](arch/cpu/README.md) for the processors,
[projects/kohakumpe](projects/kohakumpe/README.md) for the programmable units.

### ship

One complete, self-contained accelerator assembly — mesh, system node, host
interface — elaborated for a specific mesh shape and floorplanned for a specific
device. Everything inside is fixed at elaboration; everything crossing its
boundary is AXI, plus one clock and one reset. That boundary shape is what makes
a ship droppable into a vendor block design without hand-wiring.
→ [arch/ship](arch/ship/README.md).

### SLL

The dedicated wire crossing an [SLR](#slr) boundary. The budget per boundary is a
total shared between both directions, not a per-direction figure, and an SLL joins
only *adjacent* SLRs — which is why a set of meshes spread across a stack of dice
forms a line rather than an arbitrary graph. In practice SLL count is rarely the
binding constraint; the crossing's timing is.
→ [arch/physical/floorplan](arch/physical/floorplan.md).

### SLR

Super Logic Region — one of the several fabric dice joined by an interposer inside
a large FPGA package. Most of this tree calls it a *die region* rather than by the
vendor's acronym. Which SLR a block lands on is the largest single lever on timing
in a design of this size, and a compute unit cannot span a boundary between two.
→ [arch/physical/floorplan](arch/physical/floorplan.md),
[workflow/timing-closure](workflow/timing-closure.md).

### staging

An on-chip store inside the [system node](#system-node), reached by ordinary
addresses through the [aperture](#aperture) bit rather than by any new
instruction. **It is not a cache**: no tags, no associativity, no replacement, no
coherence and no write policy. Where the store sits decides who can reach it — one
store on the converged path is shared by every requester, while a copy inside each
memory port is reachable by that port's flit traffic alone and is therefore not a
shared store at all.
→ [arch/sysnode/edge-and-control](arch/sysnode/edge-and-control.md).
*(**Customizable addon**.)*

### station / station bus

The AXI fabric *outside* a mesh, carrying host traffic across the card. It is a
line of identical stations, each with any number of local masters and slaves and
exactly two neighbours — **there is no root**. It replaces a large crossbar,
whose cost grows with masters times slaves, with a structure whose per-port cost
does not depend on the port count. Where an AXI master joins is a *manager*;
where a slave hangs off is a *subordinate*.
→ [projects/kohakuaxi/station-bus](projects/kohakuaxi/station-bus.md),
[arch/axi](arch/axi.md).

### system node

The component every mesh has exactly one of, and the single point where a mesh
touches anything outside it: [MAG](#mag), the [mover](#mover), the
[transform slot](#transform-slot), the [interlink](#interlink), a control
processor and the [hub](#hub) they all share. **It is a system node, never "a
node"** — see [node](#node). Its two halves do not ship separately: the memory
half cannot start work without a host round trip, and the processor alone cannot
reach memory or another mesh.
→ [arch/sysnode](arch/sysnode/README.md).

### transform slot

The socket on the [mover](#mover)'s read-return path where a format conversion
may be plugged in, selected by an id in a descriptor. The framework owns the
slot's position, its selection mechanism and its handshake; a project supplies
what fills it, and the framework never names the occupant. Because it sits on the
mover's return path, operands arrive at a compute unit already in their final
form — a fetch is never transformed.
→ [arch/sysnode/transform-stage](arch/sysnode/transform-stage.md),
[spec/transform-slot](spec/transform-slot.md).
*(The slot is **fixed protocol**; the occupant is a **customizable addon**.)*

### WNS — worst negative slack

The worst timing margin in a design, across every path: how much time is left on
the tightest path, negative if it missed. It is the headline number of a timing
report and it is the least actionable one — it names how far you missed, not what
to fix, and a large design has a whole plateau of paths within a few percent of
it.
→ [workflow/timing-closure](workflow/timing-closure.md).

### XPM

The vendor's parameterised macro library — the FIFOs and memory primitives this
tree instantiates rather than infers. Naming a memory primitive explicitly
through a wrapper, instead of writing an array and hoping synthesis infers the
right thing, is a convention with real consequences: the primitive choice sets
the read latency, and read latency sets pipeline depth, which is a design
decision rather than a synthesis outcome.
→ [workflow/tooling-traps](workflow/tooling-traps.md). *(**Convention**.)*

### yours

One of the four kinds, and the largest one. The datapath, the memory structure,
what the instructions mean, the pipeline depth — the framework has no opinion and
no template. What is fully defined is how you receive and how you send; the rest
is the part you presumably came here to design.
→ [README](README.md), [integrate/what-you-own](integrate/what-you-own.md).

---

## Where else the words are defined

Three pages carry their own vocabulary tables for the readers who land there
first, and they are the fuller accounts rather than competing ones:
[integrate/README](integrate/README.md) for the build-against surface,
[arch/sysnode](arch/sysnode/README.md) for the node's own terms, and
[address-map](address-map.md) for the addressing four. This page is the
tree-wide destination; those are local orientation for a specific section.
