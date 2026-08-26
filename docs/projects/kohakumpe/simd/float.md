---
title: Float
summary: One compute format, four groups of which only the elementwise one is the base, compares that return a mask rather than a float, elements against units against passes, the rotating accumulator, the four seeds, and the rounding property stated exactly.
tags:
  - architecture
  - pe
  - simd
  - float
---

# Float

> **Kind: Yours throughout.** The compute format, the group structure, the
> mask-returning compares, the rotating accumulator and the stated rounding
> property are this project's arithmetic. The framework carries these bits and
> never interprets one.

The integer tier accumulates in one cycle because an int32 add is one carry
chain. Float is not: a binary32 fused multiply-add on this device is **six
cycles deep**, so `acc = a*b + acc` on a single accumulator issues one operation
every six cycles, and a tier built that way would be slower than the scalar core
it sits in.

Everything below follows from breaking that recurrence without shortening it.

`FLOAT_LANES = 0` elaborates none of this and leaves custom-1 unmapped, so a
float instruction faults as an illegal encoding rather than landing in a decode
case that computes something plausible.

## One compute format

```
   IEEE binary32 in   ->   binary32 compute   ->   binary32 out
```

This is the whole dtype story of the design, and none of it is configurable.
There is no second format, no conversion at the operand edge, and no parameter
anywhere in this PE that changes what the arithmetic is done in.

**Denormals flush to sign-preserved zero on input and output.** That is not a
shortcut: D3D11's functional specification requires it, so gradual-underflow
hardware would be non-conformant as well as expensive.

`funct7[1:0]` carries the element type where the integer tier's is, and `f32` is
the only value a build accepts — every other value of the field is an unmapped
encoding rather than a silent reinterpretation.

### KohakuMPE holds its own float units

`rv_fpu.v` is in the **framework**, because RV32F is a standard extension over
IEEE binary32 and binary32 is nobody's private format. Everything above it —
`khs_fp32_alu.v`, `khs_fp32_sfu.v`, `khs_fcvt.v` — is KohakuMPE's.

KohakuTPU's vector core keeps computing in E8M15 with its own modules and is not
touched by any of this. **There is no E8M15 anywhere in KohakuMPE.**

## Four groups, and only one of them is the base

A SIMD extension on an RV32 core is a CPU with SIMD, and every CPU SIMD ISA —
SSE, AVX, NEON, RVV — ships multiply, add, subtract, fused multiply-add, min,
max and compare as its *base*. Dot-product accumulators arrived later as
additions: ARM `SDOT`, x86 `VPDPBUSD`. This tier has the same shape, and the RTL
parameters follow it.

| group | instructions | parameter | default |
|---|---|---|---|
| **FALU** | `vfmul` `vfadd` `vfsub` `vfma` `vfmin` `vfmax` `vfcmplt/gt/eq` | `HAS_FALU` | **on** |
| **FSFU** | `vfexp2` `vflog2` `vfrcp` `vfrsqrt` | `FSFU_UNITS` | a unit count, 0 |
| **FMAC** | `vfmacc` `vfmsac` `vfaccz` `vfaccwr` `vfaccrd` | `HAS_FACC` | **off** |
| **FCVT** | `vfcvt.f2i` `vfcvt.i2f` | `FCVT_UNITS` | a unit count, 0 |

The accumulator is off by default because it is the SIMD PE's *extra*, not its
floor: a vertex transform, a float dot, or a long reduction justify it, and a
shader doing elementwise colour work does not. The [SIMT PE](../simt/README.md)
has no equivalent and does not want one.

Asking for a float **group** on a build with no float units is refused at
elaboration rather than silently given the widest tier.

### Everything packs at one element width

A 256-bit register is **8 binary32 elements**, which is the integer tier's own
rule (32 int8 / 16 int16 / 8 int32). `FLOAT_LANES` divides the elements to give
the issue interval, and the accumulator holds one slot per element rather than a
packing of its own.

The SIMT PE places one element per 32-bit slot too, because there a slot is a
*thread*. Same units, same format — so a SIMD float result and a SIMT float
result agree element for element, and only the addressing differs.

### A compare returns a mask, not a float

The float unit's compare result is 1.0 or 0.0 and it carries a predicate bit
beside it; `khs_fp32_alu` splats that bit, so `vfcmplt` writes **all ones or all
zeros per element** into an ordinary vector register and the integer tier's
`vand` / `vandn` / `vor` do the blend. A branchless conditional costs no new
architectural state and no select instruction.

**NaN compares false in every form.** `vfmin` and `vfmax` are IEEE minNum and
maxNum: **a NaN operand loses**, so one NaN returns the other and two return a
quiet NaN through the ordinary specials path.

## Elements, units and passes

Three numbers, and keeping them apart is the whole of understanding this tier:

```
   elements   SIMD                 how many binary32 values a vector register
                                   holds. 8 at SIMD 8. A register width divided
                                   by an element width -- not a choice anyone
                                   makes.

   units      FLOAT_LANES          how many float units are BUILT.
                                   0 means the tier is not built at all.

   passes     elements / units     the issue interval.
```

The unit count must divide the element count, and when the accumulator is built
the pass count must divide `NPART` — the second because each element's
accumulate chain is the subset of partial-turns congruent to its own pass, and
those subsets have to be disjoint and equal.

**The instruction retires once.** A multi-pass instruction issues one operation
per pass, one per cycle, and the memory stage holds until the last of them has
gone — so the vector file is written, the scalar writeback fires and the
accumulator index advances a single time, whatever the unit count. At one unit
per element the hold term is constant low and nothing in the machine changes.

| float units at SIMD 8 | passes | a `vfmacc` occupies MEM for | partials in one element's chain |
|---:|---:|---:|---:|
| 8 | 1 | 1 cycle | 16 |
| 4 | 2 | 2 cycles | 8 |
| 2 | 4 | 4 cycles | 4 |
| 1 | 8 | 8 cycles | 2 |

**Elementwise, not within-lane**, and that is the one place the float tier does
not mirror the integer one. Summing two floats before accumulating would mean a
float adder tree in front of a float accumulator — more latency, more LUT, and a
second rounding per element. Independent chains cost nothing extra and are more
accurate.

## The accumulator is rotating partials

One architectural accumulator, `NPART` partials underneath it, and a counter:

```
   part[0..NPART-1]   per SLOT, binary32
   rd_idx             a counter, advanced on every accepted accumulate --
                      which means once per PASS, not once per instruction
   wr_idx             rd_idx delayed by exactly the tier's latency, so a
                      result lands on the partial its addend came from
```

Consecutive accumulates go to different partials because the counter says so.
The program sees one accumulator and never learns the latency, and
`NPART > ALAT` is what guarantees a partial is never re-read before its write
returns.

**The rotation is architectural, not an implementation detail.** Float addition
does not associate, so a build with a different `NPART` — *or a different unit
count* — computes different answers on the same program. The golden model
rotates identically and the ISA states the order:

```
   element i, on the nth vfmacc since vfaccz, lands on partial

                (n * passes  +  i / units)  mod NPART

   at one unit per element that is the familiar  n mod NPART
```

Element *i*'s chain is therefore the turns congruent to its own pass — a disjoint
set of `NPART / passes` partials, not all `NPART`. `vfaccrd` combines each
element's own set in index order. **Fewer units means a shorter accumulation
chain as well as a longer issue interval**, and both are visible in the answers.

### Nothing converts at either edge

A partial is a binary32 word, so `vfaccwr` seeds the sweep straight from the
source register and `vfaccrd` places the folded words unchanged. The walked
converters an E8M15 tier needed at those two edges, and the 48-bit subnormal
shifter one of them carried, do not exist here.

### The partials are a memory

Two mirrored distributed-RAM instances rather than an indexed flop array, and
the difference is the single largest area decision in the tier: as flops the
array measured **29,409 LUT of a 52,532-LUT unit**, because every bit carried a
D-input mux between an accumulate result, a seed and zero with two
variable-index read muxes on top. As LUTRAM the same storage is **843**.
[memory](memory.md#the-third-array-the-float-partials) has the construction.

The cost of it being a memory is that a write port is a write port: `vfaccz` and
`vfaccwr` become an `NPART`-cycle **sweep** rather than a parallel clear, which
is the same shape a cache's invalidate-all has and for the same reason.

> **The first `passes` partials take the seed, not just partial 0.** A seed
> lands at turn 0, which belongs to pass 0; with disjoint per-pass chains every
> other pass would silently start from zero. That is what the sweep's own index
> and the widened seed window are for.

## The fold

`vfaccrd` is the expensive instruction and is meant to be. The fold walks the
partials in index order through **the same unit** the accumulate used —
combining them in a second adder would round differently from the path it is
meant to finish — so each step depends on the last and the steps are `ALAT`
apart.

| step | cycles | |
|---|---:|---|
| the fold, per pass | `(NPART / passes) × (ALAT+1)` | serial through the unit, with 1.0 on the multiplier's other port |
| placing the result, per pass | 1 | the whole built width at once |
| **`vfaccrd`, total** | **`NPART × (ALAT+1) + passes`** | **the same at every unit count** |

At the defaults — `NPART` 16, no seed units, so `ALAT` 6 — that is **112 cycles
plus the pass count**. With seed units built, `ALAT` is 10 and it is 176 plus
the pass count.

> **The fold walks one pass's own strided subset, not all `NPART` partials.**
> Element *e*'s chain is the turns congruent to its pass, so a flat fold over
> `NPART` would sum **across elements** and return a plausible wrong answer.

> **The fold index is narrower than the partial index.** Connected directly, the
> top bits stay undriven and every folded element comes back X. It drives a
> local wire and is zero-extended.

## The four seeds

`FSFU_UNITS` is a **unit count, not a boolean**: `N` of the `FLOAT_LANES` float
units carry a `khs_fp32_sfu` beside their multiply-add, and a seed walks
`SIMD / FSFU_UNITS` passes where a multiply-add walks `SIMD / FLOAT_LANES`. Real
GPUs provision transcendentals at 1/4 rate — NVIDIA, AMD GCN/RDNA and Mali
Bifrost/Valhall all do — which is what a count below the unit count buys.

One backend serves all four functions. Each produces a fixed-point magnitude in
a 40-bit field at 32 fraction bits plus a signed exponent base, so
`biased_exp = lead1(mag) + ebase` closes for every one of them and the unit
holds one leading-one search, one normalising shift and one rounder:

| f | magnitude | exponent base |
|---|---|---|
| `exp2` | `2^frac(x) · 2^32` | `95 + floor(x)` |
| `log2` | `abs((E << 32) + L)` | `95` |
| `rcp` | `(1/1.m) · 2^32` | `222 − e` |
| `rsqrt` | `2^(−r/2)/sqrt(1.m) · 2^32` | `95 − floor((e−127)/2)` |

The evaluation is **integer**, 512 segments per function and 1,024 for `rsqrt`'s
two octaves:

```
   Q = C0 + (((C1 + ((C2 * U) >> 22)) * U) >> 20)
```

`t = U · 2⁻³⁰` in every function, which is what keeps those two shifts constants
rather than a per-function mux. `C0` is the **exact** value at each segment's
origin, so `exp2(k)`, `log2(2^k)`, `rcp(2^k)` and `rsqrt(2^2k)` come out exact.

The table is generated into `src/kohakumpe/simd/generated/khs_seed_tab.v`, which
the golden model **parses** — there is one file, so the model and the unit cannot
hold different numbers — and the generator's `--report` measures the ULP error
against float64.

> **A seed is 10 cycles and a multiply-add is 6.** `khs_fp32_alu` pads the
> multiply-add path by 4 whenever seed units are built and by nothing when they
> are not, so the tier has **one** latency and the retire shadow is one depth.
> That latency moves with `FSFU_UNITS`, and both modules check the depth they
> were told against the depth they built.

Newton refinement is an **instruction sequence**, deliberately: `1/a` is
`y' = y(2−ay)`, two multiply-adds, and `rsqrt` is `y' = y(1.5−0.5ay²)`, three.

## The instructions

| | |
|---|---|
| `vfmul.f32` `vfadd.f32` `vfsub.f32` `vfma.f32` | elementwise, `HAS_FALU` |
| `vfmin.f32` `vfmax.f32` | IEEE minNum / maxNum; a NaN operand loses |
| `vfcmplt.f32` `vfcmpgt.f32` `vfcmpeq.f32` | all-ones or all-zeros per element |
| `vfexp2.f32` `vflog2.f32` `vfrcp.f32` `vfrsqrt.f32` | the seeds, `FSFU_UNITS` |
| `vfcvt.f2i.f32` `vfcvt.i2f.f32` | binary32 ↔ int32, `FCVT_UNITS` |
| `vfaccz ad` | `facc[ad] ← 0`, every slot. **Untyped** — zero is zero |
| `vfmacc.f32 ad, vs1, vs2` | `facc[ad][i] += vs1[i] * vs2[i]`, elementwise |
| `vfmsac.f32 ad, vs1, vs2` | the same, subtracted |
| `vfaccwr.f32 ad, vs1` | seed from a vector register — the bias vector |
| `vfaccrd.f32 vd, as1` | fold the partials and return one binary32 per element |
| `vfredsum.f32 xd, as1` | encoded and **NOT BUILT**; it faults |

## What waits, and what does not

**A float unit issues one operation per cycle, back to back, including into the
same accumulator** — that is the entire point of the rotation, and it is true at
every unit count. What a unit count costs an *instruction* is the pass walk,
above.

A float accumulate is still in flight `ALAT` cycles after its instruction has
left the memory stage. Folding before it lands would drop it *and* capture its
result as a fold step, so `vfaccz`, `vfaccwr` and `vfaccrd` stall in decode until
the shadow is clear. It costs nothing in a real kernel: draining happens once at
the end of a reduction, not inside it.

`vfmacc` is deliberately not on that list.

The elementwise groups have their own scoreboard: **one bit per vector
register**, set at issue and cleared at the retiring write, because the unit has
one program counter and no waves and there is nothing to switch to while a deep
result is outstanding. Write-after-write is on that list as well as
read-after-write — two writes to one register would retire out of order, since a
later short instruction reaches the memory stage long before an earlier float
leaves the unit.

## The rounding property, stated exactly

`rv_fpu` aligns the addend into a **72-bit window** and the bits that fall
outside it are carried as a plain sticky. For an addition that is right: the
discarded bits make the true sum larger, and a sticky is exactly how "larger
than the guard says" is expressed. For a **subtraction** it is wrong — the
discarded bits make the true result *smaller*, so the residue would have to be
complemented before it rounds. Treated as an ordinary sticky it rounds up where
the true value rounds down, by one ulp.

The window is 72 bits against binary32's 24-bit significand, so the corner needs
the product to exceed the addend by more than 23 binades **and** the product's
low bits to sit exactly on a rounding midpoint.

The same file's `x − x` returns **−0** for a positive `x` where IEEE gives +0: an
exact cancellation keeps the losing term's sign, and there is no cancellation
term. The golden model reproduces both rather than correcting them, because a
bench that compared against a correctly-rounded reference would fail on the
hardware's own arithmetic.

## How the arithmetic is verified

The reference is `tests/pe/tools/khs_fp32.py`, and it carries **two**
multiply-adds:

| | |
|---|---|
| `fma_exact` | the definition. Multiply exactly, add exactly, round once |
| `fpu` | the machine: `rv_fpu` stage for stage, window and sticky included |

The RTL is compared bit-for-bit against the second, and the vector generators
report how often the two disagree — the deviation is measured rather than
assumed.

| bench | what it proves |
|---|---|
| `rv_fpu_tb` | the six-stage multiply-add computes the model's answer, **one vector per cycle** — a unit that only worked when operations were spaced out would pass a one-at-a-time bench and fail every kernel |
| `khs_sfu` | the four seeds on the bits, against the same integer table the RTL reads |
| `khs_facc` | the partials are right, in the rotation's own order |
| `khs_ffold` | the value a kernel actually reads — dependent steps through the same unit, in index order. Correct partials combined in the wrong order give a wrong answer that still looks like a float |
| `khs_unit_tb` | the assembled unit, decode included, against the golden model instruction by instruction — with the unit count in its configuration guard, so a vector/build mismatch names itself |

> **An unconnected input port is tied to zero, and zero is usually a legal
> opcode.** An operation select left unconnected reads as `z` in simulation and
> as an opcode-0 pass-through in synthesis, so the unit computes a plausible
> wrong answer rather than failing. Vivado reports it — `[Synth 8-7071]`, "port
> is unconnected" — and the **inputs** in that list are the ones that matter; an
> unconnected output is ordinary. [gates](gates.md) has the filter.

## What is not built

Nothing here computes something plausible instead. Where a line says *faults*,
it is a decode term in `khs_unit.v` and the instruction halts the unit at the
offending program counter.

| not built | what happens | why it is refused |
|---|---|---|
| `vfredsum.f32` | faults | it crosses the slots, which is a second pass that does not exist. The **golden model does implement it**, so the model is ahead of the RTL here |
| an operation slot above a group's last entry | faults | each decode has a `default` arm, so an unused slot would otherwise land on `rsqrt`, on multiply-add or on `i2f` |
| any element type but `f32` | faults | binary32 is the only compute type; the field is kept where the integer tier's is |

Returning slot 0 alone for a `vfredsum` would be a plausible wrong answer, which
is the one thing a refusal exists to prevent — a kernel finishes the cross-slot
sum with the integer reduction or in scalar code.

## Which files are the machine

| file | what it is |
|---|---|
| `src/kohakuaccel/pe/rv32/core/rv_fpu.v` | the fused multiply-add. Framework, because RV32F is standard binary32 |
| `src/kohakumpe/simd/khs_fp32_alu.v` | the elementwise array: units, the pass walk, the compare splat, the seed mux, the latency pad |
| `src/kohakumpe/simd/khs_fp32_sfu.v` | the four seeds |
| `src/kohakumpe/simd/khs_lead1.v` | the leading-one search both of them use |
| `src/kohakumpe/simd/khs_fcvt.v` | binary32 ↔ int32 |
| `src/kohakumpe/simd/khs_facc.v` | the rotating partials, and the per-pass seed window |
| `src/kohakumpe/simd/khs_ffold.v` | the fold sequencer, run once per pass |
