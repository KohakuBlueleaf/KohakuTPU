---
title: The float tier
summary: FP16 in the register and E8M15 in the datapath, an accumulator of rotating partials that breaks a fifteen-cycle recurrence, the rounding property stated exactly, and what the tier costs.
tags:
  - architecture
  - pe
  - dsp
  - simd
  - float
---

# The float tier

The integer tier's `vdot` accumulates in one cycle because an int32 add is one
carry chain. Float is not: an E8M15 fused multiply-add on this device is
**fifteen cycles deep**, so `acc = a*b + acc` on a single accumulator issues one
operation every fifteen cycles, and a tier built that way would be slower than
the scalar core it sits in.

Everything below follows from breaking that recurrence without shortening it.

`DSP_F16 = 0` elaborates none of this, and the opcode major it uses is then
unmapped — a float instruction faults as an illegal encoding rather than
landing in a decode case that computes something plausible.

## FP16 in the register, E8M15 in the datapath

A vector register holds **two FP16 values per 32-bit lane**, so at SIMD 8 it is
sixteen FP16 elements. The datapath computes in **E8M15** — 24 bits, no
subnormals, always normalised — which is the vector core's own internal format,
and the conversion sits on the lane's edges rather than being an instruction.

```
   v1 lane L  | e1 (f16) | e0 (f16) |
   v2 lane L  | f1 (f16) | f0 (f16) |
                   |          |
              cvt f16 -> E8M15, EXACT in this direction
                   |          |
              +----v----+ +---v-----+
              |   FMA   | |   FMA   |    a*b + c, fifteen deep, II = 1
              +----+----+ +----+----+
                   |           |
              slot 2L+1    slot 2L        one accumulator slot per ELEMENT
```

Two structural facts make the arrangement cheap, and both belong to the format
rather than to this unit: **FP16 → E8M15 is exact**, because an 8-bit exponent
covers FP16's range with room and a subnormal normalises into an ordinary value;
and **E8M15 → FP16 rounds and saturates a finite overflow to the largest finite
FP16** rather than producing an infinity. An FP16 value therefore round-trips
unchanged through a kernel that reads and writes FP16.

Elementwise, not within-lane, and that is the one place the float tier does not
mirror the integer one. `vdot.s8` reduces four products inside a lane because an
integer adder tree is combinational and free; summing two floats before
accumulating would mean a float adder tree in front of a float accumulator —
more latency, more LUT, and a second rounding per element. Sixteen independent
chains cost nothing extra and are more accurate.

## The accumulator is sixteen rotating partials per slot

One architectural accumulator, `NPART` partials underneath it, and a counter:

```
   part[0..NPART-1]   per slot, E8M15
   rd_idx             a counter, advanced on every accepted accumulate
   wr_idx             rd_idx delayed by exactly the lane's latency, so a
                      result lands on the partial its addend came from
```

Consecutive accumulates go to different partials because the counter says so.
The program sees one accumulator and never learns the latency, and `NPART > ALAT`
is what guarantees a partial is never re-read before its write returns.

**The rotation is architectural, not an implementation detail.** Float addition
does not associate, so a build with a different `NPART` computes different
answers on the same program. The golden model rotates by the same number, and
the ISA states the order: the *n*th accumulate since `vfaccz` lands on partial
`n mod NPART`, and `vfaccrd` combines the partials in index order.

The partials are a **memory**, two mirrored `kohaku_sdpram` instances, not an
indexed flop array. As flops the array was 29,409 LUT of a 52,532-LUT unit —
every one of 12,288 bits carried a D-input mux between an accumulate result, a
seed and zero, with two variable-index read muxes on top. As LUTRAM it is 843.

The cost of the memory is that a write port is a write port: `vfaccz` and
`vfaccwr` become an `NPART`-cycle **sweep** rather than a parallel clear, which
is the same shape `rv_l1`'s invalidate-all has and for the same reason.

## The instructions

| | |
|---|---|
| `vfaccz ad` | `facc[ad] <- 0`, every slot. Untyped: zero is zero in either format |
| `vfmacc.f16 ad, vs1, vs2` | `facc[ad][i] += vs1[i] * vs2[i]`, elementwise over FP16 |
| `vfmsac.f16 ad, vs1, vs2` | the same, subtracted |
| `vfaccwr.f16 ad, vs1` | seed from a vector register — the bias vector |
| `vfaccrd.f16 vd, as1` | fold the partials and return one FP16 per element |
| `vfredsum.f16 xd, as1` | encoded and **NOT BUILT**; it faults |

`vfredsum` crosses the slots, which is a second pass that does not exist yet.
Returning slot 0 alone would be a plausible wrong answer, which is the one thing
a refusal exists to prevent — a kernel finishes the cross-slot sum with the
integer reduction or in scalar code. `f32` is likewise a legal encoding that
every current build refuses, which is what makes "this variant has no FP32" a
checkable statement.

## What waits, and what does not

`vfmacc` issues at **one per cycle**, back to back, including into the same
accumulator — that is the entire point of the rotation.

Three instructions **disturb or observe** the accumulator, and all three wait
for the lane to drain first:

```
   vfmacc  ...            fifteen cycles in flight after it retires
   vfaccrd v0, a0         waits for that, then folds
```

A float accumulate is still in flight fifteen cycles after its instruction has
left the MEM stage. Folding before it lands would drop it *and* capture its
result as a fold step, so `vfaccz`, `vfaccwr` and `vfaccrd` stall in decode
until the shadow is clear. It is the integer tier's `wait_acc` rule at the float
lane's depth, and it costs nothing in a real kernel: draining happens once at
the end of a reduction, not inside it.

`vfaccrd` is the expensive instruction and is meant to be. The fold walks the
partials in index order through **the same lane** the accumulate used —
combining them in a second adder would round differently from the path it is
meant to finish — so it takes `NPART × (ALAT+1)` cycles, plus `2 × SIMD` more to
pack the result back to FP16. That is roughly 270 cycles, once per reduction,
against a kernel of thousands.

**The conversions are walked, not replicated.** E8M15 → FP16 carries a 48-bit
subnormal shifter and measures 161 LUT; one per element would be 2,576 LUT hung
on the end of an instruction that already holds the stage for 256 cycles. There
is one converter each way and the instruction spends cycles it was spending
anyway.

## The rounding property, stated exactly

The lane is `vec_alu` with its operation tied to FMA, so it inherits the vector
core's property verbatim: **the FMA is within one ulp — correctly rounded
everywhere except one subtractive-alignment corner.**

The corner is specific. The addend is aligned into a 48-bit window and the bits
that fall outside it are carried as a plain sticky. For an addition that is
right: the discarded bits make the true sum larger, and a sticky is exactly how
"larger than the guard says" is expressed. For a **subtraction** it is wrong —
the discarded bits make the true result *smaller*, so the residue would have to
be complemented before it rounds. Treated as an ordinary sticky it rounds up
where the true value rounds down, by one ulp.

Measured, against a reference written from the definition (multiply exactly, add
exactly, round once):

| stream | steps | one ulp high |
|---|---:|---:|
| adversarial — every edge FP16 against every other, then addends deliberately far above and far below the product | 4,000 | **19 (0.47 %)** |
| ordinary magnitudes, 2⁻⁶ ≤ \|x\| < 2⁶ | 6,000 | **0** |

The first oversamples the corner on purpose; the second is what a kernel sees.
A user comparing against a correctly-rounded reference should learn this here
rather than from a mismatch.

The property is the same one `mx_fpacc` has on the matmul side, and that is
worth knowing rather than being surprised by: **both float datapaths in this
project round this way**, so it is a house convention rather than a defect in
one module.

## What it costs

OOC on `xcvu13p-fhgb2104-2L-e`, synthesis only, at the 3.333 ns ask the base
core settled on, SIMD 8 — so sixteen float elements and sixteen lanes:

| `khd_unit` | LUT | FF | DSP | BRAM | Fmax |
|---|---:|---:|---:|---:|---:|
| `HAS_F16 = 0` | 7,874 | 1,622 | 32 | 8 | 368.7 |
| `HAS_F16 = 1` | 21,862 | 10,061 | 64 | 8 | 368.2 |

**The tier costs area, not frequency**: 0.5 MHz between the build with it and
the build without, and the binding path does not move into it.

Inside one lane, and this is where the area is:

| block | LUT | share |
|---|---:|---:|
| normaliser — leading-one search and a 48-bit shift | 156 | 26 % |
| aligner — a 48-bit shift and its sticky | 113 | 19 % |
| magnitude and sign recovery | 67 | 11 % |
| exponent base and shift amount | 39 | 6 % |
| rounder, exponent bounds, assemble | 29 | 5 % |
| specials | 24 | 4 % |
| the pipeline's delay lines, as SRLs | 139 | 23 % |

Two barrel shifters are 44 % of the lane, and a variable shift is a multiply by
a one-hot power of two: rebuilt against a DSP48 the aligner is **44 LUT and one
DSP** against 113, and the normaliser **114 and one** against 156. Neither is in
the shipped lane, because taking them means this unit owning a fork of another
project's verified FMA rather than instantiating it.

The lane count is a separate purchase from the element count. Measured for the
tier alone — lanes, accumulator, fold and both walked converters:

| float lanes | issue interval per vector | LUT | DSP | Fmax |
|---:|---:|---:|---:|---:|
| 16 | 1 | 13,320 | 32 | 382.7 |
| 8 | 2 | 7,297 | 16 | 427.0 |
| 4 | 4 | 4,280 | 8 | 427.0 |
| 2 | 8 | 2,711 | 4 | 427.0 |

The marginal lane is 755 LUT at every step, so the tier is `1,240 + 755 × lanes`
and the fixed part is the accumulator, the fold and the two converters. Sixteen
lanes is the only point that gives up frequency, and it gives up none that
matters.

Assembled, which is the number a mesh has to hold:

| assembled `rv_pe` | LUT | DSP | BRAM | Fmax |
|---|---:|---:|---:|---:|
| `DSP_EN = 1`, integer tier | 10,343 | 32 | 13 | 357.1 |
| `DSP_EN = 1, DSP_F16 = 1`, sixteen float lanes | 24,631 | 64 | 13 | 346.5 |

**14,288 LUT and 10.6 MHz** for the tier at one lane per element. A DSP PE is
the replicated unit and a mesh holds about 350,000 usable LUT, so that
configuration is fourteen per mesh against the integer build's thirty-three —
which is why the lane count is a first-class parameter here and not a detail.
