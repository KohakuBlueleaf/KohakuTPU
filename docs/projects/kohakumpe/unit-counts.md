---
title: Per-feature unit counts
summary: The marginal LUT cost of one unit of every widthed feature on both PEs, measured as the difference between two synthesised rows that differ in one count, so that a configuration can be priced without synthesising it.
tags:
  - architecture
  - pe
  - performance
---

# Per-feature unit counts, and what each unit costs

> **Kind: none — this page reports measurements of parts labelled elsewhere.**
> The features priced are this project's own configurable knobs. The marginal
> method — two synthesised rows differing in exactly one count — is a
> measurement convention worth copying, not a framework contract.

Every feature of both PEs that has a width has its own parameter, settable
independently of every other. This page is the price list: what one unit of each
feature costs, so a configuration can be **inferred** rather than synthesised,
and the measured error of that inference.

## How to read every figure on this page

**Marginal, never average.** Each per-unit figure is the difference between two
synthesised rows that differ in **one** count, divided by the change in that
count. Dividing a tier's total by its unit count charges the tier's fixed
overhead — the extra register-file read port, the retire path, the scoreboard,
the pass sequencer — to the units, and manufactures a defect that is not there.
The arithmetic behind each figure is written out beside it.

**Provenance.** Every row is out-of-context **synthesis** of one PE on
`xcvu13p-fhgb2104-2L-e`, Vivado 2024.2, `-flatten_hierarchy rebuilt`,
`-directive default`, at a 3.333 ns target period. Nothing here is placed or
routed. `rebuilt` is used because it is what the ship's synthesis run takes;
`none` preserves module boundaries and is the diagnostic setting for attributing
cost to a block, and the two must never appear in one table.

**Frequency is a screen, not a result.** The Fmax columns are synthesis
estimates. They are the optimistic end — this repository has measured a module
lose 0.740 ns between synthesis and routing — and they move by tens of megahertz
between rows that differ in nothing that should matter. **No decision recorded
on this page was made on Fmax.**

**Every table names the tree it was measured on.** The RTL has changed since,
in ways that are listed below, and rows from different trees are never mixed or
subtracted.

## The trees these figures come from

Two frozen source trees carry every measurement here. Neither is the RTL as it
now stands.

**Tree W — the widths tree.** Every compute feature is an independent unit
count; the integer dot unit and its accumulator are gone. The float tier still
computes in E8M15 and accepts two operand formats, gated by `HAS_F16` and
`HAS_F32`; the integer lane still carries a per-lane multiplier depth `MULS` and
a `DOT_DSP` mapping choice; and the packed shifter and permute still sit behind
separate `HAS_SHIFT` / `HAS_PERM` booleans in addition to their widths.

**Tree W+ — the same, plus RV32IM and three more widths.** Adds `ILANES`,
`RED_UNITS` and `HAS_SHROUND`, and makes RV32IM unconditional in every RV32 core.

**What has changed since, and therefore what no figure here covers.** The float
tier was rebuilt from E8M15 into binary32 throughout, which deleted `HAS_F16`,
`HAS_F32` and both operand converters; `MULS` and `DOT_DSP` were removed; the
`HAS_SHIFT` / `HAS_PERM` / `HAS_FLOAT` booleans were removed in favour of the
counts alone; and the converter group `FCVT_UNITS` gained the datapath it had
been missing. **No absolute total on this page describes a PE that can be built
from the RTL as it now stands.** What survives is the *shape*: what a marginal
lane costs, where a width pays and where it does not, and which knobs are not
levers at all.

---

## 1. The parameters

Both PEs spell a width the same way: **0 is not built and every encoding that
would need it faults; `-1` is full rate, one unit per element or per lane; N
builds N units and an operation takes `elements / N` passes.** A width at full
rate is the plain un-walked array and costs nothing over having no width at all.

**The spelling of full rate changed.** On tree W and tree W+, a count of `0` on
several knobs meant *full width*, with a separate boolean beside it saying
whether the feature existed. Rows below that read `PERM_UNITS 0` or
`SHIFT_UNITS 0` in their source logs are written here as **8 (full width)** so
they cannot be misread as "not built" under the current spelling.

### SIMD PE — `khs_unit`, and `rv_pe`'s `SIMD_*` parameters above it

`rv_pe` prefixes each of these with `SIMD_` and passes it down; `SIMD_EN = 0`
elaborates none of the unit and leaves the base controller PE bit for bit.

| parameter | feature | 0 means |
|---|---|---|
| `SIMD` | 32-bit slots per vector register; the register is `32 × SIMD` bits | — (architecture: the register width) |
| `ILANES` | integer lanes: the packed ALU and its multipliers, which are one unit | not built; every integer vector opcode faults |
| `SHIFT_UNITS` | packed-shift units | not built; the shift opcodes fault |
| `PERM_UNITS` | cross-lane permute — 32-bit **output** words produced per pass | not built; slide, pack and unpack fault |
| `RED_UNITS` | the `vredsum` / `vredmax` trees | not built; both reduce opcodes fault |
| `FLOAT_LANES` | binary32 fused multiply-add units | no float tier; every float opcode faults |
| `FSFU_UNITS` | seed units (`exp2`, `log2`, `rcp`, `rsqrt`), a subset of `FLOAT_LANES` | no seeds; a seed opcode faults |
| `FCVT_UNITS` | binary32 ↔ int32 converters per pass | not built; the whole FCVT group faults |
| `HAS_SHROUND` | `vsrari`'s round adder; requires `SHIFT_UNITS > 0` | `vsrari` rounds toward zero |
| `HAS_FALU` | the elementwise float group — the tier's base | its opcodes fault |
| `HAS_FACC` | the rotating float accumulator and its fold | its opcodes fault |
| `NACC` | banks in the float accumulator | — |
| `NPART` | rotating partials per accumulator slot | — (architectural: it changes float answers) |
| `VREGS` | vector registers | — |
| `WB_STAGE`, `RED_PIPE`, `VREG_PRIM`, `MEM_PRIM`, `USE_DSP`, `VSPAD_ENTRIES` | structural choices, not unit counts | — |

### SIMT PE — `kht_pe` / `kht_core` / `kht_unit`

| parameter | feature | 0 means |
|---|---|---|
| `LANES` | threads, and the integer ALU width | — (a SIMT PE is its threads) |
| `WAVES` | resident wave contexts and the scheduler's storage | — |
| `FLANES` | binary32 FMA units | no float tier; every float opcode faults |
| `FSFU_UNITS` | seed units, a subset of `FLANES` | no seeds; a seed opcode faults |
| `SHFL_UNITS` | cross-lane shuffle **output** lanes per pass | not built; `shflxor` and `bcast` fault |
| `LDS_BANKS` | banks in the shared memory | not built; the LDS goes down its serial path |
| `HAS_MASK`, `HAS_IPDOM`, `IPDOM_D` | the active mask, the divergence stack and its depth | that gate's opcodes fault |
| `VREG_PRIM`, `MEM_PRIM`, `INST_DEPTH`, `RECV_DEPTH`, memory depths | structural choices | — |

### The multiplier count is not a knob on either core

RV32IM is the instruction set, not an option: **every RV32 core in the tree is
RV32IM** — the SIMD PE, the SIMT PE and the system node's control processor
alike. `div`, `divu`, `rem` and `remu` are not built and fault, which is a
decision with arithmetic behind it: an iterative divider costs about 35 cycles
against a software routine's 60–80, a 2× on a rare instruction, where `mul` is
8–13× on a common one. Once `mul` exists, divide-by-a-constant strength-reduces
to `mulhu`, which is the case software actually meets.

The integer ALU is therefore an **IM unit** — add, subtract, compare, bitwise
and multiply through one operand path and one result path, rather than an ALU
beside a multiplier array with a dispatch mux between them. Multiplexers are the
expensive primitive on this fabric, and two units serving one issue port need an
operand mux in front and a result mux behind, both wider than the logic they
arbitrate. So the multiply count follows the lane count: `ILANES` on SIMD,
`LANES` on SIMT. It occupies DSP columns rather than fabric, and these parts are
LUT-bound, so there is nothing to gain by narrowing it and a mux to lose by
making it separable.

### What the hardware does with a count

A feature with `U` units serving `N` elements issues `N/U` passes, one per
cycle, sequenced by the hardware. **The ISA carries no count**: the same
instruction, the same binary and the same golden memory image at every `U`. The
only difference is cycles.

The two cores place a pass differently, and it is the reason a fractional rate
costs more on SIMD than on SIMT:

- **SIMT** places a pass with the register file's **per-lane write enable**.
  Thread *i* is served by unit `i mod U`, a compile-time constant, and the enable
  is a decode of the retiring pass index. No staging register, no runtime unit
  select.
- **SIMD** places a pass into a **staging register** and writes once when the
  last pass lands, because `khs_vregfile` has no per-element write enable.

---

## 2. The two reference rows, with every generic named

A LUT figure without its configuration is not a measurement: the reader fills
the gaps with zeros and prices a bare core against a fully-featured one.

### SIMD, tree W — **15,682 LUT / 9,836 FF / 13 BRAM / 56 DSP / 130 control sets / 349.3 MHz**

    SIMD_EN 1   SIMD 8   ILANES 8   MULS 4   NACC 2   VREGS 8   VSPAD 1024
    NPART 16    SHIFT_UNITS 8 (full)   PERM_UNITS 8 (full)   RED_PIPE 1
    FLOAT_LANES 4   FSFU_UNITS 0   HAS_FALU 1   HAS_FACC 0   HAS_FCVT 0
    HAS_F16 1   HAS_F32 1   DOT_DSP 0   WB_STAGE 0
    VREG_PRIM distributed   MEM_PRIM block   RECV_MEM distributed   USE_DSP yes

### SIMT, tree W — **19,461 LUT / 17,268 FF / 30.5 BRAM / 48 DSP / 202 control sets / 361.0 MHz**

    LANES 8   WAVES 16   HAS_MASK 1   HAS_IPDOM 1   IPDOM_D 8
    SHFL_UNITS 8 (full)   LDS_BANKS 8 (full)
    FLANES 8   FSFU_UNITS 0   MUL_UNITS 8   HAS_F16 1   HAS_F32 1
    VREG_PRIM block   MEM_PRIM block   INST_DEPTH 16   RECV_DEPTH 512
    IMEM 2048 words   SPAD 2048 words   L1 128 lines

Both reproduced byte-identically across separate runs of the campaign, in every
column, which is what makes rows from different suites diffable against each
other. That property is not assumed elsewhere: a configuration re-synthesised
separately has come back bit-identical on one module in this repository and four
LUT apart on another, so it holds where it has been checked and nowhere else.

---

## 3. The metric table: one feature moved at a time

Every cell is measured. A knob point that was not synthesised is left blank and
named in [§7](#7-what-is-blank-and-why); no cell is inferred or predicted.

### 3a. SIMD, against 15,682

| knob | value | LUT | ΔLUT | FF | BRAM | DSP | ctrl | Fmax |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| — | baseline | **15,682** | — | 9,836 | 13 | 56 | 130 | 349.3 |
| `PERM_UNITS` | 8 (full) | 15,682 | 0 | 9,836 | 13 | 56 | 130 | 349.3 |
| | 2 | 15,185 | **−497** | 10,077 | 13 | 56 | 130 | 335.8 |
| | 1 | 14,459 | **−1,223** | 10,080 | 13 | 56 | 128 | 318.3 |
| `SHIFT_UNITS` | 8 (full) | 15,682 | 0 | 9,836 | 13 | 56 | 130 | 349.3 |
| | 4 | 14,970 | **−712** | 10,085 | 13 | 56 | 133 | 344.4 |
| | 2 | 14,623 | **−1,059** | 10,083 | 13 | 56 | 131 | 320.6 |
| | 1 | 14,779 | **−903** | 10,098 | 13 | 56 | 134 | 345.7 |
| `HAS_SHIFT` | 0 — the gate; shifts **fault** | 14,757 | −925 | 9,749 | 13 | 56 | 122 | 343.5 |
| `FLOAT_LANES` | 8 | 20,063 | **+4,381** | 13,499 | 13 | 64 | 138 | 310.4 |
| | 4 | 15,682 | 0 | 9,836 | 13 | 56 | 130 | 349.3 |
| | 2 | 13,676 | **−2,006** | 8,011 | 13 | 52 | 115 | 324.4 |
| `FSFU_UNITS` | 0 | 15,682 | 0 | 9,836 | 13 | 56 | 130 | 349.3 |
| | 1 | 16,674 | **+992** | 10,232 | 14.5 | 57 | 134 | 318.1 |
| | 4 (full rate) | 16,668 | **+986** | 11,292 | 19 | 60 | 136 | 318.1 |
| `NACC` | 1 | 15,128 | **−554** | 9,564 | 13 | 56 | 127 | 321.6 |
| | 2 | 15,682 | 0 | 9,836 | 13 | 56 | 130 | 349.3 |
| | 4 | 16,005 | **+323** | 10,362 | 13 | 56 | 136 | 341.9 |
| `MULS` | 2 | 16,262 | **+580** | 9,974 | 13 | **32** | 131 | 327.7 |
| | 4 | 15,682 | 0 | 9,836 | 13 | 56 | 130 | 349.3 |
| `VREGS` | 4 | 15,647 | **−35** | 9,846 | 13 | 56 | 129 | 327.3 |
| `HAS_F32` | 0 | 13,932 | **−1,750** | 9,341 | 13 | 56 | 126 | 318.3 |
| `HAS_F16` | 0 | 13,656 | **−2,026** | 9,770 | 13 | 56 | 113 | 318.3 |
| `WB_STAGE` | 1 | 15,736 | **+54** | 10,110 | 13 | 56 | 128 | 341.9 |
| `VREG_PRIM` | block | 15,674 | **−8** | 9,068 | **25** | 56 | 128 | 253.7 |
| `SIMD` | 4 | 11,282 | −4,400 | 9,017 | 9 | 32 | 119 | 322.9 |
| | 8 | 15,682 | 0 | 9,836 | 13 | 56 | 130 | 349.3 |
| | 16 | 24,830 | +9,148 | 11,448 | 21 | 104 | 133 | 307.7 |
| `SIMD_EN` | 0 — the controller PE alone | 2,661 | −13,021 | 4,140 | 5 | 0 | 79 | 396.5 |
| **combined** | `PERM_UNITS` 1 + `SHIFT_UNITS` 2 | **13,586** | **−2,096** | 10,313 | 13 | 56 | 139 | 367.8 |

The `HAS_F16` and `HAS_F32` rows price a feature the RTL no longer has: they are
the cost of carrying a second **memory** format into an E8M15 datapath, and both
formats and the datapath are gone. They are kept only because they are the
sharpest measurement of one property — see [§5](#5-simd-against-simt-at-the-same-float-width).

### 3b. SIMT, against 19,461

| knob | value | LUT | ΔLUT | FF | BRAM | DSP | ctrl | Fmax |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| — | baseline | **19,461** | — | 17,268 | 30.5 | 48 | 202 | 361.0 |
| `SHFL_UNITS` | 8 (full) | 19,461 | 0 | 17,268 | 30.5 | 48 | 202 | 361.0 |
| | 4 | 19,488 | **+27** | 17,264 | 30.5 | 48 | 204 | 377.9 |
| | 2 | 19,318 | **−143** | 17,261 | 30.5 | 48 | 198 | 379.4 |
| | 1 | 18,944 | **−517** | 17,270 | 30.5 | 48 | 204 | 343.1 |
| `HAS_SHFL` | 0 — the gate; the shuffle **faults** | 18,581 | −880 | 17,267 | 30.5 | 48 | 202 | 383.0 |
| `LDS_BANKS` | 8 (full) | 19,461 | 0 | 17,268 | 30.5 | 48 | 202 | 361.0 |
| | 4 | 18,847 | **−614** | 17,267 | 26.5 | 48 | 202 | 405.2 |
| | 2 | 18,617 | **−844** | 17,277 | 24.5 | 48 | 202 | 365.5 |
| | 1 | 17,899 | **−1,562** | 17,264 | 24.5 | 48 | 202 | 361.0 |
| `HAS_LDSBANK` | 0 — the gate; no LDS | 17,656 | −1,805 | 16,930 | 24.5 | 48 | 191 | 376.4 |
| `FLANES` | 8 | 19,461 | 0 | 17,268 | 30.5 | 48 | 202 | 361.0 |
| | 4 | 16,307 | **−3,154** | 13,917 | 30.5 | 40 | 178 | 334.7 |
| | 2 | 14,100 | **−5,361** | 12,233 | 30.5 | 36 | 163 | 378.9 |
| `MUL_UNITS` | 8 | 19,461 | 0 | 17,268 | 30.5 | 48 | 202 | 361.0 |
| | 4 | 19,404 | **−57** | 15,461 | 30.5 | **32** | 203 | 376.6 |
| | 2 | 19,417 | **−44** | 14,571 | 30.5 | **24** | 203 | 368.3 |
| `FSFU_UNITS` | 0 | 19,461 | 0 | 17,268 | 30.5 | 48 | 202 | 361.0 |
| | 2 | 20,841 | **+1,380** | 17,954 | 33.5 | 50 | 211 | 346.4 |
| | 8 (full rate) | 22,084 | **+2,623** | 20,065 | 42.5 | 56 | 228 | 363.9 |
| `WAVES` | 16 | 19,461 | 0 | 17,268 | 30.5 | 48 | 202 | 361.0 |
| | 8 | 18,802 | **−659** | 16,756 | 30.5 | 48 | 175 | 380.4 |
| | 4 | 18,414 | **−1,047** | 16,497 | 30.5 | 48 | 160 | 352.6 |
| `IPDOM_D` | 4 | 19,453 | **−8** | 17,247 | 30.5 | 48 | 202 | 360.2 |
| `HAS_MASK` + `HAS_IPDOM` | 0 — the gate | 19,264 | −197 | 17,028 | 30.5 | 48 | 165 | 380.5 |
| `HAS_F32` | 0 | 17,968 | **−1,493** | 16,153 | 30.5 | 48 | 201 | 365.8 |
| `HAS_F16` | 0 | 17,479 | **−1,982** | 17,250 | 30.5 | 48 | 192 | 385.2 |
| `LANES` | 4, with `FLANES` 4 and `MUL_UNITS` 4 | 11,369 | −8,092 | 10,712 | 20.5 | 24 | 168 | 383.4 |

`MUL_UNITS` is gone from the RTL: a thread's ALU is an IM unit, so the multiply
count is `LANES`. The rows are kept because they are the measurement that
justified deleting it — the LUT column is flat and only the DSP column moves.

### 3c. Three knobs measured on tree W+

`ILANES`, `RED_UNITS` and `HAS_SHROUND` do not exist on tree W. Their reference
row is a different one and its own generics are:

    SIMD  SIMD_EN 1  SIMD 8  ILANES 8  MULS 4  SHIFT_UNITS 8 (full)
          PERM_UNITS 8 (full)  RED_UNITS 1  HAS_SHROUND 1  WB_STAGE 0
          FLOAT_LANES 4  FSFU_UNITS 1  HAS_FALU 1  HAS_FACC 0  FCVT_UNITS 0
          NPART 16  NACC 2  VREGS 8  HAS_F16 1  HAS_F32 1  DOT_DSP 0
          RECV_MEM distributed  VREG_PRIM distributed
          -> 16,782 LUT   10,487 FF   61 DSP   14.5 BRAM

| change | ΔLUT | note |
|---|---:|---|
| `ILANES` 8 → 4 | **−1,558** | DSP stays at 61 across 8, 4 and 2 |
| `ILANES` 4 → 2 | −346 | FF rises 92 then 72 — the staging register |
| `SHIFT_UNITS` 8 → 2 | −777 | |
| `PERM_UNITS` 8 → 2 | −742 | |
| `RED_UNITS` 1 → 0 | −551 | removes `vredsum` and `vredmax` |
| `HAS_SHROUND` 1 → 0 | −398 | `vsrari` rounds toward zero |
| `FSFU_UNITS` 1 → 0 | −847 | |
| `FSFU_UNITS` 1 → 4 (full rate) | **−66** | full rate is cheaper than one unit — see [§4a](#4a-a-fractional-rate-is-worst-in-the-middle) |
| `FLOAT_LANES` 4 → 8 | +4,203 | **1,051 per FMA unit** |

**`ILANES` narrows the ALU and not the multipliers, and the DSP column proves
it**: 61 DSP at `ILANES` 8, 4 and 2 alike. Fabric adders and DSP columns are two
budgets, and one knob must not span both.

---

## 4. The per-unit arithmetic, named

Every figure is a marginal difference between two rows one step apart.

| unit | arithmetic | per unit |
|---|---|---|
| SIMD FP FMA | (20,063 − 15,682)/4 and (15,682 − 13,676)/2 | **1,095** and **1,003** |
| SIMT FP FMA | (19,461 − 16,307)/4 and (16,307 − 14,100)/2 | **789** and **1,104** |
| SIMD 32-bit slot (`SIMD`) | (15,682 − 11,282)/4 and (24,830 − 15,682)/8 | **1,100** and **1,144** |
| SIMD float accumulator bank | (15,682 − 15,128)/1 and (16,005 − 15,682)/2 | **554** then **162** |
| SIMT wave slot | (19,461 − 18,802)/8 and (18,802 − 18,414)/4 | **82.4** then **97** |
| SIMT seed unit | (20,841 − 19,461)/2 and (22,084 − 20,841)/6 | **690** then **207** |
| SIMT multiply unit | LUT is flat; DSP is (48 − 24)/6 | **≈0 LUT, 4 DSP** |
| SIMD multiply depth | `MULS` 4 → 2 is +580 LUT for −24 DSP | a LUT-for-DSP trade, the wrong direction here |

**The two float-unit columns disagree by design and both are given.** SIMD's
unit is dearer at the wide end and SIMT's at the narrow end, so a single number
would be an average. This page does not average.

### Exact on every row: DSP and BRAM

    SIMT   DSP  = 2*FLANES + 4*LANES
           BRAM = 26.5 + (FLANES>0 ? 4 : 0) + 1.5*FSFU_UNITS

Checked with zero error against every SIMT row above, including the two rows
where `LANES` itself moves and the prediction row in
[§6](#6-the-additive-model-and-its-measured-error). The 4 BRAM at `FLANES > 0`
is `kht_vregfile`'s third read port, which exists only for the FMA's addend, so
it follows the float count and not the multiplier's.

**The equivalent SIMD form is withdrawn.** It was fitted with a `6 × SIMD` term
for the integer lane's multipliers, and the parameter that sized them is gone;
the term describes a lane the RTL no longer builds. The SIMT form survives
because both of its terms — `FLANES` and `LANES` — are current.

The per-unit **BRAM and DSP** figures for the float tier are identical on both
cores, which they must be: both instantiate the same units. A float unit is
2 DSP; the seed capability adds 1 DSP and 1.5 BRAM — three RAMB18, one ROM per
polynomial coefficient — to a unit that carries it.

### 4a. A fractional rate is worst in the middle

The seed count is **not monotonic in LUT**, on either core, and the reason is
structural rather than an artefact.

The finest-grained measurement of it is an earlier campaign than the metric
table — all five points on one frozen tree, so its rows are internally
comparable and are not subtracted against [§3](#3-the-metric-table-one-feature-moved-at-a-time):

    SIMT, FSFU_UNITS out of 8 float units, all deltas against that campaign's own reference
    units          0        1        2        4        8
    delta LUT      0     +461   +1,226   +1,026   +2,207
    delta BRAM     0     +1.5     +3.0     +6.0    +12.0
    delta DSP      0       +1       +2       +4       +8

BRAM and DSP are exactly linear. LUT is not, and **four units of eight is
cheaper than two.** Splitting each row into seed hardware at 276 LUT a unit and
the residual leaves:

    units   seed hardware   residual = the walk
      1        276              185
      2        552              674
      4      1,104              −78
      8      2,207                0

The residual is the sum of two terms that move in **opposite directions** with
the unit count. The **placement mux** — thread *i*'s seed source is unit
`i mod U` — does not exist at one unit and grows with `U`. The **pass decode**
has `LANES/U` values and shrinks with `U`. The sum is smallest at both ends and
worst in the middle. At full rate the walk does not exist at all: both index
arms are the same expression and the mux folds, which is why a full-rate seed
tier is pure seed hardware.

**A second, independent measurement gives the same shape**, which is what makes
it a property rather than one campaign's oddity: on tree W+, on the *other*
core, `FSFU_UNITS` at full rate measures **66 LUT below** one unit for four
times the rate. **Take `FSFU_UNITS = FLOAT_LANES` and spend the DSP and BRAM**,
or take one unit; the middle is the one place not to sit.

Against full rate, what a fractional SIMT seed tier buys:

| rate | LUT saved | BRAM saved | DSP saved |
|---|---|---|---|
| half, 4 of 8 | 1,181 — **53%** | 6 — 50% | 4 — 50% |
| quarter, 2 of 8 | 981 — 44% | 9 — 75% | 6 — 75% |
| eighth, 1 of 8 | 1,746 — **79%** | 10.5 — 88% | 7 — 88% |

Quarter rate — the ratio every desktop GPU provisions transcendentals at — saves
three quarters of the BRAM and DSP and only 44% of the LUT.

### 4b. A cross-lane width pays at one or two units and nowhere else

Two features, two cores, one property. SIMD's permute and SIMT's shuffle both
move data between lanes, and both were widened from all-or-nothing gates:

| units | SIMD `PERM_UNITS` vs 15,682 | SIMT `SHFL_UNITS` vs 19,461 |
|---:|---:|---:|
| 8 (full) | 0 | 0 |
| 4 | — | **+27** |
| 2 | −497 | −143 |
| 1 | **−1,223** | **−517** |

Both curves are strongly non-linear and both are worth having only at the narrow
end; on SIMT four units **cost** 27 LUT rather than saving any. The mechanism is
the same on both. A narrow build is a **direct select**, not a narrowed network:
a butterfly routes every lane at once and cannot be sliced, so one output lane
is a `LANES`-to-1 32-bit mux. That mux is what the width pays for, and it is why
one unit recovers 59% of what deleting the shuffle entirely saves rather than
all of it.

**The default costs exactly zero.** At full width the original full-width form
is kept in its own elaboration branch and the walk exists only in the narrow
one, so both knobs are byte-identical to the reference in every column until
they are used.

**A width can beat deleting the feature.** `SHIFT_UNITS` 2 recovers 1,059 LUT of
the 925 that removing the shifter saves — more than deletion — and keeps every
shift instruction. The arithmetic that had refused a width here charged one
operand mux per shifter *removed*, where a walk pays one mux per unit *kept*: at
two units that is two muxes against six shifters, not one against one.

### 4c. Three knobs that are not levers

| knob | measured | why |
|---|---|---|
| SIMD `VREGS` 8 → 4 | **−35 LUT** for half the file | a distributed-RAM primitive is 32 entries deep, so eight entries and four entries are the same LUTs. Read the other way it predicts the file could grow toward 32 entries for little more than it costs at 8 — a prediction, not a row; `VREGS` 16 and 32 were not synthesised, and the encoding would have to widen with them. |
| SIMT `IPDOM_D` 8 → 4 | **−8 LUT**, −21 FF | the divergence stack is storage, and it is small storage. |
| SIMD `VREG_PRIM` block | **−8 LUT for +12 BRAM**, and −95.6 MHz | distributed RAM falls but logic LUTs rise, because a shallow block RAM buys its storage back in address and enable logic. The vector file's read-to-write loop is the binding path on this PE, so block RAM lands directly on it. |

`VREG_PRIM = block` is **available on the SIMT PE and not on this one**: there
the binding path is the predecoder writing the control RAM and the register file
is not in the loop. A configuration matched across both cores would be
comparing a machine the SIMD PE cannot build.

### 4d. An area directive is not a shrink

`-directive AreaOptimized_high` is **−1,301 LUT on byte-identical source** and
takes the SIMD PE to **260.8 MHz, slack −0.502** — it does not meet the target,
and out-of-context synthesis is the optimistic end. A row that misses timing is
not a smaller PE, it is a PE that does not build. **Every other row on this page
is `-directive default`**, and nothing is claimed on the directive rows.

---

## 5. SIMD against SIMT at the same float width

The two PEs land within 1% of each other at 8 threads / 8 slots and 8 FP FMA
units, and the price list says why that is not a redundancy to remove.

Every figure in this section comes from **one earlier campaign** than the metric
table, taken before the integer dot unit was removed. Its rows are internally
comparable and are never subtracted against
[§3](#3-the-metric-table-one-feature-moved-at-a-time).

| block | SIMT | SIMD |
|---|---|---|
| PE with no float tier | **10,852** | **10,309** |
| marginal FP FMA unit | 789–1,104 | 1,003–1,095 |
| divergence: mask + IPDOM stack | 681 | — |
| divergence: subgroup butterfly | 1,224 | — |
| divergence: banked LDS + resolver | 1,948 | — |
| packed shifter | — (RV32I's shifter is inside the thread's own ALU) | 1,088 |
| cross-lane permute (slide, pack, unpack) | — | 1,884 |

Three things follow, and they answer "SIMD should be much cheaper".

**1. SIMD's base PE is 543 LUT cheaper than SIMT's** — 10,309 against 10,852 —
even though SIMD's base carries the shifter, the permute network and 32
multipliers and SIMT's carries no multiplier at all. So the divergence hardware
is real and SIMD does not pay for it.

**2. Once SIMT's optional blocks come off, SIMT is the cheaper machine.** With
mask, IPDOM, shuffle and the banked LDS all off, at 8 FMA and 8 multiply units,
the SIMT PE measures **16,118**. The comparable SIMD figure — its own reference
less the shifter and the permute — is **16,775**, which is **657 LUT, 4.1%,
dearer.** Any claim that SIMD wins on area at matched features is wrong.

**3. SIMD's float unit is dearer per unit for the same arithmetic.** On tree W
the difference is the **packed element index**: a SIMD unit selected its element
out of `2 × SIMD` slots, where a SIMT unit's element is one whole 32-bit slot at
a constant index. The dual-format rows measure the same thing from a second
direction — a SIMD unit paid 2.1–2.4× what a SIMT unit paid for the same second
memory format, and the converters account for only about a third of it; the rest
is operand slicing.

**That mechanism is the one the current RTL removes.** With binary32 as the only
compute type a 32-bit word holds exactly one element on both cores, so the
packed index is a constant index on both. The size of what remains has not been
measured.

So parity is not redundancy. SIMD is not a subset of SIMT: it carries packed
int8/int16/int32 lanes, a cross-lane permute network, a vector scratchpad and
optionally a rotating float accumulator. **"SIMD must always be much cheaper at
the same features and the same unit counts" is not reachable by removing
redundancy; it is a decision about which SIMD features to drop**, and every one
of them is priced above.

---

## 6. The additive model and its measured error

The table is only useful if single-knob deltas **add**. That is the assumption
the whole page rests on and the only thing worth testing, so it is tested on
combinations no row was fitted on.

**SIMD:** `PERM_UNITS` 1 + `SHIFT_UNITS` 2 — the two knobs that cost cycles and
nothing else — predicting 15,682 − 1,223 − 1,059 = 13,400 from
[§3a](#3a-simd-against-15682).

| | predicted | measured | error |
|---|---|---|---|
| DSP | 56 | **56** | **0** |
| BRAM | 13 | **13** | **0** |
| LUT | 13,400 | **13,586** | **+186, +1.4%** |

**SIMT:** `WAVES` 8 + `IPDOM_D` 4 + `HAS_F16` 0, predicting
19,461 − 659 − 8 − 1,982 = 16,812 from [§3b](#3b-simt-against-19461).

| | predicted | measured | error |
|---|---|---|---|
| DSP | 48 | **48** | **0** |
| BRAM | 30.5 | **30.5** | **0** |
| LUT | 16,812 | **16,552** | **−260, −1.5%** |

**Two prediction rows, five knobs, and the two LUT errors have opposite signs.**
The SIMD model under-predicts by 1.4% and the SIMT model over-predicts by 1.5%,
so adding single-knob deltas is unbiased here rather than systematically
optimistic — which is the property that makes the table usable for a
configuration nobody has synthesised. DSP and BRAM are exact on both, as they
are on every prediction this campaign made.

Every term in both predictions comes from the same frozen tree as its own
baseline. A prediction that borrows a term from a different tree is not a test
of the model.

**Where the model is weak.** The terms are marginals from a single base, so the
estimate is additive by construction and cannot see two features that share
control logic. Removing features **together** saves more than removing them one
at a time, because shared control and mux logic goes away once when its last
consumer does. A stripped configuration therefore comes in **cheaper** than the
model predicts, never dearer — so an estimate used as a ceiling is safe, and one
used as a floor is not. On rows that moved seven knobs at once the error has
reached 44%.

---

## 7. What is blank, and why

| gap | reason |
|---|---|
| **every knob, on the current RTL** | the float tier, `MULS`, `DOT_DSP` and the two memory formats all changed after these campaigns. Nothing on this page has been re-measured against the parameter set the RTL now has. |
| `FCVT_UNITS` at any value | the converter group had no datapath when these rows were taken, so its cost was never a measurement of a working converter. It has one now and has not been priced. |
| `PERM_UNITS` 4; `LDS_BANKS` and `SHFL_UNITS` beyond the points shown | three points per knob is the density these campaigns ran; the fourth was not measured. |
| `VREGS` 16 and 32 | not synthesised. That the file grows nearly free is a **prediction** from the 8 → 4 row, not a result. |
| `NPART`, `VSPAD_ENTRIES`, the instruction, scratchpad and L1 depths, `RECV_DEPTH`, `INST_DEPTH` | memory depths rather than unit counts; unmeasured here. |
| `HAS_FACC` on the metric-table tree | the accumulator is off in both baselines. Its cost was measured on an earlier tree, before the fix that connected the accumulator's operation port, and was not re-taken. |
| SIMD "how many lanes carry multipliers" | not built. SIMT has exactly this as its lane count; SIMD has no equivalent axis, because every lane carries its own multipliers. |
| a placed or routed figure for either PE | none exists. Every number here is out-of-context synthesis. |

---

## 8. On the SIMD PE, frequency does not track area

Worth more than a few hundred LUT, and established on four measurements rather
than one.

- The smallest SIMD PE with a float tier — 20% below the reference — measures
  **341.1 MHz** against the reference's 340.8. Shrinking by a fifth moved Fmax
  by 0.3 MHz.
- Halving the logic depth bought **1.1 MHz**. At `WB_STAGE` 0 the binding path
  is 13 levels with 4 CARRY8, a register-file round trip in one cycle; at
  `WB_STAGE` 1 it is 7 levels and the binding path leaves the extension
  entirely, landing on the base core's scratchpad-load-to-register-file path.
- Removing a memory format was −24 MHz; two widths together were +27.

**The ceiling is route, not logic.** Every binding path measured is 59–74% route
delay. That is the shape of a wide uniform-control datapath: one decode, many
consumers, and the delay is in the broadcast.

**`WB_STAGE = 1` is worth taking regardless.** It costs about 19 LUT and 260 FF,
halves the logic depth, and moves the binding path out of the extension into the
base core — which alone runs at 396.5 MHz.

**What is not established: that any particular clock is out of reach.** The
SIMD rows span 316.8–367.8 MHz and the SIMT rows 343.1–385.2, and the one SIMD
row that clears 350 does so with two knobs that are each individually negative.
A single row is not a property. **Settling the SIMD/SIMT frequency asymmetry
needs a placed run**, which is outside an out-of-context campaign.
