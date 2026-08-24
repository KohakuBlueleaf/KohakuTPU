---
title: Per-feature unit counts
summary: The marginal LUT cost of one unit of every widthed feature in both PEs, measured as the difference between two rows that differ in one count — so a configuration can be priced without synthesising it.
tags:
  - architecture
  - pe
  - performance
---

# Per-feature unit counts, and what each unit costs

Every feature of both PEs that has a WIDTH now has its own parameter, settable
independently of every other. This file is the price list: what one unit of each
feature costs, so that a configuration can be **inferred** instead of
synthesised, and the validation of that inference against a synthesised
combination.

**Read the arithmetic, not the totals.** Every per-unit figure below is a
MARGINAL cost — the difference between two rows that differ in one count —
never a total divided by a count. Dividing a tier by its unit count charges the
tier's fixed overhead to the units and invents a defect.

**Three frozen snapshots, and rows are never mixed across them.** Snapshot 1 is
where the price list is fitted, one knob per row. Snapshot 2 carries the fixes
this work then made, and appears only in §5 and §5b as a labelled before/after.
Snapshot 3 is §10 to §14: it opens with BOTH references re-synthesised
byte-identical to snapshot 2 (SIMD 15,741, SIMT 19,461), which is what makes a
snapshot-3 row diffable against a snapshot-2 one at all.
Every row is `-flatten_hierarchy rebuilt`, synth only, 3.333 ns, on
xcvu13p-fhgb2104-2L-e, and `-directive default` except where §10 names
`AreaOptimized_high`. Fmax figures are synthesis ESTIMATES and move by tens of
MHz between rows that differ in nothing that should matter; treat them as a
screen, not a result. **No decision in this file was made on Fmax** — area and
what a LUT buys are the whole of it; where §10 and §14 quote an Fmax it is to
state a trade the reader must be told about, not to choose one.

---

## 1. The parameters

### SIMT PE (`kht_pe` / `kht_core` / `kht_unit`)

| parameter | feature | legal | 0 means |
|---|---|---|---|
| `LANES` | threads, and the integer ALU width | 1,2,4,8 | — (a SIMT PE is its threads) |
| `FLANES` | FP FMA units | 0,1,2,4,8 | no float tier; every float opcode faults |
| `FSFU_UNITS` | seed units (exp2, log2, rcp, rsqrt), a subset of `FLANES` | 0,1,2,4,8 | no seeds; a seed opcode faults |
| `MUL_UNITS` | RV32M `mul`/`mulh`/`mulhsu`/`mulhu` units | 0,1,2,4,8 | no multiplier; `mul` faults |
| `HAS_F16` / `HAS_F32` | the two memory formats at the unit's edge | 0,1 each | that format's opcodes fault; at least one must be on |
| `SHFL_UNITS` | cross-lane shuffle OUTPUT lanes per pass | 0,1,2,4,8 dividing `LANES` | one per lane (the butterfly) |
| `LDS_BANKS` | banks in the shared memory | 0,1,2,4,8 dividing `LANES` | one per lane |
| `WAVES`, `HAS_MASK`, `HAS_IPDOM`, `HAS_SHFL`, `HAS_LDSBANK`, `IPDOM_D` | unchanged | | |

`WAVES` and `IPDOM_D` were counts with no measured price and now have one — §10.
`HAS_SHFL` and `HAS_LDSBANK` were the last all-or-nothing gates on a DATAPATH
and are now widths on top of the gates — §15. `HAS_MASK`/`HAS_IPDOM` stay
booleans and §15 gives the arithmetic for why a width there cannot pay.

`HAS_FLT` **is gone.** A count of 0 is what "not built" means, and a boolean
beside a count is two ways to say one thing. It was also a binding: `is_imul`
read `ictl[C_IMUL] && (HAS_FLT != 0)`, so there was no float-free build with a
multiplier and no float build without one.

`FLANES = LANES` **is gone as a default.** It made the float width track the
thread width, so "eight threads, two float units" could not be expressed. Each
count now defaults to 0 and a nonzero one that does not divide `LANES` is
refused at elaboration by a module that does not exist
(`kht_fpu_requires_FLANES_to_divide_LANES`).

### SIMD PE (`rv_pe` / `rv_core` / `khs_unit`)

| parameter | feature | legal | 0 means |
|---|---|---|---|
| `SIMD` | 32-bit slots per vector register (VW = 32·SIMD) | 2,4,8,16 | — (architecture: the register width) |
| `FLOAT_LANES` | FP FMA units, against 2·SIMD packed FP16 elements | 0,1,2,4,8,16 | no float units; every float opcode faults |
| `FSFU_UNITS` | seed units, a subset of `FLOAT_LANES` | 0,1,2,4,8,16 | no seeds; a seed opcode faults |
| `PERM_UNITS` | cross-lane permute units — 32-bit OUTPUT words per pass | 0,1,2,4,8,16 dividing SIMD | one per word |
| `HAS_F16` / `HAS_F32` | the two memory formats at the unit's edge | 0,1 each | that format's opcodes fault; at least one must be on |
| `SHIFT_UNITS` | packed-shift units, against `SIMD` lanes | 0,1,2,4,8 dividing `SIMD` | one per lane (inside `khs_lane`) |
| `NACC` | integer accumulator banks, each SIMD int32 wide | 1,2,4 | — (`vdot` needs one) |
| `VREGS` | vector registers | 4,8,16,32 | — |
| `HAS_FALU`, `HAS_FACC`, `HAS_SHIFT`, `MULS`, `DOT_DSP` | unchanged | | |

`NACC` and `VREGS` were already `rv_pe` parameters and simply not reachable from
the OOC script; both now have a measured price — §10.

**Both cores now spell "none" the same way: a count of 0.** `FLOAT_LANES = 0`
used to mean "one unit per element" — the WIDEST tier — where the SIMT PE's
`FLANES = 0` meant no tier at all, so the same 0 described opposite machines and
a caller who forgot the parameter got whichever the core happened to mean. A
build that wants one unit per element now says `FLOAT_LANES = 2*SIMD`, and
asking for a float GROUP with no units is refused at elaboration
(`khs_unit_float_groups_need_FLOAT_LANES_nonzero`) rather than silently given
the widest tier. The golden model, the generator, the bench and both OOC scripts
moved with it — `khs_gen` and `khs_run` default to `2*SIMD` and refuse
`--float --flanes 0` by name, and `DspMachine` distinguishes an UNSPECIFIED
count (the widest, so every caller that never cared is unchanged) from a
deliberate 0.

**Re-synthesised after the respelling: 15,741 LUT / 9,845 FF / 13 BRAM / 56 DSP
at the reference and 14,906 at `PERM_UNITS` 2 — both identical to the rows from
before it.** A spelling change must move no hardware, and this one did not.

`HAS_FSFU` was a **boolean and is now a count.** A row measured when it was a
boolean is NOT comparable: `HAS_FSFU = 1` meant "every float lane is
seed-capable", which is `FSFU_UNITS = FLOAT_LANES` here. The OOC tags carry
`sfu<N>u` so the two can never be read as the same row.

### What the hardware does with a count

A feature with `U` units serving `N` elements issues `N/U` passes, one per
cycle, sequenced by the hardware. **The ISA carries no count**: the same
instruction, the same binary, the same golden memory image at every `U`. The
only visible difference is cycles.

- SIMT places a pass with the register file's **per-lane write enable**: thread
  `i` is served by unit `i mod U`, a compile-time constant, and the enable is a
  decode of the retiring pass index. No staging register and no runtime unit
  select.
- SIMD places a pass into a **staging register** and writes once when the last
  pass lands, because `khs_vregfile` has no per-element write enable. That is
  what makes fractional rate cost more on SIMD than on SIMT — see §5.

---

## 2. SIMT: measured rows

`kht_pe`, xcvu13p-fhgb2104-2L-e, `-flatten_hierarchy rebuilt`, synth only,
3.333 ns. LANES 8, WAVES 16, mask/ipdom/shfl/ldsbank on, VREG_PRIM block,
IMEM 2048, SPAD 2048, L1 128 lines, RECV 512, FMODEL 0.

| row | FLANES | FSFU | MUL | LUT | FF | BRAM | DSP | ctrl sets | Fmax | note |
|---|---|---|---|---|---|---|---|---|---|---|
| G11 | 0 | 0 | 0 | 10,852 | 6,611 | 26.5 | 0 | 150 | 409.7 | no float tier at all |
| G7 | 1 | 0 | 8 | 13,019 | 11,394 | 30.5 | 34 | 156 | 380.5 | |
| G6 | 2 | 0 | 8 | 14,107 | 12,227 | 30.5 | 36 | 168 | 379.9 | |
| G5 | 4 | 0 | 8 | 16,133 | 13,895 | 30.5 | 40 | 181 | 334.7 | |
| **G1** | **8** | **0** | **8** | **19,949** | **17,276** | **30.5** | **48** | **202** | **383.3** | the reference |
| G4 | 8 | 1 | 8 | 20,410 | 17,630 | 32.0 | 49 | 208 | 334.7 | |
| G3 | 8 | 2 | 8 | 21,175 | 17,948 | 33.5 | 50 | 212 | 354.7 | quarter rate |
| G16 | 8 | 4 | 8 | 20,975 | 18,607 | 36.5 | 52 | 220 | 356.8 | half rate |
| G2 | 8 | 8 | 8 | 22,156 | 20,065 | 42.5 | 56 | 229 | 380.8 | full rate |
| G9 | 8 | 0 | 2 | 19,481 | 14,567 | 30.5 | 24 | 202 | 376.8 | |
| G8 | 8 | 0 | 4 | 19,665 | 15,471 | 30.5 | 32 | 202 | 362.8 | |
| G17 | 8 | 0 | 8 | 19,268 | 17,029 | 30.5 | 48 | 165 | 383.3 | `HAS_MASK`=`HAS_IPDOM`=0 |
| G18 | 8 | 0 | 8 | 18,725 | 17,258 | 30.5 | 48 | 202 | 367.4 | `HAS_SHFL`=0 |
| G19 | 8 | 0 | 8 | 18,001 | 16,942 | 24.5 | 48 | 186 | 380.2 | `HAS_LDSBANK`=0 |
| G20 | 8 | 0 | 8 | 16,118 | 16,698 | 24.5 | 48 | 154 | 389.1 | mask, ipdom, shfl, lds ALL 0 |
| **G12** | **4** | **1** | **2** | **16,268** | **11,591** | **32.0** | **17** | **186** | **361.7** | the prediction row |

G1 is the reference. The same configuration before this work
(`HAS_FSFU = 0`, `FLANES = LANES`) measured 20,086 LUT / 17,282 FF: the walk
machinery is **−137 LUT** at full rate, and `HAS_FSFU = 1` against the new
`FSFU_UNITS = 8` is 22,369 against 22,156, **−213 LUT**.

## 3. SIMD: measured rows

`rv_pe`, same part and flatten, 3.333 ns. SIMD 8, MULS 4, shift on, perm on,
WB_STAGE 0, NPART 16, RECV_MEM distributed, DOT_DSP 0, HAS_FALU 1, HAS_FACC 0,
HAS_FCVT 0, VREG_PRIM distributed.

**These rows are from a source snapshot taken BEFORE the accumulator's `.op` was
connected and before the pass-width truncation.** `HAS_FACC` is 0 on every one
of them, so the accumulator fix cannot touch them; the pass-width fix cannot
touch a row with `FSFU_UNITS` at 0 or at `FLOAT_LANES`, which is every row the
price list is fitted on. The three rows that DO depend on it — the fractional
seed counts — are re-measured on snapshot 2 in §5.

| row | SIMD | FLOAT_LANES | FSFU | LUT | FF | BRAM | DSP | ctrl sets | Fmax | note |
|---|---|---|---|---|---|---|---|---|---|---|
| S8 | 4 | 4 | 0 | 11,360 | 9,016 | 9 | 32 | 119 | 323.6 | |
| S6 | 8 | 2 | 0 | 13,214 | 7,989 | 13 | 52 | 113 | 338.8 | |
| **S2** | **8** | **4** | **0** | **15,450** | **9,828** | **13** | **56** | **123** | **320.2** | the reference |
| S3 | 8 | 4 | 1 | 16,770 | 10,230 | 14.5 | 57 | 138 | 316.6 | |
| S4 | 8 | 4 | 2 | 16,633 | 10,579 | 16.0 | 58 | 144 | 308.1 | |
| S1 | 8 | 4 | 4 | 16,510 | 11,259 | 19.0 | 60 | 136 | 363.0 | full rate |
| S5 | 8 | 8 | 0 | 19,747 | 13,499 | 13 | 64 | 144 | 323.9 | |
| S9 | 16 | 4 | 0 | 23,856 | 11,455 | 21 | 104 | 125 | 307.8 | |
| S10 | 8 | 4 | 0 | 14,362 | 9,751 | 13 | 56 | 118 | 344.4 | `HAS_SHIFT`=0 |
| S11 | 8 | 4 | 0 | 13,566 | 9,818 | 13 | 56 | 120 | 336.6 | `HAS_PERM`=0 |
| S13 | 8 | 0 | 0 | 10,309 | 5,578 | 13 | 48 | 89 | 363.9 | no float tier at all |
| **S14** | **8** | **8** | **2** | **21,538** | **14,260** | **16** | **66** | **165** | — | the prediction row |

---

## 4. The price list

### Exact on every row: DSP and BRAM

    SIMT   DSP  = 2*FLANES + 4*MUL_UNITS + 1*FSFU_UNITS
           BRAM = 26.5 + (FLANES>0 ? 4 : 0) + 1.5*FSFU_UNITS

    SIMD   DSP  = 6*SIMD + 2*FLOAT_LANES + 1*FSFU_UNITS
           BRAM = 5 + SIMD + 1.5*FSFU_UNITS

Checked with **zero error** against every row in this file that has `HAS_FACC = 0`
— both cores, both snapshots, including the two where `SIMD` itself moves (4 and
16), the two prediction rows, and the two builds that did not exist before. The
rotating accumulator adds DSP of its own and is not in these forms. The 4 BRAM
at `FLANES > 0` on SIMT is `kht_vregfile`'s third read port, which exists only
for `vfma`'s addend; it follows the FLOAT count and not the multiplier's.

The per-unit DSP and BRAM figures are **identical on both cores**, which they
must be: both instantiate the same `khs_float_lane`. A float unit is 2 DSP; the
seed capability adds 1 DSP and 1.5 BRAM (`vec_tables`) to a unit that has it.

### LUT, marginal

| feature | marginal LUT per unit | from |
|---|---|---|
| SIMT FP FMA unit | **980** | four points: 1→2 1,088, 2→4 1,013, 4→8 954 per unit |
| SIMT multiply unit | **80** | G1→G8 71/unit, G8→G9 92/unit |
| SIMT seed unit, full rate | **276** | G1→G2, 2,207 over 8 units, no walk built |
| SIMD FP FMA unit | **1,089** | fitted on S6 and S5, checked below |
| SIMD seed unit, full rate | **265** | S2→S1, 1,060 over 4 units, no walk built |
| SIMT retire slot (fixed, once) | **617** | G11 vs G1 minus the unit terms |
| SIMD float tier (fixed, once) | **727** | fitted with the unit term on S6 and S5 |

SIMD FF per FP FMA unit is **918** (S6→S2 919, S2→S5 918 — linear to one flop).

The SIMD **vector width** is not a unit count and is not in this table: `SIMD`
sets VW = 32·SIMD, so changing it moves the register file, the scratchpad, the
permute network, the reduction tree and the SWAR integer lanes together.
Measured whole at FLOAT_LANES 4 — 4 → 8 → 16 slots is 11,360 → 15,450 → 23,856:
**1,023 then 1,051 LUT per 32-bit slot**, and **6 DSP per slot** exactly on both
steps.

SIMT FF is as linear as the DSP: **452 FF per multiply unit** (the 12-deep flop
pad plus the pipeline registers, 12·32 = 384 of it) and **845 FF per FMA unit**.

### The SIMT model, and its error

    LUT = 10,852                        base PE, no float and no multiply
        + 617      if FLANES>0 or MUL_UNITS>0     the shared retire slot
        + 980 * FLANES
        +  80 * MUL_UNITS
        + seed(FSFU_UNITS)              see below
        + walk                          0 when every count equals LANES

| row | predicted | measured | error |
|---|---|---|---|
| G1  (8,0,8) | 19,949 | 19,949 | 0 (the fit point) |
| G5  (4,0,8) | 16,029 | 16,133 | **+104**, 0.6% |
| G6  (2,0,8) | 14,069 | 14,107 | **+38**, 0.3% |
| G8  (8,0,4) | 19,629 | 19,665 | **+36**, 0.2% |
| G9  (8,0,2) | 19,469 | 19,481 | **+12**, 0.06% |
| G7  (1,0,8) | 13,089 | 13,019 | **−70**, 0.5% |

Six rows off one fit point, every one inside 0.6%.

### The validation: predict a combination, then synthesise it

G12 is `FLANES = 4, FSFU_UNITS = 1, MUL_UNITS = 2` — three counts moved at once,
and none of them a row the model was fitted on.

    DSP   = 2*4 + 4*2 + 1*1                          = 17
    BRAM  = 26.5 + 4 + 1.5*1                         = 32.0
    LUT   = 10,852 + 617 + 980*4 + 80*2              = 15,549
          + seed(1) 461                              = 16,010
          + walk, FLANES 4 (+104) and MUL 2 (+12)    = 16,126

| | predicted | measured | error |
|---|---|---|---|
| DSP | 17 | **17** | **0** |
| BRAM | 32.0 | **32.0** | **0** |
| LUT | 16,126 | **16,268** | **+142, 0.87%** |

DSP and BRAM are exact. The LUT residual is the seed walk transferred from
`FLANES = 8`, where the FMA walk is one pass and here it is two — the one term
of the model that is not measured at this configuration.

### The SIMD model, its error, and its own validation

    LUT = 10,309                        base PE at SIMD 8, no float tier
        + 727      if FLOAT_LANES>0     the elementwise retire path
        + 1,089 * FLOAT_LANES
        +   265 * FSFU_UNITS
        + walk                          0 when FSFU_UNITS == FLOAT_LANES

Fitted on S6 (2 units) and S5 (8 units); the base is S13.

| row | predicted | measured | error |
|---|---|---|---|
| S2  (4 units, 0 seeds) | 15,392 | 15,450 | **+58**, 0.4% |
| S1  (4 units, 4 seeds, full rate) | 16,452 | 16,510 | **+58**, 0.35% |
| **S14 (8 units, 2 seeds)** | 20,278 + walk | **21,538** | walk = **1,260** |

S14 is the SIMD prediction row, `FLOAT_LANES = 8, FSFU_UNITS = 2` — a fractional
rate at a lane count the seed term was not measured at.

| | predicted | measured | error |
|---|---|---|---|
| DSP | 6·8 + 2·8 + 1·2 = 66 | **66** | **0** |
| BRAM | 5 + 8 + 1.5·2 = 16 | **16** | **0** |
| LUT, ignoring the walk | 20,278 | 21,538 | +1,260, 6.2% |
| LUT, walk taken from FLOAT_LANES 4 (653) | 20,931 | 21,538 | +607, 2.9% |

DSP and BRAM exact again. **The SIMD LUT walk term does not transfer across
`FLOAT_LANES`** — it is 653 at four units and 1,260 at eight, for the same two
seed units. That non-transferability is itself the evidence for §5: the walk's
cost lives in a placement whose width is set by the MISMATCH between the two
pass counts, not by either count alone.

Re-synthesised after the §5 fix, the same row is **21,230** against its own
reference's 20,069 — the walk term falls from 1,260 to **587**, a 630 LUT
saving, and DSP (66) and BRAM (16) stay exact.

### Gates that are not counts, measured at the same reference

| gate | LUT | BRAM | ctrl sets |
|---|---|---|---|
| `HAS_MASK` + `HAS_IPDOM` (the mask array and the IPDOM stack) | **681** | 0 | −37 |
| `HAS_SHFL` (the subgroup butterfly) | **1,224** | 0 | 0 |
| `HAS_LDSBANK` (the banked LDS and its address resolver) | **1,948** | 6 | −16 |

The three sum to **3,853 LUT, 19% of the reference PE**. Measured TOGETHER — G20
turns all four generics off at once — the PE is 16,118, which is **3,831 below
the reference: within 22 LUT, 0.6%, of the sum.** That is the inference table's
own premise tested directly, on the gates rather than on the counts.

### The seed term is NOT monotonic in the unit count

    FSFU_UNITS      0        1        2        4        8
    delta LUT       0     +461   +1,226   +1,026   +2,207
    delta BRAM      0     +1.5     +3.0     +6.0    +12.0
    delta DSP       0       +1       +2       +4       +8

BRAM and DSP are exactly linear. **LUT is not, and four of 8 units is cheaper
than two.** Full rate (8 of 8) builds **no walk at all** — `SEED_U == FLANES`, so
both index arms are the same expression and the placement mux folds — and is
therefore pure seed hardware at 276 LUT a unit. Splitting each fractional row
into seed hardware and residual:

    units   seed hardware   residual = walk
      1      276             185
      2      552             674
      4    1,104             -78
      8    2,207               0

The residual is the sum of two terms that move in OPPOSITE directions with the
unit count: the placement mux (thread *i*'s seed source is unit *i mod SEED_U*,
so at 1 unit there is one source and no mux, and it grows with SEED_U) and the
pass decode (`LANES/SEED_U` values, so it shrinks with SEED_U). It is smallest
at both ends and worst in the middle.

What that buys, against full rate:

| rate | LUT saved | BRAM saved | DSP saved |
|---|---|---|---|
| half (4 of 8) | 1,181, **53%** | 6, 50% | 4, 50% |
| quarter (2 of 8) | 981, **44%** | 9, 75% | 6, 75% |
| eighth (1 of 8) | 1,746, **79%** | 10.5, 88% | 7, 88% |

Quarter rate — the ratio every desktop GPU provisions transcendentals at — saves
three quarters of the BRAM and DSP and only 44% of the LUT. **One unit of eight
saves the most LUT of any configuration**, because the walk's placement mux
disappears entirely when every thread's seed source is the same unit.

---

## 5. On SIMD a fractional-rate seed was a LUT LOSS at every count, and is not now

**As first measured (snapshot 1):**

    FSFU_UNITS      0        1        2        4 (= FLOAT_LANES, full rate)
    LUT        15,450   16,770   16,633   16,510
    delta            0   +1,320   +1,183   +1,060

Monotonically DECREASING: full rate was both the widest and the cheapest in LUT,
which is the opposite of the SIMT result. The seed hardware itself is 265 LUT a
unit (SIMT measures 276 — the same lane, so they agree), so the residual walk was
1,055 / 653 / 0 and it exceeded what the missing units saved. Diagnosed to two
things in the placement:

1. `khs_falu`'s element index and `khs_unit`'s staging-register placement were
   built at the SEED walk's pass width, which is the LONGER of the two. An FMA
   lane then indexed a 16-way element select where four are reachable. **Fixed:**
   each walk now indexes at its own width (`PSW_F` in `khs_falu`, `EPSW_F` in
   `khs_unit`, `PSW_A` in `kht_fpu` and `kht_unit`).
2. SIMD writes through a `stage_r` staging register with a runtime placement
   index, where SIMT writes straight into the register file with a per-lane
   write enable and a constant source. Giving `khs_vregfile` a per-element write
   enable would let SIMD use the SIMT structure; that is NOT done here.

**After fix (1), re-synthesised on snapshot 2 against its own reference:**

    FSFU_UNITS      0        1        2        4 (full rate)
    LUT        15,741   16,572   16,962   16,888
    delta            0     +831   +1,221   +1,147
    BRAM            13     14.5     16.0     19.0
    DSP             56       57       58       60

    units    seed hardware (287)    residual = walk
      1        287                  544
      2        574                  647
      4      1,147                    0

**Quarter rate now PAYS on SIMD: one seed unit of four costs 831 LUT against
full rate's 1,147 — 316 LUT cheaper, and 4.5 BRAM and 3 DSP cheaper as well.**
The walk at one unit is 544 against the 861 of seed hardware it removes, where
before the fix it was 1,055 against 795 and lost. Two units is still dearer than
full rate, which is the same non-monotonic shape §4 measures on SIMT: the middle
is the worst place to sit.

---

## 5a. The widths that were considered and NOT built, with the arithmetic

A width is worth having when the per-unit cost times the units saved exceeds the
walk's own cost. The float tiers clear that on both cores; the SIMD seed tier
clears it after the §5 fix; the **permute** cleared it and is therefore BUILT,
not priced — §5d. These three do not clear it and are left as they are.

| feature | measured whole | per unit | a walk would cost | verdict |
|---|---|---|---|---|
| SIMD packed shifter (`HAS_SHIFT`) | **1,088 LUT** at SIMD 8 | 136 / 32-bit slot | an operand mux per unit is a SIMD:1 32-bit select, ~120 LUT at SIMD 8, plus placement | **cannot pay**: the mux is as expensive as the shifter it replaces. The permute could only pay because its 1,884 LUT is nearly twice this over the same eight slots |
| SIMD multipliers (`MULS`) | `MULS` 2 measured +46 LUT, and 6 DSP a slot | DSP, not LUT | a unit count would trade LUT for DSP | **wrong direction**: this PE is LUT-bound and the multipliers are already in DSP48 |
| SIMD integer ALU width | `SIMD` sets VW, so it is the register width | — | separating them is exactly what `FLOAT_LANES` does for the float side | possible, deepest change: every integer instruction becomes multi-pass on a single-cycle critical path |

**The SIMD packed shifter's refusal above is WRONG and §15 retracts it.** It
charged one operand mux per shifter REMOVED, where a walk pays one mux per unit
KEPT: at one unit that is a single mux against seven shifters, not one against
one. Built and measured, the width saves more than deleting the shifter does.

### Two SIMD features still have no gate at all — named, not priced

Both were found by reading `khs_unit` and `khs_lane` against the parameter list,
and neither is in any row above. They are gaps, not refusals, and saying which
is the point of this section:

- **`khs_reduce`** — the horizontal sum tree AND the signed-max tree over the
  32-bit lanes, both built unconditionally for `vredsum` / `vredmax`. There is no
  `HAS_RED`, so a shader that never reduces still pays for two `log2(SIMD)`-deep
  trees plus the `PIPE` registers.
- **`khs_lane`'s second `khs_padd32`** (`u_rnd`) — a whole extra SWAR adder per
  lane, existing only for `vsrari`'s round bit. Its own header prices the DELAY
  argument for keeping it off the main adder, and nothing prices the AREA. It
  rides on `HAS_SHIFT` and cannot be dropped without dropping the shifter.

Neither was built this round: §11's format gate was the larger number and the
budget went there. Both are one generate each.

## 5d. The permute width, built and measured

`khs_perm` is rewritten PER OUTPUT WORD — unit *u* on pass *p* produces output
word *p·UNITS + u* — and `khs_unit` walks SIMD/UNITS passes into a staging
register with one drain step. Same rv_pe configuration as §3, `PERM_UNITS` the
only knob:

| PERM_UNITS | LUT | vs the shipped full-width permute (15,741) | passes per `vsldw` |
|---|---|---|---|
| 8, per-word body | 16,217 | +476 | 1 |
| 4 | 15,955 | +214 | 2 |
| **2** | **14,906** | **−835** | 4 |
| **1** | **14,576** | **−1,165** | 8 |
| **8 and 0, dual-path body** | **15,741** | **0 — identical** | 1 |

**Two units saves 835 LUT and one unit 1,165 — 5.3% and 7.4% of the whole PE.**
The curve is strongly non-linear: 8→4 is only −262 and 4→2 is −1,049, so the
width is worth having only at the narrow end. It is NOT in the linear price list
of §4 for that reason; use the curve.

**The default width must cost nothing, so it is a separate branch.** The
per-word body measured 476 LUT ABOVE the original full-width form at
`UNITS = SIMD` — the tool shares the pack and unpack wiring across lanes, and
the per-word form asks it to build each output separately. `khs_perm` therefore
keeps the original code in a `UNITS == SIMD` branch and uses the walk only when
a narrower width is asked for. Re-measured at both `PERM_UNITS = 8` and at
`PERM_UNITS = 0`, which is what every caller passes: **15,741 LUT, 14,868 logic
LUT, 9,845 FF, 13 BRAM, 56 DSP, 130 control sets, 340.8 MHz — every column
identical to the row from before the parameter existed.** The knob is free until
it is used.

Verified on `khs_unit`'s own generated stream, which exercises every slide
index: PASS at `PERM_UNITS` 0, 8, 4, 2, 1 at SIMD 8 and at SIMD 4, and PASS with
the float walk running at the same time. Same stream, same golden vectors —
only the cycles change.

## 5b. The whole change set, re-measured from a second frozen snapshot

Snapshot 2 carries everything: the pass-width fix, the `HAS_FLT` removal, the
two lane-index faults, the `g_simd.u_khs` rename. `vec_alu` is byte-identical
between the two snapshots except for comment text, so nobody else's edit is in
these deltas.

| configuration | snap 1 | snap 2 | delta |
|---|---|---|---|
| **SIMT reference — `FLANES` 8, `FSFU_UNITS` 0, `MUL_UNITS` 8** | 19,949 | **19,461** | **−488** |
| SIMT `FLANES` 8, `FSFU_UNITS` 1 | 20,410 | 20,418 | +8 |
| SIMT `FLANES` 8, `FSFU_UNITS` 2 | 21,175 | 20,841 | −334 |
| SIMT `FLANES` 4, `FSFU_UNITS` 0 | 16,133 | 16,307 | +174 |
| SIMD `FLOAT_LANES` 8, `FSFU_UNITS` 0 | 19,747 | 20,069 | +322 |
| SIMD reference — `FLOAT_LANES` 4, `FSFU_UNITS` 0 | 15,450 | 15,741 | +291 |
| SIMD `FLOAT_LANES` 4, `FSFU_UNITS` 1 | 16,770 | 16,572 | **−198** |
| SIMD `FLOAT_LANES` 4, `FSFU_UNITS` 2 | 16,633 | 16,962 | +329 |
| SIMD `FLOAT_LANES` 4, `FSFU_UNITS` 4 | 16,510 | 16,888 | +378 |
| SIMD `FLOAT_LANES` 8, `FSFU_UNITS` 2 | 21,538 | 21,230 | **−308** |
| SIMD `FLOAT_LANES` 4, `HAS_FACC` 1 | 19,624 | 19,677 | +53 |
| SIMD `FLOAT_LANES` 4, `HAS_PERM` 0 | 13,566 | 13,801 | +235 |

**−488 LUT at the shipped SIMT configuration.** Against the figure this work
started from — the SIMT ladder's `HAS_FSFU = 0` row at 20,086 LUT — the same
configuration now synthesises at **19,461: −625 LUT, −3.1%**, with the pass walk
built and every count independent.

On SIMD the equivalent shrink is not in the default configuration but in a knob
that did not exist: a PE that keeps the permute can now have it at `PERM_UNITS`
2 or 1 for **−835 or −1,165 LUT** (§5d), at four or eight passes per `vsldw`.

The other rows move both ways by up to 378 LUT — 2% and less — which is what
restructured but logically identical generate blocks do to `rebuilt` flattening.
The two clearly attributable movements are −334 at SIMT quarter rate and −198 at
SIMD eighth rate, which is where the pass-width fix was aimed.

**On SIMD the fix takes 489 LUT off the one-unit seed walk and flips its
verdict** — see §5. Against each snapshot's OWN reference, one seed unit of four:

    snap 1   16,770 - 15,450 = +1,320    against full rate's +1,060  -- a LOSS
    snap 2   16,572 - 15,741 =   +831    against full rate's +1,147  -- a WIN

and at eight float lanes with two seed units the same fix is worth **630 LUT**
(1,791 → 1,161 over each snapshot's own reference).

**The price list in §4 is fitted entirely on snapshot 1** — every row from one
frozen tree, which is what makes the per-unit figures differences of one knob.
The SIMD seed column in §5 carries both snapshots, labelled, because that is the
before/after of a change and not a mixed fit.

## 5c. SIMD against SIMT at the same float width

The two PEs are within 1% of each other at 8 threads / 8 slots and 8 FP FMA
units — SIMT 19,949, SIMD 19,747 — and the price list says why that is not a
redundancy.

| block | SIMT | SIMD |
|---|---|---|
| PE with no float tier | **10,852** | **10,309** |
| FP FMA unit | 980 | **1,089** |
| float tier fixed cost | 617 | 727 |
| divergence: mask + IPDOM stack | **681** | — |
| divergence: subgroup butterfly | **1,224** | — |
| divergence: banked LDS + resolver | **1,948** | — |
| packed shifter | — (RV32I's shifter is inside the thread's own ALU) | **1,088** |
| cross-lane permute (slide, pack, unpack) | — | **1,884** |
| rotating float accumulator | — | `HAS_FACC` = **+3,936 LUT, +8 DSP, +4,079 FF** at FLOAT_LANES 4 (D11 19,677 against D5's 15,741), with `.op` connected. Before the fix it measured +4,174 — so repairing it from a pass-through into a real FMA cost **nothing**; it is 238 LUT cheaper. |
| integer multiply | 80 LUT + 4 DSP a unit | 6 DSP a slot, LUT-neutral |

Three things follow, and they are the answer to "SIMD should be way cheaper":

1. **SIMD's base PE is already 543 LUT CHEAPER than SIMT's** (10,309 against
   10,852) even though SIMD's base carries the shifter, the permute network and
   32 multipliers, and SIMT's carries no multiplier at all. So the divergence
   hardware is real and SIMD does not pay for it.
2. **But once SIMT's optional blocks come off, SIMT is the cheaper machine.**
   G20 measures SIMT with mask, IPDOM, shuffle and the banked LDS all off, at 8
   FMA and 8 multiply units: **16,118**. The comparable SIMD figure is S5's
   19,747 less the shifter (1,088) and the permute (1,884) = **16,775**, which
   is **657 LUT, 4.1%, dearer**.
3. **SIMD's float unit costs 109 LUT MORE than SIMT's — 11% — for the same
   `khs_float_lane`.** At 8 units that is 872 LUT, which is most of the gap.
   The difference is the PACKED ELEMENT INDEX: a SIMD unit selects its 16-bit
   element out of 2·SIMD slots, where a SIMT unit's element is one whole 32-bit
   slot and its index is a constant. That is the price of 2x FP16 density and it
   is intrinsic, not a defect.

So parity is not a redundancy to remove. SIMD is not cheaper because it is not a
subset of SIMT: it carries packed int8/int16/int32 SWAR lanes, a cross-lane
permute network, a vector scratchpad and (optionally) rotating float
accumulators, and its float unit is dearer per unit. The knobs that make SIMD
cheaper are its own — `HAS_PERM`, `HAS_SHIFT`, `HAS_FACC` and the element-type
set — and every one of them is priced above. **"SIMD must always be way cheaper
at the same features and the same unit counts" is not achievable by removing
redundancy; it is a decision about which SIMD features to drop.**

## 6. What is broken, and stays listed as broken

| item | state |
|---|---|
| `SIMD_FCVT` | **decode with no datapath.** `is_fcvt` set `wr_vreg` and the `vres` result mux had no branch for it, so `vfcvt` wrote the INTEGER lane's output. There is no converter instantiated anywhere in `khs_unit`. The default is now 0, at which `bad_fgrp` faults `vfcvt`; a nonzero value is refused at ELABORATION (`khs_unit_FCVT_decodes_but_has_no_datapath`) so it cannot be switched on by mistake. |
| `SIMD_FACC` | **was a pass-through, now fixed.** `khs_unit` left `.op` UNCONNECTED on all four `khs_float_lane` instances: `z` in simulation (every accumulated element read back X) and 0 = `OP_MOV` in synthesis, which `vec_alu` routes as `a*1.0 + 0`. Connected to `KHS_FOP_FMA`. `khs_unit`'s own float stream went from **13 errors to 1**, and the repair cost NO area — the tier measures 3,936 LUT working against 4,174 broken. |
| SIMT `FLANES < LANES` | **was zero-filled, now works.** `kht_fpu`'s `g_nolane` returned 32'd0 for every lane above `FLANES` — "a plausible float answer". Replaced by the pass walk; verified below. |
| SIMD float stream, `vfmul.f16` | **one element, 1 ULP** (0x32ef against the model's 0x32ee), case9 instruction 18. Present with every change of this work reverted, so it is pre-existing in the working tree and is not attributed here. **Re-checked on snapshot 3 with the format gates in: still 96 checks, still exactly this 1 error** — so the accumulator path is undisturbed by them, and the defect is still open. |
| `vec_alu.v` | **did not compile at all** when this work started: an in-flight edit added forward declarations for ten polynomial-path signals without the `g_poly` generate that would drive them, so all ten were declared twice and xvlog refused the module — every bench and every OOC run that touches `vec_alu` died on it. The duplicate block is removed and the file compiles; restore it WITH the generate. |
| SIMT seeds | **the arithmetic is verified, the SIMT walk is not.** `khs_float_lane` and `vec_alu` are the same modules on both PEs and `khs_run.py --float` exercises `vfexp2`/`vflog2`/`vfrcp`/`vfrsqrt` and passes at `SEED_UNITS` 16, 8, 4, 2 and 1 — so the seed datapath and the seed WALK are tested, on SIMD. What is untested is the SIMT side's own operand routing and placement for a seed, because `rv_simt_model.py` faults on funct7[2] so no SIMT shader issues one, and the finite seed path in `rv_simd_model.fsfu_e8` is a float64 reference rather than bit-exact — a SIMT bench that compares an exact DRAM checksum cannot grade one. Fixing that needs a tolerance-comparing SIMT bench, which does not exist. **Unchanged on snapshot 3: still open.** |

---

## 7. The ISA knows no count — as a test, not a claim

The same shader image and the same golden DRAM at every unit count. Only the
generic changes.

| case | FLANES | MUL | result |
|---|---|---|---|
| `simt_mul` x16 | **0** | 8 | PASS — RV32M with NO float tier |
| `simt_fwalk` x16 | 8 | **0** | PASS — float with NO multiplier |
| `simt_fwalk` x1 | 8 | 8 | PASS |
| `simt_fwalk` x1 | 4 | 8 | PASS |
| `simt_fwalk` x1 | 2 | 8 | PASS |
| `simt_fwalk` x1 | 1 | 8 | PASS |
| `simt_fwalk` x16 | 2 | 8 | PASS |
| `simt_float` x16 | 2 | 8 | PASS |
| `simt_f32` x16 | 4 | 8 | PASS |
| `simt_mul` x1 | 8 | 2 | PASS |
| `simt_mul` x16 | 8 | 4 | PASS |
| `simt_mul` x16 | 8 | 1 | PASS |

`tests/pe/prog/simt_fwalk.s` exists because **every other float shader in the
suite passes a build with the placement crossed**: in `simt_float.s` and
`simt_f32.s` every float operand is uniform across the lanes, and the one
per-lane value is built by the INTEGER lanes after the float has retired. In
`simt_fwalk.s` lane *i*'s operand is 2^i in both formats, so a unit serving the
wrong thread is a wrong word; and a masked float across a multi-pass instruction
checks that an inactive lane keeps its previous value on every pass.

Cycle counts, `simt_fwalk` x1: 1,549 (8 units) → 1,565 (4) → 1,581 (2) → 1,613
(1). Eight float instructions, so the rise is the extra launches.

The first two rows are configurations that **did not exist before this work**:
`is_imul` read `ictl[C_IMUL] && (HAS_FLT != 0)`, so a multiply-without-float
build could not be expressed and a float-without-multiply build faulted every
`mul` it never issued. Both are synthesised, on snapshot 2 against its own
19,461 reference:

| build | LUT | FF | BRAM | DSP | Fmax |
|---|---|---|---|---|---|
| `FLANES` 0, `MUL_UNITS` 8 — multiply, no float | **11,769** | 10,270 | 26.5 | 32 | 408.8 |
| `FLANES` 8, `MUL_UNITS` 0 — float, no multiply | **18,963** | 13,650 | 30.5 | 16 | 387.1 |

The price list predicts both DSP figures exactly (4·8 = 32 and 2·8 = 16) and
both BRAM figures exactly, at configurations outside its fit range.

SIMD, `khs_unit`'s own generated stream, elementwise float only (`--no-facc`):

| FLOAT_LANES | FSFU_UNITS | result |
|---|---|---|
| 16 | 16, 8, 4, 2, 1 | PASS, all five |
| 8 | 2 | PASS |
| 4 | 4, 1 | PASS |
| 2 | 1 | PASS |
| 1 | 1 | PASS |

An elementwise seed's result depends on its own element and nothing else, so the
SAME vectors must pass at every count — which is what makes this the walk's test
rather than a rebuild. (`--flanes` IS architectural for the ACCUMULATOR, where
the partial chain length changes the answer; it is not for the elementwise tier.)

### The suites, whole

| suite | result |
|---|---|
| `rv_simt_suite.py`, 41 cases: the 12 above, the single-format row, four shuffle widths and three bank counts | **41 / 41 PASS**, on the shipped tree at snapshot 3 |
| `khs_run.py` integer stream, SIMD 8 and SIMD 4 | **PASS**, 36 checks each |
| `khs_run.py --float --no-facc`, the nine rows above | **PASS**, 60 checks each |
| `khs_run.py --float` with the accumulator | 96 checks, **1 error** — the pre-existing 1 ULP in §6, down from 13 |
| `rv_simd_run.py`, the assembled SIMD PE and its kernels | **PASS** |

---

## 8. Two lane-index fields aliased instead of faulting

Both are the "ISA knows the width" class, found by audit:

- **SIMD `vextr`**: `m_lane` is `r2f[LAW-1:0]`, and `rv_simd_model` defined the
  lane as `sh % simd`. So `vextr x, v, 5` read element 5 on an 8-lane build and
  element 1 on a 4-lane one — one encoding, two meanings. `khs_unit` now faults
  on `r2f >= SIMD`; the model indexes directly and the generator clamps.
- **SIMT `bcast`**: `x_sh_idx` is `rs2[LNW-1:0]`, same shape. `kht_core`'s
  `unbuilt` now faults on `rs2 >= LANES`. `shflxor` is NOT on the list: its
  operand is an xor MASK, and folding is its definition.

`vsldw`'s 3-bit slide index is a RANGE limit, not a dependency: the operation is
"rotate {v2,v1} left by idx words" at every width, and only the reachable subset
shrinks as SIMD grows. An upper bound is allowed; a changing meaning is not.

`rv_simd_isa.encode` now refuses `vextr` with a lane at or above the build's
`SIMD`, and `khs_gen` tells it the build's width — so the assembler, the model
and the RTL all say the same thing about one encoding.

---

## 9. Every file that changed, and why

| file | change |
|---|---|
| `simd/khs_float_lane.v` | forwards `HAS_POLY` to `vec_alu`. The port was MISSING, so every float unit in both PEs built the coefficient ROM and DSP-P whether or not the build could issue a seed. This is what makes a per-unit seed count possible at all. |
| `simt/kht_fpu.v` | rebuilt: `FSFU_UNITS` seed units among `FLANES` FMA units, one element index per unit, `pass` in, one slot per UNIT out. `g_nolane`'s zero-fill is gone. Elaboration refuses a count that does not divide `LANES`. |
| `simt/kht_imul.v` | `MUNITS` and `pass`; one slot per unit out. |
| `simt/kht_unit.v` | the launch/pass sequencer, `fpass_hold`, `hold_all`, the per-pass write mask, the constant-source placement, the shadow's `seed`/`last`/`pass`, `fpend` cleared on the LAST pass. `HAS_FLT` deleted. |
| `simt/kht_core.v` | `FLANES`/`FSFU_UNITS`/`MUL_UNITS` as independent counts; `fpass_hold` into `hold`; `bad_lane` on `bcast`. |
| `simt/kht_pe.v` | the three counts. |
| `simd/khs_falu.v` | `SEED_UNITS`, per-unit `HAS_POLY`, the seed walk's own index, and the FMA walk indexed at ITS width. |
| `simd/khs_perm.v` | rewritten PER OUTPUT WORD instead of as seven full-width results and a mux, so `UNITS` selects how many words a pass produces. One saturation per output byte rather than per source element. |
| `simd/khs_unit.v` | `FSFU_UNITS` as a count, the seed walk's `last_pass` and placement, `sh_sfu` in the shadow, **`.op(KHS_FOP_FMA)` on the accumulator's four lanes**, `HAS_FCVT` refused, `bad_lane` on `vextr`, `PERM_UNITS` with its walk, staging register and drain step, and `FLOAT_LANES = 0` re-spelled as "not built" with `FL_ON` gating the three float groups. |
| `core/rv_core.v`, `rv_pe.v` | `SIMD_FSFU` is a count, `SIMD_PERM_UNITS` added; `g_dsp.u_khd` renamed `g_simd.u_khs`. |
| `core/rv_id.v` | `g_dsp` renamed `g_simd`. |
| `vector/vec_alu.v` | the duplicated forward declarations removed. The file did not compile: ten signals were declared twice and there is no `g_poly` generate to drive them, so every bench and OOC run touching `vec_alu` died. Restore the block WITH the generate. |
| `tests/pe/prog/simt_fwalk.s` | new: per-lane distinct float operands and a masked multi-pass float. |
| `tests/pe/tools/rv_simd_model.py` | `fsfu_e8` no longer dies on `OverflowError` for a large `exp2` argument (it killed the whole float stream in the GENERATOR, so `khs_run.py --float` could not run); `vextr` no longer wraps its lane index; `flanes = 0` is "not built" rather than `2*simd`. |
| `tests/pe/tools/khs_gen.py`, `khs_run.py`, `rv_simd_isa.py` | the `vextr` lane clamp and check; `--fsfu`, `--no-falu`, `--no-facc`, `--perm-units` forwarded; `--flanes` defaults to `2*SIMD` and `--float --flanes 0` is refused by name. |
| `tests/pe/tools/rv_simt_suite.py`, `tb/kht_sys_tb.v`, `tb/kht_mesh.v`, `tb/het_mesh.v`, `tb/khs_unit_tb.v` | the counts as defines, and the cases that run one image at every width. |
| `simt/kht_unit.v` | `SHFL_UNITS`: the shuffle as a width. The butterfly stays in its own `SU == LANES` branch (byte-identical), the narrow form is a per-output-lane select, and the walk shares `fpass_hold` with the float tier rather than adding a second hold. |
| `simt/kht_lds.v` | `BANKS`: the bank count as a width. The sequencer was already a drain, so only the bank field, the array and the resolver moved. `NBS` (bank bits used) is now separate from `NBW` (storage width) -- forced to 1 at one bank, `selv[1]` was read on a one-entry array and the resolver spun. |
| `simd/khs_unit.v`, `khs_lane.v` | `SHIFT_UNITS`: at a narrow width the shifter LEAVES the lane (`SH_IN_LANE`) and khs_unit walks `SHU` standalone shift+round units into a staging register, with its own arm on `vres` that is constant-false at full width. |
| `simd/khs_float_lane.v`, `khs_falu.v`, `simt/kht_fpu.v` | `HAS_F16`/`HAS_F32`: the converters and the format select are a generate, not constant propagation, and the single-format branches take the one form the build can reach. |
| `simd/khs_unit.v`, `simt/kht_core.v` | the format fault (`bad_fet`, `unbuilt`), and `m_is_f32` as a WIRE when the format is fixed -- a flop whose D is constant is not reliably folded away. |
| `core/rv_core.v`, `rv_pe.v`, `simt/kht_unit.v`, `kht_pe.v` | the two format gates plumbed to the top. |
| `scripts/tcl/ooc_simd_pe.tcl`, `ooc_simt_pe.tcl` | `nacc`, `vregs`, `hf16`, `hf32` as tclargs, APPENDED so no existing caller's positions move. |
| `scripts/tcl/ooc_simt_pe.tcl`, `ooc_simd_pe.tcl`, `ooc_khs.tcl` | the counts as generics; tags carry `sfu<N>u` and the SIMD PE's tag prefix is `simdpe`, so a row from before the boolean-to-count change can never be read as one from after. |

---

## 10. Snapshot 3: the counts and gates that had no price

Six features were settable-but-unpriced or not settable at all. Every row below
is one knob against its own core's reference, same part, same 3.333 ns, same
`rebuilt`, on one frozen tree.

**Both references first, because a snapshot is not a snapshot until it
reproduces:** SIMD **15,741** LUT / 14,868 logic / 9,845 FF / 13 BRAM / 56 DSP /
130 control sets, and SIMT **19,461** / 18,483 / 17,268 / 30.5 / 48 / 202 —
every column identical to snapshot 2. The rows below are therefore diffable
against §4 and §5b as well as against each other.

### SIMD, against 15,741

| row | knob | LUT | delta | FF | BRAM | DSP | ctrl |
|---|---|---|---|---|---|---|---|
| D0 | the reference | 15,741 | — | 9,845 | 13 | 56 | 130 |
| D-fmt11 | `HAS_F16`=1 `HAS_F32`=1, written out | **15,741** | **0** | 9,845 | 13 | 56 | 130 |
| D-nacc1 | `NACC` 2 → 1 | 15,327 | **−414** | 9,566 | 13 | 56 | 128 |
| D-vregs4 | `VREGS` 8 → 4 | 15,604 | **−137** | 9,842 | 13 | 56 | 129 |
| D-vprim | `VREG_PRIM` distributed → block | 15,660 | **−81** | 9,061 | **25** | 56 | 128 |
| D-perm1 | `PERM_UNITS` 0 → 1 | 14,576 | **−1,165** | 10,079 | 13 | 56 | 128 |
| D-f16 | `HAS_F32` = 0 | **13,955** | **−1,786** | 9,340 | 13 | 56 | 126 |
| D-f32 | `HAS_F16` = 0 | **13,671** | **−2,070** | 9,768 | 13 | 56 | 113 |
| **D-pred** | all three of `PERM_UNITS` 1, `NACC` 1, `HAS_F32` 0 | **12,524** | −3,217 | 9,319 | 13 | 56 | 131 |
| D-area | `-directive AreaOptimized_high` — no RTL change at all | **14,440** | **−1,301** | 9,845 | 13 | 56 | 128 |
| **D-area-perm1** | `AreaOptimized_high` + `PERM_UNITS` 1 | **13,273** | −2,468 | 10,074 | 13 | 56 | 134 |
| D-en0 | `SIMD_EN` = 0, the controller PE alone | **2,661** | −13,080 | 4,140 | 5 | 0 | 79 |

### SIMT, against 19,461

| row | knob | LUT | delta | FF | BRAM | DSP | ctrl |
|---|---|---|---|---|---|---|---|
| G0 | the reference | 19,461 | — | 17,268 | 30.5 | 48 | 202 |
| G-fmt11 | `HAS_F16`=1 `HAS_F32`=1, written out | **19,461** | **0** | 17,268 | 30.5 | 48 | 202 |
| G-w8 | `WAVES` 16 → 8 | 18,802 | **−659** | 16,756 | 30.5 | 48 | **175** |
| G-ip4 | `IPDOM_D` 8 → 4 | 19,453 | **−8** | 17,247 | 30.5 | 48 | 202 |
| G-f16 | `HAS_F32` = 0 | 17,968 | **−1,493** | 16,153 | 30.5 | 48 | 201 |
| G-f32 | `HAS_F16` = 0 | 17,479 | **−1,982** | 17,250 | 30.5 | 48 | 192 |
| **G-pred** | all three of `WAVES` 8, `IPDOM_D` 4, `HAS_F16` 0 | **16,552** | −2,909 | 16,724 | 30.5 | 48 | 157 |

### What each one is worth, and the three that are not levers

| feature | price | what it buys back |
|---|---|---|
| SIMT wave slot | **82.4 LUT + 64 FF each**, and 3.4 control sets | occupancy: the whole reason a 15-deep float unit does not stall |
| SIMD accumulator bank | **414 LUT + 279 FF each** | a second `vdot` chain in flight |
| SIMD permute at one unit | −1,165 LUT for **+234 FF** and 8 passes a `vsldw` | §5d; the staging register is the FF |
| SIMD vector register | **−137 LUT for HALF the file** | nothing — see below |
| SIMT IPDOM depth | **−8 LUT for half the stack** | nothing — see below |
| SIMD vector file in BRAM | **−81 LUT for +12 BRAM** | nothing — see below |

**`VREGS` is not a LUT lever, and the file's own header predicted it.** A
distributed-RAM primitive is 32 deep, so eight entries and four entries are the
same LUTs; halving the file returns 137 LUT of address decode and no storage.
Read the other way, it PREDICTS that the file could grow to 32 entries for
little more than it costs at 8 — a 32-register SIMD ISA for nearly nothing.
**That direction is a prediction and not a row: `VREGS` 16 and 32 are not
synthesised here**, and the encoding would have to widen with them.

**`IPDOM_D` is not a LUT lever either.** Halving the divergence stack is −8 LUT,
−21 FF and −8 LUTRAM. The stack is storage, and it is small storage.

**`-directive AreaOptimized_high` is RETRACTED as a shrink result.** It is
−1,301 LUT on byte-identical source, and it takes the PE to **260.8 MHz OOC
(slack −0.502)** — it does not meet the timing target, and an OOC number is the
OPTIMISTIC end: placement and routing lose slack, they never gain it. A row that
misses timing is not a smaller PE, it is a PE that does not build. The two
directive rows stay in the table as a measurement of what the tool will trade,
and **nothing in §14 is claimed on them.** Every other row in this file is
`-directive default`.

**`VREG_PRIM` = block is a priced refusal: 12 BRAM for 81 LUT.** The saving is
not what the LUTRAM count suggests — distributed RAM falls 812 → 368 (−444) but
LOGIC LUTs rise 14,868 → 15,231 (+363), because a depth-8 block RAM buys its
storage back in address and enable logic. **6.75 LUT a BRAM** is not a trade
this PE should make.

## 11. What carrying two memory formats costs, on both cores

`khs_float_lane` takes FP32 or FP16 on one port and computes in E8M15 either way.
Its header called that "the contract, not an option" and no number stood behind
it. Both cores now gate it, and the number is large:

| build | SIMD (`FLOAT_LANES` 4) | SIMT (`FLANES` 8) |
|---|---|---|
| both formats | 15,741 | 19,461 |
| FP16 only | 13,955 | 17,968 |
| FP32 only | 13,671 | 17,479 |
| **adding FP32 to an FP16 machine** | **1,786 = 446 / unit** | **1,493 = 187 / unit** |
| **adding FP16 to an FP32 machine** | **2,070 = 518 / unit** | **1,982 = 248 / unit** |

DSP and BRAM do not move on any of the four: the formats are converters and
selects, never arithmetic. Both cores agree that **FP16 is the dearer format to
add** — 61 LUT a unit dearer on SIMT, 72 on SIMD — which is `vec_cvt`'s own
accounting showing through: `e8_to_f16` carries a 48-bit subnormal shifter at
161 LUT and `e8_to_f32` is wiring.

**A SIMD unit pays 2.1–2.4x what a SIMT unit pays for the same second format,
out of the same module.** That is §5c's packed element index measured a second
time and from a different direction. On SIMT a thread is a whole 32-bit slot in
either format, so `e_idx` does not move and only the converters and two muxes
go. On SIMD the format IS the element granularity: `a_el`, `b_el` and `d_el` are
each a 16-way 16-bit select AND an 8-way 32-bit select with a runtime choice
between them, three operands deep, and dropping a format collapses all three.

Against `vec_cvt`'s own figures the converters cannot be the bill: a SIMD unit
loses `u_ca32`, `u_cb32` and `u_cc32` at 46 LUT each and `u_cy32`, which is
wiring — **about 138 of the 446**. The other ~300 is the operand slicing, and
the SIMT unit, whose slicing does not change, comes in at 187.

**§5c said SIMD's float unit costs 109 LUT more than SIMT's for the packed
index. This says the same index costs 259 LUT more per unit again the moment the
build carries both formats** — so the 109 was the floor, measured where the
element width was the only thing varying.

### It is a refusal, not a reinterpretation

`bad_fet` on SIMD and `unbuilt` on SIMT fault the absent format, and that is
tested rather than asserted: the FULL dual-format stream run against an
`HAS_F32`=0 build is refused instruction by instruction (`the unit refused
0220b1ab (illegal 1 ...)`), and every FP16 case ahead of it runs clean.

| bench | result |
|---|---|
| `khs_run.py --float --no-facc` | **PASS**, 60 checks — unchanged with the gates present |
| `khs_run.py --float --no-facc --no-f32` | **PASS**, 48 checks |
| `khs_run.py --float --no-facc --no-f16` | **PASS**, 48 checks |
| dual-format stream vs an `HAS_F32`=0 build | **every FP32 instruction refused**, no FP16 case disturbed |
| `rv_simt_suite.py --only f32` incl. `[KHT_F16=0]` | **4 / 4 PASS** — same image, same golden DRAM |
| `rv_simt_suite.py`, the whole 34-case suite | **34 / 34 PASS** with the gates in |

`simt_f32.s` is the only SIMT shader with no `_h` in it, so it is the only one
that can run on a single-format build; `simt_float.s` and `simt_fwalk.s` mix the
two deliberately. **There is no FP16-only SIMT shader, so the FP16-only SIMT
build is synthesised but not simulated** — that is a gap, not a result.

## 12. The extension against the core it hangs off

`SIMD_EN` = 0 is the shipped RV32 controller PE: **2,661 LUT, 4,140 FF, 5 BRAM,
0 DSP.** So of the assembled SIMD PE's 15,741 LUT, **13,080 — 83% — is the SIMD
extension**, and every shrink in this file lives inside it. The per-instance
report agrees from the other side: `u_core/g_simd.u_khs` is 14,388 LUT
primitives of the PE's 17,489.

## 13. The inference table, validated on the NEW knobs

§4's validation moved three counts that already had a price. This moves three
knobs that did not have one until §10, on a combination no fit row covers, and
predicts it by ADDING the single-knob deltas — which is the assumption the whole
table rests on and the only thing worth testing.

**D-pred: `PERM_UNITS` = 1, `NACC` = 1, `HAS_F32` = 0.**

    LUT   = 15,741  the reference
          − 1,165   PERM_UNITS 1   (D-perm1)
          −   414   NACC 1         (D-nacc1)
          − 1,786   HAS_F32 0      (D-f16)
          = 12,376
    DSP   = 56      none of the three touches a multiplier
    BRAM  = 13      none of the three touches storage

| | predicted | measured | error |
|---|---|---|---|
| DSP | 56 | **56** | **0** |
| BRAM | 13 | **13** | **0** |
| LUT | 12,376 | **12,524** | **+148, 1.2%** |

DSP and BRAM exact, LUT inside 1.2% over a 3,217 LUT swing — the same shape as
G12's 0.87% and for the same reason: the residual is interaction between knobs
the single-knob rows cannot see. Every term here was measured on snapshot 3, so
unlike S14 there is no cross-snapshot transfer in the prediction at all.

**G-pred: `WAVES` = 8, `IPDOM_D` = 4, `HAS_F16` = 0** — the same test on the
other core, and all three of these knobs were unpriced before §10.

    LUT   = 19,461 − 659 (G-w8) − 8 (G-ip4) − 1,982 (G-f32) = 16,812
    DSP   = 48      unchanged by all three
    BRAM  = 30.5    unchanged by all three

| | predicted | measured | error |
|---|---|---|---|
| DSP | 48 | **48** | **0** |
| BRAM | 30.5 | **30.5** | **0** |
| LUT | 16,812 | **16,552** | **−260, 1.5%** |

**Two prediction rows, six knobs, four of them new, and the two LUT errors have
OPPOSITE SIGNS** — +1.2% on SIMD and −1.5% on SIMT. Adding single-knob deltas is
therefore unbiased here rather than systematically optimistic, which is the
property that makes the table usable for a configuration nobody has synthesised.
DSP and BRAM are exact on both, as they have been on every prediction row in
this file.

A third row exists and is the tightest, though it is not a shippable
configuration: `AreaOptimized_high` + `PERM_UNITS` 1 predicts 15,741 − 1,301 −
1,165 = 13,275 against a measured **13,273 — +2 LUT, 0.015%**. It says the
additive model survives even a whole-netlist re-map, which is worth knowing
about the MODEL; the configuration itself misses timing and §10 retracts it.

## 14. The SIMD shrink, and what each step costs in capability

The target was under 14,000 from the shipped 15,741. **It is reached at 13,579
with the whole ISA intact**, on cycles alone, at `-directive default` and 367.8
MHz. The rest of the table is what each further step costs the machine.

| configuration | LUT | vs shipped | what it gives up |
|---|---|---|---|
| shipped | 15,741 | — | — |
| `VREG_PRIM` = block | 15,660 | −0.5% | nothing, but +12 BRAM |
| `NACC` 1 | 15,327 | −2.6% | the second `vdot` accumulator bank |
| `PERM_UNITS` 1 | 14,576 | **−7.4%** | **cycles only** — 8 passes a `vsldw`, same answer |
| `HAS_F32` 0 | **13,955** | **−11.3%** | the FP32 memory format |
| `HAS_F16` 0 | **13,671** | **−13.2%** | the FP16 memory format (and 2x density) |
| `SHIFT_UNITS` 2 (§15) | 14,614 | **−7.2%** | **cycles only** — 4 passes a shift, same answer |
| **`PERM_UNITS` 1 + `SHIFT_UNITS` 2** | **13,579** | **−13.7%** | **cycles only — every instruction intact** |
| `PERM_UNITS` 1 + `NACC` 1 + `HAS_F32` 0 | **12,524** | **−20.4%** | a bank and the FP32 format |

Every row above is `-directive default` and every one meets timing with positive
slack. The two `AreaOptimized_high` rows are NOT in this table: they miss timing
(260.8 and 249.3 MHz OOC, negative slack) and §10 retracts them.

**Under 14,000 with NO instruction removed IS reachable, and only because of
§15's shifter width.** `PERM_UNITS` 1 + `SHIFT_UNITS` 2 is **13,579 LUT at
367.8 MHz, slack +0.614** — the whole ISA intact, eight passes a `vsldw` and
four a shift, and it is the fastest SIMD row in this file as well as one of the
smallest. Before the shifter became a width the best cycles-only configuration
was the permute alone at **14,576**, which missed.

It also validates again: predicting 15,741 − 1,165 − 1,127 gives 13,449 against
a measured **13,579 — +130 LUT, +1.0%**, with DSP 56 and BRAM 13 exact.

Removing an encoding on top of that is still available and still priced —
`HAS_F32` = 0 is the cheapest at −1,786 — but it is no longer NECESSARY to hit
the target. Still unmeasured: the reduction trees and the rounding shift's own
adder (§5a).

---

## 15. Three booleans that should have been widths

The owner's rule is a power-of-two unit count on every feature of both cores,
with a multi-cycle drain when units < lanes and an ISA that never learns the
count. Three features were still all-or-nothing gates. All three are now counts,
all three pay, and one of them **beats deleting the feature outright**.

Every row is `-directive default` against its own core's snapshot-3 reference.

### SIMT `SHFL_UNITS` — the shuffle, which SIMD's permute already had

`HAS_SHFL` was 1,224 LUT all-or-nothing on snapshot 1 and measures **880** on
snapshot 4 (§17c), against which the width at one unit recovers 517 — 59% — with
the instruction intact. Unit *u* on pass *p* produces output
lane *p·U + u*, and **the register file's per-lane write enable places it — so
there is no staging register at all**, which is why FF barely moves. SIMD's
`khs_perm` needs one only because `khs_vregfile` has no per-element enable.

| `SHFL_UNITS` | LUT | vs 19,461 | FF | ctrl | Fmax | passes per `shflxor` |
|---|---|---|---|---|---|---|
| 8 / 0 (the butterfly) | **19,461** | **0** | 17,268 | 202 | 361.0 | 1 |
| 4 | 19,488 | **+27** | 17,264 | 204 | 377.9 | 2 |
| 2 | 19,318 | **−143** | 17,261 | 198 | 379.4 | 4 |
| 1 | 18,944 | **−517** | 17,270 | 204 | 343.1 | 8 |

**The default costs exactly zero** — `SHFL_UNITS` = 8 is byte-identical to the
reference in every column, so the butterfly is kept in its own branch and the
knob is free until used. A narrow build is a DIRECT SELECT, not a narrowed
butterfly: a butterfly routes every lane at once and cannot be sliced, so one
output lane is a LANES:1 32-bit mux. That mux is why one unit saves 517 of the
1,224 rather than all of it — and why **four units COST 27 LUT** rather than
saving any. The curve is worth having only at the narrow end, which is the same
shape §5d measured on SIMD's permute (8→4 only −262, 4→2 −1,049). Two features,
two cores, one property: **a cross-lane width pays at 1 or 2 units and nowhere
else.**

### SIMD `SHIFT_UNITS` — and the refusal it overturns

`HAS_SHIFT` measured **1,616 LUT** as a gate on snapshot 3. §5a refused a width
here on the grounds that "the operand mux is as expensive as the shifter it
replaces". **That arithmetic was wrong**: it charged one mux per shifter
REMOVED, where the walk pays one mux per unit KEPT.

| `SHIFT_UNITS` | LUT | vs 15,741 | FF | Fmax | passes per shift | shift works? |
|---|---|---|---|---|---|---|
| 8 / 0 (inside every lane) | **15,741** | **0** | 9,845 | 340.8 | 1 | yes |
| 2 | **14,614** | **−1,127** | 10,076 | 320.6 | 4 | yes |
| 2, with `PERM_UNITS` 1 | **13,579** | **−2,162** | 10,313 | **367.8** | 4 | yes |
| 1 | 14,804 | **−937** | 10,076 | 337.4 | 8 | yes |
| `HAS_SHIFT` = 0 | 14,125 | −1,616 | 9,748 | 331.8 | — | **NO, it faults** |

**Two units recover 70% of what deleting the shifter saves, and keep every shift
instruction.** The +231 FF is the staging register, the same one the permute
pays (+234) and for the same reason. The curve is non-monotonic — two units
beats one by 190 LUT — which is the shape §4 already measured on the seed count:
the placement decode and the operand mux move in opposite directions with the
unit count.

### SIMT `LDS_BANKS` — a width whose drain already existed

`HAS_LDSBANK` was 1,948 LUT all-or-nothing on snapshot 1 and measures **1,805**
on snapshot 4 (§17c), against which one bank recovers 1,562 — **87%** — with the
LDS still working. `kht_lds`' sequencer already walks
passes until the outstanding set empties, so **fewer banks is simply more
conflicts and more passes and needed no new mechanism** — only the bank index,
the bank array and the resolver's width became a parameter.

| `LDS_BANKS` | LUT | vs 19,461 | BRAM | Fmax | worst-case passes |
|---|---|---|---|---|---|
| 8 / 0 | **19,461** | **0** | 30.5 | 361.0 | 8 |
| 2 | 18,617 | **−844** | 24.5 | 365.5 | 8 |
| 1 | **17,899** | **−1,562** | 24.5 | 361.0 | 8 |

**One bank recovers 80% of the gate's saving and the LDS still works** — every
access serialises, which is what a one-bank memory means, and forward progress
is unchanged because the resolver still serves the lowest outstanding lane.

### SIMD `MULS` — a count that FAULTED instead of taking more cycles

Not on the "still boolean" list, and the worst of the four, because it was
already a count and still broke the rule. `MULS` = 2 builds two multipliers a
lane instead of four, and int8 needs four products — so `khs_unit`'s `bad_cfg`
**faulted every int8 `vdot` and `vmul`**:

    || ((is_dot || is_mul) && (et == KHS_ET_S8) && (MULS < 4))

That is a NARROWER INSTRUCTION SET on a narrower machine — the one outcome the
mechanism exists to prevent, and the thing §7 and §8 of this file are about.
`khs_lane`'s own header had claimed the correct behaviour all along —
"`MULS = 2` drops int8 to two passes and is the configuration that prices that
choice" — and the RTL never did it. **A comment is not a feature.**

Built: at `MULS` < 4 an int8 multiply issues **two passes**, byte pair (0,1)
then (2,3), on the same two multipliers. The operand width does not change and
neither does the answer.

**The lane accumulates its own passes, so `DOT_LAT` and the II = 1 outer
accumulator are untouched** — pass 1 adds into the lane's own sum rather than
firing the outer accumulate twice, which integer addition permits and float
addition would not. `vmul` stages pass 0's two bytes and joins them on the read.

The fault is gone from `bad_cfg`; `MULS` = 2 and 4 now differ only in cycles.

### The ISA still knows no count

Same image, same golden DRAM, only the cycles change:

| bench | result |
|---|---|
| `rv_simt_suite.py --only shfl` at `SHFL_UNITS` 8, 4, 2, 1 and x16 at 2 | **5 / 5 PASS** |
| `rv_simt_suite.py --only lds` at `LDS_BANKS` 8, 4, 2, 1 and `HAS_LDSBANK`=0 | **5 / 5 PASS** |
| `khs_run.py` integer stream at `SHIFT_UNITS` 8, 2, 1 | **PASS**, 36 checks each |
| `khs_run.py --muls 4` and `--muls 2`, both now emitting int8 | **PASS**, 36 checks each |
| `khs_run.py --float` at `SHIFT_UNITS` 8 and 2 | 96 checks, **the same single pre-existing 1-ULP** either way — the width does not touch the float path |
| `rv_simt_suite.py`, the whole 41-case suite | **41 / 41 PASS** |

**The int8 two-pass dot FAILED first, and the bug is the sharpest lesson here:**
the bench defaults `KHS_DOTDSP` = 1, so what it builds is the DSP48 cascade —
and the two-pass accumulation had only gone into the FABRIC adder branch.
`vmul` passed throughout (it reads the products directly) while `vdot` was wrong,
which is exactly the shape that makes a partial fix look like a subtle timing
bug. **Two dot-sum paths means every dot change lands twice**; the fix is one
term in each, constant-zero at `MULS` >= 4.

`LDS_BANKS` = 1 FAILED first too, and its bug is worth recording: `NBW` was
`$clog2(NB)` floored at 1, so a one-bank build still took an address bit as its
bank index, `selv[1]` was read on a one-entry array, the resolver served nobody
and the sequencer spun. **A zero-width field forced to one bit is not a
harmless default** — the bank bits actually used (`NBS`) and the storage width
(`NBW`) are now separate localparams.

### What is left, and why each one is a refusal rather than a gap

| feature | verdict | the arithmetic |
|---|---|---|
| SIMT `HAS_MASK` + `HAS_IPDOM` | **priced refusal, and the price fell** | 681 LUT on snapshot 1; re-measured on snapshot 4 in §17c it is **−197 LUT** for the whole gate. A width time-shares a DATAPATH and this is one bit per lane per wave plus a per-lane AND on the write — and §17c measures the one dimension that IS storage, `IPDOM_D` 8→4, at **−8 LUT**. There is not 197 LUT of walkable datapath here, and a walked mask would have to store the bits while it walked. |
| SIMT accumulator | **does not exist** | `kht_pe` has no accumulator; the float tier retires straight to the vector file. There is no count to add, which is not the same as a missing one. |
| SIMT vector register file | **priced refusal** | §10's `VREGS` row: halving SIMD's file returned **137 LUT**, because a distributed-RAM primitive is 32 deep and the entry count does not set the LUTs. Banking the SIMT file is the same argument against `block`. |
| SIMD `MULS` | **was a count that FAULTED; now multi-cycles** — see above | The old refusal ("wrong direction, DSP not LUT") answered a question nobody asked: the defect was not that narrowing costs DSP, it was that narrowing REMOVED int8 from the ISA. |
| SIMD "how many LANES carry multipliers" | **NOT BUILT — a real gap, not a refusal** | SIMT has exactly this as `MUL_UNITS`; SIMD has no equivalent, because every lane carries its own `MULS` multipliers. `MULS` is the per-lane depth, a different axis. Building it means walking `vdot`'s accumulate across lane groups, which touches the II = 1 recurrence — the one structure in this file nothing has yet been allowed to disturb. |
| SIMD `HAS_FACC` width | **bound on purpose** | `khs_facc` takes `SLOTS(FLANES)`. Separating them changes the partial chain length per element, and §7 already records that float addition does not associate — so an independent count would change the ANSWERS. That breaks "the ISA never learns the count", which is the rule the width exists to satisfy. |
| SIMD `DOT_DSP` | **not a width** | A mapping choice between the fabric adder tree and the DSP48 cascade, priced in `khs_lane`'s header at 256 LUT + 32 CARRY → 0 + 0 across 8 lanes. |

## 17. THE METRIC TABLE — every feature, both cores, one snapshot each

**Snapshot 4.** Every number below is measured, `-directive default`, synth only,
3.333 ns, `-flatten_hierarchy rebuilt`, xcvu13p-fhgb2104-2L-e. **No cell is
inferred or predicted**; a knob point that was not synthesised is left blank and
named in §17e. Every generic in every row was checked to appear on the
`synth_design` command line — no row here was parsed-but-not-applied.

Rows from §10–§15 are NOT reused: the multiply walk moved the SIMD default, so
every SIMD row was re-measured against the baseline below. The SIMT reference
reproduced **byte-identically** (19,461 in all six columns) after every SIMT
change in this file, which is what lets the SIMT rows share one baseline.

### 17a. The shipped-ISA minimum, named in full

**13,586 LUT / 10,313 FF / 13 BRAM / 56 DSP / 139 control sets / 367.8 MHz
(slack +0.614)** — the whole ISA intact, cycles the only thing given up:

    SIMD_EN 1  SIMD 8  MULS 4  NACC 2  VREGS 8  VSPAD 1024  NPART 16
    HAS_SHIFT 1  SHIFT_UNITS 2      <-- 4 passes a shift, not 1
    HAS_PERM  1  PERM_UNITS  1      <-- 8 passes a vsldw,  not 1
    HAS_FLOAT 1  FLOAT_LANES 4  FSFU_UNITS 0  HAS_FALU 1  HAS_FACC 0  HAS_FCVT 0
    HAS_F16 1  HAS_F32 1  DOT_DSP 0  WB_STAGE 0  RED_PIPE 1
    VREG_PRIM distributed  MEM_PRIM block  RECV_MEM distributed  USE_DSP yes

Every other count is at its default and is written out above. Against the
shipped **15,682** that is **−2,096 LUT, −13.4%**, and it is the fastest SIMD row
in this file.

### 17b. SIMD — one feature moved at a time off 15,682

Baseline **d0c-ref: 15,682 LUT / 9,836 FF / 13 BRAM / 56 DSP / 130 ctrl /
349.3 MHz.**

| knob | value | LUT | ΔLUT | FF | BRAM | DSP | ctrl | Fmax |
|---|---|---|---|---|---|---|---|---|
| — | baseline | **15,682** | — | 9,836 | 13 | 56 | 130 | 349.3 |
| `PERM_UNITS` | 8 (=0) | 15,682 | 0 | 9,836 | 13 | 56 | 130 | 349.3 |
| | 2 | 15,185 | **−497** | 10,077 | 13 | 56 | 130 | 335.8 |
| | 1 | 14,459 | **−1,223** | 10,080 | 13 | 56 | 128 | 318.3 |
| `SHIFT_UNITS` | 8 (=0) | 15,682 | 0 | 9,836 | 13 | 56 | 130 | 349.3 |
| | 4 | 14,970 | **−712** | 10,085 | 13 | 56 | 133 | 344.4 |
| | 2 | 14,623 | **−1,059** | 10,083 | 13 | 56 | 131 | 320.6 |
| | 1 | 14,779 | **−903** | 10,098 | 13 | 56 | 134 | 345.7 |
| `HAS_SHIFT` | 0 (gate; shift FAULTS) | 14,757 | −925 | 9,749 | 13 | 56 | 122 | 343.5 |
| `FLOAT_LANES` | 8 | 20,063 | **+4,381** | 13,499 | 13 | 64 | 138 | 310.4 |
| | 4 | 15,682 | 0 | 9,836 | 13 | 56 | 130 | 349.3 |
| | 2 | 13,676 | **−2,006** | 8,011 | 13 | 52 | 115 | 324.4 |
| `FSFU_UNITS` | 0 | 15,682 | 0 | 9,836 | 13 | 56 | 130 | 349.3 |
| | 1 | 16,674 | **+992** | 10,232 | 14.5 | 57 | 134 | 318.1 |
| | 4 | 16,668 | **+986** | 11,292 | 19 | 60 | 136 | 318.1 |
| `NACC` | 1 | 15,128 | **−554** | 9,564 | 13 | 56 | 127 | 321.6 |
| | 2 | 15,682 | 0 | 9,836 | 13 | 56 | 130 | 349.3 |
| | 4 | 16,005 | **+323** | 10,362 | 13 | 56 | 136 | 341.9 |
| `MULS` | 2 (int8 = 2 passes) | 16,262 | **+580** | 9,974 | 13 | **32** | 131 | 327.7 |
| | 4 | 15,682 | 0 | 9,836 | 13 | 56 | 130 | 349.3 |
| `VREGS` | 4 | 15,647 | **−35** | 9,846 | 13 | 56 | 129 | 327.3 |
| `HAS_F32` | 0 | 13,932 | **−1,750** | 9,341 | 13 | 56 | 126 | 318.3 |
| `HAS_F16` | 0 | 13,656 | **−2,026** | 9,770 | 13 | 56 | 113 | 318.3 |
| `WB_STAGE` | 1 | 15,736 | **+54** | 10,110 | 13 | 56 | 128 | 341.9 |
| `VREG_PRIM` | block | 15,674 | **−8** | 9,068 | **25** | 56 | 128 | 253.7 |
| `SIMD` | 4 | 11,282 | −4,400 | 9,017 | 9 | 32 | 119 | 322.9 |
| | 8 | 15,682 | 0 | 9,836 | 13 | 56 | 130 | 349.3 |
| | 16 | 24,830 | +9,148 | 11,448 | 21 | 104 | 133 | 307.7 |
| `SIMD_EN` | 0 (controller alone) | 2,661 | −13,021 | 4,140 | 5 | 0 | 79 | 396.5 |
| **combined** | `PERM_UNITS` 1 + `SHIFT_UNITS` 2 | **13,586** | **−2,096** | 10,313 | 13 | 56 | 139 | **367.8** |

### 17c. SIMT — one feature moved at a time off 19,461

Baseline **g0b-ref: 19,461 LUT / 17,268 FF / 30.5 BRAM / 48 DSP / 202 ctrl /
361.0 MHz.**

| knob | value | LUT | ΔLUT | FF | BRAM | DSP | ctrl | Fmax |
|---|---|---|---|---|---|---|---|---|
| — | baseline | **19,461** | — | 17,268 | 30.5 | 48 | 202 | 361.0 |
| `SHFL_UNITS` | 8 (=0) | 19,461 | 0 | 17,268 | 30.5 | 48 | 202 | 361.0 |
| | 4 | 19,488 | **+27** | 17,264 | 30.5 | 48 | 204 | 377.9 |
| | 2 | 19,318 | **−143** | 17,261 | 30.5 | 48 | 198 | 379.4 |
| | 1 | 18,944 | **−517** | 17,270 | 30.5 | 48 | 204 | 343.1 |
| `HAS_SHFL` | 0 (gate; shuffle FAULTS) | 18,581 | −880 | 17,267 | 30.5 | 48 | 202 | 383.0 |
| `LDS_BANKS` | 8 (=0) | 19,461 | 0 | 17,268 | 30.5 | 48 | 202 | 361.0 |
| | 4 | 18,847 | **−614** | 17,267 | 26.5 | 48 | 202 | 405.2 |
| | 2 | 18,617 | **−844** | 17,277 | 24.5 | 48 | 202 | 365.5 |
| | 1 | 17,899 | **−1,562** | 17,264 | 24.5 | 48 | 202 | 361.0 |
| `HAS_LDSBANK` | 0 (gate; no LDS) | 17,656 | −1,805 | 16,930 | 24.5 | 48 | 191 | 376.4 |
| `FLANES` | 8 | 19,461 | 0 | 17,268 | 30.5 | 48 | 202 | 361.0 |
| | 4 | 16,307 | **−3,154** | 13,917 | 30.5 | 40 | 178 | 334.7 |
| | 2 | 14,100 | **−5,361** | 12,233 | 30.5 | 36 | 163 | 378.9 |
| `MUL_UNITS` | 8 | 19,461 | 0 | 17,268 | 30.5 | 48 | 202 | 361.0 |
| | 4 | 19,404 | **−57** | 15,461 | 30.5 | **32** | 203 | 376.6 |
| | 2 | 19,417 | **−44** | 14,571 | 30.5 | **24** | 203 | 368.3 |
| `FSFU_UNITS` | 0 | 19,461 | 0 | 17,268 | 30.5 | 48 | 202 | 361.0 |
| | 2 | 20,841 | **+1,380** | 17,954 | 33.5 | 50 | 211 | 346.4 |
| | 8 | 22,084 | **+2,623** | 20,065 | 42.5 | 56 | 228 | 363.9 |
| `WAVES` | 16 | 19,461 | 0 | 17,268 | 30.5 | 48 | 202 | 361.0 |
| | 8 | 18,802 | **−659** | 16,756 | 30.5 | 48 | 175 | 380.4 |
| | 4 | 18,414 | **−1,047** | 16,497 | 30.5 | 48 | 160 | 352.6 |
| `IPDOM_D` | 4 | 19,453 | **−8** | 17,247 | 30.5 | 48 | 202 | 360.2 |
| `HAS_MASK`+`HAS_IPDOM` | 0 (gate) | 19,264 | −197 | 17,028 | 30.5 | 48 | 165 | 380.5 |
| `HAS_F32` | 0 | 17,968 | **−1,493** | 16,153 | 30.5 | 48 | 201 | 365.8 |
| `HAS_F16` | 0 | 17,479 | **−1,982** | 17,250 | 30.5 | 48 | 192 | 385.2 |
| `LANES` | 4 (with `FLANES` 4, `MUL_UNITS` 4) | 11,369 | −8,092 | 10,712 | 20.5 | 24 | 168 | 383.4 |

### 17d. The per-unit arithmetic, named

Every figure is a MARGINAL difference between two rows one step apart, never a
tier divided by its count.

| unit | arithmetic | per unit |
|---|---|---|
| SIMD FP FMA | (20,063−15,682)/4 and (15,682−13,676)/2 | **1,095** and **1,003** |
| SIMT FP FMA | (19,461−16,307)/4 and (16,307−14,100)/2 | **789** and **1,104** |
| SIMD 32-bit slot (`SIMD`) | (15,682−11,282)/4 and (24,830−15,682)/8 | **1,100** and **1,144** |
| SIMD accumulator bank | (15,682−15,128)/1 and (16,005−15,682)/2 | **554** then **162** |
| SIMT wave slot | (19,461−18,802)/8 and (18,802−18,414)/4 | **82.4** then **97** |
| SIMT seed unit | (20,841−19,461)/2 and (22,084−20,841)/6 | **690** then **207** |
| SIMT multiply unit | LUT is flat; DSP is (48−24)/6 | **≈0 LUT, 4 DSP** |
| SIMD multiply depth | `MULS` 4→2 is **+580 LUT for −24 DSP** | a LUT-for-DSP trade |

The two float-unit columns disagree by design and both are given: SIMD's is
dearer per unit at the wide end and SIMT's at the narrow end, so a single number
would be an average and this file does not average.

### 17e. What is blank, and why

| gap | reason |
|---|---|
| `PERM_UNITS` 4, `LDS_BANKS` at other counts, `SHFL_UNITS` beyond 8/4/2/1 | 3 points measured per knob, which is the agreed density; the 4th point was not run |
| `VREGS` 16 / 32 | not synthesised. §10's prediction that the file grows nearly free is still a PREDICTION |
| `NPART`, `VSPAD_ENTRIES`, `IMEM/SPAD/L1` depths, `RECV_DEPTH`, `INST_DEPTH` | memory depths, not unit counts; unmeasured here |
| `HAS_FACC` on this tree | the accumulator tier is off in the baseline; its cost is §5c's +3,936 on snapshot 2 and was NOT re-measured on snapshot 4 |
| `khs_reduce`, `khs_lane`'s second `khs_padd32` | still have no gate at all, so there is nothing to set — §5a |
| SIMD "lanes carrying multipliers" | not built; §15 names why |

No row in §17b or §17c was parsed-but-not-applied: every generic was read back
off the `synth_design` command line in its own run log.

## 18. Why no SIMD row reaches 350 MHz, and SIMT always does

Never reported before, and it matters more than a few hundred LUT. Across every
`-directive default` row in this file:

- **SIMD, with a float tier: 316.8 – 367.8 MHz**, and all but one row below 350.
- **SIMT: 343.1 – 385.2 MHz**, all but one row above it.
- The SIMD controller PE ALONE (`SIMD_EN` = 0) runs at **396.5 MHz**.

**The one SIMD row that clears 350 is what stops this being a ceiling result.**
`PERM_UNITS` 1 + `SHIFT_UNITS` 2 reads **367.8 MHz at 13,579 LUT** — but each of
those knobs ALONE reads BELOW the reference (320.6 and 324.5 against 340.8). The
two cannot both be "the fix" and separately be losses. A single row is not a
property, and this file's own preamble says why: these numbers move by tens of
MHz between rows that differ in nothing that should matter.

**It is not size.** The smallest SIMD PE with a float tier, D-pred at 12,524 LUT
— 20% below the reference — measures **341.1 MHz** against the reference's
340.8. Shrinking the PE by a fifth moved Fmax by 0.3 MHz.

**It is not logic depth either, and that is the surprise.** The reference's
binding path is 13 levels with 4 CARRY8: `u_vrf` read port → the SWAR lane →
`u_vrf` write port, a register-file round trip in one cycle, which is exactly
what `WB_STAGE` = 0 buys a cycle for. Setting `WB_STAGE` = 1:

| | LUT | FF | logic levels | binding path | Fmax |
|---|---|---|---|---|---|
| `WB_STAGE` 0 | 15,741 | 9,845 | **13** (4 CARRY8) | inside `khs_unit` | 340.8 |
| `WB_STAGE` 1 | **15,722** | 10,105 | **7** | `u_spad` → `u_core/u_rf`, the BASE core | **341.9** |

**Halving the logic depth bought 1.1 MHz**, for −19 LUT and +260 FF. And the
binding path LEFT the extension entirely — at `WB_STAGE` = 1 the critical path
is the base RV32 core's scratchpad-load-to-register-file path, which the base
core alone runs at 396.5 MHz.

So the ceiling is **route, not logic**: every binding path measured is 59–74%
route delay (the reference 1.884 ns route of 2.834 ns; SIMT 1.809 of 2.439).
The extension costs ~55 MHz on a path it is not even in.

**What IS established, on four measurements: SIMD Fmax here does not track
area.** 20% smaller is +0.3 MHz, half the logic depth is +1.1 MHz, a format
removed is −24, and two widths together are +27. Every binding path is
route-dominated, so the number moves with placement rather than with what the
RTL costs.

**What is NOT established, and I am not claiming it: that 350 MHz is out of
reach.** One row reaches 367.8. What that row does not do is explain itself —
its two knobs are individually negative — so the honest reading is that these
OOC Fmax figures are a screen with tens of MHz of scatter, exactly as the
preamble says, and **the SIMD/SIMT asymmetry needs a PLACED run to settle.**
That is outside an OOC-only campaign and is the next thing someone should do.

`WB_STAGE` = 1 is worth taking regardless of how that lands: it costs **19 LUT**
and +260 FF, halves the logic depth, and moves the binding path out of the
extension and into the base core.

The two unpriced features in §5a — the reduction trees and the rounding shift's
own adder — are the only remaining candidates that have not been measured, and
both also remove an instruction.
