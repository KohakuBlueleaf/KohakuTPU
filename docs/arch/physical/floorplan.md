---
title: Floorplan
summary: Pblocks, what is pinned, and what is deliberately left unconstrained.
tags:
  - architecture
  - physical
  - floorplan
---

# Floorplan: pblocks, and what is deliberately not constrained

A region assignment is expressed as a placement constraint covering that
region's clock-region rows, with the assembly's cell added to it.

Two properties of that constraint are the whole technique:

**Placement is pinned; routing is not contained.** The purpose is locality — keep
an assembly's cells together and in the right region — not to build a wall. A
constraint that contained routing would also pin the paths that are *meant* to
leave, including the boundary crossings, which is the opposite of what is
wanted.

**Assignment must be enforced, not assumed.** Left unpinned, two meshes will
happily land on each other's region and cross a boundary for their own memory —
a design that meets timing, works, and is slower than it should be for a reason
no report names. The floorplan is an input.

Boundary-crossing pipeline registers are the exception: they are given a stage
count and left for the tool to size and place. Pinning them would pin the very
path they exist to relax.

## Conventions

Neither is enforced by anything. Both exist because skipping them produced a
design that worked and was wrong in a way no report named.

**State the floorplan; do not let it fall out.** *(Free.)* Left unpinned, two
meshes will land on each other's region and cross a boundary for their own
memory. The design meets timing and runs. Nothing announces it.

**Pin placement, not routing.** *(Free.)* The point of a region constraint is
locality, not a wall. Containing routing also pins the paths that are meant to
leave — including the boundary crossings, which is the opposite of the intent.
Leave the crossing pipeline unconstrained entirely and let the tool place it.

Checking that the floorplan is what you asked for is
[measurement](measurement.md).
