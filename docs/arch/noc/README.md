---
title: Fabric
summary: The flit, the link, the router, and the port every compute unit attaches through.
tags:
  - architecture
  - noc
  - fabric
---

# Fabric

`src/kohakuaccel/noc/` — the on-chip network, and the socket a compute unit plugs
into.

## What it owns

Four things, and nothing else:

- **The flit.** One fixed-width message, one cycle on a link. A routing header
  and a payload the network never reads.
- **The link.** A `data` / `valid` / `busy` triple with a retry rule.
- **The router.** Five ports, dimension-order routing, round-robin arbitration.
- **The compute-unit port.** `noc_cu_base` — the module that makes an endpoint
  a legal node without its author writing any network logic.

The fabric moves messages between endpoints. It does not know what any of them
mean.

## The problem it solves

A machine with tens of compute units needs a way to get instructions to them
and operands in and out. The obvious answer is an AXI interconnect, and it is
the wrong shape twice over.

AXI4-Full wide enough to feed tens of units is a crossbar whose cost grows with
masters times slaves, and it carries machinery this kind of machine never uses:
out-of-order completion by ID, burst reordering, exclusive access, four
independent channel handshakes per transaction. AXI4-Lite drops all of that and
drops the bandwidth with it.

What is actually needed is narrower than either. One clock domain. Messages
that are one flit, or a short run of them. Traffic that is mostly
nearest-neighbour with gateways at the edge. A mesh built for exactly that is
smaller than an interconnect configured down to it.

The cost of the choice is that nothing off the shelf speaks it, so every bridge
to the outside world is written here rather than instantiated. That is what
[axi](../axi.md) is for.

## The pages

| Page | What is in it |
|---|---|
| [flits-and-links](flits-and-links.md) | the flit and its message classes, the link handshake and why both halves are mandatory, the two kinds of flow control and why credits live at the endpoints |
| [routing](routing.md) | the coordinate space, the edge ring and the clamp, and why XY routing makes deadlock-freedom a proof rather than a test result |
| [compute-unit-port](compute-unit-port.md) | `noc_cu_base`, the handshake a datapath is written against, the five properties that constrain it, and the measurement instrument that is not a template |
| [router-circuit](router-circuit.md) | what a router actually costs: input ports, output ports, and the four knobs that move the number |

If you are writing a compute unit, [compute-unit-port](compute-unit-port.md) is
the one that matters and [flits-and-links](flits-and-links.md) is the one that
will catch you out. If you are choosing a mesh shape, read
[router-circuit](router-circuit.md) first and then [ship](../ship/).

## Fixed protocol, addon, convention, or yours

| Thing | Category |
|---|---|
| the compute-unit port: its signals, its obligations, its handshake rules | **fixed protocol** — [spec/compute-unit-port](../../spec/compute-unit-port.md) |
| the flit header and the message classes | **fixed protocol** — [spec/flit-format](../../spec/flit-format.md) |
| the control-register block every unit answers | **fixed protocol** — [spec/control-registers](../../spec/control-registers.md) |
| the link handshake and its retry rule | **fixed protocol**. Not negotiable at any level |
| XY routing, and the acyclic argument behind it | **fixed protocol**. Changing it is designing a different fabric |
| **the endpoint-side L2 adapter**, between a router's local link and an endpoint | **customizable addon** — it presents the same six signals on both faces, so a pass-through is a straight wire and a staging or caching version drops into the same place |
| `FLIT_WIDTH`, `FIFO_DEPTH`, `MEMORY_TYPE`, `INST_DEPTH`, `RECV_MEM` | **customizable** — sized per instance; [spec/parameters](../../spec/parameters.md) |
| the conventions, in [flits-and-links](flits-and-links.md#conventions) and [compute-unit-port](compute-unit-port.md#conventions) | **convention** — one is forced, the rest are free |
| the flit layout *as currently enforced* | **fixed protocol, held by convention.** `noc_pkt.vh` is the protocol; nothing includes it, so agreement is by hand. See [flits-and-links](flits-and-links.md#where-todays-source-disagrees) |
| what an instruction *means* | **yours** |
| your unit's memories: count, width, depth, read latency, primitive | **yours**, entirely |
| how many credits your endpoint holds, and its reassembly buffer | **yours** |

## What this system does not own

| Not owned | Who owns it |
|---|---|
| what an instruction means | you, the compute-unit author |
| descriptors, addresses, memory semantics | [sysnode](../sysnode/) |
| DRAM, host DMA, anything AXI | [axi](../axi.md) |
| how many credits an endpoint holds, and its reassembly buffer | the endpoint. The fabric defines that credits are required, not how many |
| clock domain crossing | [axi](../axi.md). The fabric is one clock domain by construction |
| which coordinate a given endpoint occupies | [ship](../ship/) |
| where a router is placed, and what a link may cross | [physical](../physical/) |
| carrying flits between meshes | [ship](../ship/), through the interlink. The fabric ends at the mesh edge |

The last one is worth stating twice. A flit for another mesh is recognised at
the edge complex by a marker bit and handed to the interlink; the router never
knows another mesh exists. Extending the fabric across a die boundary was
tried and rejected on measurement — see [physical](../physical/).

## Where today's source disagrees

**`noc_orchestrator.v` is in `src/kohakuaccel/noc/` and is not part of this system.**
It is the control agent: an AXI slave, a staging RAM, an instruction
dispatcher, a credit counter and a status mirror. It owns a fabric local port,
which is presumably how it ended up here, but so does every compute unit. It is
instantiated by exactly one module — `mag.v` — and it belongs with the control
plane described in [sysnode](../sysnode/).

The other divergence in this system is larger and belongs with the thing it is
about: the flit layout is fixed protocol enforced only by convention, and the
convention has now failed twice. See
[flits-and-links](flits-and-links.md#where-todays-source-disagrees).
