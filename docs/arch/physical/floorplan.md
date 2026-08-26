---
title: Floorplan
summary: How an SLR assignment is expressed, what is pinned, what is deliberately left unconstrained, and how to check that the floorplan is the one you asked for.
tags:
  - architecture
  - physical
  - floorplan
---

# Floorplan: what is pinned, and what is deliberately not

A **floorplan** here means one thing: a statement of which assembly lands on
which SLR. An SLR is one die of the several this part is built from — see
[device-facts](device-facts.md#an-slr-in-this-machines-usage).

The statement is expressed as a placement constraint — a **pblock** — covering
that SLR's clock-region rows, with the assembly's cell added to it. One pblock
per SLR, holding everything that SLR owns: the mesh, its share of the
interconnect, its memory controller, its clock generators, and on the master die
also the host bridge and the debug bridge.

Two properties of that constraint are the whole technique.

## Placement is pinned; routing is not contained

The purpose is **locality** — keep an assembly's cells together and on the right
die — not to build a wall. The option that also contains routing is left off
deliberately: a constraint that contained routing would pin the paths that are
*meant* to leave, including the boundary crossings, which is the opposite of
what is wanted.

**The crossing pipeline is the explicit exception, and it is left entirely
unpinned.** Those registers *are* the die crossing. They are given a stage count
and left for the tool to size and place, because the whole reason the stage
count exists is to let the tool distribute the pipeline across the boundary. A
pblock would force them to one side of the thing they span, and pinning them
would pin the very path they exist to relax.

## Assignment must be enforced, not assumed

Left unpinned, two meshes will land on each other's SLR and cross a boundary for
their own memory. The result meets timing, works, and is slower than it should
be for a reason no report names. **The floorplan is an input, not an outcome.**

This is also where the fixed loads make identical dice non-interchangeable. The
die that carries the host bridge gives up real fabric to do it, and the die that
carries the interconnect's root gives up more; whichever die those land on holds
the smallest share of compute. Which SLR is emptiest is therefore a question
about a *specific build*, and the answer changes when the interconnect or the
host bridge changes — so it is measured per build rather than assumed once.
The reference instance's own assignment and its measured per-die occupancy are
in [projects/kohakutpu/ship](../../projects/kohakutpu/ship.md).

## Checking that you got it

A floorplan that is asserted and not verified is a comment. The check is a
report line: **per-SLR spread for each top-level block**, read out of the placed
design. If a block's cells appear on two dice, either the pblock did not apply
or the block does not fit — and those need different fixes.

Read it from the report file rather than by opening the checkpoint; see
[measurement](measurement.md#read-the-report-file-not-the-checkpoint).

## Conventions

Neither is enforced by anything, and both are free.

**State the floorplan; do not let it fall out.** *(Free.)* Left unpinned, two
meshes will land on each other's SLR and cross a boundary for their own memory.
The design meets timing and runs. Nothing announces it.

**Pin placement, not routing.** *(Free.)* The point of an SLR constraint is
locality, not a wall. Containing routing also pins the paths that are meant to
leave — including the boundary crossings, which is the opposite of the intent.
Leave the crossing pipeline unconstrained entirely and let the tool place it.

What a floorplan can and cannot fix, and where it sits in the order of levers
for closing timing, is
[workflow/timing-closure](../../workflow/timing-closure.md#6-floorplanning-is-the-first-lever).
