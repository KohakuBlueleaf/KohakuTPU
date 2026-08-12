---
title: Assembly
summary: What a ship is, how a mesh picture becomes a module, and how several meshes become one device image.
tags:
  - architecture
  - ship
  - assembly
---

# Assembly

`src/synth_top/`, and the generator behind it — how the systems become a thing
you can build.

## What it owns

**A ship.** One complete, self-contained accelerator: a mesh of routers, the
endpoints on them, one edge complex, the memory boundary behind it, and the AXI
surface in front. A ship has a name, a fixed shape, and a boundary consisting of
clock, reset and AXI interfaces — and nothing else.

**Generation.** Turning a picture of a mesh into that module, so that topology
is a described thing rather than a thousand lines of hand-written instance
wiring.

**Composition.** Joining several ships into one device image through the
interlink, and deciding the address map that lets a host address all of them.

## The problem it solves

Every other system in the framework is parameterised over something a specific
machine has to pin down: how many routers, in what rectangle, with what on each
port, how many memory ports and where, one memory channel or several, one mesh
or four. Those choices interact — a memory port's coordinate is meaningful only
against the routing rule, an endpoint's coordinate is meaningful only against
the grid bounds — and getting one wrong produces a design that elaborates
cleanly and routes traffic to a plausible wrong place.

Assembly is where those choices are made **once, together, from one
description**.

## The pages

| Page | What is in it |
|---|---|
| [what-is-a-ship](what-is-a-ship.md) | the boundary shape and why it is exactly clock, reset and AXI; the two forms of memory boundary; what a ship costs |
| [generation](generation.md) | the mesh picture, the token grammar, what the generator emits, and the conventions for choosing a shape |
| [interlink](interlink.md) | several ships in one image: the second routing layer, the three structural properties of a link, what crosses, and the address map |

Choosing a shape, and what it costs, is
[integrate/mesh-topology](../../integrate/mesh-topology.md).

## Fixed protocol, addon, convention, or yours

Assembly is the system with the most in the last row, and that is the point: it
is where the framework asks you what machine you want.

| Thing | Category |
|---|---|
| the ship's boundary shape: clock, reset, AXI, and nothing else | **fixed protocol**. It is what makes a ship instantiable without hand-wiring |
| the token grammar — corners empty, edges outside the router rectangle, tied-off ports | **fixed protocol**. It follows from the routing rule |
| the interlink's topology rule, credit classes, registered crossing and encapsulation format | **fixed protocol**. Each has a deadlock or timing argument behind it |
| the address map's *shape* — a segment per mesh for memory, a segment per mesh for control, the mesh id in the high address bits | **fixed protocol** |
| mesh id as a writable register with the parameter supplying only its reset value | **fixed protocol**. It is what lets one module serve every position |
| plain versus concentrated memory boundary | **customizable addon** — a device-image decision |
| the endpoint-type registry the generator instantiates from | **customizable addon** — see [generation](generation.md#where-todays-source-disagrees) |
| the conventions in [generation](generation.md#conventions) | **convention** — one forced, three free |
| **the mesh picture** — grid size, what is on every port, how many memory ports and on which rows | **yours**. This is the primary design input the framework takes |
| **which ships an image contains, and their positions in the mesh grid** | **yours** |
| the address map's *values* | **yours**, per device image |

## What a compute-unit author must know

1. **Your coordinate is assigned by the map, not chosen by you.** Write your
   unit against `POS_X` / `POS_Y` parameters and never assume a value.
2. **Your unit may land on a router's local port or on an edge ring**, and the
   two are indistinguishable from inside it. Do not assume you have four
   neighbours, or any.
3. **How many of you exist is a topology decision.** If your unit only works in
   groups of a particular size, that constraint has to be expressible in the
   map — otherwise it will be violated by a shape that looks reasonable.
4. **Nothing you write should know that another mesh exists.** Cross-mesh
   traffic is recognised at the edge, by a bit in the header, on the way past.

## What this system does not own

| Not owned | Who owns it |
|---|---|
| routing, links, the compute-unit port | [noc](../noc/) |
| what a memory port does once traffic reaches it | [mas](../mas/) |
| the AXI interfaces at the boundary, and their discipline | [axi](../axi.md) |
| which die region a ship lands in, its pblock, its clocks | [physical](../physical/) |
| the vendor block design that instantiates ships, memory controllers and the host bridge | the build flow — [workflow/build](../../workflow/build.md) |
| what any endpoint computes | the accelerator being built |

The last boundary in that table is the one under most pressure. Assembly has to
name endpoint types to instantiate them, so it is the one framework layer that
cannot be entirely ignorant of the project. The right shape is a **registry** —
the generator knows how to place a thing with a fabric port and a coordinate,
and each accelerator supplies its own list of what those things are.

## Where today's source disagrees

- **The generator has a hardcoded endpoint vocabulary**, ship modules carry a
  project prefix, topology is described twice, and the edge complex's port count
  is capped by its parameter list —
  [generation](generation.md#where-todays-source-disagrees).
- **A reusable composition sits in a directory of device tops** —
  [what-is-a-ship](what-is-a-ship.md#where-todays-source-disagrees).
