---
title: The measurement discipline
summary: Out-of-context synthesis as the unit of iteration, and what makes a number mean something.
tags:
  - architecture
  - physical
  - measurement
---

# The measurement discipline

Placement decisions need numbers, and numbers need instruments.

**Out-of-context synthesis is the unit of iteration.** A block synthesised on
its own gives frequency and resource figures fast enough to make a design loop,
which a full implementation does not. What it does not give is placement or
routing, so an out-of-context frequency is an upper bound and must be labelled
as one. A page that quotes an out-of-context number as if a placed design
achieved it is making a claim nobody checked.

**Report where a block's cells actually landed.** Region spread per top-level
block turns "the floorplan is what I asked for" from an assumption into a
report line — see [floorplan](floorplan.md).

**A number without a named instrument is not a number.** Which top, which
parameters, which part, which speed grade, and whether it was placed. The
convention, and the scripts that enforce it, are
[workflow/measure](../../workflow/measure.md).

## Convention

**Name the instrument on every number.** *(Free.)* Which top, which parameters,
which part, which speed grade, and whether it was placed. An out-of-context
frequency is an upper bound; quoting one as though a placed design achieved it
is a claim nobody checked.

The corresponding rule for where numbers live: framework pages carry none, and
measured figures belong with the project that produced them. See
[arch/README](../README.md#no-numbers-here).
