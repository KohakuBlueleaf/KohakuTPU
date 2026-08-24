---
title: SIMT PE — the measurement ladder
summary: Why the design is parameters rather than branches, what each gate measured, and the one arithmetic identity the whole result rests on.
tags:
  - architecture
  - pe
  - gpu
  - simt
  - performance
---

# The measurement ladder

The SIMT PE exists to answer one question: **what does SIMT cost on this
fabric?** A number produced by building a SIMT core and comparing it to
somebody else's SIMD core answers nothing, because the two differ in the lane
count, the memory system, the ALU, the process and the tool version at the same
time.

So the design is arranged so the answer is an identity:

> **cost(SIMT) = G8 − G0**, on one fabric, one lane lineage, one memory system.

G0 and G8 are the same module with different generics. Everything between them
is a **controlled difference**: one gate turns one parameter, and its delta is
attributable to that parameter and nothing else.

## The rule that makes it true

**The ladder is parameters, not branches.**

A gate that is not built must elaborate *none* of its logic. If `HAS_IPDOM = 0`
left a stack in the netlist with its enables tied low, the tool would trim some
of it, keep some of it, and the G3 delta would be the cost of the parts it
happened to keep. `kht_unit` uses `generate` blocks whose unbuilt branches
contain no storage and no datapath, which is what makes *"the total is the sum
of the measured deltas"* true rather than intended.

**It leaks if you are not watching.** G8's first measurement had `HAS_SHFL = 0`
at 3,232 LUT against the pre-G8 build's 3,204: the network was correctly inside
a generate, but its *writeback mux input* was not, so 28 LUT of a gate that was
switched off survived. The fix is to make the select constant-false at
elaboration — `(HAS_SHFL != 0) && m_shfl` — so the input is trimmed. Worth 28
LUT? No. Worth the property? Yes: the rule is what every delta on this page
rests on, and a rule that holds "mostly" is not one you can add up.

The consequence is that there is **one synthesis script**
(`scripts/tcl/ooc_simt_pe.tcl`) for the whole ladder, and the rows are comparable
to each other by construction rather than by care.

## The gates

| Gate | Generics | What it adds | Built |
|---|---|---|---|
| G0 | `WAVES 1, HAS_MASK 0, HAS_IPDOM 0` | the arithmetic substrate: lanes, register file, writeback | yes |
| G1 | `WAVES 16` | wave-indexed storage — many wave contexts | yes |
| G2 | `HAS_MASK 1` | the active mask, `tmc`, predication | yes |
| G3 | `HAS_IPDOM 1` | `split`/`join`, the bounded stack, the overflow fault | yes |
| G4 | `HAS_LDSBANK` | divergent LDS addressing, bank conflicts in hardware | **yes** |
| G5 | — | the coalescer: one gather becomes one request when lanes agree | no |
| G6 | — | MSHRs: more than one miss in flight | no |
| G7 | `WAVES` on `kht_core` | the wave scheduler: waves genuinely issuing, not merely stored | **yes** |
| G8 | `HAS_SHFL` | the subgroup butterfly for `shflxor` and `bcast` | **yes** |
| G9 | `HAS_FLT`, `FLANES` | the float tier — and, riding its retire slot, RV32M integer multiply | **yes** |

**G9 is a gate on `kht_pe`, not on `kht_unit`**, and it is the one gate this page
does not carry a row for. Its deltas are measured on the assembled PE and live in
[status](status.md#read-this-before-quoting-a-lut-figure), because the arithmetic
it adds is inherited from the SIMD tier rather than built here — measuring it on
`kht_unit` would price a lane array this project deliberately does not own. That
is the same reason `cost(SIMT) = G8 − G0` stops at G8.

## What has been measured

All rows: top `kht_unit`, `LANES = 8`, `VREG_PRIM = block`,
`xcvu13p-fhgb2104-2L-e`, **out-of-context synthesis at 3.333 ns**, synth only —
these are not placed-and-routed figures and are not presented as such.

> **Provenance.** `python scripts/py/ooc_sweep.py gpu-ladder` writes
> `build/sweep_gpu-ladder.md`, and that file is the source for this table.
> Measured 2026-08-22 on the current tree. `HAS_SHFL = 0` on every row — the
> butterfly is G8's and must not be inside a G0–G3 figure.

| Gate | WAVES | mask | ipdom | LUT | ΔLUT | FF | BRAM | ctrl sets | Fmax |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| G0 | 1 | 0 | 0 | 2,952 | — | 307 | 8 | 2 | 324.1 MHz |
| G1 | 16 | 0 | 0 | 2,952 | **+0** | 311 | 8 | 2 | 324.1 MHz |
| G2 | 16 | 1 | 0 | 3,016 | +64 | 447 | 8 | 18 | 324.1 MHz |
| G3 | 16 | 1 | 1 | 3,204 | +188 | 516 | 8 | 36 | 324.1 MHz |

**Every SIMT gate built so far costs 252 LUT in total** — G3 minus G0, on eight
lanes, against a substrate of 2,952.

### G1 is free

**Sixteen wave contexts cost +0 LUT, +0 BRAM, +0 control sets and +4 FF.** Not
"almost nothing" — nothing, to within four flops of the wave-id register. The
register file was already a memory, so a wave id is *address bits*. Sixteen
contexts deepen a BRAM that was already there rather than adding one.

This is the single most load-bearing measurement on the page, because "many
resident contexts to hide memory latency" is the part of SIMT that sounds
expensive and is the whole reason the model tolerates a cache miss.

The caveat that used to sit here — *"`WAVES` sizes storage, not issue"* — has
been lifted: [G7](#g7-the-scheduler-storage-against-issue) builds the scheduler,
and it is measured as `WAVES` on `kht_core` against this same sweep on
`kht_unit`. **G1 remains a storage result**, and that is the point: subtracting
the two separates what wave contexts cost to *hold* from what they cost to
*schedule*.

### Fmax never moves

Every gate lands at **324.1 MHz**, identically. The SIMT machinery — the mask,
the stack, the wave indexing — is not on the binding path at eight lanes. The
binding path is in the lane array, where it was at G0.

This is worth stating because the usual objection to SIMT is that divergence
tracking is on the critical path. At this width, on this fabric, it measurably
is not.

### G3 was over its bracket, and why it is not now

G3 was first built with the IPDOM stack as an **indexed flop array**: `WAVES ×
IPDOM_D` entries behind a wide read mux and an equally wide write decoder. It
came in far over its `<+1k` bracket. That is a shape this project has been
billed for twice before — `khs_facc` at 29,409 LUT and `rv_l1`'s valid/dirty at
701, both fixed the same way.

Rebuilt as a `kohaku_sdpram` in `distributed` mode with `READ_LAT 0`, the whole
gate is **+188 LUT**, and the sweep shows where it went: G3 is the only row with
`lut_mem` non-zero, at **20 LUT of distributed RAM** — the stack itself. The
rest is the pointer, phase and fault logic, visible as control sets going 18 →
36.

`READ_LAT 0` is what keeps a `join` combinational, so the stack costs no cycle.

(The flop version's exact figure is not quoted here: it was measured before the
sweep suite existed and its log did not survive. The shape it belongs to is the
point, and the current number is cited.)

One word holds the pair a `split` pushes — `{outer, false}` — so two pushes are
**one write** and a single write port suffices. A phase bit per wave says which
half the next `join` takes.

## The register-file primitive

Not a ladder gate — a configuration choice, measured because it is the largest
single lever in the unit. At 8 lanes, 16 waves, full gate set.
Source: `build/sweep_gpu-vregprim.md`, via
`python scripts/py/ooc_sweep.py gpu-vregprim`.

| `VREG_PRIM` | LUT | of which LUTRAM | FF | BRAM | ctrl sets | Fmax |
|---|---:|---:|---:|---:|---:|---:|
| `block` | 3,204 | 20 | 516 | 8 | 36 | 324.1 MHz |
| `distributed` | 9,430 | 5,140 | 1,028 | **0** | 165 | 475.3 MHz |
| **Δ** | **+6,226** | +5,120 | +512 | −8 | +129 | **+151.2 MHz** |

`block` is the default and is what every ladder row above uses. **+6,226 LUT is
more than twice the entire G0 substrate**, and it is spent to trade 8 BRAM for
megahertz that are not needed — the clock is already met at 324.1.

The `vp-block` row is bit-identical to `g3-ipdom` in the ladder table, which is
the cross-check that the two sweeps measured the same design.

## Lane scaling

Not a ladder gate either — the lane count is the design's biggest free variable,
so it is measured rather than extrapolated from the 8-lane row. Full gate set,
16 waves, `block`. Source: `build/sweep_gpu-lanes.md`, via
`python scripts/py/ooc_sweep.py gpu-lanes`.

| LANES | LUT | LUT/lane | FF | BRAM | ctrl sets | Fmax |
|---:|---:|---:|---:|---:|---:|---:|
| 4 | 1,659 | 415 | 316 | 4 | **36** | 324.1 MHz |
| 8 | 3,204 | 401 | 516 | 8 | **36** | 324.1 MHz |
| 16 | 6,355 | 397 | 916 | 16 | **36** | 324.0 MHz |
| 32 | 12,478 | 390 | 1,716 | 32 | **36** | 324.1 MHz |

Both curves are straight to within noise across a factor of eight:

```
   LUT  =  112  +  386.4 x LANES        exact at 4, 8 and 32; +61 at 16
   FF   =  116  +  50    x LANES        EXACT at all four points
   BRAM =                1   x LANES
```

Three readings:

**Control sets do not move.** 36 at four lanes, 36 at thirty-two. The active
mask is a *write enable per bank* and the divergence state is *per wave*, not
per lane — so widening the array adds datapath and adds no control. This is the
[mask-is-a-write-enable](microarchitecture.md#the-mask-is-a-write-enable-not-a-datapath-input)
claim showing up as a measurement instead of an argument.

**Fmax does not move either — 324 MHz from 4 lanes to 32.** The reason is
structural and it comes with an expiry date: **there is no cross-lane network in
`kht_unit`.** Lanes are independent, so a wider array is wider and not deeper.

That caveat has since been collected on. [G4](#g4-the-banked-lds-and-the-first-gate-that-is-not-free)
is the first cross-lane network in the design, and it is both quadratic in area
and **below this clock at 32 lanes**. Do not extrapolate this row past a gate
that makes lanes talk to each other.

**The intercept is ~112 LUT.** Everything that is not a lane — the wave
pointer, the stack, the phase and fault logic — is about a hundred LUT. At any
useful width this unit *is* its lane array.

## G4: the banked LDS, and the first gate that is not free

`kht_lds` at the full gate set, `xcvu13p`, OOC at 3.333 ns. Source:
`build/sweep_gpu-lds.md`, via `python scripts/py/ooc_sweep.py gpu-lds`.

| LANES | LUT | ×2 growth | FF | BRAM | ctrl sets | Fmax |
|---:|---:|---:|---:|---:|---:|---:|
| 4 | 509 | — | 319 | 4 | 8 | 643.1 MHz |
| 8 | 1,633 | **3.2×** | 603 | 8 | 12 | 514.9 MHz |
| 16 | 6,194 | **3.8×** | 1,171 | 16 | 20 | **339.2 MHz** |
| 32 | 25,961 | **4.2×** | 2,307 | 32 | 36 | **317.7 MHz** |

```
   LUT  ~=  25 x LANES^2         every doubling costs FOUR times as much
```

**This is quadratic, and it is the resolver.** Finding the lowest outstanding
lane for each of LANES banks is a LANES × LANES comparison, and nothing about
that is hidden: the flops and BRAM stay linear (`FF ≈ 35 + 71 × LANES`, one BRAM
a bank) while the LUT goes as the square.

### It is also the first gate to touch the clock

Every gate up to G3 landed at 324.1 MHz and none of them moved it. This one
does:

```
   643 MHz  ->  515  ->  339  ->  318
    L=4         L=8     L=16     L=32
                                  ^^^^ BELOW the 324.1 the rest of the unit runs at
```

At 32 lanes the LDS resolver **becomes the binding path**. The
[lane-scaling](#lane-scaling) row's flat 324 MHz carried a caveat that it would
not survive a cross-lane network; G4 is the first cross-lane network, and it did
not.

### What this settles about the lane count

Against the 20–25k budget for the **assembled** PE (30k review, 35k ceiling):

| LANES | `kht_unit` | `kht_lds` | these two alone |
|---:|---:|---:|---:|
| 4 | 1,659 | 509 | 2,168 |
| 8 | 3,204 | 1,633 | **4,837** |
| 16 | 6,355 | 6,194 | **12,549** |
| 32 | 12,478 | 25,961 | **38,439** |

**32 lanes is out.** Two blocks alone exceed the 35k ceiling before the core,
the L1, the requestor, the port or any unbuilt gate is counted — and they do it
while missing the clock. 8 is comfortable; 16 is the interesting case and costs
about half its budget on these two.

If 16 or 32 lanes is wanted later, the answer is a **cheaper resolver**, not a
bigger budget: an all-to-all lowest-lane pick is the most expensive way to
resolve conflicts, and a staged or butterfly resolution trades passes for area.
That is a design question the ladder has now priced rather than assumed.

## G7: the scheduler, storage against issue

G7 lives in the front end, which `kht_unit`'s top cannot see. So it is measured
as `WAVES` on **`kht_core`** against the same sweep on **`kht_unit`**, and the
difference is scheduling rather than storage. Sources:
`build/sweep_gpu-sched.md` and `build/sweep_gpu-waves.md`.

| WAVES | `kht_unit` LUT | `kht_core` LUT | **scheduler** | core FF | core Fmax |
|---:|---:|---:|---:|---:|---:|
| 1 | 3,076 | 7,754 | — | 1,094 | 277.9 MHz |
| 2 | 3,105 | 8,054 | | 1,140 | 294.6 MHz |
| 4 | 3,097 | 8,231 | | 1,234 | 273.5 MHz |
| 8 | 3,148 | 8,661 | | 1,420 | 262.7 MHz |
| 16 | 3,200 | 9,653 | | 1,797 | 279.5 MHz |
| **1 → 16** | **+124** | **+1,899** | **+1,775 LUT** | +703 | flat |

**Sixteen waves cost +1,775 LUT to schedule and +124 LUT to store.** Fmax does
not trend with WAVES — the 263–295 MHz spread is noise, not a slope.

### This refines G1 rather than contradicting it

[G1](#g1-is-free) measured `WAVES` with the mask and IPDOM stack **off** and got
+0, which is right: the register file is a memory, so a wave id is address bits.
The `kht_unit` column above is the same sweep with them **on**, and it costs +124
LUT and +184 FF — because the active mask and the divergence stack are per-wave
arrays and those do scale.

So the honest statement is three-part, and the ladder can now make all three:

```
   wave contexts in STORAGE          +0 LUT     (the register file is a memory)
   wave contexts with MASK + IPDOM   +124 LUT   (per-wave arrays)
   wave contexts SCHEDULED           +1,775 LUT (the front end)
```

"Sixteen waves are free" is true only of the first line, which is exactly why
the other two are measured separately.

### One caveat on the `kht_unit` column: 3,200 against 3,204

The `WAVES = 16` row above is **3,200 LUT** (`build/sweep_gpu-waves.md`, tag
`gpu-kht_unit-l8-w16-m1-i1-s0-block-t3.333`). Three other sweeps report
**3,204** for a tag that is character-for-character the same one —
`build/sweep_gpu-lanes.md` at `l-8`, `build/sweep_gpu-shfl.md` at `sh-8-off`,
and `build/sweep_gpu-ladder.md` at `g3-ipdom`. The split is 3,180 + 20 LUTRAM
against 3,184 + 20; FF, BRAM and control sets agree exactly at 516 / 8 / 36.

Four LUT, 0.12%. It is recorded rather than reconciled because the two
candidate explanations cannot be separated from the sweep files alone: the
suites were run at different times and the tree moved between them (the same
row read 3,232 before the mux-trim fix), or synthesis is not bit-reproducible
on this module. **The station bus measured the opposite** — a configuration
re-synthesised separately came back bit-identical
(`docs/projects/kohakuaxi/station-bus.md` §2.4) — so "OOC runs repeat exactly"
is not a property to assume project-wide. It holds where it has been checked.

Nothing on this page turns on 4 LUT. It matters only if someone subtracts two
rows that came from different suites and reads the residue as a gate.

## G8: the butterfly, and why complexity class is the whole argument

`kht_unit` with `HAS_SHFL` off against on, everything else held. Source:
`build/sweep_gpu-shfl.md`, via `python scripts/py/ooc_sweep.py gpu-shfl`.

| LANES | off | on | **ΔLUT** | Δ per lane | Fmax off | Fmax on |
|---:|---:|---:|---:|---:|---:|---:|
| 4 | 1,659 | 1,927 | **+268** | 67 | 324.1 | 302.3 MHz |
| 8 | 3,204 | 4,139 | **+935** | 117 | 324.1 | 285.5 MHz |
| 16 | 6,355 | 9,374 | **+3,019** | 189 | 324.0 | 303.7 MHz |
| 32 | 12,478 | 18,478 | **+6,000** | 188 | 324.1 | 301.7 MHz |

```
   ΔLUT  ~=  39 x LANES x log2(LANES)
```

— which is a 32-bit mux per lane per stage, plus the mask-indexed control: the
network as designed, arriving as a measurement.

**The `off` column is bit-identical to the [lane-scaling](#lane-scaling) table** —
1,659 / 3,204 / 6,355 / 12,478 LUT, 316 / 516 / 916 / 1,716 FF, 324 MHz — which
is two independent sweeps agreeing exactly, and the proof that `HAS_SHFL = 0`
now elaborates *nothing*. Before the mux-trim fix that row read 1,666 / 3,232 /
6,393 / 12,358 at 312–323 MHz: the leak was costing clock as well as area, and
at 32 lanes it was costing 12 MHz.

### Against G4, at the same widths

This is the comparison the two gates exist to make. Both are cross-lane
networks; they differ in complexity class and in nothing else that matters:

| LANES | G8 butterfly (N log N) | G4 resolver (N²) | ratio |
|---:|---:|---:|---:|
| 4 | 268 | 509 | 1.9× |
| 8 | 935 | 1,633 | 1.7× |
| 16 | 3,019 | 6,194 | 2.1× |
| 32 | 6,000 | 25,961 | **4.3×** |

Per doubling of LANES, **G8 grows about 2–3.5× and G4 approaches 4×.** At four
lanes they are within a factor of two of each other; at thirty-two the resolver
costs four times the butterfly and is still climbing faster. That divergence is
the complexity class becoming visible, and it is the argument for replacing an
all-to-all conflict resolver with a staged one if wide lanes are ever wanted.

### G8 also costs clock

| LANES | 4 | 8 | 16 | 32 |
|---|---:|---:|---:|---:|
| ΔFmax | −21.8 | **−38.6** | −20.3 | −22.4 MHz |

The second gate to move the clock, after G4. At eight lanes it takes the unit
from 324.1 to **285.5 MHz** — the butterfly is `log2(LANES)` muxes deep in
series and lands squarely on the writeback path. Unlike G4 this does not get
*worse* with width: the depth grows as log, while everything it competes with
grows faster, so the cost is roughly flat at 20–39 MHz across an eightfold
change in LANES.

## Where every number on this page comes from

Seven suites, one script, one part — `xcvu13p-fhgb2104-2L-e`, Vivado 2024.2,
out-of-context **synthesis only**. Each writes a markdown file under `build/`
carrying a resource table, a per-clock Fmax table and a **Configuration table**;
the configuration row is what makes a figure quotable, because it carries the
top module, every generic and the target period.

| suite | what it varies | top | rows | section |
|---|---|---|---:|---|
| `gpu-ladder` | `WAVES`, `HAS_MASK`, `HAS_IPDOM`, one at a time | `kht_unit` | 4 | [the gates](#what-has-been-measured) |
| `gpu-waves` | `WAVES` 1→16 at the full gate set | `kht_unit` | 5 | [G7](#g7-the-scheduler-storage-against-issue) |
| `gpu-sched` | the same `WAVES` sweep one level up | `kht_core` | 5 | [G7](#g7-the-scheduler-storage-against-issue) |
| `gpu-lanes` | `LANES` 4→32 at the full gate set | `kht_unit` | 4 | [lane scaling](#lane-scaling) |
| `gpu-lds` | `LANES` 4→32 on the banked LDS | `kht_lds` | 4 | [G4](#g4-the-banked-lds-and-the-first-gate-that-is-not-free) |
| `gpu-shfl` | `HAS_SHFL` off/on at four widths | `kht_unit` | 8 | [G8](#g8-the-butterfly-and-why-complexity-class-is-the-whole-argument) |
| `gpu-vregprim` | `VREG_PRIM` block vs distributed | `kht_unit` | 2 | [the primitive](#the-register-file-primitive) |
| `gpu-pe` | `LANES`, `WAVES` on the **assembled PE** | `kht_pe` | 4 | [the assembled PE](#the-assembled-pe-across-shapes--the-baseline-superseded-at-816) |

Every row above holds `period = 3.333` and `VREG_PRIM = block` unless the suite
is `gpu-vregprim`. `HAS_SHFL = 0` on every suite except `gpu-shfl` and
**`gpu-pe`**, which builds the butterfly because the shipped PE carries it — so
a `gpu-pe` row is not comparable to a G0–G3 row without accounting for G8.

```
python scripts/py/ooc_sweep.py gpu-ladder      # -> build/sweep_gpu-ladder.md
```

`ooc_sweep.py` runs each configuration in its own working directory under its
own Vivado, four at a time for the GPU suites. The Tcl emits `@@@REC`,
`@@@FMAX` and `@@@HIER` lines from **one** synthesis
(`ooc_record` in `scripts/tcl/ooc_class.tcl`) and the script collects them, so
the resource table, the Fmax table and the hierarchical breakdown all describe
the same netlist and no two rows can disagree about which design they measure.

Two guards in that path are worth knowing about, because both failed silently
once. `ooc_simt_pe.tcl` **errors if `get_clocks` comes back empty** — a Vivado
timing query returns nothing rather than failing, and an unconstrained run
reports no Fmax line while every LUT figure is the unconstrained one, which
reads exactly like a clean design. And `ooc_record` reads `Block RAM Tile` as a
**double**, because a lone RAMB18 makes the row 26.5 and an integer test drops
it — reporting 0 BRAM for a design that uses it.

## What the ladder does not price: arithmetic

**Every gate on this page is measured on an integer-only lane array.**
`kht_valu` is LANES copies of the base RV32I ALU — ten operations, no multiplier
and no float *inside that module* — so `G0` is `G0(int)`, and the whole ladder
measures *what SIMT costs around a lane* rather than what the lane itself must
eventually contain.

That is the right scope for the SIMT question and the wrong scope for a budget.
It is also no longer a description of the PE. **The shipped PE has eight float
lanes and RV32M**, and they arrive as sibling modules — `kht_fpu` and `kht_imul`
beside `kht_valu`, not inside it — which is exactly why the ladder never sees
them and why the ladder's totals must never be quoted as the PE.

| what | where it is | on the ladder? |
|---|---|---|
| ten RV32I lane operations | `kht_valu`, inside `kht_unit` | **yes**, G0–G8 |
| eight float lanes | `kht_fpu`, inside `kht_unit` at `HAS_FLT` | no — G9, measured on the PE |
| `mul`/`mulh`/`mulhsu`/`mulhu` | `kht_imul`, beside it, same retire slot | no — G9, measured on the PE |

### Lanes are a separate purchase from threads

```
   8 threads over 8 float lanes  ->  1 cycle per float instruction   <- BUILT
   8 threads over 4 float lanes  ->  2 cycles per float instruction
   8 threads over 2 float lanes  ->  4 cycles per float instruction
```

The built configuration is interval **1**, so no walk sequencer exists in either
PE class. The same trade applies to **integer** — an all-float shader should not
pay for eight idle ALUs, so `ILANES <= LANES` with interval `LANES / ILANES` is
wanted on both sides — and neither side has it. Resolving that is `ask-03`, and
it belongs to the DSP realm because both PE classes inherit the arithmetic.

`FLANES` exists as a parameter, and that is *not* the same as the configuration
being expressible: `kht_fpu` ties the lanes above `FLANES` to zero rather than
sequencing them, so `FLANES < LANES` returns zeros today rather than taking more
cycles. An earlier revision of this section priced 2 and 4 float lanes as
ESTIMATES from the SIMD tier's rate (`1,270 + 635 × lanes`); those rows are
withdrawn — the eight-lane point is measured and the reduced points are not
buildable.

### What this means for `G0`

The headline identity is `cost(SIMT) = G8 − G0` on one lane lineage. **Today's
G0 is `G0(int)`** — an integer-only lane array — and it stays that way on
purpose, because the float lane is the SIMD tier's and pricing SIMT around a lane
this project did not design would answer a different question. If a second G0 is
ever built against the float lineage, the subtraction must be done **within** one
of them. A `G8 − G0` quoted without naming which lane array it stands on is not
a result.

## Reporting rules

Two rules, both of which exist because breaking them produces a number that
looks authoritative and is not.

**Name which G0.** The headline is `G8 − G0`, and today's G0 is **`G0(int)`** —
the base RV32I ALU replicated, with no float lane and no multiplier. An earlier
revision of this page claimed G0 was measured against a float lane tier; it was
not, and that sentence is what allowed an integer-only figure to be read against
a float-capable budget. Any other lane array produces a second G0 and a second
headline. **A G8 total quoted without naming its lane array is not a result.**

**Synth is not route.** Every figure here is out-of-context synthesis. This
project has measured a module lose 0.740 ns between synthesis and routing; a
small negative slack at synth is not something placement absorbs. These numbers
size the design and rank the gates. They do not close timing, and nothing here
claims a closed clock.

**Name the flatten.** Every row on this page is `-flatten_hierarchy none`, and
**`none` is not the ship**. Nothing in `scripts/tcl` sets `FLATTEN_HIERARCHY` on
the ship's synthesis run, so it takes Vivado's own default, which is `rebuilt`.
Measured on the assembled PE at the same ask: **22,257 LUT at `none` against
21,621 at `rebuilt`** — `none` reads **636 LUT high**, because a preserved
boundary cannot trim an unread output port or fold a constant across a module
edge. `none` is what makes the per-block rows attributable and it stays the
diagnostic; a row quoted against a budget must be `rebuilt`. `ooc_simt_pe.tcl`
defaults to `rebuilt` for that reason.

**Name the top.** Every gate through G8 is measured on `kht_unit`, and a ladder
whose top is one submodule **cannot see a path that leaves it**. Synthesising
`kht_core` for the first time — for G7 — found it at **71.7 MHz** while the unit
inside it closed at 324: the cross-lane reduction was a serial chain, 44 logic
levels, and it lives in `kht_core` where no row on this page looked. Fixed as a
tree. A gate's Fmax column is a statement about **the top it was measured on**,
and nothing else.

## Shrinking it: ten attempts, three that paid

A LUT-reduction campaign on the assembled PE, all at 8 int + 8 float lanes, 16
waves, 2.857 ns. **Result: 21,621 → 20,086 LUT at `rebuilt`, −1,535 (−7.1%),
and 364.8 → 392.0 MHz.** Measured on the tree as of 2026-08-23 16:08.

Three changes took −1,745; a fourth, required for spec compliance rather than for
area, gave +210 back. It is in the table because a mandated change still has a
price and the price should be on the record.

The per-signal figures below are **LUT primitives** from `ooc_lut_census` at
`none`, which is why they do not add up to the CLB-LUT-site totals above.

### The three that paid

| change | signal | before | after | delta |
|---|---|---:|---:|---:|
| `kht_valu`: one carry chain for add/sub/slt/sltu where four expressions built four; the three logic ops collapsed into one LUT6; result case 9 arms → 5 | `u_vt/u_alu/y` | 2,909 | 1,901 | **−1,008** |
| `kht_core`: `rnode`'s `f7` decodes hoisted out of the tree, one signed comparator serving both max and min, and/or sharing a select | `g_rlvl.g_rn.g_live.rq` | 1,290 | 677 | **−613** |
| `kht_unit`: the vector-file write mux named once instead of written out on both the port and the probe; `dbg_wr_data` narrowed to lane 0, the only part anything reads | `u_vt/dbg_wr_data` | 238 | below cut | **≈−238** |

### The one that cost, and was made anyway

`kht_predec.v`'s float bound moved from `KHT_FLT_VFSUB_H` to `KHT_FLT_VFRSQRT_H`,
so `funct7` 12–15 stop decoding illegal. **+210 LUT and +4.7 MHz at `rebuilt`**
(19,876 → 20,086, 387.3 → 392.0), cleanly attributable: no shared source moved
between the two runs.

The cost is real and diffuse — the census's top rows are unchanged within noise
and the 206 extra LUT primitives are spread over ~1,670 signals. The mechanism is
that a fault decision moved OFF the predecoded path: those four encodings used to
be `C_ILLEGAL`, one stored bit computed once on the write side, and are now
`is_flt` faulting through `unbuilt`, which is live logic in `kht_core`.

It also **moved the binding path onto the predecoder**: `gw_buf_reg[97]` →
`u_ictl/u_ram/.../DINADIN[2]`, 8 levels, the CU_DATA granule walking through
`kht_predec` into the control RAM. That path has no `HAS_FSFU` dependence, which
is why the seeds-on and seeds-off builds now report the *same* 392.0 MHz.

### The seven that did not, and the number that killed each

| attempt | result |
|---|---|
| one shared PC incrementer instead of `WAVES` of them | `nxt` **+15**, and it became the binding path at `rebuilt` — 12 levels, 4 CARRY8 |
| one address adder a lane instead of two and a mux | `ea_all_q` **+41** |
| the float retire folded into the writeback mux as a sixth arm | −145 LUT but **361.5 → 339.4 MHz** |
| a bidirectional barrel shifter instead of one direction between two bit reversals | `u_vt/u_alu/y` **+193** |
| `kht_fpu`'s operand select hoisted, the vfsub sign flip folded into the addend mux | **±0** |
| `kht_imul`'s product narrowed 66 → 64 bits (exact for all four signednesses) | **±0** LUT, **±0** DSP |
| a dual-format `vec_cvt` converter picking on `wide` inside | **+429** at `none`, **+578** at `rebuilt` |

### What the seven have in common

**Vivado already shares carry chains across a generate loop, and it packs its own
inferred shifter tighter than a hand-written one. What pays is reducing the
number of distinct functions feeding ONE mux, and not writing the same wide mux
twice. Sharing arithmetic operators does not.**

That contradicts the instinct the campaign started with, which is why the seven
rows are here: re-running them costs a day and buys nothing. The converter row
generalises furthest — the FP16 and FP32 conversions share **no** datapath (one
is a leading-one search and a shift, the other a 15-bit round), so choosing
between their *results* is one mux and choosing *inside* them is three: exponent,
significand, and the format entering the specials.

### `HAS_FSFU` is default-off by measurement

At `rebuilt`, the four seeds cost, on the assembled PE:

| | LUT | FF | BRAM | DSP | MHz |
|---|---:|---:|---:|---:|---:|
| `HAS_FSFU = 0` | 20,086 | 17,282 | 30.5 | 48 | 392.0 |
| `HAS_FSFU = 1` | 22,369 | 20,073 | 42.5 | 56 | 392.0 |
| **cost** | **+2,283** | +2,791 | **+12** | **+8** | **±0** |

Four PEs of that is **+9,132 LUT**. Off is a priced decision, not a preference.

The +12 BRAM tiles are 24 RAMB18, which is **3 per lane** — `vec_tables`, one ROM
per coefficient. The +8 DSP is **one per lane**: DSP-P, the polynomial multiply,
which at `HAS_FSFU = 0` folds away and leaves the lane at 2 DSP rather than 3.
That fold is the direct evidence the seed tables are gone at the default, and it
is why an `HAS_FSFU = 0` float lane is not comparable with one that issues
min/max/compare or the seeds. **±0 MHz because both builds bind on the
predecoder**, which does not depend on this parameter.

**The seeds are reachable at both widths.** The decode now admits `funct7` 12–15,
so `vfexp2_h`/`vflog2_h`/`vfrcp_h`/`vfrsqrt_h` are no longer illegal — the ISA
table defined them and the RTL refused them, which broke the rule that an
optional float feature supports both input formats.

### What is checked, and how

Per case, not per bench, because a blanket tolerance over a bench containing
exactly-reproducible cases is how an exact case silently stops being exact.

| | how | bound |
|---|---|---|
| **specials** — zero, −0, both infinities, NaN, all four ops, both widths | **EXACT** | — |
| `vfexp2` on finite data | tolerance | 0.509 ulp |
| `vflog2` | tolerance | 0.638× its limit (0.99 ulp, or 2⁻¹⁸ absolute near zero) |
| `vfrcp` | tolerance | 0.546 ulp |
| `vfrsqrt` | tolerance | 0.549 ulp |

The finite path is a float64 reference rather than bit-exact because `vec_alu`
computes it from a 32-segment table plus a range reduction; the bounds are
`vec_alu_tb`'s own measured worst case. `rv_simd_fsfu_test.py` additionally holds
the finite path to 1e-4 relative, so a specials fix cannot pass while the
arithmetic rots.

**WHY THE SPECIALS ARE PINNED EXACTLY AND NOT TOLERANCED.** `rsqrt(-0)` returned
`+inf` where IEEE requires `-inf` — `vec_alu`'s `OP_RSQRT` hardcoded
`spec_sign_c = 1'b0` while `OP_INV` two blocks up took the sign from its input
and was right. **No kernel in the library issues `vfrsqrt` at all**, so no
workload could ever have found it, and no tolerance would have either: the
magnitude was infinite and correct, and only the sign was wrong. It was found by
reading what IEEE requires and then checking the RTL against it, which is the
only instrument that reaches a corner nothing executes.

Two cautions from finding it. **Derive the expectation before reading the RTL** —
a corner transcribed from the hardware makes the bench defend whatever the
hardware does, and the first version of that assertion pinned the defect. And
**a corner can be checked carefully along the wrong axis and still be wrong**: the
assertion read "= +inf, *not NaN*", so it was checked on the inf-versus-NaN axis,
which is where a different bug had just been, and the sign was never questioned.

## Budget

The target for the assembled PE is **20–25k LUT**, with 30k a review line and
35k a ceiling.

**The shipped PE is inside it, with the whole arithmetic tier in the number:**
**21,586 LUT at 365.6 MHz**, 8 int + 8 float lanes with RV32M, at the
2.857 ns ask ([status](status.md#the-configuration-of-record)). For scale on the
same device: the base controller PE is 2,477 LUT and the SIMD PE at SIMD 8 +
4 float lanes is **13,772 LUT, measured** at the same ask — a figure that was
15,119 DERIVED when this section was first written and could not then be built.

**Every figure under the target on this page is still integer-only**, and that is
the trap the band exists around: the ladder tops out at `kht_unit` and the budget
is about `kht_pe`. The two differ by the whole front end, the L1, the requestor,
the fabric port *and* the arithmetic tier.

**An earlier revision of this section said the scheduler and the butterfly were
"where the budget will actually go, and none of them is measured yet." Both are
now measured**, so is the LDS, so is the assembled PE, and so — on the PE — is
G9. What is left unmeasured is G5 and G6.

Everything measured so far, at 8 lanes, `block`, 16 waves, each row naming the
top it was synthesised on:

| top | what it contains | LUT | Fmax | ask | source |
|---|---|---:|---:|---:|---|
| `kht_unit` | G0–G3, `HAS_SHFL 0` | 3,204 | 324.1 MHz | 3.333 | `build/sweep_gpu-ladder.md` |
| `kht_unit` | G0–G3 **+ G8** | 4,139 | 285.5 MHz | 3.333 | `build/sweep_gpu-shfl.md` |
| `kht_lds` | G4, on its own | 1,633 | 514.9 MHz | 3.333 | `build/sweep_gpu-lds.md` |
| `kht_core` | the pipeline, **`kht_unit` inside it** | 9,653 | 279.5 MHz | 3.333 | `build/sweep_gpu-sched.md` |
| `kht_pe` | the assembled PE, integer-only — the **baseline** | 16,115 | 182.0 MHz | 3.333 | `build/sweep_gpu-pe.md` |
| **`kht_pe`** | **the shipped PE — + G8, G9 and RV32M** | **21,586** | **365.6 MHz** | **2.857** | `build/sweep/g-350-pad` |

**Only the last two rows are a PE, and the rows above them must not be added up.**
`kht_core` contains a `kht_unit`, so 9,653 already includes one of the first two;
whether it also contains the LDS is not something a column of separate OOC runs
can tell you. The 20–25k budget is about the last row and nothing else.

### The assembled PE across shapes — the BASELINE, superseded at 8×16

`kht_pe` with the full gate set and `HAS_SHFL = 1` — G8 is **in** these rows,
unlike every G0–G3 figure on this page. `VREG_PRIM = block`, 3.333 ns,
`xcvu13p-fhgb2104-2L-e`, OOC synth, **integer-only lane array**. Source:
`build/sweep_gpu-pe.md`, via `python scripts/py/ooc_sweep.py gpu-pe`.

| LANES | WAVES | LUT | of which LUTRAM | FF | BRAM | ctrl sets | Fmax |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | 16 | 11,487 | 3,538 | 6,595 | 11 | 154 | 174.9 MHz |
| 8 | 1 | 14,709 | 2,836 | 6,642 | 19 | 106 | 190.5 MHz |
| **8** | **16** | **16,115** | 3,548 | 7,342 | 19 | 162 | **182.0 MHz** |
| 16 | 16 | 29,961 | 3,568 | 8,834 | 35 | 178 | 170.9 MHz |

**The 8×16 row is where the frequency campaign started, not where it ended.** It
is an integer-only machine with no multiplier; the shipped PE at that shape is
21,586 LUT at 365.6 MHz. The 4- and 16-lane rows have never been re-swept, so
they remain the only figures that exist for those widths and they carry the same
float-free, multiply-free lane array.

**Sixteen lanes is 29,961** — past the 25k target, at the 30k review line, and
that is *before* the arithmetic tier. The [lane-scaling](#lane-scaling) and
[G4](#g4-the-banked-lds-and-the-first-gate-that-is-not-free) rows put
`kht_unit + kht_lds` alone at 12,549 there; assembled it is 2.4× that, and the
LUTRAM column says where the rest is not — 3,538 to 3,568 across a fourfold
change in LANES, so the growth is all logic.

### The clock was the result, and then it was fixed

Every submodule row on this page sits at 324.1 MHz. **The assembled PE first sat
at 170–190** — 182.0 MHz at the shipped 8 × 16 shape, against what was then a
300 MHz mesh clock. It did not close, and no arrangement of the rows above could
have told you that.

That is the third and most expensive time [name the top](#reporting-rules) has
been collected on:

| top | Fmax | found by |
|---|---:|---|
| `kht_unit` | 324.1 MHz | the ladder |
| `kht_core` | **71.7 MHz** when first synthesised | measuring G7 — a 44-level serial reduction |
| `kht_pe` | **182.0 MHz** when first synthesised | this suite |

**The binding path was then read out nineteen times over, and the PE now closes
365.6 MHz with the whole arithmetic tier in it** — see
[the frequency campaign](status.md#the-frequency-campaign). Every fix was found
by reading the reported critical path, and almost all of them were the same fix:
a cone that starts at a block RAM begins a third of its budget in debt.

A gate's Fmax column is still a statement about the top it was measured on.
**No frequency claim for this PE may be taken from a `kht_unit` row** — including
the reassuring ones.
