---
title: Closing timing
summary: Reading a critical path, telling control-bound from compute-bound, and why floorplanning is the first lever rather than the last.
tags:
  - workflow
  - timing
  - floorplan
---

# Closing timing

A design closes timing when every path meets its requirement after routing.
Getting there is not one activity. It is three, and they are usually needed in
this order:

1. **Read the failing paths** and find out what kind of failure it is.
2. **Floorplan**, if the failure is about distance.
3. **Change the RTL**, if the failure is about logic.

Most projects do these in the reverse order, spend weeks pipelining a datapath
that was never the problem, and arrive at the floorplan last with no schedule
left. The rest of this page is an argument for the order above.

Before anything here is useful, the block-level answer must already be known: see
[measure.md](measure.md). Out-of-context measurement tells you whether the logic
*can* make the frequency. This page is about whether the assembled device
*does*.

## Read the path before doing anything

`report_timing_summary` gives a number. The number is not actionable. The path
is.

For each of the worst paths, four properties decide what to do:

| property | how to read it |
|---|---|
| **logic levels** | how many LUT/carry stages the signal passes through |
| **datapath delay split** | how much is logic delay and how much is route delay |
| **start and end pin** | which module, and whether it is datapath or control |
| **the clocks involved** | one domain, or a crossing |

The split between logic and route delay is the single most informative number in
the report, and it partitions the work cleanly:

- **Mostly logic delay, many levels** — compute-bound. Pipeline it, or restructure
  the logic. Floorplanning will not help.
- **Mostly route delay, few or zero logic levels** — distance-bound. Pipelining
  the datapath will not help either; the signal is not passing through logic, it
  is travelling. This is a placement problem and it is fixed by placement.

A path that is almost entirely route delay at **zero** logic levels is a
placement failure stated as plainly as a tool can state it. Adding a pipeline
stage there adds latency and moves nothing.

### Do not read one path

A large design does not have *a* critical path. It has a plateau of paths within a
few percent of each other. Remove the worst and the next appears almost
immediately, so closing means fixing several in sequence — and choosing which,
based on which structure owns the most of them.

Report ten paths, not one, and count which module owns them. A measurement
harness that reports a single path will repeatedly point at whichever module
happens to hold the worst one, and that is often not the module holding six of
the ten. Two separate wrong conclusions were reached this way before the harness
was changed to report ten by default: one component looked like a severe
constraint on the strength of a single path, and another looked like the
machine's limit when it was not.

## Control-bound versus compute-bound

The most common misdiagnosis is treating an interconnect failure as a datapath
failure.

**Compute-bound** looks like: start and end both inside your datapath, high logic
levels, delay dominated by logic, and the same structure appearing repeatedly in
the top ten. The fix is RTL — a pipeline stage, a restructured search, a wider
adder split in two.

**Control-bound** looks like: paths inside vendor interconnect, address decoders,
arbiters, downsizers, reset trees, or a state machine whose next-state logic
touches everything. The fix is almost never in the datapath, and quite often not
in RTL at all.

The distinction is worth stating sharply because of what it implies for
schedule. On one full-chip build every failing path lived inside the vendor AXI
interconnect's data-width downsizer — **not one** was in compute. Weeks of
datapath optimisation would have changed nothing. The fix was structural: split
one large crossbar into a small root plus several small leaves.

That fix generalises:

> **You cannot spread one interconnect instance across a device. You place
> several small ones.**

A monolithic crossbar is a single placeable object. Its internal paths are
whatever the tool makes of them, and it must sit somewhere, which means every
master and slave reaches it from wherever they are. A hierarchy of small
crossbars can be placed *with* the things they serve — one per region, each
short — and the only long path left is the one between levels, which is a single
well-defined crossing you can pipeline deliberately. It is usually cheaper in
area as well, because port-pair count falls quadratically.

### Reset and enable fanout

A signal that reaches every register in a block is a distance problem wearing a
control signal's clothes. If the top ten paths end at reset pins, enable pins or
clock enables, the fix is fanout reduction — replicate the driver, pipeline it,
or make the reset synchronous and locally buffered — not datapath work.

In out-of-context measurement this shows up as an artefact of a missing false
path ([measure.md](measure.md)); in a real implementation it is genuine.

## Floorplanning is the first lever

On a multi-die device — where the fabric is several dice joined by an
interposer — the largest single lever on timing is **which die each block lands
on**. Not the RTL, and not the strategy setting.

The vendor's own guidance orders it this way: keep tightly coupled modules
together first, and pipeline only what cannot be kept together. That ordering is
right and it is routinely inverted in practice.

### Why it dominates

A crossing between dice is slow, limited in number, and hold-sensitive. A design
placed without a floorplan will scatter blocks by whatever the placer's cost
function prefers, and the placer does not know that your compute block and its
memory controller must be neighbours. The result is a high-bandwidth path — the
one carrying the most bits at the highest rate — spread across the interposer on
a thousand-odd wires.

Constraining each block to sit with the resources it uses removes those wires
entirely. That is a categorically better outcome than pipelining them, because
pipelining adds latency to every access on the path and the wires are still
there.

**Do not pipeline the highest-bandwidth path to fix a crossing. Remove the
crossing.**

### How to floorplan

Soft region constraints, placement only:

```tcl
create_pblock pb_region0
resize_pblock [get_pblocks pb_region0] -add {CLOCKREGION_X0Y0:CLOCKREGION_X7Y3}
add_cells_to_pblock [get_pblocks pb_region0] [get_cells -quiet {top_i/block_0}]
add_cells_to_pblock [get_pblocks pb_region0] [get_cells -quiet {top_i/leaf_ic_0}]
set_property CONTAIN_ROUTING false [get_pblocks pb_region0]
```

Four things in that snippet are deliberate:

- **The region is a whole die.** The goal is "this block and its memory
  controller are on the same die", not "this block is in this corner". A tight
  pblock over-constrains the placer and usually makes things worse.
- **`CONTAIN_ROUTING false`.** Contain placement, not routing. Fencing the router
  inside the region gives it fewer options for the very paths you are trying to
  help.
- **The block's own interconnect leaf goes in with it.** Placing the compute and
  leaving its crossbar outside re-creates the crossing you removed.
- **`-quiet` on every `get_cells`.** The XDC then survives being read against a
  design that does not contain the cell — during an out-of-context run, or after
  a rename. Without it, one stale path aborts constraint processing and takes
  every constraint after it with it.

**Generate the pblock file from the same description that generates the
design.** Hand-written region constraints drift from the hierarchy they name, and
a pblock naming a cell that no longer exists silently constrains nothing. Emit
it, and mark it as generated at the top of the file.

### What a floorplan is worth

The general result, stated without the numbers that belong to
[the project that measured them](../projects/kohakutpu/results.md):

> The same device, the same compute, the same RTL. Unpinned, the design failed at
> placement with a large negative worst slack and enormous total negative slack.
> With per-die region constraints and a hierarchical interconnect — **no RTL
> change at all** — it was positive at placement.

The lesson is not that floorplanning is a fine-tuning step that recovers a few
percent. It is that a design without a floorplan on a multi-die part is not
really a design yet, and its timing report is not evidence about the RTL.

### What a floorplan cannot fix

Hard blocks do not move. A PCIe block, a memory controller's I/O banks, a
transceiver — each is pinned to a site, and everything that must be adjacent to
it inherits that pinning. The consequences are structural:

- The die holding the host interface is permanently the most congested one. Put
  the smallest partition there, and plan for it rather than discovering it.
- If there is one memory controller per die, then a block that needs memory has
  already had its die chosen for it.
- If the topology contains a cycle, at least one edge must span a long distance.
  It is not possible to embed every edge locally. **Identify which edge that is,
  pipeline that one, and pipeline no others** — a pipeline stage added
  everywhere "for safety" costs latency everywhere.

Work these constraints out before floorplanning, not during. They determine the
answer.

### Crossings want registers on both sides

Devices with dedicated crossing flip-flops only use them if there is a register
available to pull into them. A path that is registered on one side and
combinational on the other has nothing to place there, and the tool routes it as
ordinary fabric.

One measured build used **zero** of its several thousand crossing sites, for
exactly this reason: the crossing was registered outbound and bare inbound.

Register both directions at any interface that may end up spanning dice. It costs
one cycle of latency and it is the difference between using the dedicated
resource and not.

Vendor register-slice IP usually has a setting that means "this crossing spans
dice" and sizes the pipeline itself. Prefer it to a hand-built one, and **do not
region-constrain the slice** — pinning it defeats the point, which is that the
tool places the pipeline along the crossing.

### Expect hold violations, not setup, on crossings

Variability between dice is larger than within one, so the timing engine budgets
crossings pessimistically for hold. A crossing that fails hold is normal and is
fixed with delay insertion, which the tool does automatically given somewhere to
put it. A crossing that fails setup usually means it should not have been a
crossing.

## Utilisation: what is actually scarce

This is the most consistently misread part of a utilisation report, and getting
it wrong changes architectural decisions.

> **CLB percentage is routing and packing pressure. It is not a capacity
> ceiling. Never quote it as one.**

A CLB (or slice) holds several LUTs and roughly twice as many flip-flops. The
percentage reported as "CLB" is the fraction of CLBs with *anything* in them, not
the fraction of their contents used. A design can sit near 90% CLB while using
under 60% of the LUTs — meaning the occupied CLBs are averaging around five LUTs
of eight, and **added logic largely packs into CLBs already in use**.

Consequences:

- "We are at 90% CLB, there is no room for this feature" is usually **false**.
  Check LUT percentage before believing it.
- A high CLB figure alongside a low LUT figure is a *packing* signal. It says the
  design has more control sets than the fabric packs well, not that it is full.
- **Control sets are the scarce resource**, not LUTs. A control set is a distinct
  combination of clock, set/reset and clock enable; registers with different
  control sets cannot share a slice. Two regions of a device with similar LUT
  counts can differ substantially in achievable density purely on control-set
  count.

So the first lever on utilisation is **control-set reduction**, not logic
reduction: fewer distinct resets, fewer distinct clock enables, enables expressed
as data-path muxes where they are cheap. Better packing is free capacity, and it
buys frequency as well as area, because a less fragmented placement routes
shorter.

### Flip-flops are nearly free; use them

If the fabric offers two flip-flops per LUT and the design uses one, then half
the flip-flops in already-occupied slices are idle. A pipeline register added
there costs no additional CLB — the flop is already sitting in the slice you are
paying for.

**That is the resource to spend when closing timing.** Pipelining is cheap in a
LUT-bound, FF-underused design; it is the same trade everyone assumes is
expensive and, measured, usually is not.

### Composed area is not the sum of the parts

Adding up per-module utilisation over-predicts the assembled design, sometimes by
a substantial fraction: constant propagation, shared logic and boundary
optimisation all cross module boundaries in the assembly and cannot in isolated
runs.

Per-component sums are an **upper bound**. Only a composed synthesis is a number.

The exception is hard blocks. A DSP or a memory block is either claimed or it is
not, so those counts do sum exactly — and that makes them the right currency for
capacity planning when a composed build is not available yet.

### Composition costs frequency too

An assembled design closes lower than its own slowest component measured alone,
and out-of-context numbers can be substantially optimistic against a routed
result. Budget for the drop. A component that exactly meets the target in
isolation will miss it in the machine.

## When the RTL really is the problem

Once the failure is genuinely compute-bound and inside your datapath:

- **Add a pipeline stage** where logic levels are high. Cheap, per the flip-flop
  argument above.
- **Restructure serial dependencies.** A loop that carries a flag across
  iterations synthesises as a chain as long as the loop. Searches want
  smear–isolate–encode; sticky accumulation wants mask-then-reduce. See
  [tooling-traps.md](tooling-traps.md).
- **Break a wide operation** into stages rather than trusting retiming to find
  the split.
- **Remove variable part-select writes**, which build a mux across an entire
  register.

After each change, **re-measure in context**, not only out of context. A
standalone measurement can attribute an improvement to the wrong thing, and a
change that helps a block in isolation can hurt the assembly.

## A runtime-tunable clock changes the economics

If the clock feeding the compute region comes from a reconfigurable generator
with a control interface, then "the frequency missed" costs a register write
instead of a full rebuild.

This is worth arranging early. The rebuild cycle for a large design is measured
in hours; the ability to sweep frequency on real hardware turns a multi-day
question into a multi-minute one.

Two things to know about it:

- **The control plane must not stand on the clock it changes.** Use a separate,
  fixed generator for the control path. A control interface clocked by the clock
  it is retuning cannot recover from a bad setting.
- **Timing analysis does not know the clock is tunable.** The tool constrains the
  generated clock from its build-time settings, so *that* frequency is the
  verified ceiling. At or below it, the design is analysed. Above it, you are in
  a deliberately unmeasured sweep — which is a legitimate thing to do, and must
  be labelled as such wherever the resulting numbers are quoted.

## Build strategy comes last

Implementation strategy directives — explore variants, extra optimisation
passes, retiming — are real but small compared to floorplanning and interconnect
structure. They also multiply build time, which is already the binding
constraint on iteration.

Reach for them when a design is close and structurally settled. Reaching for them
while the failing paths are still in an unfloorplanned interconnect wastes hours
per attempt to recover a fraction of what a pblock file would have given for
free.

Keep the strategy settings in one file, applied by the build flow, and make sure
that file is actually the one in effect — a strategy file that no script sources
is a strategy nobody is using, and it will be quoted in a design review as if it
were.

## Checklist

When a build misses:

1. Read the ten worst paths. Note the module, the logic-level count, and the
   logic/route delay split.
2. Are they in **your** logic or in interconnect, reset, or address decode? If
   not yours, stop optimising the datapath.
3. Is the delay mostly **route**? If so it is a placement problem. Floorplan.
4. Do paths cross between dice? If so, is the block on the same die as the memory
   and hosts it uses? Fix that before anything else.
5. Is a single monolithic interconnect involved? Split it.
6. Check LUT percentage before believing a CLB percentage. Check control-set
   counts before believing a packing problem is a capacity problem.
7. Only then, pipeline.

## Open questions

- Region constraints are generated from the assembly description; the assembly
  description itself is currently project-specific. See [build.md](build.md).
- Crossing-register discipline is a convention, not something any script checks.
  A rule that "any interface which may span dice is registered in both
  directions" belongs in [the compute-unit port spec](../spec/compute-unit-port.md)
  rather than in a workflow page.
