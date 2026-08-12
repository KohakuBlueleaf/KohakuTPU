---
title: Clock domains
summary: The four domains, why the fabric has no crossing in it, and the retunable mesh clock.
tags:
  - architecture
  - physical
  - clocking
---

# Clock domains

A machine of this shape has four kinds of clock, and they are mutually
asynchronous:

| Domain | Carries | Notes |
|---|---|---|
| **mesh** | fabric, edge complex, compute units | one domain for the whole mesh, by construction |
| **control** | host bridge, debug bridge, control interconnect | **fixed** — see below |
| **memory** | one per memory controller | each on its own controller's user clock |
| **host** | the DMA engine's own interface | vendor IP |

Three consequences.

**The fabric has no clock crossing in it.** That is a property worth protecting:
the crossings are at the memory boundary — asynchronous FIFOs carrying their own
scoped constraints — and inside whichever vendor interconnect already spans
domains for the host. Adding a third place is a change to the architecture, not
a wiring detail.

**Asynchronous domains must be declared as such.** Otherwise the tool times
crossings that were never meant to be timed and spends its effort on paths that
do not exist.

**A constraint file is not a script.** Constraint parsing runs in a restricted
mode that rejects control flow — and rejects it as a warning rather than an
error, so the block is *silently skipped* and every crossing gets timed anyway.
Write constraints flat. This one is in
[workflow/tooling-traps](../../workflow/tooling-traps.md) because it costs hours
and leaves no obvious symptom.

## A retunable mesh clock

If the mesh frequency is baked in at build time, then "it did not close timing"
costs a full rebuild to try a lower number — the wrong unit of iteration for a
value nobody can predict in advance. Worse, it means the frequency at which the
silicon actually stops computing correctly is never measured: static timing
analysis is a verdict at worst-case process, voltage and temperature, and the
gap between that and reality is unknown and unknowable from the reports.

The arrangement that fixes both is **two clock generators**, because the control
plane must never stand on the clock it is changing:

```
   reference clk --+--> fixed generator  ---> debug bridge, interconnect,
                   |                          resets, and the reconfiguration
                   |                          port of the generator below
                   |
                   +--> variable generator --> every mesh
```

The variable one is an ordinary clocking primitive with dynamic reconfiguration
enabled, driven over a narrow bus clocked from the *fixed* domain. Changing the
frequency is a register write.

The arithmetic to get right when choosing its configuration is which multiplier
step the output moves by, and the temptation to push the phase-detector
frequency low for finer steps should be resisted twice: the multiplier field
saturates, truncating exactly the top of the range being hunted, and jitter
rises as the phase-detector frequency falls. Jitter is clock uncertainty on real
silicon; it eats setup margin the same way a slow path does, so a low-resolution
configuration measures the clock generator rather than the design. For finer
steps near a chosen frequency, use fractional multiplication at a high
phase-detector frequency.

Three rules come with it:

- **One knob, not one per mesh.** Anything spanning meshes — the interlink —
  shares a clock, so they retune together.
- **A retune resets every mesh.** On-chip state is lost; memory survives, since
  it is on its own controller and clock.
- **Quiesce first.** The interlink is credit-based, and retuning with packets in
  flight leaves credits inconsistent on both sides.

The sequence is: quiesce, retune, wait for lock, reset, re-initialise, re-upload
anything that lived on chip.

**Timing analysis only verifies up to the built-in frequency.** The tool
constrains the mesh clock from the generator's build-time configuration, so that
frequency is the *verified ceiling*: at or below it the design is analysed;
above it is a deliberately unmeasured sweep. Both are useful; they are not the
same claim, and a page that conflates them is wrong.
