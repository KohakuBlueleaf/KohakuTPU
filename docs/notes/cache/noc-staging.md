---
title: Mesh staging
summary: A URAM node that speaks the existing memory protocol and sits on a local link — no router change, no new instruction, and placement freedom bought with per-node bandwidth.
tags:
  - notes
  - memory
  - mesh
  - research
---

# Mesh staging: URAM as an endpoint

A node that owns URAM, sits on a spare local port, and answers memory requests
like any other endpoint. No router change, no new instruction, no new protocol.

Status: discussion, nothing built.

## Why this is the cheap one

The mesh already carries memory read requests and responses between compute units
and the memory agent. A store that speaks the same flit types is indistinguishable
from the agent as far as a compute unit is concerned -- it is a memory that
happens to be two hops away instead of across the AXI fabric.

Concretely it reuses, unchanged:

- the compute-unit port base for the local port, instruction queue and receive
  queue
- the descriptor mechanism (one flit names a whole run; the server streams)
- the memory flit types and the transaction/word tagging that lets responses
  arrive out of order
- the address-range decode from [mag-staging](mag-staging.md) -- the L2 range
  simply resolves to a mesh coordinate rather than to agent-local URAM

**Nothing in the router is touched**, which matters: it is MEASURED at >= 450 MHz
(2.5 ns, +0.278 ns, 7 logic levels) and it is instanced many times, so a
regression there is expensive.

## Two forms, and the second is the important one

**Form 1: a new endpoint.** The store hangs off an otherwise-unused local port.
Simple, but it requires a spare local -- and not every mesh has one. A two-router
mesh carrying six clusters and one agent (MEASURED from the placed hierarchy) has
no room.

**Form 2: an adapter in the local link.** The store is inserted *between* a router
and an endpoint that already exists:

    router  <->  L2 adapter  <->  existing endpoint (cluster / vector / agent)

It consumes no local port, works on any mesh, and can be placed next to whichever
URAM columns are convenient. This is the form that generalises.

**Form 3: merged into an existing endpoint.** The compute unit itself owns URAM
and answers L2-range requests alongside its normal work. Costs no port and no
adapter, but it modifies the endpoint -- so it is not hot-pluggable, and every
endpoint type would need the change separately. Mentioned for completeness; form 2
dominates it unless the URAM must be inside the unit for another reason.

## Hot-plug: the property that makes form 2 worth it

The adapter should be insertable and removable **without changing the router, the
endpoint, or the protocol**. That is achievable here because the local port is a
narrow, symmetric interface -- six signals, the same shape on both sides:

    in :  data[FLIT_WIDTH]   valid    busy
    out:  data[FLIT_WIDTH]   valid    busy

An adapter presents the endpoint side of that interface to the router and the
router side of it to the endpoint. With the cache disabled it is a wire plus a
register stage, so **removing it is deleting an instantiation**, and adding it is
wrapping one.

Requirements for that to hold:

1. **No flit format change.** The adapter must not add fields, consume transaction
   ids, or require a new type. It decodes the reserved L2 address range out of the
   existing read-request header and is otherwise blind.
2. **Retry-based flow control preserved.** The sender holds `valid` until a cycle
   with `busy` low. The adapter must obey that on both faces and must never assert
   `busy` forever.
3. **Bypass must be free, not conditional.** A parameter that generates a straight
   wire, so an adapter that is present but disabled costs nothing and cannot
   misbehave.
4. **No dependence on which endpoint is behind it.** A cluster, a vector core and
   the agent all speak the same local-port protocol; the adapter must not care.

## Why this is deadlock-safe where router caching is not

[noc-auto](noc-auto.md) §3 flags deadlock as the hard problem: a router that
generates responses turns a pure forwarder into a source, and the mesh's freedom
argument has to be redone.

**The adapter does not have that problem**, because it sits on a *local link*, not
in the mesh fabric. A local link has exactly one producer and one consumer and
takes no part in routing or virtual-channel allocation. Injecting a response there
is indistinguishable, from the mesh's point of view, from the endpoint having
produced it -- which endpoints do constantly.

The obligation reduces to: never deadlock the single link it sits on. That is a
local property provable by inspection, not a network-wide argument.

## The enabler nobody has to add: responses are already order-independent

A fill response carries its own `{entry, word}` tags and the compute unit places
words wherever they land; nothing about placement depends on the order flits
arrive in ([projects/kohakutpu/isa.md](../../projects/kohakutpu/isa.md) §3).

This is what makes an adapter viable at all. It can serve some words from URAM
immediately while others are still coming back from DRAM, interleaved in any
order, and the unit reassembles correctly **with no change**. A design that
required in-order responses would need reordering buffers in the adapter; this one
does not.

## The spare local ports

The shipped meshes (MEASURED from the placed hierarchy) are a 2x2 router grid
carrying six clusters, two vector cores and one agent, and a 2x1 grid carrying six
clusters and one agent. The 2x2 has local ports to spare, and the reason that is
worth caring about is the same reason a cluster has one mesh port rather than two
([projects/kohakutpu/isa.md](../../projects/kohakutpu/isa.md) §2.1): eight
clusters at two locals each would force a 4x4 grid where one local each fits 2x4,
at **3,281 LUT per router** saved.

A staging node on an otherwise-unused local costs **one endpoint, zero routers**.
That is the whole argument for this option.

## Placement follows URAM columns, not topology

The reach problem in [mag-staging](mag-staging.md) §3 is what this option solves.
URAM sits in columns spread across the die; a centralised block cannot reach them
all at frequency, and the most crowded SLR is at 95.80% CLB (MEASURED) so there is
no room to route around it.

Distributed staging nodes are placed **next to the URAM columns they own**. Each
node is small -- a local port, an address decoder, and its URAM bank -- so each can
sit where its memory is. The mesh provides the reach that fabric routing could
not.

This also means the per-SLR URAM budget stops being a single-block constraint:
several nodes of 32-64 URAMs each are easier to place than one block of 160.

## Capacity and width

Same arithmetic as [mag-staging](mag-staging.md): one URAM288 is 288 Kb (36 KB),
natively 4096 x 72 b, and a 936-bit line (13 URAMs in parallel) delivers exactly
one L1 entry per read.

| node size | capacity | 936-b lines |
|---|---|---|
| 13 URAM | 468 KB | 4,096 |
| 26 URAM | 936 KB | 4,096 (1,872-b line) |
| 52 URAM | 1.9 MB | 4,096 x 4 banks |

Two or three such nodes per SLR reach the same 3.5-5.9 MB as a centralised L2,
with none of the reach problem.

## What it costs that agent staging does not

**Hops.** A request travels to the staging node and the response travels back. On
a 2x2 mesh that is 1-2 hops each way. At ~450 MHz router frequency this is a few
nanoseconds -- still far inside the 30-40 ns DRAM round trip (ASSUMED), but not
free, and it consumes link bandwidth that agent traffic would otherwise have.

**Bandwidth ceiling.** A local port is one flit per cycle. A 288-bit flit at
300 MHz is ~10.8 GB/s per node. A centralised L2 reading 13 URAMs in parallel
inside the agent is not limited this way -- it can hand a full 936-bit entry to the
fill path per cycle. **This is the real trade: mesh staging buys placement freedom
and pays in per-node bandwidth.**

Whether that matters depends on how many nodes share the load. Three nodes at
10.8 GB/s each is 32 GB/s, against ~77 GB/s of DDR4 -- so this only wins if the
hit rate is high enough that DRAM is not the limiter anyway.

## Inside the router, packaged differently

An adapter can equally be instantiated *inside* the router, wrapping its local
port rather than sitting outside it. Structurally identical -- same six signals,
same position in the path -- so everything above still holds.

The difference is packaging, and it cuts both ways:

- **For:** one module to instantiate instead of two; the router's local port and
  its cache are placed together by construction.
- **Against:** it puts the storage inside a module that is instanced many times at
  **3,281 LUT apiece** (MEASURED), so the bypass parameter has to genuinely
  optimise away or every router pays. And it ties the store's placement to the
  router's, which is the reach problem this option exists to avoid -- URAM columns
  and routers are not in the same places.

**Prefer outside.** The adapter's whole value is that it can be placed where the
URAM is; putting it inside the router gives that up for a packaging convenience.
Keep the option in mind only if the tools place a separate instance badly.

## Open questions

- Does the fill path want one wide response or several narrow ones? The compute
  unit already assembles an entry from four 256-bit words, so narrow is native --
  which weakens the bandwidth objection above.
- Can a staging node serve the shared-fetch multicast, or does that have to stay
  with the agent? If it can, the mechanism works unchanged and the node is a
  drop-in.
- Does the host reach these nodes? Under agent staging the host gets access via
  URAM port B; here it would have to go through the mesh, which means the AXI
  bridge must be able to issue memory flits.
