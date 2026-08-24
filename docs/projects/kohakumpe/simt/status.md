---
title: SIMT PE — build status
summary: What exists, what is measured, and what is not yet built. Updated as the work lands; nothing is listed here that has not been run.
tags:
  - architecture
  - pe
  - rv32
  - gpu
  - simt
  - status
---

# SIMT PE — build status

**This page is the ground truth for progress.** A thing appears here when it
exists and has been run, with the command that ran it. A thing that is planned
and not built is in [not built yet](#not-built-yet) and nowhere else.

Last updated: **2026-08-23**.

## The configuration of record

Everything below is one PE on one part, `xcvu13p-fhgb2104-2L-e`, out-of-context
synthesis. **Quote nothing from this page without its ask** — the target moved
three times during this work (3.333 → 2.500 → 2.857 ns) and figures at different
asks are not comparable.

`kht_pe`, 8 lanes / 16 waves, **8 integer lanes + 8 float lanes, with RV32M**,
at the **2.857 ns ask (350 MHz)**:

| | LUT | FF | BRAM | DSP48 | ctrl sets | Fmax | slack |
|---|---:|---:|---:|---:|---:|---:|---:|
| SIMT PE, full ISA — the original record | 21,586 | 17,268 | 30.5 | 48 | 201 | 365.6 MHz | +0.122 |
| its reproduction on the renamed sources | 21,621 | 17,270 | 30.5 | 48 | 202 | 364.8 MHz | +0.116 |
| **after the shrink campaign — OF RECORD** | **20,086** | 17,282 | 30.5 | **48** | 201 | **392.0 MHz** | **+0.306** |

Sources: `build/sweep/g-350-pad/run.log` for the first row, tag
`gpu-khg_pe-l8-w16-m1-i1-s1-block-frebuilt-fp1.8.w1-t2.857`; the top was `khg_pe`
before the rename. `build/simt-shrink/seed-off/run.log` for the third.
**All three are `-flatten_hierarchy rebuilt`**, which is what the ship's synthesis
run takes — see [ladder](ladder.md#reporting-rules). The same design at `none`
reads 636 LUT high, and the new script's `none` rows are the diagnostic.

The `.w1` field in the old tag is from the script revision that still carried a
generic for the wide operand edge; the generic is gone and both operand widths are
unconditional, so `ooc_simt_pe.tcl` takes eleven arguments after the top today —
the eleventh is `has_fsfu` — and emits no `w` field.

The −1,535 LUT is three changes worth −1,745 and one spec fix worth +210, and
seven more were tried and reverted with the number that killed each:
[ladder](ladder.md#shrinking-it-ten-attempts-three-that-paid).

## The target, and what it is a target *for*

**350–400 MHz on the INTEGER-ONLY build; at 350, float goes back in and the
combined machine is pushed as far as it will go.** Owner's call, and the order
was the point: float is a separate purchase that can only be added once the
integer machine has headroom to give it. 300 MHz was the earlier floor.

```
   integer only  ---- reaches 350 ----> add the float tier ----> push again
        ^                                       |
        +-- headroom must exist FIRST ----------+
```

**That plan has now run to completion and the ask is met.** The integer build
reached 394.3 MHz, the float tier and integer multiply went in, and the combined
machine closes **365.6 MHz against a 350 MHz ask with +0.122 ns of slack**.

The reason the integer build had to overshoot was measurable rather than
cautious: on the SIMD PE, which shares this lineage, adding its float tier cost
**357.1 → 346.5 MHz, −10.6**. An integer-only PE sitting exactly at its floor
would have dropped under it the moment float arrived. This one did not.

**Do not subtract 365.6 from 394.3 and call it the cost of the arithmetic tier.**
Those are different asks. The **same configuration** — 8 int + 8 float lanes
with RV32M — reports **381.2 MHz at 2.500 ns** and **365.6 MHz at 2.857 ns**: a lower
Fmax against the *looser* constraint, because the tool stops optimising once the
ask is met and spends the rest on area. The 2.857 row is the one of record
because 2.857 is the ask, and it carries positive slack.

## The LUT-shrink round, and the mesh total

**A parameter that was never passed cost 3,000 LUT on every SIMT PE.**
`noc_cu_base` takes `RECV_MEM`, defaulting to `"distributed"`; `kht_pe` set
`RECV_DEPTH = 512` and left `RECV_MEM` alone, so a 512-deep × 288-bit receive
queue built in **LUTRAM** — 2,560 LUTRAM and 3,362 LUT, 15 % of the whole PE,
for a buffer that holds no logic. The header above it had already argued the
block-RAM case; the parameter carrying that argument was simply not connected.

| step | LUT | BRAM | Fmax |
|---|---:|---:|---:|
| before | 22,215 | 26.5 | 405.2 |
| `RECV_MEM("block")` | **19,216** | 30.5 | 383.1 |
| **+ the word write registered** | **19,215** | 30.5 | **405.2** |

The middle row is the same trap the whole frequency campaign was about: block
RAM moved the queue's output onto a cone that reached the LDS banks' write
enables in seven levels, and −22 MHz followed. Registering the CU_DATA **word**
write — the granule path was already registered behind `gw_buf` — gave it back
in full. Image load is a burst, so the cycle is free.

**Net: −3,000 LUT and −2,452 FF for +4 BRAM, at identical Fmax.**

### The mesh, at the 2.857 ns ask

A mesh is **8 SIMD PEs + 4 SIMT PEs + 2 controller PEs**. All three figures are
measured at the same ask, which is what makes the sum meaningful:

| | count | LUT each | LUT | DSP48 | BRAM | Fmax | float lanes | FMA/clk |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| SIMD PE, SIMD 8 + 4 float | 8 | 13,772 | 110,176 | 576 | 104 | 353.4 | 4 | 32 |
| SIMT PE, 8 int + 8 float | 4 | 21,586 | 86,344 | 192 | 122 | 365.6 | 8 | 32 |
| controller PE, `SIMD_EN = 0` | 2 | 2,477 | 4,954 | 0 | 10 | 377.9 | — | — |
| **mesh** | **14** | | **201,474** | **768** | **236** | | | **64** |
| capacity | | | ~350,000 | 3,072 | 672 | | | |
| **used** | | | **58 %** | **25 %** | **35 %** | | | |

Sources: `build/sweep/g-350-pad/run.log` and `build/sweep/d-350/run.log`; the
controller row is [dsp/performance](../../../arch/pe/simd/performance.md); the ~350,000 usable
LUT is that page's mesh capacity and the DSP48/BRAM denominators are **per SLR**
on this part ([ship](../../../projects/kohakutpu/ship.md)), which is the right
denominator because a mesh is placed in one.

**64 FP FMA per clock is the rendering target met exactly on width** — one
Mali-G610/G710 shader core. See [comparison](comparison.md).

### The 190,000 LUT target, which is a different budget

The LUT-shrink round was run against an internal **190,000** target for the
twelve compute PEs alone, excluding the controllers. On that basis:

```
   at the 2.500 ns ask, before RV32M   8 x 14,579  +  4 x 19,215  =  193,492
   at the 2.857 ns ask, full ISA       8 x 13,772  +  4 x 21,586  =  196,520
```

**That target is still missed, by 6,520.** The DSP side gave back 807 LUT per PE
and the GPU side spent 2,371 finishing its arithmetic tier, and the second is
larger than the first. Nothing here should be read as the 190,000
having moved — but it is worth naming that it is a *different* budget from the
~350,000 mesh capacity above, which the same mesh uses 58 % of. The shrink round
is open; the mesh fits.

### What the mesh costs at each ISA level

Every row measured at the **2.500 ns** ask, `kht_pe` top, against the SIMD PE
figure of that round (14,579 LUT). These are the **steps taken on the way to the
configuration of record**, not configurations on offer — the float tier and the
multiplier are both required, and neither operand width is removable:

| GPU build step | GPU LUT | Fmax | 12-PE mesh LUT |
|---|---:|---:|---:|
| integer only, no float tier and no multiply | 15,794 | 394.3 | 179,808 |
| + the float tier, 8 lanes, narrow operands only | 19,215 | 405.2 | 193,492 |
| + RV32M integer multiply | 19,985 | 392.2 | 196,572 |
| + the wide operand edge — the full ISA | 22,142 | 381.2 | 205,200 |

**Rows 2 and 3 no longer describe anything buildable.** They were measured on a
revision that still carried a generic to leave the wide operand edge out; that
generic is gone, both widths are the contract, and the only reason the rows
survive is that each step's cost is a controlled difference and the 2.500 ns
column is the only place those deltas exist. The integer-only row is still a real
configuration — `HAS_FLT = 0` — and is a measurement knob, not an offer.

Attempts measured and rejected so far, recorded so they are not retried:

| attempt | LUT | Fmax | verdict |
|---|---:|---:|---|
| GPU `-flatten_hierarchy none` | +566 | −27.4 | rejected, worse both ways |
| DSP `WB_STAGE = 1` at 4 float lanes | +89 | −28.3 | rejected (it *helps* the 8-lane build) |
| DSP `RECV_MEM("block")` | −97 | −5.0 | rejected, +4 BRAM for 97 LUT |

The last one is the same class of bug as the GPU's unconnected `RECV_MEM`, and
it is worth recording that it did **not** repeat: at `RECV_DEPTH = 32` a
288-bit-wide queue is what LUTRAM is for. The GPU's 3,000 LUT came from its
depth of 512, not from the primitive. `rv_pe` now carries the parameter so the
choice is explicit, and it stays `"distributed"`.

## G9 — the float tier is BUILT

**`gpu_float.s` passes at 1 wave and at 16**, bit-exact against the golden
model, `vfma` chain included. The arithmetic is the SIMD tier's — every lane is
one `khs_float_lane`, operands converted in, E8M15 through `vec_alu`'s FMA,
converted back out — so the model is the SIMD tier's `e8_fma_hw` and needed no new
emulation.

| | |
|---|---|
| instructions | `vfma`, `vfmul`, `vfadd`, `vfsub` and the `_h` forms (custom-2 funct3 = 5) |
| operand width | **per instruction**, funct7[2]: 0–3 wide, 4–7 narrow. Not a build option |
| compute format | **E8M15**, always — `vec_alu`'s, unchanged |
| lanes | `FLANES = 8`, issue interval 1 |
| latency | **15 cycles**, II = 1 |
| int → float | **no conversion instruction** — see below |

```
   FP32 or FP16 operands in  ->  E8M15 compute  ->  FP32 or FP16 out

   vfma, vfmul, vfadd, vfsub          the DEFAULT encoding
   vreg[31:0]   one FP32 element

   vfma_h, vfmul_h, vfadd_h, vfsub_h
   vreg[31:16]  element 1  RESERVED, must be written zero, reads undefined
   vreg[15:0]   element 0  one FP16 element
```

**There is no dtype knob.** `funct7[2]` reaches `kht_fpu` as `half` and drives
`wide(!half)` into `khs_float_lane`, whose `wide` is a port rather than a
parameter — that lane's header states that both input formats and the one compute
format *are the contract, not options*. What is parameterised is whether the tier
exists (`HAS_FLT`) and how many lanes it has (`FLANES`).

**The wide form is the default encoding**, which reverses the order the tier
first shipped in, and the reason is a property of the conversions rather than a
preference: `FP32 → E8M15` copies the exponent field **verbatim** and rounds off
only mantissa below bit 8, so it cannot surprise a shader with an overflow;
`FP16 → E8M15` is exact; and `E8M15 → FP16` is the one direction that is both
lossy *and* range-limited — a finite overflow saturates silently to the largest
finite FP16. The format that can only lose precision is the safer default; the
one that can lose *magnitude* is the one you ask for on purpose.

Both witnesses are in `gpu_f32.s` and neither is an arithmetic check:

| witness | why it is there |
|---|---|
| `2^100 * 2^-100 = 1.0` | in FP16 `2^100` is `+inf` and the answer is NaN, so a build that decodes the width bit and then converts narrow anyway fails **here and nowhere else** |
| `(1.0 + 1ulp) * 1.0 = 1.0` | E8M15 keeps 15 mantissa bits, so FP32's low 8 are dropped going in. Input ≠ output, on purpose |

**The width must arrive with the RESULT, not with the operands.** `y_e8`
emerges 15 cycles after launch, by which time `op` belongs to whatever the
scheduler picked next — selecting the output conversion from the live `op`
writes a narrow result into a wide destination for any wave that is not running
alone. `kht_fpu` delays the bit by `ALAT` in `hpipe`, and Vivado maps that to an
SRL.

**The SIMD tier drives `wide` low, and that is a design finding rather than an
omission.** Its float unit is an *accumulator*: a 256-bit vector register holds
8 FP32 against 16 FP16, so FSLOTS, the partial count and the fold order all
change with the operand width — and float addition does not associate, which
makes the accumulation order architectural. The GPU tier has one element per lane
either way and so pays none of that. The lane is the same module in both; the DSP
instantiates it with `.wide(1'b0)` and costs exactly what it did before the wide
edge was ever exercised.

**Reserved, not "unused".** Undefined bits become somebody's undefined
behaviour, and packed 2×FP16 later turns element 1 live *without changing the
layout* — so it becomes an opcode addition rather than a migration. `kht_fpu`
asserts the reserved half is zero on every narrow-operand float read in
simulation, and drives it to zero on the way out.

**Eight float lanes is the destination, not a stepping stone.** A mesh is 8 DSP
+ 4 SIMT PEs, and the target is one mesh ≈ one Mali-G610 shader core at 64 FP FMA
per clock:

```
   8 int + 4 float:   8x4 + 4x4 = 48     short of the target
   GPU at 8 float:    8x4 + 4x8 = 64     parity
```

Four float lanes is therefore the **reduced** configuration for area-constrained
meshes, and reaching it needs the lane/interval walk sequencer that belongs to
the DSP realm. `FLANES` has been a parameter from day one, but **it is not yet a
value change**. The parameter is the hook for that work, not the work.

> **`FLANES < LANES` returns ZERO in the upper lanes, and zero is a plausible
> float answer.** `kht_fpu`'s `g_nolane` assigns `32'd0` to every lane above
> `FLANES` because there is no walk sequencer to feed them, so a shader run on a
> reduced build gets a silently wrong result rather than a fault — and zero is a
> number a float kernel meets constantly, so no downstream check trips on it
> either. This is the **one place in this PE that breaks its own rule** that a
> build which cannot do something faults instead of answering plausibly, and it
> is guarded by convention only: `kht_fpu`'s header states that `FLANES` must
> equal `LANES` in any build that runs a shader, the configuration of record is
> `FLANES = 8`, and every shader in the suite runs against that. **Treat
> `FLANES < LANES` as an area-measurement configuration and never dispatch to
> one.**

**There is no int↔float conversion instruction, deliberately.** `vec_cvt`
carries FP16/FP32 ↔ E8M15 and nothing integer, so an `int→float` opcode would
mean inventing normalise-and-round arithmetic in the GPU realm — the fork the
tier ruling refuses. It is also not needed: a float bit pattern *is* an integer,
so a shader builds constants with `saddi` + `s2v` and reads real data out of
memory, which is where a shader's floats come from anyway.

### The 15-cycle latency, and why it is not a scoreboard

`vec_alu` is II = 1 and **latency 15**. The machine's whole argument for many
waves is that with `WAVES ≥ pipeline depth` no two in-flight instructions belong
to the same wave, so forwarding and interlocks are **deleted, not optimised**. A
15-cycle unit breaks that precondition. One bit per wave restores it:

```
   16 resident waves x 1 instruction each = a 16-cycle round trip
   vec_alu latency                        = 15 cycles
                                             ^^ covered
```

**This is not a scoreboard** — no per-register tracking, no out-of-order retire,
both of which are refused elsewhere in this machine. It is the barrel-scheduling
invariant being enforced where a long unit would otherwise violate it.

**And "covered" is occupancy-dependent, not a guarantee.** Sixteen runnable
waves give a 16-cycle round trip; at four the round trip is 4 cycles and a
dependent float waits out the remaining ~11. That degrades gracefully into the
simple-stall case, but a low-occupancy kernel will *look* like a bug unless this
is read first.

Measured, same shader, same answer:

| launched | cycles | work |
|---:|---:|---|
| 1 | 1,274 | 1× |
| 16 | 3,731 | **16×** |

16× the work for 2.9× the cycles.

## RV32M — integer multiply is BUILT

**`gpu_mul.s` passes at 1 wave and at 16.** The SIMT PE previously had no
integer multiply at all, which made every `y * width + x` a software shift-add
chain and made any LUT comparison against the SIMD PE unfair in the SIMT PE's
favour.

| | |
|---|---|
| instructions | `mul`, `mulh`, `mulhsu`, `mulhu` |
| encoding | the **existing** register-register group at funct7 = `0000001` |
| divide | **not built** — funct3 `100`–`111` stay illegal and fault |
| lanes | one 33×33 signed multiply per lane |
| latency | 15 cycles, II = 1 — the float tier's, deliberately |

**One 33×33 signed multiply serves all four forms.** Only the extension bits
differ (`a_sgn = op != 3`, `b_sgn = op == 1`), and `mul`'s low half is
extension-independent, so the sign variants are three different *readings* of
one product rather than three multipliers.

**It reuses `fpend` and adds no per-register scoreboard.** The barrel-scheduling
invariant is that with `WAVES ≥ pipeline depth` no two in-flight instructions
share a wave, so forwarding and interlocks are *deleted* rather than optimised.
A multi-cycle unit breaks that precondition; the per-wave pending bit restores
it. Giving the multiply the float tier's exact latency means it rides the
existing shadow pipe — `fsh_mul[FLAT]` picks which result retires — instead of
needing a second mechanism.

Measured cost at the 2.500 ns ask, against the 19,215 float baseline:

| | LUT | FF | BRAM | DSP48 | Fmax |
|---|---:|---:|---:|---:|---:|
| float tier, no multiply | 19,215 | 12,559 | 30.5 | 16 | 405.2 |
| **+ RV32M** | **19,985** | 12,846 | 30.5 | **48** | **392.2** |
| delta | **+770** | +287 | 0 | **+32** | **−13.0** |

**+770 LUT, less than half the ~1,600 estimated.** +32 DSP48 is 4 per lane,
which is what the full family costs; `mul` alone would need 3.

### The pad is FLOPS, and that reverses what this page used to say

The 12 stages that pad the multiplier's 3 real stages out to the float tier's 15
were first written as a shift register, which Vivado maps to SRL16Es. An earlier
revision of this section credited the small `+770` to exactly that — *"the delay
pad maps to SRLs rather than flops"*. **That is now the wrong choice and the RTL
carries `(* srl_style = "register" *)` to refuse it**, because an SRL16E costs
**one LUT per bit at any depth** and this PE is LUT-bound, while the flip-flop
half of the CLB is idle. Measured at the 2.857 ns ask:

| pad form | LUT | of which SRL | FF | Fmax |
|---|---:|---:|---:|---:|
| shift register (SRL) | 21,842 | 436 | 13,939 | 365.6 |
| **flops** (built) | **21,586** | 180 | 17,268 | **365.6** |
| delta | **−256** | −256 | **+3,329** | **0.0** |

**−256 LUT for +3,329 FF at an identical Fmax** — 8 lanes × 12 stages × 32 bits
is 3,072 of those flops, and they go where nothing else wanted to sit.

At the **2.500 ns** ask the same change instead read as −15.6 MHz. That was an
artifact of asking for timing the design was not going to meet, not a property of
the pad — which is the reason this table names its ask twice.

**The CPU PE was investigated and not changed**, per the owner's instruction.

## Read this before quoting a frequency

**Name the top, and name the ask.** The frequency campaign, the mesh table and
the configuration of record are all `kht_pe`; every row under
[what is measured](#what-is-measured) and every G0–G8 ladder row is `kht_unit` or
`kht_lds`, and **a `kht_unit` figure is not a frequency claim for this PE.**

That rule was learned the expensive way. The ladder's top is the SIMT unit, and
synthesising the whole `kht_core` for the first time — to measure G7 — found the
core at **71.7 MHz** while the unit it contains closed at 324.

The binding path was the cross-lane reduction: written as a sequential loop it
is LANES chained 32-bit operations, and the report named it exactly — **44 logic
levels, 9 CARRY8**, `vt_rd1 → red_r → sfile`. Rebuilt as a balanced tree
(log2(LANES) deep), which every shader still passes.

| `kht_core`, WAVES = 1 | LUT | FF | Fmax |
|---|---:|---:|---:|
| the reduction as a serial chain | 7,899 | 865 | **71.7 MHz** |
| as a balanced tree | 8,498 | 865 | 154.1 MHz |
| **as a pipelined tree** (built) | **7,754** | 1,094 | **277.9 MHz** |

The pipelined form is **cheaper in LUT than the chain it replaced** — registering
each level breaks the long combinational cone, so the tool packs simpler logic —
at +229 FF and **3.9× the clock**. A `redux` now takes `log2(LANES)` extra
cycles, which is free in practice: it is a rare instruction and it already
stalled for its operand.

The lesson is about the method, not the adder: **a ladder whose top is one
submodule cannot see a path that leaves it.** G0–G4 and G8 were all measured on
`kht_unit`, and the reductions live in `kht_core`, so no row on this page could
ever have caught it.

## The frequency campaign

Every fix below was found by **reading the reported critical path**, never by
guessing, and each is followed by the path that replaced it. All figures are
`kht_pe`, 8 lanes / 16 waves, OOC synth at 3.333 ns.

| # | change | LUT | FF | Fmax | slack | source |
|---:|---|---:|---:|---:|---:|---|
| 0 | baseline | 16,115 | 7,342 | 182.0 | — | `build/sweep/pe-l8-w16` |
| — | branch zero-shadow as a **separate flop array** | 16,817 | — | 178.3 | — | **rejected** |
| 1 | `l1_addr` registered, 3-phase LSU walk, LDS decision + addresses registered | 16,278 | 7,469 | **209.2** | −1.446 | `diag-l8-w16` |
| 2 | hazard **compare-then-mux**, 33rd `sfile` bit deleted | 16,269 | 7,457 | **220.3** | −1.207 | `flt-none` |
| 3 | *(no RTL change)* `-flatten_hierarchy rebuilt` | 16,650 | 7,222 | **230.2** | −1.011 | `flt-rebuilt` |
| 4 | **predecode into the instruction memory** | 16,981 | 7,241 | **237.0** | −0.886 | `pd-rebuilt` |
| — | scalar writeback stage **+ a `sres == 0` zero flag** | 16,680 | 7,266 | 213.8 | −1.345 | **the flag rejected** |
| 5 | **scalar writeback stage**, zero flag deleted | 16,911 | 7,282 | **260.1** | −0.511 | `swb2` |
| 6 | one scalar ALU; `f2_wave` replicated; two-stage `all_lds` | 16,353 | 7,332 | **267.7** | −0.403 | `salu` |
| 7 | branch zero flag stored from the **writeback register**; `warm_stall` compares first | 16,347 | 7,343 | 253.7 | −0.608 | `pe-zflag-r` |
| 8 | **`hold` factored out of the PC update**; `hold` replicated | 16,436 | 7,322 | 263.2 | −0.467 | `pe-nxt` |
| 9 | scalar ALU split into **four parallel classes**; halt FSM hold-factored | 16,354 | 7,333 | **280.0** | −0.238 | `pe-alu` |
| 10 | hazard split; halt FSM hold-factored | 16,248 | 7,339 | 281.3 | −0.222 | `pe-hz` |
| 11 | **operand register in front of the scalar ALU**, distance-1 interlock | 16,195 | 7,411 | 279.3 | −0.248 | `pe-opreg` |
| 12 | **the instruction registered in fabric** (three-stage fetch) | **16,018** | 7,602 | **301.7** | **+0.018** | `pe-f2r` |
| 13 | **writeback stage in `kht_unit`** — the lane ALU off a flop | **15,719** | 8,442 | **307.3** | +0.079 | `pe-vwb` |
| 14 | every lane's address registered; reduction **leaves** registered | 16,026 | 8,593 | **335.8** | — | `pe-eaall-400` |
| 15 | *(rejected)* replicate the round-robin pick | 15,697 | 8,850 | 333.8 | — | a wash |
| 16 | **the round-robin pick registered** | 15,665 | 8,843 | 337.6 | — | `pe-curq-400` |
| 17 | **`lane_on` and the store data registered** in the walk's phase 0 | 15,637 | 8,892 | **387.4** | −0.081 | `pe-lon-400` |
| 18 | `rs1`/`rs2`/`hold` replicated at 16 | **15,638** | 9,021 | **392.8** | −0.046 | `pe-fan-400` |
| 19 | **G9 lands**; the PC-advance and re-commit fixes it forced | **15,794** | 9,016 | **394.3** | −0.036 | `pe-int-final` |
| **19f** | **the same build with 8 FLOAT LANES** | **22,215** | 14,962 | **405.2** | **+0.032** | `pe-flt8b-400` |
| 20 | `RECV_MEM("block")` + the CU_DATA **word write registered** | 19,215 | 12,559 | 405.2 | — | the LUT-shrink round |
| 21 | **RV32M** | 19,985 | 12,846 | 392.2 | — | — |
| 22 | **the wide operand edge** — the full ISA | 22,142 | — | 381.2 | — | — |
| **23** | **the ask moves to 2.857; the multiplier pad becomes FLOPS** | **21,586** | **17,268** | **365.6** | **+0.122** | `build/sweep/g-350-pad` |

**Rows 0–13 are at 3.333 ns, 14–22 at 2.500, and row 23 at 2.857** — the ask
moved twice. Rows 12 and 13 were each measured at 3.333 *and* 2.500 and reported
an identical Fmax, which is what let the earlier part of the table be read as one
sequence; a tighter constraint bought tens of LUT and no megahertz, which is what
a structural ceiling looks like. **That equivalence does not extend to row 23**:
the same design reports 381.2 at 2.500 and 365.6 at 2.857, so the last row is not
a regression from the one above it, it is a different question being asked.

**182.0 → 394.3 MHz — 2.17× — for 321 LUT LESS than the baseline.** The design
is smaller as well as twice as fast, because breaking long combinational cones
lets the tool pack simpler logic; the only thing that grew is flip-flops
(7,342 → 9,016), which is what the registers that did the work cost.

**And the FLOAT build was the faster of the two.** At 8 float lanes it closed
**405.2 MHz with POSITIVE slack** against a 2.500 ns ask — both rows at the same
ask, so the comparison is a real one:

| build, both at 2.500 ns | LUT | FF | BRAM | DSP | Fmax |
|---|---:|---:|---:|---:|---:|
| integer only | 15,794 | 9,016 | 22 | 0 | 394.3 |
| **+ 8 float lanes** | **22,215** | 14,962 | 26.5 | **16** | **405.2** |
| the float tier costs | **+6,421** | +5,946 | +4.5 | +16 | **+10.9** |

**Float cost zero megahertz.** That is not luck and it is not noise: the float
instructions redirect their own wave and defer their commit, which breaks up
exactly the cones the integer campaign spent nineteen rows shortening. The area
is within 1 % of the estimate; the **DSP count is 16, not the 24 estimated** —
`vec_alu` maps to 2 DSP48E2 per lane here, not 3, which is why that column was
flagged as wanting measurement rather than trust.

The two things that *did* cost clock are the two that came after: RV32M is
−13.0 MHz and the wide operand edge −11.0, both at 2.500 ns and both bought
deliberately. The full ISA still clears the ask.

Rows 0–2 are
`-flatten_hierarchy none`; 3 onward are `rebuilt`, and row 3 measures that knob
alone on unchanged RTL — **+9.9 MHz for +381 LUT**, which is why it is a row and
not a footnote.

**Rows 7 and 8 read as a step backwards on Fmax and are not.** They traded a
broad shallow problem for one deep one: the number of failing endpoints went
**1,633 → 70**, and the whole front-end cone family — both 512-endpoint per-wave
PC groups and all four instruction-window write-address groups — disappeared.
Row 9 then collected the win, because with the front end clear there was one
cone left to aim at instead of five.

Shapes other than 8×16 were last measured at row 1 and are **stale**:

| `kht_pe` | baseline | row 1 |
|---|---:|---:|
| 8 lanes / 16 waves | 182.0 | *(now **267.7**)* |
| 8 lanes / 1 wave | 190.5 | 219.4 MHz |
| 4 lanes / 16 waves | 174.9 | 221.2 MHz |
| 16 lanes / 16 waves | 170.9 | 205.4 MHz |

### The structural cause, and the fix that addressed it

`kht_core` had **no decode stage**: `wire instr = imem_data`, and then decode,
operand read, address generation, fault and the PC update all happened in one
cycle. The base core splits `rv_if`/`rv_id`/`rv_ex` and runs at 410 MHz; the DSP
PE is 357.1. Every binding path found starts at `u_imem` and ends at a **control**
register — never a datapath one.

**Row 4 is the answer to that, and it costs no cycle.** Every decode signal is a
pure function of the instruction word, so it is computed **once on the write
path** as the shader image lands and stored in a second memory beside the
instruction. `kht_predec.v` produces a **60-bit** control word — it was 50 before
the float tier and RV32M each claimed bits, and `kht_ctrl.vh` is included by both
the producer and the consumer so the two cannot disagree about the layout;
`kht_core` reads control out of memory instead of computing it between the window
and its own registers. Decode-stage timing, zero added latency, and no
branch-latency cost — which a real decode stage would have had.

**Row 5 is the read-modify-write split**, and **row 11 finished it.** A census
found 1,728 of 1,833 failing endpoints in two cones, both hanging off the scalar
file's read: `sfile → ALU → sfile write`, and `sfile → sv1==0 → redirect → PC`.
Row 5 moved the write a cycle later and forwarded it. Row 11 then put the
**operand register in front of the ALU instead of behind it** and **deleted the
forwarding entirely**: at distance 1 the result does not exist yet in either
arrangement, so the scalar half interlocks (`s_hz`) and distance 2 needs nothing
at all, because the file was written at the end of the previous cycle. The
forward comparator that deleted was the late input to the L1 tag cone.

### Three things that were measured and rejected

- **A zero flag on the branch, twice.** As a separate `reg [WAVES*32-1:0]` array
  it cost +702 LUT, +512 FF and *lost* 3.7 MHz — the indexed-flop-array
  anti-pattern this project has now paid for four times. Computed as `sres == 0`
  after the ALU it was one endpoint at 17 levels and −1.345, worth **−23 MHz**.
  A stored flag only pays when it is computed from something already registered.
- **`-flatten_hierarchy none`** costs 9.9 MHz. It is kept for the *ladder* rows,
  where cross-boundary duplication would make the per-gate deltas
  unattributable, and dropped for the assembled PE.

### Where it stands against the target

**It clears it.** At the 2.857 ns ask the full-ISA PE closes with **+0.122 ns of
slack**, so there is no failing cone to report.

The three below are what remained on the integer-only build at the tighter
**2.500 ns** ask, worth 0.046 ns. They are kept because they name where the
structural ceiling is, not because anything currently fails:

| endpoints | worst | levels | cone |
|---:|---:|---:|---|
| 1 | −0.046 | 10 | `i2 → rs1 → hazard → hold → go → live/D` |
| 32 | −0.034 | 8 | the CU_DATA **image-load** path into the LDS banks |
| 2 | −0.016 | 9 | `i2 → … → hst/R` |

The 32-endpoint one is not the execution path at all — it is the burst that
writes a shader image into the banked LDS, which runs once per dispatch.

### The whole campaign in one sentence

**Every fix that mattered was the same fix.** A cone that starts at a block RAM
begins 0.85–0.91 ns in debt on a budget of 2.5 ns, so the work is to make cones
start at flip-flops, and to stop putting `hold` in series with decisions it only
needs to gate:

| what | where it was | what it became | worth |
|---|---|---|---|
| decode | between the window and the control registers | on the memory's **write** path (`kht_predec`) | +55 MHz |
| the instruction itself | a block RAM output | a **fabric register** (`i2`/`c2`) | +22 MHz |
| the scalar ALU | after the file read, same cycle | **before** it, operands registered | — |
| the lane ALU | straight off `kht_vregfile` | behind `w1_q`/`w2_q` | +6 MHz |
| every lane's address | indexed off the vector file per lane | computed once into `ea_all_q` | +28 MHz |
| the round-robin pick | 5 LUT levels into the window's address | a **register** | +4 MHz |
| `lane_on` | a 16:1 mux into `l1_req` → `l1_stall` → `hold` | registered in walk phase 0 | **+50 MHz** |
| `hold` | inside `go`, three LUTs deep | one AND at the clock enable | — |
| the branch's zero test | a 32-bit reduce on the read | a **stored bit** in the file | — |

Three memories each cost the same 0.85–0.91 ns and each needed the same answer:
the instruction window, the scalar file, and the vector register file. The
largest single win — 50 MHz — was one register on a one-bit signal, because
`mask` reached rv_l1's `stall` and `stall` reaches every clock enable in the
core.

## The assembled PE across shapes — a BASELINE, long superseded

The whole unit — core, windows, banked LDS, L1, requestor, fabric port.
`python scripts/py/ooc_sweep.py gpu-pe`, source `build/sweep_gpu-pe.md`,
**3.333 ns ask, integer-only lane array, G8 on**:

| LANES | WAVES | LUT | FF | BRAM | ctrl sets | Fmax |
|---:|---:|---:|---:|---:|---:|---:|
| 4 | 16 | 11,487 | 6,595 | 11 | 154 | 174.9 MHz |
| **8** | **16** | **16,115** | 7,342 | 19 | 162 | **182.0 MHz** |
| 8 | 1 | 14,709 | 6,642 | 19 | 106 | 190.5 MHz |
| 16 | 16 | 29,961 | 8,834 | 35 | 178 | 170.9 MHz |

**None of these rows describes the machine.** The 8×16 row is the campaign's
starting point — 182.0 MHz, integer-only, no multiplier — and the shipped PE at
that shape is now 21,586 LUT at 365.6 MHz with the whole arithmetic tier in it.
The other three shapes have never been re-swept, so they are the only figures
that exist for 4 and 16 lanes and they carry a float-free, multiply-free lane
array.

What the table still says that no submodule figure could:

- **16,115 LUT at 8 lanes**, against the ~13,600 estimated by adding submodules.
  Integration cost 2,515 LUT — 18 % — which is why the assembly has to be
  measured rather than summed.
- **182 MHz, not the 279.5 `kht_core` reached.** Diagnosed since: the core had no
  decode stage, so the window's output reached a control register through the
  whole decode. See [the frequency campaign](#the-frequency-campaign).
- **16 lanes is 29,961 LUT** — nearly double 8 lanes, on the integer tier alone.
  It reinforces what
  [G4's quadratic resolver](#g4---kht_lds-the-first-gate-that-is-not-free)
  already said: 8 is the shape this design is for.

## Read this before quoting a LUT figure

**Name the ISA level and name the ask.** Both moved during this work and a figure
without them is not a result. The rule that outlived every revision of this
section:

> **No G8 total is quotable without naming which lane array its G0 stands on.**

An integer-only figure and a float-capable figure are different machines, and the
same now applies to a PE with and without RV32M.

| SIMT PE at 8 threads / 16 waves | LUT | DSP48 | ask |
|---|---:|---:|---:|
| integer only, no float tier and no multiply | 15,794 | 0 | 2.500 ns |
| + the float tier, 8 lanes, narrow operands only | 19,215 | 16 | 2.500 ns |
| + RV32M integer multiply | 19,985 | 48 | 2.500 ns |
| + the wide operand edge — the full ISA | 22,142 | 48 | 2.500 ns |
| **the same full ISA, flop pad — OF RECORD** | **21,586** | **48** | **2.857 ns** |

**Every row is a measurement.** They replace every earlier estimate on this page,
including the ~13,600 submodule build-up, the "~21k for 4 float lanes" figure,
the "~22k for a float-capable PE" estimate, and the 22,215 that preceded the
`RECV_MEM` fix.

**21,586 LUT for the full ISA is inside the 25k band and clear of the 30k review
line**, and it is the configuration the rendering target actually asks for rather
than a compromise toward it. Note the coincidence worth not being fooled by: the
pre-shrink build was 22,215 with neither the multiplier nor the wide operand
edge, so the PE has arrived back *below* where it started while gaining the
`RECV_MEM` fix, integer multiply, the wide edge and the flop pad.

For scale, the DSP class's 8-int + 4-float point is **13,772 LUT, measured** at
the same 2.857 ns ask. It was 14,579 a round earlier and 15,119 DERIVED before
that.

### Arithmetic lanes are a separate purchase from thread count

A wave of 8 threads does not need 8 of every functional unit — it needs some, and
the issue interval is `threads / lanes`:

```
   8 threads over 8 float lanes  ->  1 cycle per float instruction   <- BUILT
   8 threads over 4 float lanes  ->  2 cycles per float instruction
   8 threads over 2 float lanes  ->  4 cycles per float instruction
```

**The built configuration is interval 1**, so no walk sequencer exists and none
is needed. `FLANES` is a parameter, but `FLANES < LANES` is **not a working
configuration**: `kht_fpu` ties the lanes above `FLANES` to zero rather than
sequencing them, so a reduced build would silently return zeros in the upper
lanes. Reaching one needs the walk sequencer, which is ruled to the **DSP realm
and instantiated here, never forked** — the same rule the arithmetic itself
follows. `kht_valu` builds one integer ALU per thread unconditionally, and
`ILANES ≤ LANES` on the integer side is the same unbuilt mechanism.

**The LUT-shrink pass has happened on the GPU side** and is what the `RECV_MEM`,
registered-word-write and flop-pad rows above record. The DSP side was attacked
and did not yield: two structural attempts (a 5-way result mux, an explicit
8-way `khs_perm` select) both made it **larger**, and both are recorded in the
RTL with the measured regression so they are not retried.

## The headline

The SIMT PE **runs shaders end to end on the machine bench** — through the real
L1, the real memory agent and an AXI RAM — and the resulting DRAM matches the
golden model exactly. **Divergence works on hardware**, not only in the model.

| Shader | Exercises | Result |
|---|---|---|
| `gpu_smoke.s` | kick argument, `vlaneid`, RV32I per lane, lane-linear store | **PASS**, 4 checks |
| `gpu_diverge.s` | `split`/`join`: odd and even lanes take different paths and reconverge | **PASS**, 4 checks |
| `gpu_nested.s` | a split inside a split — two stack pairs, the phase bit toggling at both levels | **PASS**, 4 checks |
| `gpu_gather.s` | a **real gather**: `s2v`, per-lane base `lw` across five 32-byte lines, six fills | **PASS**, 4 checks |
| `gpu_isa.s` | **execution coverage**: 29 instruction results, every scalar ALU form, every subgroup path, reductions under a non-trivial mask and over negative data | **PASS**, 4 checks |
| `gpu_lds.s` | **G4**: the banked LDS at both ends of its range — conflict-free, reversed (crossbar), and every lane on one bank | **PASS**, 4 checks |
| `gpu_shfl.s` | **G8**: the butterfly — every stage exercised in turn, full reversal, `bcast`, and a lane whose source is masked off | **PASS**, 4 checks |
| `gpu_waves.s` | **G7**: a real dispatch — every wave writes its own slice, 1 to 16 waves | **PASS**, 4 checks |
| `gpu_chain.s` | **G7's witness**: a 20-deep dependency chain, where interleaving actually pays | **PASS**, 4 checks |
| `gpu_fault.s` | the **region fault**: a per-lane access to an unmapped region halts with cause 3 at the faulting PC | **PASS**, 4 checks |
| `gpu_float.s` | **G9**: the float tier on narrow operands, `vfma` chain included, at **1 wave and at 16** — one wave is the *worst* case, because with nothing else runnable the 15-cycle latency is exposed rather than hidden | **PASS**, 4 checks |
| `gpu_f32.s` | the **wide-operand format witnesses**, at 1 wave and at 16 | **PASS**, 4 checks |
| `gpu_mul.s` | **RV32M**: the sign corners — `mulh`, `mulhu` and `mulhsu` are three different high halves of the same two bit patterns — at 1 wave and at 16 | **PASS**, 4 checks |

The whole set runs from one command, which is the only way to get one verdict:

```
python tests/pe/tools/rv_simt_suite.py --gates
```

**It is serial by construction**, because `xsim` names its build directory after
the bench and two concurrent runs of `kht_sys` destroy each other's work area —
which surfaces as a random shader failing, or as a `PermissionError` on
`build/xsim_kht_sys`, rather than as a collision.

```
--- one shader through SIMT PE + router + MAG + RAM, 8 lanes x 16 waves ---
    halt word      00000055   (model 00000055)
    halt cause     1          (model 1)
    kick to done   475 cycles
    memory         8 request(s) over 1 gather(s)
  PASS -- 4 checks, 0 errors
```

The ladder measures what the IPDOM stack **costs**; only `gpu_nested.s` proves
the split pushed a pair, the first join took the false half, the second took the
outer mask, and the pointer came back to zero.

`8 requests over 1 gather` is the coalescer witness reading its **pre-coalescer**
value. The LSU serialises lanes today, so the ratio is LANES by construction and
is expected to **fall** when the coalescer lands. It is counted now, before the
optimisation exists, so the improvement is a measured change rather than a
number that appears from nothing.

`gpu_gather.s` is the case the ratio will actually be judged on. Lane *i* reads
word 5*i*, so eight lanes fall on **five** distinct 32-byte lines:

| | today | with G5 |
|---|---:|---:|
| requests for the gather | 8 | **5** |
| requests for the store | 8 | 1 |
| reported | `16 request(s) over 2 gather(s)` | `6 over 2` |

Run it before and after; the bench line is the whole measurement.

## What exists

### G7 is built: waves now ISSUE, not just exist

`WAVES` used to size storage and nothing else — one fetch pointer, one wave ever
live. It no longer does. Each wave has its own pointer, round-robin picks a live
wave every cycle, and the hazard compares waves so a dependency between
*different* waves does not exist.

**The kick's `op` field is the wave count**, clamped to what the build carries.
`op = 1` is the single-wave case it always was, so this generalises the field
rather than redefining it and a launch needs no new protocol word.

`gpu_waves.s` runs a real dispatch — wave *w* writes 32 bytes at `base + 32w`,
so the image is right only if every wave ran, none collided, and none was killed
by another's `ecall`:

| launched | 1 | 2 | 4 | 8 | 16 |
|---|---:|---:|---:|---:|---:|
| cycles | 475 | 596 | 842 | 1,334 | 2,318 |

**The witness is `gpu_chain.s`**, because `gpu_waves.s` cannot show what
interleaving buys — it is memory-bound on the serial LSU. A 20-deep dependency
chain stalls every instruction with one wave and none with two:

| | solo wave | with ≥2 waves |
|---|---:|---:|
| marginal cost of the 21-instruction chain | 40 cycles | 19 cycles |
| per instruction | **1.90** | **0.90** |

**2.1× on the dependent section**, against a theoretical 2.0× for removing a
one-cycle distance-1 hazard. The bubbles fill exactly as the design says they
should.

**What G7 does not do is hide memory latency.** A stalled wave still holds the
whole front end — there is one instruction in flight, and a miss blocks it. That
is G6's work, and it is why `gpu_waves.s` scales linearly at ~123 cycles a wave.

### G4 is built and works

The LDS is **banked, LANES ways, word-interleaved**, with the conflict resolver
inside it. `gpu_lds.s` proves both ends of the range on hardware, and the
request count is the witness:

| Access | Banks touched | Passes |
|---|---|---|
| lane *i* → word *i* (conflict-free) | 8 distinct | **1** |
| lane *i* → word *7−i* (reversed) | 8 distinct | **1** |
| lane *i* → word 8*i* (worst case) | all bank 0 | **8** |

**The same shader, the same correct answer, the gate off and on:**

| `HAS_LDSBANK` | requests | gathers | result |
|---:|---:|---:|---|
| 0 (serial walk) | **48** | 6 | PASS |
| 1 (banked) | **34** | 6 | PASS |

34 is `1 + 1 + 8 + 8 + 8 + 8` exactly; 48 is `8 × 6`. That is the witness as a
**controlled difference** — one parameter, one shader, one number — rather than
an argument about what a resolver ought to do.

**The reversed case is the one that proves the return crossbar**: lane 0 must
take bank 7's word rather than always taking bank 0.

```
python tests/pe/tools/rv_simt_run.py tests/pe/prog/gpu_lds.s --arg 0x80000000 --dram zero
python tests/pe/tools/rv_simt_run.py tests/pe/prog/gpu_lds.s --arg 0x80000000 --dram zero -d KHT_LDSBANK=0
```

### RTL — `src/kohakumpe/simt/`

| File | What it is | State |
|---|---|---|
| `kht_valu.v` | the per-lane **integer** ALU array, 10 RV32I operations. It has no multiplier — that is `kht_imul`, beside it, not inside it | built, exercised by the bench |
| `kht_imul.v` | **RV32M**: one 33×33 signed multiply per lane serving `mul`/`mulh`/`mulhsu`/`mulhu`, padded to the float tier's 15 cycles | built, `gpu_mul.s` |
| `kht_fpu.v` | **G9**: `FLANES` float lanes, each one `khs_float_lane`. Selects operands, drives the per-instruction `wide` bit and converts the result back; the arithmetic is the SIMD tier's | built, `gpu_float.s` / `gpu_f32.s` |
| `kht_vregfile.v` | the wave-indexed per-thread register file, `PRIM` selectable, **`RD3` third read port at `HAS_FLT`** for `vfma`'s addend | built, both primitives measured |
| `kht_unit.v` | the SIMT half: register file, active mask, IPDOM stack, lane array, writeback, and the **shadow pipe both multi-cycle units retire through** | built, **carries G0–G3 and G9 as parameters** |
| `kht_lds.v` | **G4**: the banked shared memory, its conflict resolver and its sequencer | built, exercised at both ends of its range |
| `kht_predec.v` | decode, moved onto the image-load **write** path — a **60-bit** control word per instruction | built, +55 MHz |
| `kht_ctrl.vh` | the control word's bit layout, included by both the producer and the consumer | built, `KHT_CW = 60` |
| `kht_core.v` | the pipeline: per-wave PCs, the scalar half + its writeback stage, the lane-serialising LSU, `fpend`, halt-and-flush | built, runs a shader |
| `kht_pe.v` | the unit: window, **predecode window**, kick, completion, L1, fabric port | built, runs a shader |
| `generated/kht_isa.vh` | the decode header | **generated** from the field table; a hand edit fails a test |

**`HAS_FLT` defaults to 0 in all three of `kht_pe`, `kht_core` and `kht_unit`.**
A build that does not pass it gets the integer-only machine — and, because the
multiplier shares the float tier's retire slot, **no RV32M either**: `is_imul` is
`ictl[C_IMUL] && (HAS_FLT != 0)`, so `mul` faults at `HAS_FLT = 0` rather than
falling through to the ordinary ALU. Every figure of record on this page is at
`HAS_FLT = 1, FLANES = 8`.

### Toolchain — `tests/pe/tools/`

| File | What it is | State |
|---|---|---|
| `rv_simt_isa.py` | **the field table** — the single source for all four consumers | **106 instructions** (98 custom-2, 8 custom-3) |
| `rv_simt_model.py` | the golden SIMT model: waves, masks, IPDOM, coalescing, float, RV32M | built |
| `rv_simt_emit.py` | renders the RTL decode header; `--check` fails on drift | built |
| `rv_simt_asm.py` | the assembler extension and the disassembler | built |
| `rv_simt_isa_test.py` | encode/decode round-trip over the whole table | **427 checks, 0 errors** |
| `rv_simt_check.py` | the model's own behaviour: divergence, coalescing, subgroup ops | **32 checks, 0 errors** |
| `rv_simt_run.py` | assemble → model → image → `xsim` → compare | built |
| `rv_simt_suite.py` | **every shader through the bench, one verdict** — 19 cases, serial by construction | built |

RV32M is **not** in `rv_simt_isa.py` and should not be: `mul` and its three high
halves are ordinary RISC-V, so they live in the base assembler `rv_asm.py`
alongside every other RV32I opcode. That is what "no new opcode major was spent"
looks like in the toolchain.

### Bench — `tests/pe/tb/`

| File | What it is |
|---|---|
| `kht_mesh.v` | one router, the SIMT PE, a memory agent, an AXI RAM |
| `kht_sys_tb.v` | image-driven: **any** shader the toolchain emits runs without editing it |
| `tests/pe/prog/gpu_smoke.s` | lane-linear store of `lane*7+1` |
| `tests/pe/prog/gpu_diverge.s` | one divergent if/else, reconverged |
| `tests/pe/prog/gpu_nested.s` | two nested divergent levels |
| `tests/pe/prog/gpu_gather.s` | a per-lane-base gather over five lines — the coalescer's witness case |
| `tests/pe/prog/gpu_isa.s` | execution coverage: 29 results, one per instruction form |
| `tests/pe/prog/gpu_lds.s` | the banked LDS, conflict-free and worst case |
| `tests/pe/prog/gpu_shfl.s` | the G8 butterfly, every stage, and a masked-off source |
| `tests/pe/prog/gpu_waves.s` | a real dispatch, 1 to 16 waves |
| `tests/pe/prog/gpu_chain.s` | a 20-deep dependency chain — G7's witness |
| `tests/pe/prog/gpu_fault.s` | the region fault |
| `tests/pe/prog/gpu_float.s` | the float tier on narrow operands, and a `vfma` chain |
| `tests/pe/prog/gpu_f32.s` | the two wide-operand format witnesses |
| `tests/pe/prog/gpu_mul.s` | RV32M and its three sign corners |

### Build — synthesis

| File | What it is |
|---|---|
| `scripts/tcl/ooc_simt_pe.tcl` | one script measures every ladder gate, because the gates are generics on one module |
| `scripts/xdc/ooc_khg.xdc` | the OOC clock — **unconditional**, and the script errors if `get_clocks` comes back empty |

## What is measured

The area ladder, G0 through G3 — see [ladder](ladder.md) for the method and the
reading. All rows: `kht_unit`, 8 lanes, `xcvu13p-fhgb2104-2L-e`, OOC synthesis
at 3.333 ns, `VREG_PRIM = block`.

**Source: `build/sweep_gpu-ladder.md`**, written by
`python scripts/py/ooc_sweep.py gpu-ladder`. Measured 2026-08-22.

| Gate | WAVES | mask | ipdom | LUT | ΔLUT | FF | BRAM | ctrl sets | Fmax |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| G0 the arithmetic substrate | 1 | 0 | 0 | 2,952 | — | 307 | 8 | 2 | 324.1 MHz |
| G1 wave-indexed storage | 16 | 0 | 0 | 2,952 | **+0** | 311 | 8 | 2 | 324.1 MHz |
| G2 active masks, `tmc` | 16 | 1 | 0 | 3,016 | +64 | 447 | 8 | 18 | 324.1 MHz |
| G3 `split`/`join`, bounded stack | 16 | 1 | 1 | 3,204 | +188 | 516 | 8 | 36 | 324.1 MHz |

**G7 — the scheduler.** `WAVES` on `kht_core` minus the same sweep on
`kht_unit`, sources `build/sweep_gpu-sched.md` and `build/sweep_gpu-waves.md`:

| WAVES 1 → 16 | LUT | FF |
|---|---:|---:|
| storage (`kht_unit`, mask + IPDOM on) | +124 | +184 |
| **scheduling** (`kht_core` − `kht_unit`) | **+1,775** | +519 |

Core Fmax does not trend with WAVES (263–295 MHz, noise). Note this **refines**
G1's +0 rather than contradicting it: G1 measured `WAVES` with mask and IPDOM
*off*, where the register file is a memory and a wave id is address bits. Turn
them on and the per-wave mask and stack arrays cost +124.

Three results worth stating plainly:

- **G1 costs +0 LUT, +0 BRAM and +0 control sets for sixteen waves** — four
  flops. Wave contexts are free in logic because the register file was already a
  memory and the wave id is address bits, not storage.
- **Fmax never moves.** All four gates land at 324.1 MHz. Every SIMT gate built
  so far is off the binding path.
- **The whole SIMT machinery so far is +252 LUT** on a 2,952-LUT substrate.

**G8 — the subgroup butterfly.** Source `build/sweep_gpu-shfl.md`, `HAS_SHFL`
off against on with everything else held:

| LANES | off | on | ΔLUT | ΔFmax |
|---:|---:|---:|---:|---:|
| 4 | 1,659 | 1,927 | +268 | −21.8 MHz |
| 8 | 3,204 | 4,139 | +935 | **−38.6 MHz** |
| 16 | 6,355 | 9,374 | +3,019 | −20.3 MHz |
| 32 | 12,478 | 18,478 | +6,000 | −22.4 MHz |

```
ΔLUT ~= 39 x LANES x log2(LANES)     -- one 32-bit mux per lane per stage
```

The `off` column is **bit-identical** to the independently-measured lane-scaling
table below, which is the proof that a gate switched off elaborates nothing.

**Against G4, the same widths, both cross-lane networks:** 1.9× / 1.7× / 2.1× /
**4.3×**. Per doubling, G8 grows 2–3.5× and G4 approaches 4×. That divergence is
the complexity class becoming visible, and it is the argument for a staged
conflict resolver over an all-to-all one if wide lanes are ever wanted —
see [ladder](ladder.md#against-g4-at-the-same-widths).

G8 is the second gate to cost clock, worst at 8 lanes. Unlike G4 it does **not**
get worse with width: its depth grows as log while everything it competes with
grows faster.

**G4 — `kht_lds`, the first gate that is not free.** Source
`build/sweep_gpu-lds.md`:

| LANES | LUT | FF | BRAM | Fmax |
|---:|---:|---:|---:|---:|
| 4 | 509 | 319 | 4 | 643.1 MHz |
| 8 | 1,633 | 603 | 8 | 514.9 MHz |
| 16 | 6,194 | 1,171 | 16 | **339.2 MHz** |
| 32 | 25,961 | 2,307 | 32 | **317.7 MHz** |

```
LUT ~= 25 x LANES^2      -- the resolver is a LANES x LANES comparison
```

Two things follow, and both are new:

- **The area is quadratic.** Every doubling of LANES costs about four times as
  much. Flops and BRAM stay linear; only the resolver goes as the square.
- **It is the first gate to touch the clock.** G0–G3 all sat at 324.1 MHz and
  none moved it. At 32 lanes this block runs at **317.7 MHz — below the rest of
  the unit** — so the resolver becomes the binding path. The flat-Fmax caveat on
  the lane-scaling table said it would not survive a cross-lane network; this is
  the first one, and it did not.

**32 lanes is ruled out**: `kht_unit` + `kht_lds` alone is 38,439 LUT, past the
35k ceiling before the core, L1, requestor, port or any unbuilt gate. 8 lanes is
4,837; 16 is 12,549. Wanting 16+ means a cheaper resolver, not a bigger budget.

Register-file primitive at the full gate set — source
`build/sweep_gpu-vregprim.md`:

| `VREG_PRIM` | LUT | BRAM | Fmax |
|---|---:|---:|---:|
| `block` (default) | 3,204 | 8 | 324.1 MHz |
| `distributed` | 9,430 | 0 | 475.3 MHz |

`distributed` costs **+6,226 LUT** to buy megahertz the design does not need.

Lane scaling at the full gate set — source `build/sweep_gpu-lanes.md`:

| LANES | LUT | FF | BRAM | ctrl sets | Fmax |
|---:|---:|---:|---:|---:|---:|
| 4 | 1,659 | 316 | 4 | 36 | 324.1 MHz |
| 8 | 3,204 | 516 | 8 | 36 | 324.1 MHz |
| 16 | 6,355 | 916 | 16 | 36 | 324.0 MHz |
| 32 | 12,478 | 1,716 | 32 | 36 | 324.1 MHz |

```
LUT = 112 + 386.4 x LANES        FF = 116 + 50 x LANES
```

**Control sets are 36 at every width**, which is the active-mask design showing
up as a measurement. **Fmax is flat at 324 MHz** — but only because no
cross-lane network exists yet; G8's butterfly is what would change that, so do
not extrapolate this row past it.

## Not built yet

Nothing below has been built or measured. It is listed so the gap between the
plan and the machine is visible rather than implied.

| Gate | What it adds |
|---|---|
| G5 | the coalescer — one gather becomes one request when the lanes agree |
| G6 | MSHRs — more than one miss in flight |

**G5 needs a memory path that does not exist yet, and this is the open design
question.** The model, this ISA and the bench witness all define coalescing at
**32-byte line** granularity — but `rv_l1`'s CPU-side read port is 32 bits, and
that is a deliberate decision in a shared component:

> Lines are walked as eight 32-bit words on the array's second port … eight
> cycles against a DRAM round trip is free, and a 256-bit CPU-side read is not.

Against a 32-bit port a line-granular coalescer buys **nothing** — eight lanes on
five lines still need eight word reads, because the cache already coalesces the
*fills* by itself. Delivering the promised `8 → 5` requires a wide read path
that is contained to this PE, plus a per-lane word crossbar. That crossbar **is**
G5's headline number, which is the reason to keep it out of the shared L1: a
widened `rv_l1` would smear the cost of coalescing across classes that do not
coalesce, and `G5 − G4` would understate it.

Also outstanding:

- **Memory-latency hiding.** G7 interleaves *issue*, but one instruction is in
  flight at a time and a miss holds the whole front end. Until G6 lets a stalled
  wave step aside, more waves buy hazard-free issue and nothing else.
- **The lane/interval walk sequencer, on either side.** `FLANES < LANES` and
  `ILANES ≤ LANES` both need it, it is ruled to the DSP realm, and neither is
  built. Today `kht_fpu` **zeroes** the lanes above `FLANES` rather than
  sequencing or faulting them, so a reduced float build is not merely unmeasured,
  it is wrong. An earlier revision of this page said the float side "has it" —
  it does not; what it has is a parameter and one working value.
- **Wide operands on the SIMD tier.** Its float unit is an accumulator, so the
  operand width changes FSLOTS, the partial count and the fold order —
  architecture, not a port widening. The lane already takes both: the DSP
  instantiates `khs_float_lane` with `.wide(1'b0)` and never raises it.
- **Divide and remainder.** Deliberately not built: funct3 `100`–`111` in the
  RV32M group stay illegal and fault. Divide-by-constant strength-reduces to
  `mulhu`, which now exists.
- **`bar`, the workgroup barrier.** It **encodes** — `kht_predec` sets `C_BAR` —
  and nothing in `kht_core` or in the golden model reads it, so it retires as a
  no-op. It is the one unbuilt thing in this ISA that does not fault, and that
  makes it the one that can produce a wrong answer with no witness.

**The float tier, both of its operand widths and integer multiply are BUILT** and
have moved to their own sections above. Earlier revisions of this page listed them
here and said `G0` was `G0(int)` with "no float lane, no multiplier and no float
encoding anywhere in this PE" — all three are now false. The warning that
outlived them is still worth keeping, because it is what made the earlier LUT
comparisons wrong:

> **No G8 total is quotable without naming which lane array its G0 stands on.**

An integer-only figure and a float-capable figure are different machines, and
the same applies now to a PE with and without RV32M — which is why
[the mesh table](#what-the-mesh-costs-at-each-isa-level) names the build step of
every row.

## How to reproduce

```
# the ISA, end to end
python tests/pe/tools/rv_simt_isa_test.py
python tests/pe/tools/rv_simt_check.py
python tests/pe/tools/rv_simt_emit.py --check

# the machine -- every shader, one verdict, gates included.  SERIAL: do not run
# two of these at once, and do not run one beside a bare rv_simt_run.py.
python tests/pe/tools/rv_simt_suite.py --gates

# or one shader at a time
python tests/pe/tools/rv_simt_run.py tests/pe/prog/gpu_smoke.s   --arg 0x80000000 --dram zero
python tests/pe/tools/rv_simt_run.py tests/pe/prog/gpu_gather.s  --arg 0x80000000 --dram ramp
python tests/pe/tools/rv_simt_run.py tests/pe/prog/gpu_float.s   --arg 0x80000000 --dram zero --launch 16
python tests/pe/tools/rv_simt_run.py tests/pe/prog/gpu_mul.s     --arg 0x80000000 --dram zero --launch 16
python tests/pe/tools/rv_simt_run.py tests/pe/prog/gpu_f32.s     --arg 0x80000000 --dram zero --launch 16

# one ladder row -- top lanes waves mask ipdom period vreg_prim has_shfl flatten
vivado -mode batch -source scripts/tcl/ooc_simt_pe.tcl -tclargs \
    kht_unit 8 16 1 1 3.333 block 0

# the assembled PE, as the campaign table measures it
vivado -mode batch -source scripts/tcl/ooc_simt_pe.tcl -tclargs \
    kht_pe 8 16 1 1 3.333 block 1 rebuilt

# THE CONFIGURATION OF RECORD -- 21,586 LUT / 365.6 MHz
#   top lanes waves mask ipdom period vreg_prim has_shfl flatten has_flt flanes
vivado -mode batch -source scripts/tcl/ooc_simt_pe.tcl -tclargs \
    kht_pe 8 16 1 1 2.857 block 1 rebuilt 1 8

# a whole suite, four Vivados at a time -> build/sweep_<name>.md
python scripts/py/ooc_sweep.py gpu-ladder
python scripts/py/ooc_sweep.py gpu-waves
python scripts/py/ooc_sweep.py gpu-sched
python scripts/py/ooc_sweep.py gpu-lanes
python scripts/py/ooc_sweep.py gpu-lds
python scripts/py/ooc_sweep.py gpu-shfl
python scripts/py/ooc_sweep.py gpu-vregprim
python scripts/py/ooc_sweep.py gpu-pe        # the assembled PE
```

**The eighth argument is not optional in practice.** It defaults to 0, and a
ladder row measured with it defaulted is a G0–G3 figure; passing 1 puts G8's
butterfly inside a number that does not name it. Every G0–G3 row on this page
and on [ladder](ladder.md) was measured with `has_shfl 0` — and every
`gpu-pe` row with `has_shfl 1`, because the shipped PE carries the butterfly.
