---
title: The integer dot unit and its accumulator — removed
summary: What `vdot` and the integer accumulator were, why they were removed, what replaces them, and what an encoding table's reserved VMAC group means today.
tags:
  - architecture
  - pe
  - simd
  - removed
---

# The integer dot unit and its accumulator — removed

> **Kind: Yours throughout, and now removed.** The integer dot unit and its
> accumulator were this project's datapath, and removing them changed nothing
> outside it. The reserved encoding group left behind sits in this project's own
> instruction space, not in a framework namespace.

**This hardware is not built.** `vdot`, `vdotn`, `vaccz`, `vaccrd`, `vaccwr`,
the integer accumulator banks, the `MULS` per-lane multiplier depth and the
`DOT_DSP` mapping choice are all gone from the RTL. The `VMAC` group in the
encoding — `funct3 = 5` on custom-0 — is **reserved and unmapped**, so those
encodings fault rather than decoding as something adjacent.

This page exists because the reserved group is visible in
[programming](programming.md) and a reader is entitled to know what used to be
there and why it is not.

## What replaces it

**A dot product is `vmul` followed by `vredsum`**, or a multiply whose partial
products the scalar core accumulates. The elements are still multiplied by the
lane array and the reduction tree still crosses the lanes; what is gone is the
dedicated sum path and the architectural state behind it.

A part that needs high-rate integer dot products carries dedicated matrix units,
which is where that work belongs — [KohakuTPU](../../kohakutpu/README.md) is
what that looks like. The vector core covers the intermediate case.

## Why it went

Three reasons, in the order they weigh.

**It constrained every other width.** The dot's accumulate was a single-cycle
recurrence into a small architectural state, and every width in the unit had to
be arranged around it: narrowing a lane count meant walking the accumulate
across lane groups, which touches the recurrence directly. The width mechanism
that the rest of this PE is built on — one unit count per feature, a walk when
the count is below full, and an instruction set that never learns the count —
could not be applied to it without changing the recurrence.

**It cost a second sum path.** A within-lane dot needs an adder tree or a DSP48
cascade with its own latency pipeline, in addition to the multipliers that
`vmul` already reads. The two cannot be shared: a product with two consumers
cannot be cascaded inside the DSP column, so a build that kept both a visible
`vmul` result and a cascaded dot sum instantiated the multipliers twice.

**No shipped workload issued it.** The one workload that wants a high-rate
integer dot is quantised inference, and that is served by matrix units. The
float tier's accumulator, which is a different structure with a different
justification, remains and is documented in [float](float.md).

## What it was

`vdot.s8 acc0, v1, v2` reduced **within each 32-bit lane**. One lane held four
int8 elements from each source; the four products were summed to one int32, and
that int32 was added into the lane's slot of the accumulator. At eight lanes one
instruction performed 32 multiply-accumulates and produced eight independent
running totals.

That is the ARM `SDOT` and x86 `VPDPBUSD` shape rather than a whole-vector dot.
The accumulator was `SIMD × int32` — exactly one vector register wide — which is
what made reading it back a move rather than a narrowing.

Two durable facts came out of building it, and both still apply to anything
built here.

**A running total must not make a round trip through the register file.** A
register-to-register multiply-accumulate reads the total, adds and writes it
back, which puts file read, multiply, add and file write inside one cycle on the
widest structure in the unit. That loop is what sets the clock, and it is the
loop the DSP48's own output-register idiom exists to avoid. The float
accumulator keeps the same property for the same reason.

**A DSP48 cascade must be pipelined and must free-run.** Each cascade hop is a
cycle, so term *k*'s operands wait *k* cycles; folding several terms into one
register makes the primitive multiply and post-add combinationally, which
measured a 12 MHz loss when it was written that way. And a multi-stage cascade
must not be gated by a per-instruction enable: gating freezes hops 2..N and the
result never arrives. Flops are the cheap resource on this device and an
unpipelined DSP48 is not.
