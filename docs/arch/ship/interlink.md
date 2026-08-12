---
title: Several ships in one image
summary: The second routing layer, the three structural properties of a link, what crosses a boundary, and the address map.
tags:
  - architecture
  - ship
  - interlink
---

# Several ships in one image

One mesh is bounded by how much fabric a die region can hold. Past that, the
answer is not a bigger mesh — it is several, joined at their edge complexes.

That decision was made on measurement rather than on argument, and the losing
option is instructive: a single mesh spanning several die regions was
implemented, and its worst path was almost entirely routing with no logic in it.
Stretching the fabric across a boundary does not fail because of wire count; it
fails because a fabric whose premise is locality stops having any. See
[physical](../physical/) for the general form, and
[projects/kohakutpu/ship](../../projects/kohakutpu/ship.md) for the worked
instance — the device, the mesh populations, the alternative that failed, and
what has actually been placed.

## The interlink is a second routing layer

It does not inherit the fabric's deadlock proof, so it gets its own by the same
argument: dimension-order routing on **mesh** coordinates over a rectangular
grid of meshes.

```
    mesh id -> (x, y) = (id[0], id[1])

        (0,0) mesh0 -- mesh1 (1,0)      link0 is the X neighbour
          |             |               link1 is the Y neighbour
        (0,1) mesh2 -- mesh3 (1,1)
```

X first, then Y. Two consequences, both load-bearing:

- A forwarded packet **always turns X into Y**, never Y into X. So link0's
  forward class feeds link1, and link1's forward class is provably dead — traffic
  there is the turn the model forbids, which makes it a fault to report rather
  than a case to handle.
- The channel dependency graph is X to Y and nothing else. Acyclic, hence
  deadlock-free — and only while the mesh-of-meshes stays a grid.

There are two links per mesh, not `N`. The mesh id is a fixed narrow field, so
adding a fifth mesh is a change to the message format rather than a parameter
change, and a port count that cannot vary should not be written as though it
can.

## Three structural properties of a link

Each is a rule rather than a preference, and each has a physical reason behind
it that belongs to [physical](../physical/) but shows up here as protocol.

**Nothing combinational crosses.** Every output is a register and every input is
registered before use. A die-boundary crossing register *is* a flip-flop, so the
tool can only use one when the path is flop to flop; one gate anywhere in the
crossing forfeits it and the path becomes ordinary interconnect.

**`TREADY` does not cross, and the sending end never reads it.** The receiver is
always ready because credit reserved the space before the beat was sent. Wiring
a real slave at the far end would put a combinational path back across the
boundary, which is the thing the whole arrangement exists to avoid — so a
simulation assertion watches for it.

**Credit is per class** — does this packet stop at the peer, or does the peer
forward it. One shared pool would let a stalled forward path stop traffic that
was going to terminate anyway. Credit returns are absorbed into a counter on
arrival and never enter a queue, so no credit return waits on the space it is
about to release.

One small uniformity is worth copying: every packet has at least one beat,
including the two that carry no data. Their beat is zero and ignored. One wasted
beat on two rare packet kinds removes a special case from the framing, both
queues, the arbiter and both benches.

## What crosses, and what "arrived" means

The endpoint carries three kinds of traffic and one rule:

- **Memory writes to another mesh's memory**, split out by address. They are
  answered locally and at once — a posted write is the entire point, since
  waiting for a far memory would put a boundary round trip inside a per-word
  loop.
- **Fabric flits marked for another mesh**, encapsulated at the sending edge and
  injected into the receiving mesh's fabric.
- **A doorbell**, which is the synchronisation primitive between meshes.

**Completion means landed.** An inbound doorbell waits for every write ahead of
it to have its write response before it counts, so a consumer released by a
doorbell is released by data that is in memory rather than in a queue. Without
that rule, posted writes and a doorbell are a race with no observable ordering.

**The source coordinate is preserved across a crossing.** Rewriting it to the
receiving edge's own coordinate would make two remote bursts arriving at one
endpoint indistinguishable — and telling senders apart is how a receiving unit
avoids merging two senders' data into one region. The cost is that "answer the
sender" no longer resolves, so a remote transfer must name its acknowledgement
destination explicitly, and a fault register reports one that does not.

A memory request from a compute unit naming another mesh is **not** forwarded.
It aliases to local memory with the mesh bits ignored, exactly as it would in a
single-mesh build, and a fault register records that a program did something the
compiler should have caught. That is a scope decision, not a limitation of the
transport: remote reads would need a return path with its own credit class.

## The address map

The map is a ship-level fact because it is the only place the whole machine is
visible at once. Its shape:

- Each mesh's **memory** occupies its own aligned segment. The high bits of a
  memory address therefore name the mesh, which is exactly what the address
  split uses to recognise a remote write — one field serving both the host's
  view and the interlink's.
- Each mesh's **control window** occupies its own segment in a separate region,
  as does each memory controller's own control interface.

A flit likewise carries a spare header bit meaning "this is for another mesh",
which is zero on every flit a single-mesh build ever produces. That is what lets
one compiler target both: the single-mesh case is the multi-mesh case with a
field left at zero, rather than a different encoding.

The mesh id itself is **writable at runtime**, with the elaboration parameter
supplying only its reset value. So several instances of the same generated
module can occupy different positions in the grid, and the instances differ by
configuration rather than by being different modules.

## What it costs

**Several ships in one image cost the interlink once per ship**, plus the
boundary crossing registers. Against a mesh, that is small. Against the
alternative — one mesh stretched across the same area — it is the difference
between a design that closes and one that does not.

Disabled, it costs nothing at all; see
[generation](generation.md#generation-is-elaboration-not-runtime).
