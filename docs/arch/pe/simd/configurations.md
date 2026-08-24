---
title: Configurations
summary: What each float group costs measured rather than estimated, why the integer lane count is fixed while the float lane count is not, and why the eight SIMD PEs of a mesh should not all be the same build.
tags:
  - architecture
  - pe
  - simd
  - configuration
---

# Configurations

Every number here is out-of-context synthesis of `khs_unit` alone on
`xcvu13p-fhgb2104-2L-e` at a **2.857 ns** ask, 8 integer lanes throughout. The
unit alone and not the assembled PE, because the marginal cost of making a
controller PE a SIMD PE is what a configuration matrix is about.

## The two axes are not alike

```
   integer lanes  <-  the memory granule  (8 x 32 bit = 256 bit = one flit)   8, fixed
   float lanes    <-  arithmetic demand   (throughput against LUT)            a knob
   float groups   <-  which arithmetic exists at all                          knobs
```

The integer lanes **are the address path**: a contiguous 32-bit load by eight
threads is exactly one `MEM_RD_REQ`, one native memory entry, one flit.
Narrowing them breaks single-request coalescing permanently. Float carries no
such constraint, which is why it is the knob and the integer side is not.

## Measured

All at `WB_STAGE = 0` so the rows compare with each other; the shipped setting
is 1 and costs about 350 LUT more.

| float lanes | groups built | **LUT** | FF | BRAM |
|---:|---|---:|---:|---:|
| 2 | FALU only | 12,545 | 3,909 | 8 |
| **2** | **FALU + FSFU** | **13,190** | 4,796 | 14 |
| 4 | FALU only | 15,298 | 5,619 | 8 |
| 4 | FALU + FSFU | 16,838 | 7,392 | 20 |
| 8 | FALU + FSFU | 24,077 | 12,587 | 32 |

Derived, and each is a subtraction of two rows above rather than an estimate:

| | cost |
|---|---|
| **a float lane** carrying the elementwise set and the seeds | **~1,815 LUT** — (16,838−13,190)/2 = 1,824 and (24,077−16,838)/4 = 1,810, so it is linear in lanes over the whole range |
| **FSFU**, the four seeds | **+645 LUT at 2 lanes, +1,540 at 4** — ~322 and ~385 per lane, plus **3 BRAM per lane** for the tables |

BRAM is where the seeds actually land, and it is the resource this fabric has in
surplus: even the eight-lane row is 32 BRAM against an SLR's 672.

**Measured on the RTL that passes its component test**, after the three bugs in
the verification section below. Earlier figures in this project's notes — 16,580
for the four-lane full row, 13,237 for the shipped one — were taken on RTL whose
elementwise writeback did not work at the shipped `WB_STAGE`, and should not be
quoted.

Two rows are **not** re-measured and are quoted from the pre-fix RTL, because
both depend on the accumulator and the accumulator does not currently work:
`FMAC only` at four lanes was 11,982, and `FALU + FSFU + FACC` at two lanes was
15,391 with `u_facc` itself at **76 LUT**. That 76 against a +2,154 delta is the
defect below, not the price of the feature.

## The first row is not a baseline, and reading it as one is the trap

`FMAC only` measures 11,982 with the accumulator's own block at **124 LUT**, and
that number is small for a reason that has nothing to do with the accumulator
being cheap: with `khs_float_lane`'s operation tied to a constant FMA, sixteen of
`vec_alu`'s seventeen opcodes are constant-folded out of the netlist. That
configuration has an accumulator and **no float arithmetic a program can reach**
— no multiply, no add, no compare, nothing that writes a vector register except
reading the accumulator back.

So the ~3.2k LUT between it and a FALU build at the same width is not a
regression. It is the cost of the SIMD PE having float arithmetic at all, and it
was always going to be paid the first time somebody asked for `vfmul`.

## The accumulator's +2,154 is a defect

`u_facc` is 76 LUT. The other ~2,078 is a **second float lane array**: `g_fel`
and `g_float` each instantiate their own `khs_float_lane` group, so a build with
both carries two elementwise lanes *and* two accumulator lanes — four lanes of
arithmetic on a machine with one program counter that can only ever use one at a
time.

They should share one array. The unit is in-order and single-issue, so the two
paths are never live in the same cycle; what the share needs is a source mux in
front of the lanes and two hazards that already have precedent here — a float
accumulate in flight must block an elementwise issue exactly as it already
blocks `vfaccrd`, and vice versa. Shared, the accumulator should cost about what
`u_facc` plus the fold FSM measures, which is a few hundred LUT.

**Until that lands, treat the FACC rows above as an upper bound rather than the
price of the feature.**

## The shipped default

**8 integer lanes, 2 float lanes, FALU + FSFU, accumulator off.**

| | LUT | FF | BRAM |
|---|---:|---:|---:|
| at `WB_STAGE = 0`, comparable with the table above | 13,190 | 4,796 | 14 |
| **at `WB_STAGE = 1`, which is what `rv_pe` ships** | **13,538** | 5,061 | 14 |

It is the widest configuration that carries the whole elementwise float set
*and* the four seeds inside the 14k budget. **The sweep above is all at
`WB_STAGE = 0`** — the rows are comparable with each other, and about 350 LUT
below the shipped setting.

What that buys, per instruction: 16 FP16 elements or 8 FP32 elements over 2
lanes, so `vfmul.f16` issues in 8 passes and `vfmul.f32` in 4. Halving the
lanes halves float throughput and changes nothing else — not the element count,
not the results, not the ISA.

## Why the eight SIMD PEs of a mesh should differ

Every group is a parameter, so the PEs in one mesh need not be the same build.
That turns the feature mix into an axis of the balance study rather than one
global choice, and it is cheaper than it sounds:

```
   8 x (8+2, FALU+FSFU)                     = 105,520 LUT, 16 float lanes
   4 x (8+2, FALU+FSFU) + 4 x (8+4, FALU)   = 113,952 LUT, 24 float lanes
   8 x (8+4, FALU+FSFU)                     = 134,704 LUT, 32 float lanes
```

The middle row is the interesting one: it buys **50% more float lanes than the
first for 8% more LUT**, by giving the seeds only to the PEs that will run
transcendental-heavy work and the extra lanes to the ones that will not.

A mesh does not have to answer "how much float does a SIMD PE need" once. It can
carry several answers and let the classifier route to them.

A mesh does not have to answer "how much float does a SIMD PE need" once. It can
carry several answers and let the classifier route to them.

## The elementwise group is latency-bound, not throughput-bound

**One elementwise instruction is in flight at a time**, so the issue interval is
the lane's depth plus the pass count — about **19 cycles for FP16 at four
lanes**, not the one-per-pass a SIMD unit ought to reach.

The reason is the writeback, not the arithmetic. Each pass places its `FLANES`
results into a single staging register and the whole register is written to the
vector file when the last pass lands. That makes the write port a mux rather
than an arbitration, and it needs no per-element write enable — but the staging
register is shared, so two instructions in flight overwrite each other. That was
measured, not reasoned about: the component bench reported X in the upper
elements and a write carrying another instruction's value, and the scoreboard
did not catch it because it only blocks *dependent* instructions.

Serialising is the correct fix and the cheap one. The fast fix is to make the
writeback **per pass** with a per-element write enable on the register file,
which removes the staging register entirely and takes the interval back to the
pass count. That is the first performance work this group needs, and it is
larger than a parameter.

## Verification state

| | |
|---|---|
| ISA, four consumers agree | ✅ 106 instructions |
| golden model, numerically | ✅ FALU at both widths |
| `khs_float_lane` against the model | ✅ 4000 vectors, 0 mismatches |
| `khs_unit`, integer configuration | ✅ 36 checks, 0 errors |
| **`khs_unit`, FALU at both widths** | ✅ **60 checks, 0 errors** |
| elaboration, every configuration measured | ✅ |
| FSFU against the model | ✗ not bit-exact — see below |
| the float accumulator | ✗ **broken on arrival** — see below |

Run it as `python scripts/py/xsim.py khs_unit -d KHS_FLOAT=1 -d KHS_FLANES=4
-d KHS_FACC=0 -d KHS_FSFU=0`, against vectors from `khs_gen.py --float
--flanes 4 --no-facc --no-fsfu`. The bench and the generator take the same
group switches, so a configuration is verified **as itself**.

Three bugs it caught, none of which inspection had found:

1. **`el_hold` was missing from `hz_fold`.** It held EX but not MEM, so a
   multi-pass instruction left MEM after its first pass and the result never
   retired — the bench hung rather than failed.
2. **The writeback was wired only into the `WB_STAGE = 0` branch.** The shipped
   setting is 1, so every elementwise result was computed and then dropped:
   the instructions ran, the lanes worked, and nothing drove the write port.
   `WB_STAGE = 1` also needs *two* cycles of warning rather than one.
3. **`FLANES[PSW-1:0]` is zero.** Four truncated to two bits, so the operand
   index collapsed to the lane number and every pass read the same four
   elements. Placement used the untruncated constant, so the slots varied while
   the data did not — which is exactly the shape the failure had.

### FSFU is verified by tolerance, not bit-exactly

No bit-exact reference for `vec_alu`'s four seeds exists anywhere in the tree —
`compiler/kohakutpu/model.py:358` uses `np.exp2`, and the vector core is
verified by relative-error percentile. This model computes them in float64, so
model and RTL disagree at the edges: measured, `vfexp2` on an overflowing input
gives **+infinity** from the model and a **quiet NaN** from the lane. Porting
`vec_alu`'s table and range reduction would make these exact. Until then the
seeds are excluded from the bit-exact run and that exclusion is stated rather
than hidden.

### The accumulator is broken independently of any of this

With FALU and FSFU elaborated entirely away, the accumulator alone still fails
every one of its six cases with X in the result — so the breakage is not the
elementwise group's. It is an unfinished FP32 transition in the readback path
that predates this work. `SIMD_FACC` defaults **off**, so no shipped
configuration carries it, and it needs finishing before it can be turned on.

## What is not measured here

- **Fmax.** Every row above is LUT at a fixed ask. The frequency of these
  configurations has not been measured and no claim about it is made.
- **The shared-lane configuration**, which is the row that decides whether the
  accumulator ships by default.
- **FCVT**, which is built as a parameter but defaults off: its `f2f` half falls
  out of the lane's existing conversions, and its `f2i`/`i2f` half needs
  integer-to-float logic `vec_cvt` does not carry. That is new arithmetic and it
  deserves its own measurement before it is enabled.
