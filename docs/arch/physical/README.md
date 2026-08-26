---
title: Physical layer
summary: SLRs, pblocks and clock domains — and why placement is a design input rather than a build outcome.
tags:
  - architecture
  - physical
  - floorplan
  - clocking
---

# Physical layer

The die is not flat, its resources are not uniformly reachable, and several of
its constraints are correctness rules rather than performance advice. This is
the part of the architecture that lives in geometry.

A large FPGA of this class is several dice on an interposer. Each die is an
**SLR** — a super logic region — and the connection between two of them is not
ordinary routing. That one fact generates everything on these pages.

## What it owns

- **Die facts**: what an SLR provides, what may cross a boundary, what may not,
  and what a crossing costs.
- **Floorplan**: which assembly lands on which SLR, expressed as placement
  constraints, and what is deliberately left unconstrained.
- **Clock domains**: which exist, where each boundary between them falls, and
  how a domain's frequency is changed at runtime.
- **The measurement discipline** that makes any of the above checkable — and
  that defines what every number in `arch/` means.

## Why this is architecture and not a build step

A framework that treated placement as something the tool does afterwards would
be wrong four times over.

**Some structures cannot be split at all.** Carry chains, arithmetic-block
cascades and memory-block cascades do not propagate across a die boundary; the
only connection between SLRs is the crossing register. So a datapath built on a
cascade is *by construction* a unit of placement, and how you decompose your
compute unit is a floorplan decision made at design time. This is a correctness
rule, not an optimisation.

**The largest fixed blocks cannot move.** A memory interface is anchored to a
die by its pinout — its I/O banks and its clocking must all be on one — and a
host bridge is anchored by its transceivers. Both consume a die's budget, and
they consume it unevenly: the die holding the host bridge gives up real compute
to do so. Identical silicon does not mean interchangeable dice.

**The crossing is registered, so it costs latency.** Any protocol that crosses a
boundary must tolerate that latency, and whether it does is decided when the
protocol is designed, not when it is placed. The interlink in
[ship](../ship/interlink.md) is credit-based precisely because a ready signal
travelling back across a boundary is the combinational crossing all of this
exists to avoid.

**How much fits is a per-die question.** The resource that runs out first sets
how many compute units a machine can have, and it differs by die once fixed
loads are placed. A device-wide total is the wrong number to plan against.

Put together: **the number of compute units in a machine is set by geometry, not
by a throughput knee.** That is the single most consequential thing in this
system, and it is why unit count appears in the architecture rather than in a
tuning guide.

## The pages

| Page | What is in it |
|---|---|
| [device-facts](device-facts.md) | what the part actually provides, the limits that are correctness rules, the 72-bit memory port, what is scarce and in what order |
| [where-the-boundary-falls](where-the-boundary-falls.md) | which side of a boundary a thing belongs on, why a fabric spanning SLRs was rejected on measurement, and the three hard constraints that leave one shape standing |
| [floorplan](floorplan.md) | pblocks, what is pinned, what is deliberately left unconstrained, and how to check you got what you asked for |
| [clocking](clocking.md) | the domains, where each boundary falls, why the fabric has no crossing inside it, and the retunable mesh clock |
| [measurement](measurement.md) | **what a number in this tree means.** Every other page here points at it |

**Start with [measurement](measurement.md) if you are about to read a figure
anywhere in `arch/`.** It states, once, that no Fmax in this repository is a
closed-timing result, and what the five things are that a figure has to name
before it means anything.

## Fixed protocol, addon, convention, or yours

| Thing | Category |
|---|---|
| what may not cross a boundary — cascades, memory-interface pinout, combinational paths | **fixed by the silicon**. Not a framework choice at all |
| every crossing signal is flop to flop, and a crossing protocol must be credit-based | **fixed protocol**. See [ship](../ship/interlink.md) |
| one mesh per SLR, each with its own memory channel | **fixed in practice, and load-bearing** — the arrangement the rest of the framework is shaped around, arrived at by measurement rather than assumption |
| the fabric is one clock domain, router to router | **fixed protocol**. The routing rule's deadlock argument assumes it |
| two clock generators, one fixed and one retunable | **customizable addon** — drop the retunable one and you lose the ability to find the real frequency ceiling, and nothing else changes |
| per-unit-type clocking, and clocking the system node separately from the fabric | **customizable** — both are elaboration parameters, both cost a crossing FIFO per direction, and both are off by default |
| pipeline depth on a crossing bus | **customizable** — let the tool size it; assume more than the raw delay suggests |
| the conventions, in [device-facts](device-facts.md#convention), [floorplan](floorplan.md#conventions) and [measurement](measurement.md#convention) | **convention** — all free, and all have cost real time when skipped |
| **the floorplan itself** — which ship on which SLR, and the pblock for each | **yours**, and it must be stated. Unstated, it will be wrong |
| **the clock frequencies, and the profile table** | **yours**, per board |

## What a compute-unit author must know

1. **Your unit must fit on one SLR, entirely.** If it is built on a cascade,
   this is a correctness requirement and not advice.
2. **Your unit's size decides how many exist**, and the ceiling is per die after
   fixed loads, not per device.
3. **You do not choose your die.** Write against parameters; assume nothing
   about neighbours or distance.
4. **Registers are the resource you have spare.** Pipelining to close timing is
   close to free on this part — which is licence to spend a few registers on a
   path, not licence to duplicate a large array.
5. **If you build something that must span dice, it needs its own credit-based,
   fully registered protocol** — and at that point you are building an
   interlink, so use the one in [ship](../ship/interlink.md) instead.

## What this system does not own

| Not owned | Who owns it |
|---|---|
| any logic at all | every other page. This one constrains; it does not compute |
| the interlink's protocol | [ship](../ship/interlink.md). Physics dictates its shape — registered, credit-based, no reverse combinational path — but not its message set |
| clock crossing logic at the memory boundary | [axi](../axi.md), which owns the asynchronous FIFOs there |
| the block design that instantiates controllers, bridges and clock generators | the build flow — [workflow/build](../../workflow/build.md) |
| how to run a measurement, and the ways one lies | [workflow/measure](../../workflow/measure.md). This tree says what a figure *means*; that page is the mechanism |
| what to do when timing fails | [workflow/timing-closure](../../workflow/timing-closure.md) |
| the part, the board and their pinout | the target. This system says which facts to establish, not what they are |

## Open questions

Stated rather than buried, because a floorplan built on a guess is expensive to
unwind.

- **No published guideline exists for how many crossing registers may be used
  before routing becomes critical.** Vendor documentation says only to check that
  usage matches the design's expectations. Treat the site count as a hard
  ceiling and do not plan to approach it.

  *In practice this has not been the binding constraint on the reference
  instance, which uses a small fraction of one boundary's wires — see
  [projects/kohakutpu/ship](../../projects/kohakutpu/ship.md) for that device's
  budget and what did bind instead. Do not generalise one design's headroom into
  a guideline; the point of the open question is that nobody has published one.*
- **No published figure exists for how much logic headroom to reserve for
  crossing routing.** The nearest available guidance is a general "keep any one
  resource well below saturation on a single die".
- **No part of the software stack models locality.** Two compute units are
  treated as interchangeable, which stops being true the moment one of them is
  across a boundary from the operand it needs. That is the same shape as the
  residency constraints the compiler already carries, and it is not there yet —
  see [integrate/software-stack](../../integrate/software-stack.md).
