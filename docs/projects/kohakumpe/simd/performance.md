---
title: SIMD PE performance
summary: What the reference configuration costs, which knobs are worth turning and what each one costs in latency, what a mesh of these holds, what the instructions buy — and which previously published figures were not carried forward, with the reason for each.
tags:
  - architecture
  - pe
  - simd
  - performance
---

# SIMD PE performance

Two independent questions, and this page keeps them apart: **what the vector unit
costs**, which is a synthesis result, and **what it buys**, which is a cycle count
on real programs.

## Reading the numbers on this page

Resource and frequency figures are out-of-context synthesis on
`xcvu13p-fhgb2104-2L-e` (Vivado 2024.2, synth only — pre-placement, so a routed
result will be somewhat worse). **Two asks are in play and every table says
which:**

| ask | what is constrained at it |
|---|---|
| **2.857 ns (350 MHz)** | the **reference float build**, which does not close with slack to spare |
| 3.333 ns (300 MHz) | the integer-only unit and PE, and the block-level probes |

Rows from the two asks are not comparable to each other, and neither are
float-tier figures taken before the operand edge became unconditional — see
[what was not carried forward](#what-was-not-carried-forward).

Cycle figures are read from the PE's own `CTL_CYCLE` counter on the full system:
one PE, real routers, the real memory agent, RAM behind it.

**And every table says which `-flatten_hierarchy` it was taken at.** See
[the flatten trap](#the-flatten-trap-rebuilt-for-totals-none-for-attribution)
— it moved the reference by more than most of the knobs on this page.

## The flatten trap: `rebuilt` for totals, `none` for attribution

**The ship does not synthesise at `none`.** Nothing in `scripts/tcl/` sets
`FLATTEN_HIERARCHY` on the ship run, so it takes Vivado's own default, which is
`rebuilt`. Every OOC script hard-coded `none`, and two shrink campaigns were
therefore measuring a PE the ship does not build. `ooc_simd_pe.tcl` now defaults
to `rebuilt`; `none` remains available as a diagnostic.

**The gap is configuration-dependent, so it cannot be carried between rows.** At
`SIMD_DOTDSP = 0` it is 647 LUT and *all* of it is the tool inferring DSP48
post-adders for `sum_r` (DSP 44 → 60). At `SIMD_DOTDSP = 1` — which is what
`rv_pe` defaults to — the RTL already puts that sum in the DSP column, so the
gap is only **243 LUT**. Extrapolating the 647 across the knob was wrong.

**The two settings answer different questions and must not be mixed in one
table:**

| question | setting | why |
|---|---|---|
| what does the PE cost, what does a knob save | **`rebuilt`** | it is what the ship builds |
| which block inside `khs_unit` is the cost | **`none`** | boundaries survive, so the census names real cells |

`rebuilt` **re-parents leaves**, so a per-block row taken there is not that
block's cost. The demonstration, from the reference build: `u_vrf` reports
**5,657 LUT while holding 444 LUTRAM** — the register file has absorbed
several thousand LUT of logic that is not its own. A subtraction between two
`rebuilt` totals is sound; a `rebuilt` hierarchy row is not.

## The measured reference

```
SIMD PE, 8 integer lanes + 4 float lanes, FALU + FSFU

   16,461 LUT  ·  11,796 FF  ·  19 BRAM  ·  68 DSP48  ·  344.4 MHz
```

MEASURED 2026-08-23 — assembled `rv_pe`, **2.857 ns**, **`-flatten_hierarchy
rebuilt`**, `SIMD_DOTDSP = 1`, `SIMD_WB = 1`, `RECV_MEM = distributed`. At
`RECV_MEM = block` it is **16,309 LUT** for 4 more BRAM.

This supersedes [the 13,772 figure below](#the-reference-superseded), which was
taken at `none` on a tree whose base core has since changed — the two differ by
about 2,000 LUT *outside* `khs_unit`. The census inside `khs_unit` is identical
between them, so the old page's per-block reasoning stands; only the total moved.

### The float-lane curve, assembled

Same RTL, same ask, only `SIMD_FLOAT_LANES` varying. This is the assembled PE,
which is what the withdrawn tier-alone curve never was:

| float lanes | LUT | Fmax | slack |
|---:|---:|---:|---:|
| 0 (no float tier) | **10,371** | 362.2 | **+0.096** |
| 2 | 14,174 | 360.4 | **+0.082** |
| **4 (the reference)** | **16,461** | 344.4 | −0.047 |
| 8 | 22,241 | 345.7 | −0.036 |

**Price a float lane MARGINALLY, never by dividing the tier.** The tier at four
lanes is 6,090 LUT, which reads as 1,352 per lane — and that number is an
artefact. The **marginal** lane, 2 → 4, is **1,144**; the other ~1,515 is FIXED
overhead (the third register-file port `vfma` needs, the retire path, the
scoreboard, the pass sequencer) that does not scale with lanes. Dividing by four
charges the lanes for all of it and manufactures a defect that is not there:
against the SIMT PE's 1,105 per lane, the average says SIMD's lane is 22 % dearer
and the marginal says it is 3.5 % — and the SIMT lane is FMA-only
(`kht_fpu` defaults `HAS_FSFU = 0` and drives `lane_op` as a constant), where
this one issues thirteen `vec_alu` opcodes.

**`FLANES = 3` is not on that list because it does not work.** `FLANES` must
divide `2 × SIMD`. Three synthesises cleanly and reports a plausible 330.4 MHz
and fails the component bench 10 of 66 — the same silent-illegal family as
`FLANES[PSW-1:0]`.

### Multi-pass earns 5,780 LUT

The design question behind the curve, in one subtraction:

```
   4 float lanes + the pass machinery      16,461
   8 float lanes, no passes at all         22,241
                                          -------
   multi-pass is worth                     -5,780
```

The pass machinery is ~1,515 LUT and it avoids four lanes at ~1,445 each. The
8-lane shape also costs more than the **entire** SIMT PE at 8 int + 8 float
(21,621), which has twice the float lanes and a wave scheduler besides. **"Just
give the SIMD PE eight float lanes like the SIMT PE" is closed by this pair of
numbers.**

## What each feature costs, against the kernel that uses it

Every LUT figure is a measured A/B delta at `rebuilt`; every cycle pair is the
same workload written twice, scalar and vector, from
`tests/pe/tools/rv_simd_kernels.py`, run on the assembled PE.

**Both writeback settings are given, because the bench does not default to the
shipped one.** `rv_simd_tb.v` defines `RV_SIMD_WB` to **0** while `rv_pe.v`
defaults it to **1**; it does not pass `SIMD_DOTDSP` at all, so that one takes
the shipped 1 in both columns. `SIMD_WB = 1` registers the vector result before
the write, which makes a distance-2 dependency a hazard that does not otherwise
exist — so it costs cycles and changes no answers. **`WB = 1` is what the card
runs; the `WB = 0` column is kept beside it rather than replaced.**

| feature | RTL | LUT | kernel | scalar | vec `WB=0` | vec **`WB=1`** | speedup `WB=0` | **speedup `WB=1`** |
|---|---|---:|---|---:|---:|---:|---:|---:|
| `vsldw`, `vunpk`, saturating `vpack` | `khs_perm` | **1,496** | `fir_i16_v` | 3,407 | 559 | **745** | 6.1× | **4.6×** |
| " | " | " | `epilogue_v` | 8,028 | 245 | **325** | 32.8× | **24.7×** |
| packed shift `vslli` / `vsrari` | `khs_pshift32` + `u_rnd` | **876** | `epilogue_v`, `fir_i16_v` | 8,028 | 245 | **325** | 32.8× | **24.7×** |
| `vdot.s8` + two accumulators | `khs_lane` multipliers, `g_accbank` | — | `dot_i8_v` | 1,300¹ | 55 | **60** | 23.6× | **21.7×** |
| the `SIMD_DOTDSP` cascade | `khs_lane` `g_sumdsp` | **−399**² | `dot_i8_v` | — | — | — | — | — |
| `vredsum` / `vredmax` | `khs_reduce` | 534³ | `reduce_i32_v` | 3,093 | 251 | **283** | 12.3× | **10.9×** |
| vector `vld` / `vst` | `khs_vspad` | 136³ + 8 BRAM | `memcpy32_v` | 783 | 239 | **271** | 3.3× | **2.9×** |
| **the four FSFU seeds** | `vec_alu` poly path | **941** | **none** | — | — | — | — | — |
| **the float tier itself** | `khs_falu` | **6,090** | **none** | — | — | — | — | — |

**No verdict moves.** The shipped writeback costs between 1.9 % and 33.3 % of a
vector kernel's cycles, and the worst speedup loss is **25.0 %** (`fir_i16_v`,
6.1× → 4.6×) with `epilogue_v` next at 24.6 %. Every feature still wins by 2.9×
to 24.7×, so none of them was an artifact of a writeback the card does not have.

### Why the two columns are comparable

Both were taken on the same tree in the same session, at `DOTDSP = 1`, differing
only in `-d RV_SIMD_WB`. That is the claim; **this is the control that tests
it.** `WB_STAGE` is inside the vector writeback and touches nothing else, so a
scalar kernel must be unaffected by it — and all seven are identical **to the
cycle** across the pair:

```
   memcpy32 783 · dot_i8 8,224 · dot_i8_nomul 1,300 · fir_i16 3,407
   stencil_i16 7,774 · reduce_i32 3,093 · epilogue 8,028
```

If anything else had drifted between the runs — a generator change, a different
RTL revision, a stale image — it would almost certainly have moved one of those
seven. None moved, so the vector deltas below are the writeback and not the run.
**A paired cycle measurement without a control of this kind is two runs, not a
comparison.**

The second half of believing it is that the cost lands where the mechanism
predicts rather than evenly:

`fir_i16_v` (+186 cycles, +33.3 %) and `epilogue_v` (+80, +32.7 %) are the two
kernels built from long chains of dependent vector operations, and distance 2 is
exactly what `WB = 1` turns into a stall. `vsw_hazard` pays +2 because it was
never dependency-bound. The dots pay ~9 % because `vdot` streams at II = 1 into
an accumulator rather than through the register file, so the new hazard barely
touches them. **A cost that lands where the hazard's shape predicts is a
measurement; one that landed evenly would have been a reason to look again.**

¹ `dot_i8_nomul`, the twin whose multiply is costed at one instruction — the
honest baseline, since the base core is RV32I. Against the real software
multiply it is 8,224 → 55, or 149×.
² **negative: the cascade SAVES 399 LUT** and spends 8 more DSP48.
³ from the `none` census — neither block has a generic to A/B, so these two are
attribution rather than deltas, and they are labelled as such.

### The column this table does not have

**No SIMD feature has shipping-workload evidence, because there is no compiler
path to this PE.** Nothing under `compiler/` references the SIMD PE or any of its
instructions — not `vdot`, `vredsum`, `vsldw`, `vpack`, `vsrari`, `khs_*`, nor
the string `simd`. Its only kernel library is
`tests/pe/tools/rv_simd_kernels.py`, which exists to exercise the RTL.

Within that limit the two halves are not in the same position:

- the **integer** features each have a paired kernel representing real DSP work —
  a dot product, an 8-tap FIR, a reduction, a requantise epilogue — and each wins
  between 3.3× and 32.8×;
- the **float tier is 6,090 LUT, 37 % of the PE, and that library contains zero
  float instructions.** Neither does anything else in the repository.

That is the weakest column in the table and it is stated rather than omitted. The
question it raises is not whether the integer extras earn their LUT — measured,
they do — but whether this PE has a workload at all, which is a compiler question
and not an RTL one.

## Every knob, priced

One tree, one flatten (`rebuilt`), one period (2.857 ns), `xcvu13p-fhgb2104-2L-e`.
Every row is a real `synth_design`; **no row is a subtraction of two others.**
Reference is the measured one above with `RECV_MEM` and `SIMD_VREG_PRIM` at
`distributed`, so each row is one knob from one baseline.

| knob | LUT | **Δ** | FF | BRAM | DSP | Fmax | how the knob was verified to reach the netlist |
|---|---:|---:|---:|---:|---:|---:|---|
| **reference** | 16,457 | — | 11,796 | 19 | 68 | 344.4 | |
| `SIMD_PERM=0` | 14,962 | **−1,495** | 11,790 | 19 | 68 | 344.4 | `khs_perm` gone from the hierarchy |
| `SIMD_FSFU=0` | 15,520 | **−937** | 10,338 | 13 | 64 | 341.2 | DSP −4 (DSP-P), BRAM −6 = **3 RAMB18/lane** = `vec_tables` |
| `SIMD_SHIFT=0` | 15,593 | **−864** | 11,729 | 19 | 68 | 341.2 | control sets 139 → 135 |
| `RECV_MEM=block` | 16,305 | **−152** | 11,252 | 23 | 68 | 341.2 | LUTRAM 812 → 652, BRAM +4 |
| `SIMD_VREG_PRIM=block` | 16,334 | −123 | 11,033 | 31 | 68 | **272.8 ✗** | LUTRAM 812 → 368 |
| `SIMD_MULS=8` | 16,457 | **0** | 11,796 | 19 | 68 | 344.4 | **verified no-op** |
| `SIMD_NPART=32` | 16,457 | **0** | 11,796 | 19 | 68 | 344.4 | **verified no-op** |
| `SIMD_MULS=2` | 16,503 | +46 | 11,683 | 19 | **36** | 341.2 | DSP −32; **removes int8 entirely** |
| `SIMD_WB=0` | 16,552 | +95 | 11,547 | 19 | 68 | **324.0** | FF −249 |
| `SIMD_DOTDSP=0` | 16,857 | +400 | 11,560 | 19 | 60 | 341.2 | DSP −8; the cascade **saves** LUT |
| **`SIMD_FCVT=1`** | 16,506 | +49 | 11,813 | 19 | 68 | 341.2 | **BROKEN — not a knob, see below** |
| **`SIMD_FACC=1`** | 19,337 | +2,880 | 14,128 | 19 | 76 | 318.5 | **BROKEN — not a knob, see below** |

**✗** `SIMD_VREG_PRIM=block` is **not available to this PE**: −123 LUT for
**−71.6 MHz**. The critical path runs through the vector register file's
read-to-write loop, so BRAM lands directly on it. The SIMT PE holds 392 MHz with
the same setting because its binding path is the predecode write into its control
RAM and its register file is not in the loop — **the knob is available there and
not here, and a config matched across both would be a comparison of a machine
this PE cannot build.**

**The two zero rows are genuine no-ops, and that was proved rather than assumed.**
A knob that never reached the design looks identical to one with nothing to do.
`MULS=8` is byte-identical because `khs_lane` branches on `MULS >= 4`. `NPART=32`
was re-run at **`FACC=1`**, where it *does* move — 19,337 → **19,409 (+72 LUT)**,
LUTRAM 868 → 924, control sets 194 → 199 — so the generic lands and simply has
nothing to act on when the accumulator is not built.

### Lane combinations

| int × float | LUT | FF | BRAM | DSP | Fmax |
|---|---:|---:|---:|---:|---:|
| 4 × 4 | 12,360 | 10,741 | 15 | 40 | 341.2 |
| 4 × 8 | 17,307 | 15,829 | 21 | 52 | 341.2 |
| **8 × 4** | **16,457** | 11,796 | 19 | 68 | 344.4 |
| 8 × 8 | 22,242 | 16,890 | 25 | 80 | 345.7 |
| 16 × 4 | 26,252 | 13,945 | 27 | 124 | 307.6 |
| 16 × 8 | 31,035 | 19,009 | 33 | 136 | 307.7 |

Float lanes ship at **4 or 8** only. The 8 × 8 row at the SIMT PE's own
configuration (`RECV_MEM` and `VREG_PRIM` both `block`) is **22,306 LUT at
272.0 MHz** — quoted for completeness and unshippable for the reason above; the
comparable figure is **22,242 at 345.7**.

### `SIMD_FCVT` and `SIMD_FACC` are BROKEN, not knobs

**Do not price either as working silicon.**

**`SIMD_FCVT` is worse than a no-op.** `m_is_fcvt` and `m_fcvt_op` are registered
in the MEM stage and **never read** — there is no `m_is_fcvt` branch in the `vres`
result mux, so `vfcvt` writes the **integer lane output**. At `FCVT = 0` those six
encodings correctly **fault**; setting it to 1 makes them *legal* and *silently
wrong*. **49 LUT buys a worse PE.**

**`SIMD_FACC` is a multiply-accumulator that neither multiplies nor
accumulates.** `khs_unit.v:1177` instantiates the accumulator's four
`khs_float_lane`s **without connecting `.op`** — Vivado warns
`port 'op' ... is unconnected` once per lane and ties it to 0, which is `OP_MOV`,
a pass-through. The corroboration is the area column: those lanes synthesise at
**~256 LUT each against ~1,150** for the elementwise ones, and a full FMA lane
cannot be 256 LUT. **+2,880 LUT for a datapath that does neither operation in its
name.**

Both are instances of a named defect class with four members —
[decode without datapath](gates.md#decode-without-datapath--a-named-defect-class-four-instances).

### Restructuring a mux: five measurements, and the rule they support

| change | measured | inputs to the merged select |
|---|---:|---:|
| `khs_perm`, one narrower for both pack widths | **+165** | 14 |
| float-tier wide element index sized to the FP32 view | **+32** | 16 |
| accumulator banks as parallel `vres` sources | **+307** | 7 |
| `el_wdata` folded into the `vres` chain | −16, **+1 cycle** | 7 |
| *(SIMT)* retire select merged with the int/float select | **won** | **5** |

**Count the select inputs before spending a run.** Five or fewer, data and select
together, and one LUT6 does the whole thing — that is where a merge pays. More
than six and the tool is already using a **MUXF7**, dedicated silicon at 0.067 ns
against 0.22 ns for a routed LUT, which a "simpler" flat form throws away. Spare
inputs on an existing wide mux are **not** the same thing as fitting in one LUT6:
`vres` has seven sources, so absorbing the accumulator's bank select into it
collapsed nothing and only lengthened the chain.

### A shrink does not transfer between the two PEs

Measure it on the tier you are shrinking. `HAS_UNARY`/`HAS_FNMA`/`HAS_SEL` opcode
gating is **−161 LUT here** and **+78 on the SIMT PE**, where the parameters
reshape `vec_alu`'s operand select whether or not the opcode can vary. `DLY_FF=16`
is −60 here and −56 there, which is roughly a wash per lane rather than the
tier-specific win it was first reported as. Same file, same parameters, opposite
signs on one of them.

## The reference (superseded)

```
SIMD PE, 8 integer lanes + 4 float lanes

   13,772 LUT  ·  10,126 FF  ·  13 BRAM  ·  72 DSP48  ·  353.4 MHz
```

MEASURED, assembled `rv_pe`, **2.857 ns**, `SIMD_DOTDSP = 1`, `SIMD_WB = 1` —
but at **`-flatten_hierarchy none`**, and on a tree whose base core has since
changed. Kept for the reasoning it supports, not as a current total; the figure
to quote is [16,461](#the-measured-reference).

Against a 300 MHz mesh clock that is **18 % margin**. Hold beside it the same PE
with the extension switched off — **2,477 LUT at 377.9 MHz**, measured at
3.333 ns, so read it as a scale and not as a subtraction.

Where the 72 DSP48 comes from is worth writing out, because it is a decision
rather than a rounding:

```
   8 integer lanes  x  (4 khs_mul  +  4 cascaded DSP48 for the dot sum)  =  64
   4 float lanes    x  2 (vec_alu's DSP-E and DSP-M)                     =   8
                                                                      -------
                                                                          72
```

At `SIMD_DOTDSP = 0` the integer half is 32 and the total is 40. The second set of
multipliers exists because `p0..p3` must still surface for `vmul` and an operand
with two consumers cannot be cascaded
([accumulator](accumulator.md#simd_dotdsp-builds-a-second-set-of-multipliers-deliberately)).
On this device LUT is the binding resource and DSP is not.

### The two knobs the tighter ask turned on

`rv_pe` defaults **`SIMD_DOTDSP = 1` and `SIMD_WB = 1`**. `khs_unit`'s own parameter
defaults are still 0 and 0, so a probe or a bench that instantiates the unit
directly gets the other machine unless it says otherwise — `khs_unit_tb` defaults
both to 1 to match what ships.

| ask | knobs | LUT | Fmax |
|---:|---|---:|---:|
| 2.857 ns | neither | 14,982 | 322.0 |
| **2.857 ns** | **both** | **13,772** | **353.4** |
| 3.333 ns | `SIMD_WB` alone, same vehicle | +89 | −28 MHz |

**They are worth having only at a binding constraint.** At 3.333 ns the PE closes
with positive slack and a knob that adds a register is pure cost; at 2.857 ns the
pair is 1,210 LUT and 31.4 MHz.

**Neither is free in cycles**, and that is the part a resource table hides:

- `SIMD_DOTDSP` takes `DOT_LAT` from 2 to 4, so `vaccrd` / `vaccz` / `vaccwr`
  behind a dot in flight wait up to **4** cycles instead of 2.
- `SIMD_WB` registers the result before the write, so a distance-1 vector
  dependency costs a second stall **and distance 2 becomes a hazard that did not
  exist** ([pipeline](pipeline.md#hazards)).

Both are throughput trades, not correctness ones. **The kernel cycle figures in
[what the instructions buy](#what-the-instructions-buy) were taken with both knobs
off.** That open item is now closed at the other end of the page: [the feature
table](#what-each-feature-costs-against-the-kernel-that-uses-it) carries the same
workloads at `DOTDSP = 1` with **both** writeback settings, measured 2026-08-23.
The shipped pair costs a vector kernel between 1.9 % and 33.3 % of its cycles and
moves no verdict — worst speedup loss 25.0 %.

## What a mesh holds

The SIMD PE is a replicated unit, so its LUT count is machine capacity rather than
a line in a report. Against roughly **350,000 usable LUT** per mesh:

```
   8 SIMD PEs    8 x 16,461  =  131,688
   4 SIMT PEs    4 x 20,086  =   80,344
                              --------
                               212,032      the PE array
   2 controllers                  4,954
                              --------
                               216,986      against a ~350k budget
```

PROJECTED — arithmetic over per-PE measurements, not a placed mesh. Both PE
figures are `rebuilt` 2026-08-23 measurements of the assembled unit; the
controller figure is 2,477 each and has not been re-taken.

**Against the 200,000-LUT line the PE array is asked to fit, that is 12,032
over**, and the shapes that fit are worth stating rather than leaving to be
re-derived:

| shape | PE array | |
|---|---:|---|
| 8 SIMD (8+4) + 4 SIMT | 212,032 | over by 12,032 |
| **7 SIMD (8+4) + 4 SIMT** | **195,571** | fits, 4,429 spare |
| **8 SIMD (8+2) + 4 SIMT** | **193,736** | fits, 6,264 spare |
| 8 SIMD (8+0) + 4 SIMT | 163,312 | fits, 36,688 spare |

Dropping one SIMD PE keeps four float lanes on the remaining seven, which is a
different trade from halving the float width on all eight — the first costs a
whole PE's integer throughput, the second costs half the float throughput
everywhere. Neither is an area decision alone.

The float throughput of that array is:

```
   8 SIMD PEs x 4 float lanes  +  4 SIMT PEs x 8 float lanes  =  64 FMA / clock
```

which is **exactly one Mali-G610 shader core**. That is the comparison worth
holding onto: the whole mesh's float arithmetic is one mobile shader core's worth
— computed at 1.5e-5 rather than the 4.9e-4 such a core would run at.

## The integer configuration matrix

**These rows are `khs_unit` ALONE, integer only, at 3.333 ns, with
`SIMD_DOTDSP = 0` and `WB_STAGE = 0`.** They are not the shipped configuration and
their absolute LUT is not the reference's. What they are still good for is the
question they were run to answer: **what each optional block costs relative to the
others**, on modules that have not changed since.

| config | LUT | FF | DSP | BRAM | Fmax | vs the baseline |
|---|---:|---:|---:|---:|---:|---|
| **`s8`, the baseline of this table** | **7,961** | 1,617 | 32 | 8 | **368.7** | — |
| `s4` | 4,138 | 966 | 16 | 4 | 367.4 | −3,823 LUT, −1.3 MHz |
| `s2` | 2,091 | 512 | 8 | 2 | 402.1 | −5,870 LUT, +33.4 MHz |
| `s8` no shifter | 6,448 | 1,551 | 32 | 8 | 393.1 | −1,513 LUT, +24.4 MHz |
| `s8` no permute | 6,327 | 1,615 | 32 | 8 | 402.6 | −1,634 LUT, +33.9 MHz |
| `s8` 2 multipliers | 7,330 | 1,621 | 16 | 8 | 369.0 | −631 LUT, +0.3 MHz |
| `s8` multipliers in fabric | 15,068 | 2,437 | **0** | 8 | 295.3 **✗** | +7,107 LUT, −73.4 MHz |
| `s8` 32 vector registers | 7,853 | 1,621 | 32 | 8 | 368.9 | −108 LUT, +0.2 MHz |
| `s8` 4 vector registers | 7,965 | 1,617 | 32 | 8 | 368.7 | +4 LUT, ±0.0 MHz |
| `s8` 1 accumulator | 7,575 | 1,357 | 32 | 8 | 368.9 | −386 LUT, +0.2 MHz |
| `s8` 4 accumulators | 10,164 | 2,142 | 32 | 8 | 368.6 | +2,203 LUT, −0.1 MHz |
| `s8` block-RAM vector file | 8,158 | 1,107 | 32 | **16** | 270.8 **✗** | +197 LUT, −97.9 MHz |

**✗** marks a configuration that does not meet the 3.333 ns request. Both are
priced for the comparison rather than offered: fabric multipliers and a block-RAM
register file are the two ways to build this unit that are worse on every axis at
once.

### What the rows say

**Everything on the critical path is the register file's read-to-write loop**, so
what moves the frequency is what sits *in* that loop. Removing the permute network
buys 33.9 MHz, removing the shifter 24.4 — both shorten the result mux that feeds
the write port. Replacing the hard multipliers with fabric costs 73.4 MHz and
replacing the register file with block RAM costs 97.9, because both put something
slower *into* the loop. The knobs that touch neither — register count, accumulator
count, multipliers per lane — move it by less than half a megahertz.

**The permute network costs 1,634 LUT and 33.9 MHz** — the largest optional block
on both counts. It buys `vsldw` (a stencil's or a filter's misaligned neighbour),
the saturating `vpack` an epilogue ends with, and the widening `vunpk`. It is also
the one structure whose cost grows with lane count, since each output lane selects
from `2 × SIMD` inputs. A build that only computes dot products should not carry
it.

**A DSP column is worth about 230 LUT.** Moving the 32 multipliers into fabric
costs 7,107 LUT to save 32 DSP and loses 73.4 MHz. On this device LUT is the
binding resource and DSP is not, so the hard multipliers stay — and the same
argument is what buys `SIMD_DOTDSP` 32 more of them.

**Two multipliers per lane saves 16 DSP and costs int8 entirely.** `vdot.s8` and
`vmul.s8` become illegal encodings on that build — the honest outcome, because
there is no way to get two independent int8 multiplies from one DSP48E2
([accumulator](accumulator.md#the-multipliers-and-why-there-are-four-per-lane)).

**The vector register count is free in both directions.** Thirty-two registers
cost 108 LUT *less* than eight, and four save nothing, because a distributed-RAM
primitive is 32 entries deep and a small file wastes the depth it does not use.

**Accumulators are the one structure that grows badly**: the second costs 386 LUT
and going from two to four costs 2,203 more, because the read mux in front of the
array grows with both the count and the width. Two is the shipped number.

### The path all of it lands on

```
   m_alu_op_reg/C  ->  u_vrf/.../mem_reg/RAMF_D1/I        12 levels, 2.860 ns
```

The registered decode bit that selects the lane's operation, through the packed
adder, to the vector register file's write port. Its shape is the design's, and
worth reading rather than counting:

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
> Counting levels without reading them would call this path half again as deep as
> it is, and would point at the one structure that made the datapath fast: a
> 32-bit SWAR add is a single native carry chain precisely so that it is *not*
> seven gated ones.

The other reading is that **77 % of the delay is interconnect**, concentrated on
the broadcast of the decode bits to every lane. That is the shape of a wide
uniform-control datapath: one decode, many consumers — and it is why the masks are
built once in EX rather than `SIMD` times in MEM.

### Two structural shrinks that were tried and made it larger

Recorded because the reasoning was sound each time and the measurement was not:

| change | measured | verdict |
|---|---|---|
| the permute slide as an explicit 8-way select | 1,600 → **1,824 LUT** | **reverted** — a priority chain is not a mux, and the tool was already pruning the modulo |
| the result mux as an encoded `case` | 10,343 → **10,397 LUT**, 357.1 → 355.2 MHz | **reverted** — seven `else if` arms across 256 bits *look* like six 2:1 muxes in series; the tool was already balancing them |

**The useful conclusion is that the SIMD PE is not carrying obvious fat.** Every
knob that removes LUT also removes instructions.

## What the instructions buy

Kernel-only cycles, same PE, same data, same independently computed reference for
both forms. **Measured on the integer-only build with `SIMD_DOTDSP = 0` and
`SIMD_WB = 0`**; both shipped knobs add a stall class, so these have not been
re-taken against the shipped pair.

| kernel | scalar | vector | speedup | against |
|---|---:|---:|---:|---|
| int8 dot, 128 elements | 8,221 | 52 | **158.1×** | the core as it ships |
| int8 dot, 128 elements | 1,297 | 52 | **24.9×** | a scalar core that has a multiplier |
| requantise epilogue, 256 elements | 8,025 | 242 | **33.2×** | — |
| int32 sum and signed max, 256 | 3,090 | 248 | **12.5×** | — |
| 8-tap int16 FIR, constant taps | 3,404 | 556 | **6.1×** | — |
| 256-word copy | 780 | 236 | **3.3×** | — |

Three readings matter more than the numbers.

**The multiplier and the width are separate purchases.** The base core is RV32I
and has no multiplier, so an int8 dot's scalar loop spends 84 % of its cycles in a
software multiply — eight unrolled shift-add steps per element, about 54 cycles
each. Quoting 158× would be mostly a statement that the base core cannot multiply.
Every multiplying kernel therefore carries a twin whose multiply is costed at one
instruction, and **24.9× is the honest SIMD number**.

**Loop overhead bounds everything at short vectors.** The copy moves eight times
the data per instruction and measures 3.3×, because the two pointer bumps, the
counter and the branch do not shrink. That is the Amdahl ceiling for any kernel on
this machine, and it is a property of the loop rather than of the datapath.

**A kernel whose scalar form is already good wins the least.** The FIR's taps are
compile-time constants, so its scalar form strength-reduces to two instructions
per tap with no software multiply to remove. 6.1× is width alone, and it is the
narrowest frontier in the suite.

### Width, in cycles

Halving the lane count does not double the cycles, because the part of a kernel
that is loop control and reduction does not shrink with the datapath:

| kernel | 8 lanes | 4 lanes | 2 lanes |
|---|---:|---:|---:|
| requantise epilogue | 242 | 466 | 914 |
| int32 sum and max | 248 | 472 | 918 |
| 256-word copy | 236 | 460 | **908** |
| int8 dot | 52 | 84 | 147 |
| int8 dot, two accumulators | 68 | 108 | 186 |

Halving the width costs about 1.95× on the streaming kernels and only about 1.6×
on the dot products, whose fixed prologue and final cross-lane reduction do not
shrink. **At two lanes the vector copy loses to the scalar one** — 908 cycles
against 780 — because the scalar copy is unrolled by four while the two-lane vector
loop moves two words per iteration, so the same loop overhead is amortised over
less work. A wide datapath does not help a kernel whose cost is the loop.

## Instruction timing

| Event | Cost |
|---|---|
| ALU, logic, shift, permute, moves, `vld`, `vst` | 1 cycle |
| `vdot`, including back to back | 1 cycle; the accumulate lands `DOT_LAT` later — **4 as shipped**, 2 at `SIMD_DOTDSP = 0` |
| `vmul` | 2 cycles |
| `vredsum` / `vredmax` at more than two lanes | 2 cycles |
| RAW on a vector register, distance 1 | 1 stall |
| RAW at distance 2 | **1 stall at `SIMD_WB = 1`**, which is what ships |
| `vld` behind a `vst` | 1 stall |
| `vaccrd` / `vaccz` / `vaccwr` behind a dot in flight | up to `DOT_LAT` stalls — **4 as shipped** |
| `vfmacc` / `vfmsac`, **including back to back into the same accumulator** | `passes` cycles — **4 at four float lanes**, 1 at one lane per element. It retires once, and each pass's accumulate lands 15 cycles later in the background |
| `vfaccz` | `NPART` cycles — a sweep of a one-write-port memory, 16 by default |
| `vfaccwr` | the element count again, to walk one converter over the seed, then the `NPART` sweep |
| `vfaccrd` | ≈ **270 cycles** — `NPART × (ALAT+1)` for the fold plus `2 × SIMD` to pack, **the same at every lane count** |
| `vfaccz` / `vfaccwr` / `vfaccrd` behind a float accumulate in flight | up to 15 stalls |

The measured CPI of the integer vector kernels is 1.17 to 1.53 — the stalls above,
plus the loop's own mispredicted exit. The float rows are the tier's shape and not
a defect: a float lane never waits, and everything that waits does so once per
reduction rather than inside it
([float](float.md#what-waits-and-what-does-not)).

## What was not carried forward

The SIMD PE changed substantially, so a number that was true of the old build is
not automatically a worse measurement of this one — it can be a measurement of a
different machine. These were dropped rather than updated, and each is listed here
so that nobody re-derives them from an older page.

| dropped | why |
|---|---|
| **every assembled-PE total on this page taken at `-flatten_hierarchy none`** — 13,772 / 14,982 and the mesh arithmetic built on them | the ship synthesises at Vivado's default, `rebuilt`; `none` was hard-coded in every OOC script and nothing set it on the ship run. The gap is configuration-dependent (243 LUT at `SIMD_DOTDSP = 1`, 647 at 0), so it cannot be applied as a correction either. Superseded by [the measured reference](#the-measured-reference). **The per-block census inside `khs_unit` is unaffected and still stands** — that one has to be taken at `none` |
| **every float-tier LUT figure taken before the operand edge became unconditional** — the assembled 14,579 / 17,844 / 22,743 rows at 3.333 ns, and the `khs_unit` float-on/float-off pair | the float lane now carries **both** operand converters unconditionally; those builds carried only the narrow one. It is a different lane, so the totals are not comparable and every one of them is low |
| **the tier-alone lane-count curve** — 11,432 / 6,353 / 3,808 / 2,475, and `1,270 + 635 × lanes` | **withdrawn for provenance, not because it was wrong.** It came from the tier probe, whose script and module have both since been renamed, so no run can be tied to the module that exists now — and the probe's accumulator **folds differently** from the unit's in any case. See the open item below |
| **8 int + 4 float = 15,119 LUT / 9,720 FF / 40 DSP** | a probe delta subtracted from an assembled build — arithmetic across two scopes, never a measurement. The measured reference is 13,772 / 10,126 / 72 |
| **8 int + 8 float = 16,214 LUT** | derived from a SIMD = 4 proxy whose vector register is 128 bits rather than 256, so it was a floor rather than an estimate, and it never paid for the walk sequencer |
| "the float tier costs 12,400 LUT and 10.6 MHz" | the sentence rested on one float lane per element being the only expressible build. Both halves of that are gone |
| "sixteen float lanes, unconditionally" and everything resting on it | the lane count is a parameter and the reference is four |

The per-block table of `vec_alu`'s FMA — normaliser 156 LUT, aligner 113, and the
DSP48 rebuilds at 44 + 1 DSP and 114 + 1 — is still sound, because those probes
transcribe stages of `vec_alu` that did not change. It lives on
[float](float.md#which-files-are-the-machine-and-which-are-probes) with its
provenance attached, and it is **not** a lane total: the lane also carries two
operand converters that the block probe does not measure.

### OPEN — the tier-alone lane-cost curve needs re-measuring

**What the float tier costs per lane, on its own, is currently unknown**, and
that is a gap rather than a settled answer. The withdrawn figures were withdrawn
because their provenance broke, not because anyone found them wrong.

Re-establishing it is one run: `khs_float_tier` at each lane count, through
`ooc_float_tier.tcl`, which now targets the right module. Two things to record
with the result so it does not go stale the same way:

- **the ask**, because the reference PE is constrained at 2.857 ns and the old
  curve was taken at 3.333;
- **that the probe still folds flat** over `NPART × passes` where `khs_unit`
  walks one pass's strided subset — so the curve is comparable to the unit in
  **lane count and area shape**, and not in arithmetic. Its own header says so.

Until that run exists, the assembled figures in [the reference](#the-reference-superseded)
are the only float numbers this page will quote.
