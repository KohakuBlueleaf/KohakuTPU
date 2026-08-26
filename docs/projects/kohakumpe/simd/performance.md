---
title: SIMD PE performance
summary: What the vector unit costs and what it buys in cycles, kept apart; how a figure here is obtained and what a flatten setting does to it; the critical path read out; and which previously published figures were withdrawn, with the reason for each.
tags:
  - architecture
  - pe
  - simd
  - performance
---

# SIMD PE performance

> **Kind: none — this page reports measurements of parts labelled elsewhere.**
> Cost and benefit here are properties of this project's own configurable unit,
> not of the framework. The withdrawn-figures section is the page's most
> transferable content, and it is a measurement convention rather than a result.

Two independent questions, and this page keeps them apart: **what the vector
unit costs**, which is a synthesis result, and **what it buys**, which is a
cycle count on real programs.

## Reading the numbers on this page

Resource and frequency figures are out-of-context **synthesis** on
`xcvu13p-fhgb2104-2L-e`, Vivado 2024.2. Nothing is placed or routed, so every
frequency is an upper bound on what a placed design would reach and not a closed
result — this repository has measured a module lose 0.740 ns between synthesis
and routing.

Every table names its target period, its `-flatten_hierarchy` setting and the
configuration it was taken at. Rows taken at different periods are not
comparable to each other and are never subtracted.

Cycle figures are read from the PE's own cycle counter on the full system: one
PE, real routers, the real memory agent, RAM behind it.

**Absolute totals for this PE are in [unit-counts](../unit-counts.md), with the
tree each was measured on named.** They are not repeated here, because they
predate the current float tier and repeating a superseded total in two places is
how one of them ends up quoted without its caveat.

## Price a lane marginally, never by dividing the tier

This is the single most important rule for reading any figure about this PE, and
it is easy to get wrong in a direction that manufactures a defect.

A float tier is not `units × cost(unit)`. It is `units × cost(unit)` **plus a
fixed overhead that does not scale with units at all**: the third register-file
read port the fused multiply-add's addend needs, the retire path, the
scoreboard, and the pass sequencer. On this PE that overhead has been fitted at
several hundred LUT and it is paid once, at any nonzero unit count.

So dividing a tier's total by its unit count charges the units for the
overhead, and the error is not small. The **marginal** unit on this PE measures
**1,095 and 1,003 LUT** at the two steps where it was taken, and on the SIMT PE
**789 and 1,104** — the two are the same arithmetic, and their marginals bracket
each other. Every average ever computed from a tier total on either PE has said
one of them is far dearer than the other, and every one of those was a
measurement of the overhead rather than of a unit.

**The average is not a worse estimate of the marginal cost; it is a measurement
of a different thing.** [unit-counts](../unit-counts.md#4-the-per-unit-arithmetic-named)
writes out the subtraction behind every per-unit figure for exactly this reason.

## The flatten trap: `rebuilt` for totals, `none` for attribution

**The ship does not synthesise at `none`.** Nothing in the build scripts sets
`FLATTEN_HIERARCHY` on the ship's synthesis run, so it takes Vivado's own
default, which is `rebuilt`. Any out-of-context script that hard-codes `none` is
measuring a PE the ship does not build.

**The gap is configuration-dependent, so it cannot be carried between rows.** On
this PE it has measured 647 LUT at one setting of a knob and 243 LUT at another,
because at the first setting *all* of the difference was the tool inferring DSP48
post-adders that the RTL placed explicitly at the second. Extrapolating one
figure across the knob was wrong.

**The two settings answer different questions and must not be mixed in one
table:**

| question | setting | why |
|---|---|---|
| what does the PE cost; what does a knob save | **`rebuilt`** | it is what the ship builds |
| which block inside the unit is the cost | **`none`** | boundaries survive, so the census names real cells |

`rebuilt` **re-parents leaves**, so a per-block row taken there is not that
block's cost. From a reference build, the vector register file reported
**5,657 LUT while holding 444 LUTRAM** — it had absorbed several thousand LUT of
logic that is not its own. **A subtraction between two `rebuilt` totals is
sound; a `rebuilt` hierarchy row is not.**

## What each feature buys, against the kernel that uses it

Every cycle pair is the same workload written twice, scalar and vector, from
`tests/pe/tools/rv_simd_kernels.py`, run on the assembled PE. **Both writeback
settings are given**, because the bench does not default to the shipped one:
`rv_pe` defaults `SIMD_WB` to 1 and the component bench defaults it to 0.
`SIMD_WB = 1` registers the vector result before the write, which makes a
distance-2 dependency a hazard that does not otherwise exist — so it costs
cycles and changes no answers.

| feature | kernel | scalar | vector `WB=0` | vector **`WB=1`** | speedup `WB=0` | **speedup `WB=1`** |
|---|---|---:|---:|---:|---:|---:|
| the permute — `vsldw`, `vunpk`, saturating `vpack` | `fir_i16_v` | 3,407 | 559 | **745** | 6.1× | **4.6×** |
| " | `epilogue_v` | 8,028 | 245 | **325** | 32.8× | **24.7×** |
| the packed shift — `vslli` / `vsrari` | `epilogue_v` | 8,028 | 245 | **325** | 32.8× | **24.7×** |
| the reduction trees | `reduce_i32_v` | 3,093 | 251 | **283** | 12.3× | **10.9×** |
| vector `vld` / `vst` | `memcpy32_v` | 783 | 239 | **271** | 3.3× | **2.9×** |
| **the seed units** | **none** | — | — | — | — | — |
| **the float tier itself** | **none** | — | — | — | — | — |

**No verdict moves between the two columns.** The shipped writeback costs between
1.9% and 33.3% of a vector kernel's cycles, and the worst speedup loss is 25.0%.
Every feature still wins by 2.9× to 24.7×, so none of them was an artefact of a
writeback the card does not have.

**The last two rows are the weakest part of this page and are stated rather than
omitted.** The float tier is the largest single block in the PE and no kernel in
this repository issues a float instruction. The question that raises is not
whether the integer extras earn their LUT — measured, they do — but whether this
PE has a float workload at all, which is a compiler question and not an RTL one.

### Why the two writeback columns are comparable

Both were taken on the same tree in the same session, differing only in the
writeback define. That is the claim; **this is the control that tests it.** The
writeback is inside the vector result path and touches nothing else, so a
**scalar** kernel must be unaffected by it — and all seven scalar kernels are
identical **to the cycle** across the pair. If anything else had drifted between
the runs — a generator change, a different RTL revision, a stale image — it would
almost certainly have moved one of the seven. **A paired cycle measurement
without a control of this kind is two runs, not a comparison.**

The second half of believing it is that the cost lands where the mechanism
predicts rather than evenly. The two kernels built from long chains of dependent
vector operations pay +33%, and distance 2 is exactly what the shipped writeback
turns into a stall; the kernel that was never dependency-bound pays two cycles.
**A cost that lands where the hazard's shape predicts is a measurement; one that
landed evenly would have been a reason to look again.**

## What the instructions buy, in cycles

Kernel-only cycles, same PE, same data, same independently computed reference
for both forms, on the integer-only build at `WB_STAGE = 0`.

| kernel | scalar | vector | speedup |
|---|---:|---:|---:|
| requantise epilogue, 256 elements | 8,025 | 242 | **33.2×** |
| int32 sum and signed max, 256 | 3,090 | 248 | **12.5×** |
| 8-tap int16 FIR, constant taps | 3,404 | 556 | **6.1×** |
| 256-word copy | 780 | 236 | **3.3×** |

Two readings matter more than the numbers.

**Loop overhead bounds everything at short vectors.** The copy moves eight times
the data per instruction and measures 3.3×, because the two pointer bumps, the
counter and the branch do not shrink. That is the Amdahl ceiling for any kernel
on this machine, and it is a property of the loop rather than of the datapath.

**A kernel whose scalar form is already good wins the least.** The FIR's taps are
compile-time constants, so its scalar form strength-reduces to two instructions
per tap. 6.1× is width alone, and it is the narrowest frontier in the suite.

### Width, in cycles

Halving the **register width** does not double the cycles, because the part of a
kernel that is loop control and reduction does not shrink with the datapath.
These rows move `SIMD`, not `ILANES`:

| kernel | SIMD 8 | SIMD 4 | SIMD 2 |
|---|---:|---:|---:|
| requantise epilogue | 242 | 466 | 914 |
| int32 sum and max | 248 | 472 | 918 |
| 256-word copy | 236 | 460 | **908** |

Halving the width costs about 1.95× on the streaming kernels. **At two slots the
vector copy loses to the scalar one** — 908 cycles against 780 — because the
scalar copy is unrolled by four while the two-slot vector loop moves two words
per iteration, so the same loop overhead is amortised over less work. A wide
datapath does not help a kernel whose cost is the loop.

## Instruction timing

| event | cost |
|---|---|
| ALU, logic, shift, permute, moves, `vld`, `vst` at full width | 1 cycle |
| any of the above at a width below full | `SIMD / units` cycles |
| `vmul` | 2 cycles |
| `vredsum` / `vredmax` at more than two slots | 2 cycles |
| read-after-write on a vector register, distance 1 | 1 stall |
| read-after-write at distance 2 | **1 stall at `SIMD_WB = 1`**, which is what ships |
| `vld` behind a `vst` | 1 stall |
| an elementwise float instruction | issues once every `ALAT + passes` cycles — see [configurations](configurations.md#the-one-rate-limit-the-float-tier-still-has) |
| `vfmacc` / `vfmsac`, **including back to back into the same accumulator** | `passes` cycles; it retires once and each pass's accumulate lands `ALAT` later in the background |
| `vfaccz` | `NPART` cycles — a sweep of a one-write-port memory, 16 by default |
| `vfaccwr` | the same sweep, seeded from a vector register |
| `vfaccrd` | `NPART × (ALAT+1) + passes` — **112 + passes** at the defaults, the same at every unit count |
| `vfaccz` / `vfaccwr` / `vfaccrd` behind a float accumulate in flight | up to `ALAT` stalls |

The measured cycles-per-instruction of the integer vector kernels is 1.17 to
1.53 — the stalls above, plus the loop's own mispredicted exit.

## The path the frequency lands on

Read out on the integer configuration at `-flatten_hierarchy none`, so the cells
are attributable:

```
   the registered decode bit  ->  the vector register file's write port
                                  12 levels, 2.860 ns
```

The shape is worth reading rather than counting:

| segment | cells | cumulative |
|---|---|---:|
| decode register to the adder's `sub` input | FDRE, LUT4 | 0.409 ns |
| into the carry chain | LUT3 | 0.705 ns |
| **the packed adder** | **CARRY8 × 4** | 1.121 ns |
| the signed-compare spread | LUT4, LUT6 | 1.741 ns |
| min/max select, then the operation mux | LUT6, LUT4 | 2.215 ns |
| the result mux | LUT6 × 2 | 2.661 ns |
| the register file's write port | RAMD32 | 2.888 ns |

> **Four of the twelve levels are the carry chain, and they cost 0.23 ns between
> them** — 0.027 ns per level against 0.038–0.090 ns for every LUT on the path.
> Counting levels without reading them would call this path half again as deep
> as it is, and would point at the one structure that made the datapath fast: a
> 32-bit packed add is a single native carry chain precisely so that it is
> *not* seven gated ones.

The other reading is that **77% of the delay is interconnect**, concentrated on
the broadcast of the decode bits to every lane. That is the shape of a wide
uniform-control datapath — one decode, many consumers — and it is why the masks
are built once in the execute stage rather than once per lane in memory.

**Everything on the critical path is the register file's read-to-write loop**,
so what moves the frequency is what sits *in* that loop. Removing the permute
network or the shifter shortens the result mux that feeds the write port and
buys tens of megahertz; replacing the hard multipliers with fabric, or the
register file with block RAM, puts something slower into the loop and costs
73–98 MHz. The knobs that touch neither — register count, accumulator count —
move it by less than half a megahertz.

## Two rules for shrinking this PE

### Count the select inputs before spending a run

| change | measured | inputs to the merged select |
|---|---:|---:|
| one narrower serving both pack widths | **+165** | 14 |
| the float tier's wide element index resized | **+32** | 16 |
| accumulator banks as parallel result sources | **+307** | 7 |
| one source folded into the result chain | −16, **+1 cycle** | 7 |
| *(on the SIMT PE)* the retire select merged with the integer/float select | **won** | **5** |

**Five inputs or fewer, data and select together, and one LUT6 does the whole
thing** — that is where a merge pays. More than six and the tool is already
using a **MUXF7**, dedicated silicon at 0.067 ns against 0.22 ns for a routed
LUT, which a "simpler" flat form throws away. Spare inputs on an existing wide
mux are **not** the same thing as fitting in one LUT6: a result mux with seven
sources collapses nothing when another select is absorbed into it, and only
lengthens the chain.

### A shrink does not transfer between the two PEs

Measure it on the tier you are shrinking. Gating three opcode groups measured
**−161 LUT here** and **+78 on the SIMT PE**, where the same parameters reshape
the operand select whether or not the opcode can vary. Same file, same
parameters, opposite signs.

Two structural rewrites were tried on this PE and both made it **larger**:

| change | measured | verdict |
|---|---|---|
| the permute slide as an explicit 8-way select | 1,600 → **1,824 LUT** | **reverted** — a priority chain is not a mux, and the tool was already pruning the modulo |
| the result mux as an encoded `case` | 10,343 → **10,397 LUT**, 357.1 → 355.2 MHz | **reverted** — seven `else if` arms across 256 bits *look* like six 2:1 muxes in series; the tool was already balancing them |

**The useful conclusion is that this PE is not carrying obvious fat.** Every
knob that removes LUT also removes instructions or adds cycles.

## What was withdrawn, and why

A number that was true of an older build is not automatically a worse
measurement of this one — it can be a measurement of a different machine. These
were dropped rather than updated, and each is listed so that nobody re-derives
them from an older page.

| withdrawn | why |
|---|---|
| **every assembled-PE total taken at `-flatten_hierarchy none`** — and the mesh arithmetic built on them | the ship synthesises at Vivado's default, `rebuilt`, and the gap is configuration-dependent, so it cannot be applied as a correction either. The per-block census taken at `none` is unaffected and still stands — that one *has* to be taken at `none` |
| **every figure for the integer dot unit, its accumulator, `MULS` and `DOT_DSP`** | that hardware is not built — [accumulator](accumulator.md) |
| **every float-tier figure taken on the E8M15 datapath**, including both operand-format gates and every total that contains them | it is a different tier: different arithmetic, different converters, different element granularity. The totals are not comparable in either direction |
| **every accumulator area figure taken before its operation port was connected** | those units were a pass-through, not a fused multiply-add. Any accumulator figure published before the fix priced the wrong thing |
| **every converter-group figure** | the group had no datapath when it was priced. It has one now and has not been re-measured |
| **the tier-alone unit-count curve** and the linear fit taken from it | withdrawn for provenance rather than for being wrong: the probe's script and module have both since been renamed, so no run can be tied to the module that exists now |
| **any total derived by subtracting a probe delta from an assembled build** | arithmetic across two scopes is never a measurement |
| **a per-block table of the E8M15 multiply-add** — its normaliser, aligner and DSP48 rebuilds | it described a datapath this PE no longer contains |
| **"the float tier costs *N* LUT and *M* MHz"**, in every form it was written | each rested on one float unit per element being the only expressible build. Both halves of that are gone |
