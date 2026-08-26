---
title: Clock domains
summary: Which domains exist, where each boundary between them falls, why the fabric has none inside it, and how a domain's frequency is changed at runtime.
tags:
  - architecture
  - physical
  - clocking
---

# Clock domains

A machine of this shape has four kinds of clock, and they are mutually
asynchronous:

| Kind | Carries | Notes |
|---|---|---|
| **fabric and units** | the mesh's routers, the system node, the compute units | several clocks, one **domain boundary set** — below |
| **control** | host bridge, debug bridge, control interconnect | **fixed**, never retuned; the plane that retunes the others stands on it |
| **memory** | one per memory controller | each on its own controller's user clock |
| **host** | the DMA engine's own interface | vendor IP |

The first row is the one with structure in it, so it is worth opening.

## What a ship's clock boundary actually is

A **ship** — one complete floorplanned assembly, see
[ship](../ship/what-is-a-ship.md) — does not take one clock. It takes up to six
clock inputs and two resets, and which of them are distinct *domains* is an
elaboration choice:

| Port | Carries | Present |
|---|---|---|
| `axi_aclk` / `axi_aresetn` | every AXI and AXI-Stream interface on the boundary: the memory window, the control window, and the interlink's stream ports. It is the **system node's** clock — those interfaces all terminate inside the node | always |
| `noc_clk` | the fabric. Router to router, and **no interface at all** | always |
| one clock per endpoint type | the compute units of that type | always; tied to `noc_clk` when per-unit clocking is off |
| a doubled clock | a compute unit that internally runs a datapath at twice the fabric rate | only when such a unit is built |
| `dram_aclk` / `dram_aresetn` | the master to the memory controller | only on the concentrated memory boundary, which crosses into the memory's domain inside the ship |

So the useful statement is not "a ship has one clock". It is:

> **Every interface on a ship's boundary belongs to exactly one named clock, and
> the port attributes on the module say which.** Interface-inference attributes
> name each bus and its associated clock and reset, so a block design ties them
> up without hand-wiring, and a clock that carries no interface is visibly one
> that carries no interface.

Neither clock nor reset carries a direction prefix, because the same pair serves
masters and slaves alike within its domain.

## Three consequences

**The fabric has no clock crossing in it.** Router to router is one clock,
untouched — and that is load-bearing rather than tidy, because the deadlock
argument for the routing rule assumes it. Crossings live at the *edges* of the
fabric: on a compute unit's network port, on the system node's attachment, at
the memory boundary, and inside whichever vendor interconnect already spans
domains for the host. Adding a crossing anywhere else is a change to the
architecture, not a wiring detail.

**Per-unit clocking is one rate per unit *type*, not per instance.** When it is
enabled, each unit sits behind one crossing FIFO per direction on its own
network port, and every unit of the same type takes the same clock. That keeps
the number of domains equal to the number of *kinds* of thing on the mesh rather
than to the population, which is what makes the arrangement affordable at all.
Disabled, those clock ports are tied to the fabric clock and the FIFOs are not
built.

**Putting the system node behind the same kind of crossing is not free, and not
optional for the reason you would guess.** With the node clocked directly from
the fabric, its encoder's busy signal reaches a router's flow control
combinationally — and that path, not anything in a compute unit, has repeatedly
been the mesh's worst. Crossing the node onto its own clock breaks it. The
crossing FIFO's flags stay registered even when both sides are driven from one
clock, so enabling the crossing is a timing decision that does not have to be a
frequency decision.

## A doubled clock is one generator's two outputs

A datapath that runs at twice the fabric rate takes a second clock, and the two
must come from **one** source: both derived by dedicated dividers off a single
buffer input, sharing a clear, or the pair phase-shifts against each other and
the ratio the design assumes stops being true.

The same fact constrains what a runtime retune can do. **One clock generator has
one voltage-controlled oscillator**, so its several outputs are several dividers
of one number — never several independent frequencies. Retuning a generator
moves everything it drives, in fixed ratio. That is a property to design the
profile table around, not one to discover when two outputs refuse to take the
values asked for.

## Reset enters each domain separately

Only the top-level reset crosses domains. Each domain takes it as an
**asynchronous assert with a synchronous release** at that domain's own clock,
through one small synchroniser per domain, so no register anywhere is released
by an edge of a clock it does not run on.

The fabric is the exception, and for a stated reason: the reset block feeding it
already releases on the fabric clock, so it takes the top-level reset raw rather
than re-synchronising something that is already synchronous to the right thing.
A second synchroniser there would add a cycle of skew between the fabric and
nothing else.

## Asynchronous domains must be declared as such

Otherwise the tool times crossings that were never meant to be timed and spends
its effort on paths that do not exist. Two failure modes, opposite and with the
same symptom:

- a genuinely asynchronous pair left ungrouped — a correct crossing FIFO is
  reported as failing setup **and** hold by a wide margin;
- a ratio-locked pair grouped as asynchronous — a real failure is hidden.

Decide which case applies before reading the number.
[workflow/measure](../../workflow/measure.md#5-ratio-locked-clocks-need-a-multicycle-path)
has both, with the constraints.

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
silicon; it eats setup margin the same way a slow path does, so a
low-resolution configuration measures the clock generator rather than the
design. For finer steps near a chosen frequency, use fractional multiplication
at a high phase-detector frequency.

Three rules come with it:

- **One knob per mesh, not one per clock.** A generator's outputs move together
  anyway, and anything spanning meshes — the interlink — shares a rate, so
  meshes joined by a link retune together.
- **A retune resets the mesh.** On-chip state is lost; memory survives, since it
  is on its own controller and clock.
- **Quiesce first.** The interlink is credit-based, and retuning with packets in
  flight leaves credits inconsistent on both sides.

The sequence is: quiesce, retune, wait for lock, reset, re-initialise, re-upload
anything that lived on chip.

### The resting state is low, and a boost is a lease

Once frequency is a register write, it becomes a policy question rather than a
build-time one, and the policy that survives contact with a real card is:

- **A set of named profiles**, each fixing every output of a mesh's generator at
  once — because they are dividers of one oscillator and cannot be set
  independently.
- **A resting profile that every mesh sits at whenever nothing holds it up.**
  The card has no serious cooling; running at the built ceiling while idle buys
  nothing and costs thermal headroom that a real run wants.
- **Boosts are scoped leases.** A run declares the level it needs and the meshes
  it will touch; only those meshes are raised, held by a token, and dropped back
  once released and an idle window has passed. Dropping immediately on release
  would make back-to-back runs thrash the generators.
- **Reprogramming the device voids all of it.** Loading a bitstream returns
  every generator to its *built* configuration, which is full speed, whatever
  policy last applied. The step that re-applies the resting profile therefore
  belongs immediately after programming, before anything else touches the card.

The profile table is a property of a board rather than of the framework: the
reference instance's lives in its board description, `boards/multimesh_v7t.json`,
which names the levels and the frequency each output takes at each.

## The built-in frequency is the verified ceiling

Timing analysis only verifies up to the frequency the generator was configured
for at build time. The tool constrains the mesh clock from that configuration,
so at or below it the design is analysed; above it is a deliberately unmeasured
sweep. Both are useful. They are not the same claim, and a page that conflates
them is wrong — see [measurement](measurement.md).
