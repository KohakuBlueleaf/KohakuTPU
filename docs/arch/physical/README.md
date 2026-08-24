---
title: Physical layer
summary: Die regions, pblocks and clock domains — and why placement is a design input rather than a build outcome.
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

## What it owns

- **Die regions**: what may cross a boundary, what may not, and what a crossing
  costs.
- **Floorplan**: which assembly lands where, expressed as placement constraints,
  and what is deliberately left unconstrained.
- **Clock domains**: which exist, which are asynchronous to which, and how a
  domain's frequency is changed.
- **The measurement discipline** that makes any of the above checkable.

## Why this is architecture and not a build step

A framework that treated placement as something the tool does afterwards would
be wrong four times over.

**Some structures cannot be split at all.** Carry chains, arithmetic-block
cascades and memory-block cascades do not propagate across a die boundary; the
only connection between regions is the crossing register. So a datapath built
on a cascade is *by construction* a unit of placement, and how you decompose
your compute unit is a floorplan decision made at design time. This is a
correctness rule, not an optimisation.

**The largest fixed blocks cannot move.** A memory interface is anchored to a
region by its pinout — its I/O banks and its clocking must all be in one region
— and a host bridge is anchored by its transceivers. Both consume a region's
budget, and they consume it unevenly: the region holding the host bridge gives
up real compute to do so. Identical silicon does not mean interchangeable
regions.

**The crossing is registered, so it costs latency.** Any protocol that crosses a
boundary must tolerate that latency, and whether it does is decided when the
protocol is designed, not when it is placed. The interlink in
[ship](../ship/interlink.md) is credit-based precisely because a ready signal
travelling back across a boundary is the combinational crossing all of this
exists to avoid.

**How much fits is a per-region question.** The resource that runs out first
sets how many compute units a machine can have, and it differs by region once
fixed loads are placed. A device-wide total is the wrong number to plan against.

Put together: **the number of compute units in a machine is set by geometry, not
by a throughput knee.** That is the single most consequential thing in this
system, and it is why unit count appears in the architecture rather than in a
tuning guide.

## The pages

| Page | What is in it |
|---|---|
| [device-facts](device-facts.md) | what has to be established about a part before a floorplan exists, and how to establish it rather than infer it |
| [where-the-boundary-falls](where-the-boundary-falls.md) | the traffic classes ranked, why a fabric spanning regions was rejected on measurement, and the three hard constraints that leave one shape standing |
| [floorplan](floorplan.md) | pblocks, what is pinned, what is deliberately left unconstrained |
| [clocking](clocking.md) | the four domains, why the fabric has no crossing in it, and the retunable mesh clock |
| [measurement](measurement.md) | out-of-context synthesis as the unit of iteration, and what makes a number mean something |

## Fixed protocol, addon, convention, or yours

| Thing | Category |
|---|---|
| what may not cross a boundary — cascades, memory-interface pinout, combinational paths | **fixed by the silicon**. Not a framework choice at all |
| every crossing signal is flop to flop, and a crossing protocol must be credit-based | **fixed protocol**. See [ship](../ship/interlink.md) |
| one mesh per die region, each with its own memory channel | **fixed in practice, and load-bearing** — the arrangement the rest of the framework is shaped around, arrived at by measurement rather than assumption |
| two clock generators, one fixed and one retunable | **customizable addon** — drop the retunable one and you lose the ability to find the real frequency ceiling, and nothing else changes |
| pipeline depth on a crossing bus | **customizable** — let the tool size it; assume more than the raw delay suggests |
| the conventions, in [device-facts](device-facts.md#convention), [floorplan](floorplan.md#conventions) and [measurement](measurement.md#convention) | **convention** — all free, and all four have cost real time when skipped |
| **the floorplan itself** — which ship on which region, and the pblock for each | **yours**, and it must be stated. Unstated, it will be wrong |
| **the clock frequencies** | **yours** |

## What a compute-unit author must know

1. **Your unit must fit in one die region, entirely.** If it is built on a
   cascade, this is a correctness requirement and not advice.
2. **Your unit's size decides how many exist**, and the ceiling is per region
   after fixed loads, not per device.
3. **You do not choose your region.** Write against parameters; assume nothing
   about neighbours or distance.
4. **If you build something that must span regions, it needs its own credit-based,
   fully registered protocol** — and at that point you are building an interlink,
   so use the one in [ship](../ship/interlink.md) instead.

## What this system does not own

| Not owned | Who owns it |
|---|---|
| any logic at all | every other page. This one constrains; it does not compute |
| the interlink's protocol | [ship](../ship/interlink.md). Physics dictates its shape — registered, credit-based, no reverse combinational path — but not its message set |
| clock crossing logic | [axi](../axi.md), which owns the asynchronous FIFOs at the memory boundary |
| the block design that instantiates controllers, bridges and clock generators | the build flow — [workflow/build](../../workflow/build.md) |
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
  instance, which uses a small fraction of one boundary's sites — see
  [projects/kohakutpu/ship](../../projects/kohakutpu/ship.md) for that device's
  budget and what did bind instead. Do not generalise one design's headroom into
  a guideline; the point of the open question is that nobody has published one.*
- **No published figure exists for how much logic headroom to reserve for
  crossing routing.** The nearest available guidance is a general "keep any one
  resource well below saturation in a single region".
- **No part of the software stack models locality.** Two compute units are
  treated as interchangeable, which stops being true the moment one of them is
  across a boundary from the operand it needs. That is the same shape as the
  residency constraints the compiler already carries, and it is not there yet —
  see [integrate/software-stack](../../integrate/software-stack.md).
