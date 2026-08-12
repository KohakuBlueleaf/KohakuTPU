---
title: Where the boundary should fall
summary: The traffic classes ranked, why a fabric spanning regions was rejected on measurement, and the three hard constraints that leave one shape standing.
tags:
  - architecture
  - physical
---

# Where the boundary should fall

The traffic classes in a machine of this shape are very unequal, so a boundary
should land on the cheapest one. Roughly, in descending order of bandwidth:

1. inside a compute unit;
2. compute unit to memory agent;
3. between compute units;
4. control traffic;
5. host to control plane.

Class 1 is not a candidate at all — see the cascade rule in the
[README](README.md#why-this-is-architecture-and-not-a-build-step). That leaves
the question of whether a fabric can be stretched across a boundary, and the
answer came back on measurement: a mesh spanning several regions was
implemented, and its worst path was almost entirely routing with essentially no
logic in it. It was not the wire count that killed it; a full-width fabric link
is a small fraction of one boundary's crossing registers. It was that a fabric
whose whole premise is locality stops having any.

What replaced it is the arrangement in [ship](../ship/interlink.md): **one mesh
per region, each with its own memory channel, joined edge to edge by an explicit
registered link.** The instance that was built that way, and the alternative it
beat, are in [projects/kohakutpu/ship](../../projects/kohakutpu/ship.md).

Three constraints made that the only shape left standing, and all three are hard
rather than preferential:

- a datapath on a cascade cannot cross a boundary;
- a memory channel cannot cross a boundary;
- every crossing signal is flop to flop, one cycle plus pipelining.

Assume more pipeline stages than the raw crossing delay suggests. For wide buses
at the frequencies this kind of machine targets, vendor guidance asks for
several stages, and its own worst case needs more than that. On the reference
part, the crossing delay alone consumes something close to a quarter of the
clock period before any fabric routing to reach the transmit register or leave
the receive one.
