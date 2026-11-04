---
title: Dot products on the SIMD PE
summary: Why the SIMD PE has no integer dot unit or accumulator, how a dot product is computed instead, what the reserved VMAC encoding group means, and the two design rules any sum path here follows.
tags:
  - architecture
  - pe
  - simd
---

# Dot products on the SIMD PE

> **Kind: Yours throughout.** The PE's datapath is this project's own; the
> reserved encoding group sits in this project's instruction space, not in a
> framework namespace.

**The PE has no integer dot unit and no integer accumulator.** The `VMAC` group
in the encoding — `funct3 = 5` on custom-0 — is **reserved and unmapped**, so
those encodings fault rather than decoding as something adjacent
([programming](programming.md)).

## How a dot product is computed

**A dot product is `vmul` followed by `vredsum`**, or a multiply whose partial
products the scalar core accumulates. The lane array multiplies the elements and
the reduction tree crosses the lanes; there is no dedicated sum path and no
architectural state behind one.

A part that needs high-rate integer dot products carries dedicated matrix units,
which is where that work belongs — [KohakuTPU](../../kohakutpu/README.md) is
what that looks like. The vector core covers the intermediate case. The float
tier's accumulator is a different structure with a different justification and
is documented in [float](float.md).

## Why there is no dedicated dot path

**It would constrain every other width.** A within-lane dot accumulate is a
single-cycle recurrence into a small architectural state, and every width in the
unit would have to be arranged around it: narrowing a lane count means walking
the accumulate across lane groups, which touches the recurrence directly. The
width mechanism the rest of this PE is built on — one unit count per feature, a
walk when the count is below full, and an instruction set that never learns the
count — cannot be applied to it without changing the recurrence.

**It costs a second sum path.** A within-lane dot needs an adder tree or a DSP48
cascade with its own latency pipeline, in addition to the multipliers `vmul`
already reads. The two cannot be shared: a product with two consumers cannot be
cascaded inside the DSP column, so a build with both a visible `vmul` result and
a cascaded dot sum instantiates the multipliers twice.

**The workload that wants it is served elsewhere.** High-rate integer dot
products are quantised inference, which matrix units serve.

## Two rules for any sum path built here

**A running total must not make a round trip through the register file.** A
register-to-register multiply-accumulate reads the total, adds and writes it
back, which puts file read, multiply, add and file write inside one cycle on the
widest structure in the unit. That loop sets the clock, and it is the loop the
DSP48's own output-register idiom exists to avoid. The float accumulator keeps
this property for the same reason.

**A DSP48 cascade must be pipelined and must free-run.** Each cascade hop is a
cycle, so term *k*'s operands wait *k* cycles; folding several terms into one
register makes the primitive multiply and post-add combinationally, which costs
about 12 MHz. A multi-stage cascade must not be gated by a per-instruction
enable: gating freezes hops 2..N and the result never arrives. Flops are the
cheap resource on this device and an unpipelined DSP48 is not.
