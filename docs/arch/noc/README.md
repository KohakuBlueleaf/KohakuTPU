---
title: Fabric
summary: The on-chip network — the flit, the link, the router, and the socket every compute unit plugs into.
tags:
  - architecture
  - noc
  - fabric
---

# Fabric

`src/kohakuaccel/noc/` — the on-chip network that carries instructions to
compute units and results back, and the socket a unit plugs into.

**Where it sits.** Above it is the [system node](../sysnode/), which turns
memory descriptors into DRAM traffic and dispatches instructions; the node is a
client of the fabric, not its owner. Below it are the **endpoints** — your
compute units, the memory ports, the control agent — each hanging off one
router port. Outside it, at the mesh edge, is the interlink to other meshes and
the [AXI surface](../axi.md) to everything that is not this framework.

Three terms this page uses throughout, defined once:

- a **flit** is one fixed-width message that occupies one link for one cycle.
  It is not fragmented and it is not a bus transaction with separate address
  and data phases — it is one word, moved once;
- an **endpoint** is anything attached to a router. A **compute unit** is the
  kind of endpoint you write;
- a **local port** is the fifth face of a router, the one that faces an
  endpoint rather than another router.

## What it owns

Four things, and nothing else:

- **The flit.** A routing header the network reads, and a payload it never
  does.
- **The link.** A `data` / `valid` / `busy` triple with a retry rule.
- **The router.** Five ports, dimension-order routing, rotating-priority
  arbitration.
- **The compute-unit port.** `noc_cu_base` — the shell that makes an endpoint a
  legal node without its author writing any network logic.

Two optional modules sit in the local link between a router and an endpoint,
and both present the same six signals on each face, so removing either is a
straight wire: `noc_l2_adapter`, explicit staging in the link, and
`noc_local_cdc`, one direction across two clocks.

**The fabric moves messages between endpoints. It does not know what any of
them mean.**

## The problem it solves

A machine with tens of compute units needs a way to get instructions to them
and operands in and out. The obvious answer is an AXI interconnect, and it is
the wrong shape twice over.

AXI4-Full wide enough to feed tens of units is a crossbar whose cost grows with
masters times slaves, and it carries machinery this kind of machine never uses:
out-of-order completion by ID, burst reordering, exclusive access, four
independent channel handshakes per transaction. AXI4-Lite drops all of that and
drops the bandwidth with it.

What is actually needed is narrower than either. One clock domain across the
routers. Messages that are one flit, or a short run of them. Traffic that is
mostly nearest-neighbour with gateways at the edge. A mesh built for exactly
that is smaller than an interconnect configured down to it.

The cost of the choice is that nothing off the shelf speaks it, so every bridge
to the outside world is written here rather than instantiated. That is what
[axi](../axi.md) is for.

## One clock per mesh, and one exception

**Router to router is one clock domain, and that is not negotiable** — the
deadlock argument in [routing](routing.md) is stated over a single synchronous
grid, and a clock boundary inside it would need flow control the argument does
not cover.

The **local** link is different. `noc_local_cdc` puts one asynchronous FIFO in
each direction of a local link, so an endpoint may run on its own clock while
the grid it attaches to does not. The generated meshes use this: every matrix
cluster takes one clock and every vector core another — **one rate per type,
not per instance** — and the routers stay on the fabric clock throughout.

Two properties make it safe:

- **Backpressure stays exact.** The local link is valid/busy with retry and no
  credit, so the busy signal is the FIFO's own write-full flag with no
  synchroniser in the path. A late `busy` would lose a flit, not merely gap the
  sender.
- **Depth is a throughput knob, not a correctness one.** Below the pointer
  round trip the full flag deasserts late and the sender gaps; nothing is lost.

So "the fabric is one clock domain" is true of the router grid and false of a
mesh as assembled. The distinction matters when you are reading a timing
report.

## The pages

| Page | What is in it |
|---|---|
| [flits-and-links](flits-and-links.md) | the flit, which of its fields belong to whom, the message classes, the link handshake and why both halves are mandatory, and the two kinds of flow control |
| [routing](routing.md) | the coordinate space, the edge ring and the clamp, the routing function as built, and a **complete deadlock proof** with what would break it |
| [compute-unit-port](compute-unit-port.md) | `noc_cu_base`, the handshake a datapath is written against, the six properties that constrain it, and the measurement instrument that is not a template |
| [router-circuit](router-circuit.md) | the router as built, what each stage costs, the knobs that move the number, and which figures here are actually reproducible |

If you are writing a compute unit, [compute-unit-port](compute-unit-port.md) is
the one that matters and [flits-and-links](flits-and-links.md) is the one that
will catch you out. If you are choosing a mesh shape, read
[router-circuit](router-circuit.md) first and then [ship](../ship/). If you are
reviewing the design, [routing](routing.md) is where the load-bearing claim is.

## Fixed protocol, addon, convention, or yours

| Thing | Category |
|---|---|
| the compute-unit port: its signals, its obligations, its handshake rules | **fixed protocol** — [spec/compute-unit-port](../../spec/compute-unit-port.md) |
| the flit header and the message classes | **fixed protocol** — [spec/flit-format](../../spec/flit-format.md) |
| the control-register block every unit answers | **fixed protocol** — [spec/control-registers](../../spec/control-registers.md) |
| the link handshake and its retry rule | **fixed protocol**. Not negotiable at any level |
| XY routing, and the acyclic argument behind it | **fixed protocol**. Changing it is designing a different fabric |
| **the endpoint-side L2 adapter**, between a router's local link and an endpoint | **customizable addon** — same six signals on both faces, so a pass-through is a straight wire and a staging version drops into the same place |
| **the local-link clock crossing** | **customizable** — present or absent per unit type, and absent by default |
| `FLIT_WIDTH`, `FIFO_DEPTH`, `MEMORY_TYPE`, `INST_DEPTH`, `RECV_DEPTH`, `RECV_MEM` | **customizable** — sized per instance; [spec/parameters](../../spec/parameters.md) |
| the conventions, in [flits-and-links](flits-and-links.md#conventions) and [compute-unit-port](compute-unit-port.md#conventions) | **convention** — one is forced, the rest are free |
| the flit layout *as currently enforced* | **fixed protocol, held by convention.** `noc_pkt.vh` is the protocol; nothing includes it, so agreement is by hand. See [flits-and-links](flits-and-links.md#where-todays-source-disagrees) |
| what an instruction *means* | **yours** |
| your unit's memories: count, width, depth, read latency, primitive | **yours**, entirely |
| how many credits your endpoint holds, and its reassembly buffer | **yours** |

## What this system deliberately does not do

Absence here is design, not backlog:

- **No virtual channels, and no priority.** One buffer per input port, one
  class of traffic on the wire. Message-class dependencies are resolved by
  end-to-end credit at the endpoints instead, which costs logic at the edges
  and nothing in the middle.
- **No packet reassembly.** A multi-flit message is a run of flits that arrive
  in order because the route is deterministic. The router never holds one flit
  waiting for another.
- **No adaptive routing, and no alternate path.** The route from A to B is
  fixed, which is what makes the deadlock argument a proof and what makes
  per-pair ordering free. A congested link is waited on, not routed around.
- **No error detection, retry above the wire, or timeout.** A unit that stops
  answering stops answering; the fabric has no watchdog and no notion of a
  failed delivery.
- **No broadcast or multicast in the router.** A request may name extra
  destinations, but that is the memory protocol's doing at the endpoint, not a
  fabric primitive.
- **No knowledge of other meshes.** A flit for another mesh is recognised at
  the edge complex by a marker bit and handed to the interlink; the router
  never learns another mesh exists.

## What this system does not own

| Not owned | Who owns it |
|---|---|
| what an instruction means | you, the compute-unit author |
| descriptors, addresses, memory semantics | [sysnode](../sysnode/) |
| DRAM, host DMA, anything AXI | [axi](../axi.md) |
| how many credits an endpoint holds, and its reassembly buffer | the endpoint. The fabric defines that credits are required, not how many |
| clock domain crossing at the *system* boundary | [axi](../axi.md). Inside a mesh, only the local link may cross — [above](#one-clock-per-mesh-and-one-exception) |
| which coordinate a given endpoint occupies | [ship](../ship/) |
| where a router is placed, and what a link may cross | [physical](../physical/) |
| carrying flits between meshes | [ship](../ship/), through the interlink. The fabric ends at the mesh edge |

Extending the fabric across a die boundary was tried and rejected on
measurement — see [physical](../physical/).

## Where today's source disagrees

**`noc_orchestrator.v` is in `src/kohakuaccel/noc/ctrl/` and is not part of this
system.** It is the control agent: an AXI slave, a staging RAM, an instruction
dispatcher, a credit counter and a status mirror. It owns a fabric local port,
which is presumably how it ended up here, but so does every compute unit. It is
instantiated by exactly one module — `mag.v` — and it belongs with the control
plane described in [sysnode](../sysnode/).

The other divergence in this system is larger and belongs with the thing it is
about: the flit layout is fixed protocol enforced only by convention, and the
convention has now failed twice. See
[flits-and-links](flits-and-links.md#where-todays-source-disagrees).
