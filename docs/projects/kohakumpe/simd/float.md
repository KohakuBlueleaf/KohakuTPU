---
title: Float
summary: One compute format and two operand widths in every group, four groups of which only the elementwise one is the base, why FALU packs and the accumulator does not, compares that return a mask rather than a float, lanes against elements against passes, and the rounding property stated exactly.
tags:
  - architecture
  - pe
  - simd
  - float
---

# Float

The integer tier's `vdot` accumulates in one cycle because an int32 add is one
carry chain. Float is not: an E8M15 fused multiply-add on this device is
**fifteen cycles deep**, so `acc = a*b + acc` on a single accumulator issues one
operation every fifteen cycles, and a tier built that way would be slower than
the scalar core it sits in.

Everything below follows from breaking that recurrence without shortening it.

`SIMD_FLOAT = 0` elaborates none of this and leaves custom-1 unmapped, so a float
instruction faults as an illegal encoding rather than landing in a decode case
that computes something plausible. **`SIMD_FLOAT` is a presence switch and
nothing else** — it does not select a format, because there is only one.

## One compute format, two operand widths

```
   FP32 or FP16 operands in   ->   E8M15 compute   ->   FP32 or FP16 out
```

This is the whole dtype story of the design, and none of it is configurable.
`khs_float_lane` carries **one operand port and both converters,
unconditionally**; a `wide` bit picks which converter drives the datapath, and
the datapath below is E8M15 either way. The lane's own header states it as a
contract rather than an option:

> BOTH INPUT FORMATS AND THE ONE COMPUTE FORMAT ARE THE CONTRACT, not options:
> there is no parameter here that removes either edge.

So **operand width is a property of an instruction**, not of a build, and there
is no parameter anywhere in this PE that changes what the arithmetic is done in.

| conversion | what it costs |
|---|---|
| FP16 → E8M15 | **exact.** An 8-bit exponent covers FP16's range with room, and a subnormal normalises into an ordinary value |
| FP32 → E8M15 | **mantissa only.** E8 *is* FP32's exponent field, verbatim, so nothing about the range is lost; 23 mantissa bits round to 15, and an FP32 subnormal flushes because E8M15's smallest normal is 2⁻¹²⁶ |
| E8M15 → FP16 | **the one conversion that is both lossy and range-limited.** It rounds to 10 mantissa bits and **saturates a finite overflow at 65504**, the largest finite FP16, rather than producing an infinity |

An FP16 value therefore round-trips unchanged through a kernel that reads and
writes FP16, and the only conversion a kernel has to think about is the way out.

### E8M15 is not a compromise

| | rel. error, half ulp |
|---|---|
| FP16 | 4.9e-4 |
| **E8M15 — what a partial sum carries** | **1.5e-5** |
| FP32 | 6.0e-8 |

**1.5e-5 is 32× better than the FP16 that mobile fragment shaders run at**, with
an 8-bit exponent — more range than the FP24 of the DX9 era had. A dot product
of FP16 inputs accumulates 32× more accurately than its own operands, in a
format whose range covers FP32's verbatim.

Both halves of the format are chosen by the hardware they have to fit: an 8-bit
exponent makes conversion range-lossless from either source format, and 15
mantissa bits are what make a 16-bit significand times a 16-bit significand land
in exactly the 48 bits a `DSP48E2`'s `C` port has. The full argument is the
vector core's — [the vector core](../../../projects/kohakutpu/vector-core.md) §1.

## Four groups, and only one of them is the base

A SIMD extension on an RV32I core is a CPU with SIMD, and every CPU SIMD ISA —
SSE, AVX, NEON, RVV — ships multiply, add, subtract, fused multiply-add, min,
max and compare as its *base*. Dot-product accumulators arrived later as
additions: ARM `SDOT`, x86 `VPDPBUSD`. This tier has the same shape, and the RTL
parameters follow it.

| group | instructions | parameter | default |
|---|---|---|---|
| **FALU** | `vfmul` `vfadd` `vfsub` `vfma` `vfmin` `vfmax` `vfcmplt/gt/eq` | `SIMD_FALU` | **on** |
| **FSFU** | `vfexp2` `vflog2` `vfrcp` `vfrsqrt` | `SIMD_FSFU` | on |
| **FMAC** | `vfmacc` `vfmsac` `vfaccz` `vfaccwr` `vfaccrd` | `SIMD_FACC` | **off** |
| **FCVT** | float ↔ int32, f16 ↔ f32 | `SIMD_FCVT` | off, not built |

**Every group is registered at both operand widths**, optional ones included.
`funct7[1:0]` picks the width per instruction and reaches the conversion at the
lane's edge; nothing below it changes.

The accumulator is off by default because it is the SIMD PE's *extra*, not its
floor: a vertex transform accumulating into E8M15 partials, a float dot, or a
long reduction justify it, and a shader doing elementwise colour work does not.
The [SIMT PE](../simt/README.md) has no equivalent and does not want one.

### FALU packs; the accumulator does not

A 256-bit register is **16 FP16 elements or 8 FP32 elements** under FALU, which
is the integer tier's own rule (32 int8 / 16 int16 / 8 int32). FP16 therefore
gets twice FP32's throughput for the cost of an operand mux rather than for
lanes, and `FLOAT_LANES` divides the elements to give the issue interval.

The accumulator keeps its own packing, because a partial-sum machine sizes
itself by the accumulator rather than by the operand: FP16 goes two per 32-bit
slot and FP32 takes the even slot alone. That is the one place where operand
width changes a *shape* rather than just a conversion — the element count, the
partial count and the fold order all move with it, and float addition does not
associate, so the order is contract and the ISA states it.

The SIMT PE places one element per 32-bit slot in both formats instead, because
there a slot is a *thread*. Same lane, same E8M15, same conversions — so a SIMD
float result and a SIMT float result agree element for element, and only the
addressing differs.

### A compare returns a mask, not a float

`vec_alu`'s compare result is 1.0 or 0.0 and its `out_pred` carries the bit;
`khs_falu` splats that bit, so `vfcmplt` writes **all ones or all zeros per
element** into an ordinary vector register and the integer tier's `vand` /
`vandn` / `vor` do the blend. A branchless conditional costs no new
architectural state and no select instruction.

**NaN compares false in every form**, and min/max return **vs1** when either
operand is a NaN — that is `vec_alu.v:177`'s `va = cmp_lt ? s1_b : s1_a` with a
compare that a NaN makes false, and the golden model follows the silicon rather
than the other way round.

## Elements, lanes and passes

Three numbers that used to be one, and keeping them apart is the whole of
understanding this tier:

```
   elements   2 x SIMD             how many FP16 values a vector register holds.
                                   16 at SIMD 8. A register width divided by an
                                   element width -- not a choice anyone makes.

   lanes      SIMD_FLOAT_LANES      how many float lanes are BUILT. 4 at the
                                   reference; 0 means one lane per element.

   passes     elements / lanes     the issue interval. 4 at the reference,
                                   1 at one lane per element.
```

The lane count must divide the element count, and the pass count must divide
`NPART` — the second because each element's accumulate chain is the subset of
partial-turns congruent to its own pass, and those subsets have to be disjoint
and equal.

At the reference, four lanes serve sixteen elements in four passes:

```
   v1  | e15 .. e12 | e11 .. e8 | e7 .. e4 | e3  e2  e1  e0 |
                                                |
                             pass 0 drives elements 3..0 into the four lanes
                                                |
              cvt f16 -> E8M15, EXACT in this direction
                                                |
              +----v----+ +---v-----+ +---v-----+ +---v-----+
              |   FMA   | |   FMA   | |   FMA   | |   FMA   |   fifteen deep
              +----+----+ +----+----+ +----+----+ +----+----+
                   |           |           |           |
                slot 3      slot 2      slot 1      slot 0

   pass 1 then drives elements 7..4 through the SAME four lanes, and so on.
   The accumulator is sixteen slots wide regardless: one per ELEMENT.
```

**The instruction retires once.** A `vfmacc` issues one operation per pass, one
per cycle, and the MEM stage holds until the last of them has gone — so the
vector file is written, the scalar writeback fires and the accumulator index
advances a single time, whatever the lane count. At one lane per element the hold
term is constant low and nothing in the machine changes.

| float lanes at SIMD 8 | passes | `vfmacc` occupies MEM for | partials in one element's chain |
|---:|---:|---:|---:|
| 16 | 1 | 1 cycle | 16 |
| 8 | 2 | 2 cycles | 8 |
| **4 — the reference** | **4** | **4 cycles** | **4** |
| 2 | 8 | 8 cycles | 2 |

> **The pass-hold term must not read the combined hold signal.** The combined
> term *contains* the pass hold, so reading it back closes a combinational loop.
> The issue gate reads the other two hold terms individually.

**Elementwise, not within-lane**, and that is the one place the float tier does
not mirror the integer one. `vdot.s8` reduces four products inside a lane because
an integer adder tree is combinational and free; summing two floats before
accumulating would mean a float adder tree in front of a float accumulator — more
latency, more LUT, and a second rounding per element. Sixteen independent chains
cost nothing extra and are more accurate.

## The accumulator is rotating partials

One architectural accumulator, `NPART` partials underneath it, and a counter:

```
   part[0..NPART-1]   per LANE, E8M15
   rd_idx             a counter, advanced on every accepted accumulate --
                      which means once per PASS, not once per instruction
   wr_idx             rd_idx delayed by exactly the lane's latency, so a
                      result lands on the partial its addend came from
```

Consecutive accumulates go to different partials because the counter says so. The
program sees one accumulator and never learns the latency, and `NPART > ALAT` is
what guarantees a partial is never re-read before its write returns.

**The rotation is architectural, not an implementation detail.** Float addition
does not associate, so a build with a different `NPART` — *or a different lane
count* — computes different answers on the same program. The golden model rotates
identically and the ISA states the order:

```
   element i, on the nth vfmacc since vfaccz, lands on partial

                (n * passes  +  i / lanes)  mod NPART

   at one lane per element that is the familiar  n mod NPART
```

Element *i*'s chain is therefore the turns congruent to its own pass — a disjoint
set of `NPART / passes` partials, four of the sixteen at the reference, not all
sixteen. `vfaccrd` combines each element's own set in index order. **Fewer lanes
means a shorter accumulation chain as well as a longer issue interval**, and both
are visible in the answers.

### The rotation is not a preference — the alternative was built and measured

`acc <= acc + x` in one cycle would make the float tier the integer tier's shape
with a different adder, and `vfmacc` would need no rules at all. So the loop was
built alone — flop, add, flop, and nothing else, around the matmul cluster's own
reference float add (`khs_facc_loop.v`, `scripts/tcl/ooc_facc.tcl`).

**It measures 152.3 MHz and 25 logic levels standalone** on
`xcvu13p-fhgb2104-2L-e`, against a PE whose own ceiling is above 350 MHz. One
float add does not close in one cycle here, and no amount of care inside the
adder changes that; it is the same result that makes the matmul cluster split the
same work across three stages. The rotation is what that measurement forces.

There is a second way out, and it is also built rather than argued about:
`khs_facc_fixed.v` (`scripts/tcl/ooc_facc_fixed.tcl`) moves the alignment shift
**outside** the loop and accumulates into a 96-bit fixed-point field, so what
remains in the recurrence is `acc <= acc ± x` on a plain carry chain — the shape
the int32 accumulate already closes at. It is also **exact**: nothing rounds
until the accumulator is read, so a dot product's error does not grow with K.

Neither that accumulator nor `khs_f16_prod.v` — the finished-before-the-loop
product it consumes — is instantiated by any design. They are the priced
alternative to the contract, kept synthesisable so the choice can be re-opened
with numbers rather than reasoning.

### The partials are a memory

Two mirrored distributed-RAM instances, not an indexed flop array, and the
difference is the single largest area decision in the tier:

> As flops the array was **29,409 LUT of a 52,532-LUT unit** — 56 % of the whole
> thing — because every one of 12,288 bits carried a D-input mux between an
> accumulate result, a seed and zero, with two variable-index read muxes on top.
> **As LUTRAM it is 843.**

The cost of it being a memory is that a write port is a write port: `vfaccz` and
`vfaccwr` become an `NPART`-cycle **sweep** rather than a parallel clear, which
is the same shape `rv_l1`'s invalidate-all has and for the same reason.

> **The address is arithmetic, not a concatenation.** At `NACC = 1` the
> accumulator select is `$clog2(1) = 0` bits wide, so a concatenated address is
> malformed and **every partial reads back zero** — which is exactly how it
> failed once.

> **The first `passes` partials take the seed, not just partial 0.** A seed lands
> at turn 0, which belongs to pass 0; with disjoint per-pass chains every other
> pass would silently start from zero. That is what the sweep's own index and the
> widened seed window are for.

## The fold

`vfaccrd` is the expensive instruction and is meant to be. The fold walks the
partials in index order through **the same lane** the accumulate used — combining
them in a second adder would round differently from the path it is meant to
finish — so each step depends on the last and the steps are `ALAT` apart.

| step | cycles | |
|---|---:|---|
| the fold, per pass | `(NPART / passes) × (ALAT+1)` | serial through the lane, at `E8_ONE` on the multiplier's other port |
| packing back to FP16, per pass | `lanes` | one walked converter |
| **`vfaccrd`, total** | **`NPART × (ALAT+1) + 2 × SIMD` ≈ 270** | **the same at every lane count** |

The total is invariant because `passes` folds of `NPART/passes` steps is `NPART`
steps either way, and `passes × lanes` packing steps is the element count either
way. What changes is the *shape*:

> **The fold walks one pass's own strided subset, not all `NPART` partials.**
> Element *e*'s chain is the turns congruent to its pass, so a flat fold over
> `NPART` would sum **across elements** and return a plausible wrong answer.

> **The fold index is narrower than the partial index.** Connected directly, the
> top bits stay undriven and **every folded element comes back X**. It drives a
> local wire and is zero-extended.

**The conversions are walked, not replicated.** E8M15 → FP16 carries a 48-bit
subnormal shifter and measures **161 LUT**; one per element would be 2,576 LUT
hung on the end of an instruction that already holds the stage for hundreds of
cycles. There is one converter each way and the instruction spends cycles it was
spending anyway. Sixteen parallel input converters measured **720 LUT** for an
instruction that runs once per kernel.

## The instructions

| | |
|---|---|
| `vfaccz ad` | `facc[ad] <- 0`, every slot. **Untyped** — zero is zero in either format |
| `vfmacc.f16 ad, vs1, vs2` | `facc[ad][i] += vs1[i] * vs2[i]`, elementwise |
| `vfmsac.f16 ad, vs1, vs2` | the same, subtracted |
| `vfaccwr.f16 ad, vs1` | seed from a vector register — the bias vector |
| `vfaccrd.f16 vd, as1` | fold the partials and return one FP16 per element |
| `vfredsum.f16 xd, as1` | encoded and **NOT BUILT**; it faults |

The instruction count is **69: 63 integer on custom-0 and 6 float on custom-1**,
and the six above are all of the second number.

## What waits, and what does not

**A float lane issues one operation per cycle, back to back, including into the
same accumulator** — that is the entire point of the rotation, and it is true at
every lane count. What a lane count costs an *instruction* is the pass walk,
above.

Three instructions **disturb or observe** the accumulator, and all three wait for
the lane to drain first:

```
   vfmacc  ...            fifteen cycles in flight after it retires
   vfaccrd v0, a0         waits for that, then folds
```

A float accumulate is still in flight fifteen cycles after its instruction has
left the MEM stage. Folding before it lands would drop it *and* capture its
result as a fold step, so `vfaccz`, `vfaccwr` and `vfaccrd` stall in decode until
the shadow — a fifteen-bit shift register read in EX — is clear. It is the
integer tier's `wait_acc` rule at the float lane's depth, and it costs nothing in
a real kernel: draining happens once at the end of a reduction, not inside it.

`vfmacc` is deliberately not on that list.

## The rounding property, stated exactly

The lane is `vec_alu` with its operation tied to FMA, so it inherits the vector
core's property verbatim: **the FMA is within one ulp — correctly rounded
everywhere except one subtractive-alignment corner.**

The corner is specific. The addend is aligned into a 48-bit window and the bits
that fall outside it are carried as a plain sticky. For an addition that is
right: the discarded bits make the true sum larger, and a sticky is exactly how
"larger than the guard says" is expressed. For a **subtraction** it is wrong — the
discarded bits make the true result *smaller*, so the residue would have to be
complemented before it rounds. Treated as an ordinary sticky it rounds up where
the true value rounds down, by one ulp.

Measured, against a reference written from the definition (multiply exactly, add
exactly, round once):

| stream | steps | one ulp high |
|---|---:|---:|
| adversarial — every edge FP16 against every other, then addends deliberately far above and far below the product | 4,000 | **19 (0.47 %)** |
| ordinary magnitudes, 2⁻⁶ ≤ \|x\| < 2⁶ | 6,000 | **0** |

The first oversamples the corner on purpose; the second is what a kernel sees. A
user comparing against a correctly-rounded reference should learn this here
rather than from a mismatch.

The property is the same one `mx_fpacc` has on the matmul side: **both float
datapaths in this project round this way**, so it is a house convention rather
than a defect in one module.

## How the arithmetic is verified

A float datapath checked against a transcription of itself proves nothing, so the
reference is written from the definition in `tests/pe/tools/rv_simd_f16.py`, and
**it proves its own claims rather than asserting them**: `selftest()` walks the
whole 65,536-value FP16 space to show the round trip is exact, checks both paths
into FP32 agree, checks the FMA against Python's own arithmetic on values where
float64 is exact for the entire product-sum, and checks algebraic identities that
need no reference at all (`a*1+0 == a`, commutativity, sign symmetry, and
`a*1 − a == +0` — the sign of an exact cancellation being a decision rather than a
don't-care).

That model carries **two** FMAs, and the pair is the contract:

| | |
|---|---|
| `e8_fma` | the definition. Correctly rounded, always |
| `e8_fma_hw` | the machine: the 48-bit alignment window, both shift clamps, and the plain sticky |

The difference between them **is** the deviation measured above, and the RTL is
compared bit-for-bit against the second one. A bench that compared against the
first would fail on ordinary data and call the hardware wrong for matching its own
arithmetic.

Benches sit on top, and each proves something the one below it cannot:

| bench | what it proves |
|---|---|
| `khs_float_lane_tb` | the fifteen-stage lane computes the model's answer, **one vector per cycle** — a lane that only worked when operations were spaced out would pass a one-at-a-time bench and fail every kernel |
| `khs_facc_tb` | the partials are right, in the rotation's own order |
| `khs_ffold_tb` | the value a kernel actually reads — dependent steps through the same lane, in index order. Correct partials combined in the wrong order give a wrong answer that still looks like a float |
| `khs_unit_tb` | the assembled unit, decode included, against the golden model instruction by instruction — with the lane count in its configuration guard, so a vector/build mismatch names itself |

> **An unconnected `raw_e8` is `z`**, the operand select goes X, and every result
> reads as a dead accumulator rather than as a missing port. It cost two benches a
> run, and there is now a non-synthesis check that names it.

## What is not built

Nothing here computes something plausible instead. Where a line says *faults*, it
is a decode term in `khs_unit.v` and the instruction halts the unit at the
offending PC; where it says *not encoded*, the mnemonic does not exist in the ISA
table the assembler, the golden model and the RTL decode are all generated from.

| Not built | What happens | Why it is refused |
|---|---|---|
| `vfredsum.f16` | faults | crosses the slots; a second pass that does not exist. The **golden model does implement it**, so the model is ahead of the RTL here and a kernel that used it would pass in simulation and fault on the machine |
| any `.f32` form | faults | **not for want of an operand edge** — the lane has one, unconditionally, and the SIMT PE drives it. Here the operand width changes the element count, the partial count and the fold order — [above](#elements-lanes-and-passes) |
| the whole `FCVT` group | faults | the group number is allocated in the ISA table and nothing decodes it |
| **elementwise float** — `vfadd`, `vfmul`, `vfmax`, float compare | not encoded at all | they write a **vector register** from a fifteen-cycle datapath, which needs a scoreboard; an accumulating instruction needs only the accumulator's own busy shadow. That is the whole of why phase 1 is the accumulator and nothing else |
| **transcendentals** — `exp2`, `log2`, `inv`, `rsqrt` | not encoded | they are *in the lane's source* at full rate and synthesised away — below |

Returning slot 0 alone for a `vfredsum` would be a plausible wrong answer, which
is the one thing a refusal exists to prevent — a kernel finishes the cross-slot
sum with the integer reduction or in scalar code.

### The transcendentals are in the source and not in the machine

`vec_alu` computes `exp2`, `log2`, `inv` and `rsqrt` at **full rate, II = 1** —
one pass each, the same throughput as an add, which is the property the vector
core's third DSP exists to buy. The float lane instantiates that module with its
operation tied to FMA, so constant propagation removes the four seeds, their
coefficient ROMs, the Horner path and the `exp2` range reduction before anything
is placed.

**So a SIMD PE has no transcendental instruction and no transcendental hardware**,
and the two facts have the same cause. Newton refinement does not change the
picture and is not missing hardware either: `1/a: y' = y(2−ay)` is two FMAs and
`rsqrt: y' = y(1.5−0.5ay²)` is three, so refinement is an **instruction
sequence**, deliberately — and at this table accuracy the native seed is already
better than the format. `exp2` and `log2` have no such self-correcting step at
all.

## Which files are the machine and which are probes

| file | in the machine? | what it is |
|---|---|---|
| `khs_float_lane.v` | **yes** — one per float LANE | the tier's arithmetic and both operand edges |
| `khs_facc.v` | **yes** | the rotating partials, and the per-pass seed window |
| `khs_ffold.v` | **yes** | the fold sequencer, run once per pass |
| `khs_float_tier.v` | **probe** | the tier alone, for area against lane count — `ooc_float_tier.tcl`. **It folds differently**: flat over `NPART × passes`, where the unit walks one pass's strided subset. Its lane count is comparable and its arithmetic is not, and its own header says so |
| `khs_float_blocks.v` | **probe** | `vec_alu`'s FMA taken apart stage by stage, plus DSP48 alternatives to its two barrel shifters — `ooc_float_blocks.tcl` |
| `khs_e8_fma.v` | **probe** | the shipped `vec_alu` with its operation tied to FMA and a register on each side, so an out-of-context run measures the lane and not its pins — `ooc_e8fma.tcl` |
| `khs_facc_loop.v` | **probe** | the one-cycle float accumulate loop: **152.3 MHz, 25 levels** — `ooc_facc.tcl` |
| `khs_facc_fixed.v` | **probe** | the fixed-point alternative to the whole rotation contract — `ooc_facc_fixed.tcl` |
| `khs_f16_prod.v` | **probe** | the product in accumulator format, which is the only *new* float arithmetic that alternative would need |

Everything the tier uses beyond those is instantiated, never forked: `vec_alu`,
`vec_dsp`, `vec_cvt`, `vec_delay` and `mx_fpacc` are the vector core's and the
matmul cluster's shipped, verified modules. **That is the reason the DSP48-based
aligner and normaliser in the block probe are priced and not taken** — adopting
them would mean this unit owning a fork of another project's verified FMA.
