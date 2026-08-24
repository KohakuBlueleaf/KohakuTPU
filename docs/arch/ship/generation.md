---
title: Generation — the mesh is a picture
summary: The token grammar, what the generator emits, and the conventions for choosing a shape.
tags:
  - architecture
  - ship
  - generation
---

# Generation: the mesh is a picture

A mesh is described as a grid of fixed-width tokens:

```
    xxx  vec  vec  xxx
    mag  mat  mat  xxx
    mag  mat  mat  xxx
    xxx  vec  vec  xxx
```

The interior is the router grid. The first and last row are the north and south
edge rings, the first and last column the west and east edge rings. Routers sit
at `(1..NX, 1..NY)`; edge endpoints sit just outside, at `(x,0)`, `(x,NY+1)`,
`(0,y)` and `(NX+1,y)` — reachable precisely because of the coordinate clamp in
[noc](../noc/routing.md).

Three token kinds are structural rather than project-specific:

| Token | Meaning |
|---|---|
| `xxx` | nothing here. Required at the four corners, which touch no router |
| `nul` | the port exists but is tied off — how a side is left empty without changing the grid's shape |
| `mag` | one of the **system node's** fabric attachments |

The rest name endpoint types, and those are supplied by the accelerator being
built.

**There is no token for the node's control processor.** The node is one
component, its clients share the `mag` attachments through `sn_hub`, and the
processor answers at `(0,0)` — a corner, which touches no router and which the
table above requires to be `xxx`. The coordinate is free by construction in
every mesh of every shape, so the processor costs no attach point and nothing
places it. A map carrying the retired `cpu` token is rejected by name.

The generator emits the router instances with their per-axis grid bounds, the
link nets between them, the endpoint instances, the edge complex with its port
coordinates, and the AXI boundary. Non-square grids fall out of it, which is why
the router takes `GRID_X_HI` and `GRID_Y_HI` separately rather than one square
bound.

Two properties of the generated file matter more than its contents:

**It is generated, and says so.** A hand-edit is lost on the next regeneration,
and a topology that exists only as edited Verilog cannot be checked against
anything.

**One picture is the single source of the topology.** The coordinates the
software stack needs — where each endpoint is, which rows the memory ports are
on, what the grid bounds are — are the same coordinates synthesis consumed. Any
second description of the same topology is a place for the two to drift.

Choosing a shape, and what it costs, is
[integrate/mesh-topology](../../integrate/mesh-topology.md).

## Generation is elaboration, not runtime

A generated ship has no configuration registers for its shape. Every coordinate,
grid bound and endpoint type is a parameter resolved at synthesis, so the cost
of an unused feature is zero rather than small.

The clearest example is the interlink. With it disabled, every one of its nets
is tied to a constant, every use folds, and the generated top does not expose
the ports at all — the resulting build is identical to one made before the
interlink existed. That property is maintained deliberately: every addition sits
inside a generate or is gated by the parameter, because "costs nothing when off"
is only true if someone keeps checking.

## Conventions

**Regenerate; never hand-edit a generated ship.** *(Forced.)* An edit is lost on
the next regeneration, and a topology that exists only as edited Verilog cannot
be checked against the picture, the driver, or anything else.

**Put gateways on edge rings and compute on router locals.** *(Free.)* A port at
`(0, y)` draws traffic to exactly one router, so gateways on different rows
genuinely split the load. Gateways on locals work; they just compete with the
compute unit for the same router.

**Spread an endpoint's ports across adjacent routers rather than stacking them
on one.** *(Free.)* Two attachments to the same router share that router's
capacity, so a multi-port endpoint gains ports without gaining path. This is the
same argument as the gateway one, from the other end.

**Let one description produce both the hardware and the software's view of the
topology.** *(Free, and currently violated — see below.)* Coordinates that the
driver believes and coordinates that synthesis consumed should not be two
artefacts, because nothing detects the moment they stop matching.

## Where today's source disagrees

**The generator has a hardcoded endpoint vocabulary.** `scripts/py/gen_mesh.py`
knows `mat` and `vec` by name and rejects anything else. Those are two of the
reference project's compute units. This is the registry boundary described in
the [README](README.md#what-this-system-does-not-own), not yet drawn.

**Ship modules are named after the reference project.** `ktpu_ship_2x2_6c2v_il`
and its siblings are framework assemblies carrying a project prefix, in a
directory whose name (`synth_top`) describes how they are used rather than what
they are.

**Topology is described twice.** The mesh picture is one description; the board
description consumed by the software stack is another, generated separately. The
second is generated from the first for topology, but its capacity fields come
from a build log and its address fields from a block design that nothing in the
tree can read — so parts of it can only be verified against hardware. One
description should produce both.

**The edge complex's port coordinates are four named parameter pairs**, and the
generate that instantiates ports selects between them by index. The port count
is therefore capped at four by the parameter list rather than by anything
structural. A packed vector was rejected — reasonably, since one shift misaligns
a whole port and still elaborates — but the resulting form does not scale.
