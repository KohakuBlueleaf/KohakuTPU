---
title: Where the boundary should fall
summary: Which side of a die boundary a thing belongs on — the traffic classes ranked, why a fabric spanning SLRs was rejected on measurement, and the three hard constraints that leave one shape standing.
tags:
  - architecture
  - physical
---

# Where the boundary should fall

Every die boundary in the machine has to be crossed by *something*. The question
this page answers is **which thing**, and it has an answer rather than a
preference, because the traffic classes in a machine of this shape are very
unequal. A boundary should land on the cheapest one.

Roughly, in descending order of bandwidth:

1. inside a compute unit;
2. compute unit to memory agent;
3. between compute units;
4. control traffic;
5. host to control plane.

Class 1 is not a candidate at all — a datapath built on a cascade cannot be cut
by a boundary, because cascades do not propagate across one. That is a
correctness rule, not a cost:
[device-facts](device-facts.md#hard-limits-that-are-correctness-rules).

## A fabric cannot be stretched across a boundary

That leaves the question of whether the *mesh* can span SLRs, and the answer
came back on measurement rather than on argument. A mesh spanning several dice
was implemented, and its worst path was almost entirely route delay with
essentially **no logic in it at all**.

That reading is the general lesson, and it is worth stating in the form that
transfers: a path that is nearly all route delay at zero or near-zero logic
levels is a **placement failure**, stated as plainly as a tool can state it.
Pipelining it adds latency and moves nothing, because the signal is not passing
through logic — it is travelling. There was no version of that design that
pipelining would have rescued.

It was not the wire count that killed it. A full-width fabric link is a small
fraction of one boundary's cross-die wires. It was that **a fabric whose whole
premise is locality stops having any** once it spans dice.

What replaced it is the arrangement in [ship](../ship/interlink.md): **one mesh
per SLR, each with its own memory channel, joined edge to edge by an explicit
registered link.** The instance that was built that way, the alternative it
beat, and the measured worst path of each are in
[projects/kohakutpu/ship](../../projects/kohakutpu/ship.md#2-four-meshes-not-one--decided-by-measurement).

## The three constraints that leave one shape standing

All three are hard rather than preferential, and together they admit essentially
one arrangement:

- **a datapath on a cascade cannot cross a boundary** — so a compute unit is a
  unit of placement;
- **a memory channel cannot cross a boundary** — so a mesh that wants its own
  memory without crossing must sit on the die that memory is wired to;
- **every crossing signal is flop to flop**, one cycle plus pipelining — so
  anything spanning a boundary needs a protocol that tolerates latency and never
  sends a combinational answer back.

The third is why the interlink is credit-based. A `ready` travelling back across
a boundary is exactly the combinational crossing the whole arrangement exists to
avoid, so the receiver is unconditionally ready and credit reserved the space
before the beat was sent.

## Assume more pipeline stages than the delay suggests

For wide buses at the frequencies this kind of machine targets, vendor guidance
asks for several stages, and its own worst case needs more than that. On the
reference part the crossing delay alone consumes something close to a quarter of
the clock period *before* any fabric routing to reach the transmit register or
leave the receive one — the measured breakdown is in
[projects/kohakutpu/ship](../../projects/kohakutpu/ship.md#11-crossing-an-slr).

So the stage count is a parameter to set generously and leave to the tool to
place, not a number to derive. See
[floorplan](floorplan.md#placement-is-pinned-routing-is-not-contained) for why
those registers are the one thing in the design that is deliberately left
unpinned.

## Which side a thing belongs on

Given the above, the placement question for any new block reduces to three
checks:

| Ask | If yes |
|---|---|
| Does it contain a cascade — carry chain, arithmetic cascade, memory cascade? | It is one unit of placement. It goes wholly on one die, and which one is a floorplan decision made at design time |
| Does it own or attach to a memory interface? | It is pinned to that interface's die. The interface cannot move |
| Does it need an answer back within a cycle? | Both ends go on the same die. If they cannot, the protocol between them is wrong, not the floorplan |

A block that fails all three checks is free to sit anywhere, and should be
written against parameters rather than assuming a position — which is the same
advice a compute unit gets from [ship](../ship/README.md#what-a-compute-unit-author-must-know).
