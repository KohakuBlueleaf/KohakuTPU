---
title: Several ships in one image
summary: The second routing layer, the three structural properties of a link, what crosses a boundary, and the address map.
tags:
  - architecture
  - ship
  - interlink
---

# Several ships in one image

One **mesh** — a grid of routers with endpoints attached, the fabric a ship is
built around — is bounded by how much fabric one SLR can hold. An SLR is one die
of the several this part is built from. Past that bound, the answer is not a
bigger mesh: it is several meshes, joined at their system nodes by an
**interlink**.

That decision was made on measurement rather than on argument, and the losing
option is instructive: a single mesh spanning several SLRs was implemented, and
its worst path was almost entirely route delay with no logic in it. Stretching
the fabric across a boundary does not fail because of wire count; it fails
because a fabric whose premise is locality stops having any. See
[physical/where-the-boundary-falls](../physical/where-the-boundary-falls.md) for
the general form, and
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
crossing forfeits it and the path becomes ordinary interconnect. See
[physical/device-facts](../physical/device-facts.md#hard-limits-that-are-correctness-rules).

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
  injected into the receiving mesh's fabric. A **flit** is the mesh's unit of
  transfer — one fixed-width word carrying its own header, defined bit-exactly
  in [spec/flit-format](../../spec/flit-format.md).
- **A doorbell**, which is the synchronisation primitive between meshes: a
  single message a consumer waits on to learn that a producer's writes have
  landed.

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

## Its accept decision must not reach the fabric

This is a structural rule about where the interlink's flow control may look, and
it is the one place the interlink's design is dictated by the fabric next to it.

**A flit's fields are sliced straight off a router's output register.** So any
term in the interlink's accept decision that *inspects* those fields — a
packet-match comparing the flit's mesh id, transaction type and source; an
arbiter whose grant is the write-accept — puts that router's own `ready` inside
the interlink's combinational cone. From there it reaches the next router's
memory enable, and the chain zig-zags router → node → router → node → router
across three levels of hierarchy. Cells that are logically far apart get placed
far apart, so the result is a path that is mostly route delay: a placement
failure produced by a logic decision.

**The fix is a skid buffer at every such point**, chosen for one property — its
input-ready is never a function of its output-ready. That breaks the cone at the
boundary rather than pipelining what is behind it. Applied at both the landing
channel and the encoder, it takes the mesh from failing to positive worst slack
in **out-of-context synthesis** of the whole mesh top — `scripts/py/ooc_mesh.py`
on `ktpu_ship_2x2_6c2v_1m`, tag `_pe`, `xcvu13p-fhgb2104-2L-e`, Vivado 2024.2,
every clock at 3.333 ns, `-directive default`. **Synthesis slack is optimistic
and a positive number there is not a closed one** — see
[physical/measurement](../physical/measurement.md).

The extra cycle a skid costs is free here, and for a reason that belongs to the
protocol rather than to the fix: cross-mesh traffic is push-only and
synchronises on the doorbell, never on a producer going idle. Nothing observes
the latency.
