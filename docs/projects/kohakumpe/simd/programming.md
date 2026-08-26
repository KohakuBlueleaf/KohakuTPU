---
title: SIMD PE programming guide
summary: The vector instruction set — 56 packed-integer and 21 float — how it is encoded, how to write it in assembly or C, a requantise epilogue worked end to end, and the rules a kernel must respect.
tags:
  - architecture
  - pe
  - simd
  - software
---

# SIMD PE programming guide

> **Kind: Yours throughout — one project's instruction set.** The packed-integer
> and float instructions, their encoding and the rules a kernel must respect are
> this project's spending of its own payload bits. How those bits reach the unit
> is Fixed protocol
> ([spec/instruction-encoding](../../../spec/instruction-encoding.md)); what they
> mean is not.

The programmer's view is the base PE's
([programming](../../../arch/cpu/rv32-pe/programming.md)) plus one register file, one
accumulator file, one memory region, and **77 instructions — 56 integer on
custom-0 and 21 float on custom-1**. Everything else — the memory map, the
control words, boot, the push-and-doorbell idiom, the halt model — is unchanged.

The scalar half is unchanged too, and that includes what it lacks. It is
**RV32IM**: `mul`, `mulh`, `mulhsu` and `mulhu` are always built — the base
core's own
[multiplier](../../../arch/cpu/rv32-pe/microarchitecture.md#the-multiplier) —
and `div`, `divu`, `rem` and `remu` are **not built and fault**. The assembler
refuses the divide mnemonics outright and the decoder faults the encodings, so a
divide cannot be assembled or executed by accident. Divide-by-a-constant
strength-reduces to `mulhu`, which is the case software actually meets;
[why divide is a different answer](../../../arch/cpu/rv32-pe/microarchitecture.md#why-div-and-rem-are-a-different-answer)
is the base core's, not this unit's.

## The state a program can name

| | |
|---|---|
| `x0..x31` | 32-bit scalar registers. Addresses, trip counts, the results of reductions |
| `v0..v7` | vector registers, `32 × SIMD` bits. Eight in the reference build; the count is a parameter, the encoding always allows 32 |
| `facc0`, `facc1` | **float accumulators**, present when `HAS_FACC` is set: one binary32 slot per element, over `NPART` rotating partials. A program sees one accumulator per name and never learns the latency; the rotation order is contract — [float](float.md) |
| `0x4xxx_xxxx` | the vector scratchpad. `vld` and `vst` reach it; a scalar `sw` stages data into it; a scalar `lw` **faults** |

There are **no integer accumulators**. A dot product is `vmul` then `vredsum`;
the `VMAC` group is reserved and unmapped, so the old encodings fault rather
than decoding as something adjacent — [accumulator](accumulator.md).

Vector register numbers are **immediates in the instruction word**, not operands
a compiler allocates. That is what lets a stock RISC-V toolchain target this
machine with no back-end fork, and it has one consequence worth knowing before
writing C: see [the C form](#the-c-form).

## The encoding

RISC-V custom-0 (`0x0B`) carries the whole integer tier. **Custom-1 (`0x2B`)
carries the float tier**, and a build with `FLOAT_LANES = 0` lacks those
encodings at the opcode major, so a program that uses one faults rather than
computing something plausible.

```
    31      25 24   20 19   15 14  12 11    7 6      0
   |  funct7  |  rs2  |  rs1  | fn3 |  rd   | 0001011 |    R-type, custom-0
   |     imm[11:0]    |  rs1  | fn3 |  rd   | 0001011 |    I-type, custom-0
   |  funct7  |  rs2  |  rs1  | fn3 |  rd   | 0101011 |    R-type, custom-1
```

`funct3` selects the group; `funct7` selects the operation within it, and for
every typed group its low two bits are the **element type** — 0 = int8,
1 = int16, 2 = int32 on custom-0, and `f32` the only accepted value on custom-1.
The datapath reads the element type straight off the instruction word rather
than out of a decode case.

| `funct3` | custom-0 group | `funct7` |
|---:|---|---|
| 0 | `VLD` | I-type: vector load |
| 1 | `VST` | I-type: vector store |
| 2 | `VINT` | `op<<2 \| et` — packed integer arithmetic |
| 3 | `VBIT` | `op` — bitwise, untyped |
| 4 | `VSHI` | `op<<2 \| et`, with the shift amount in `rs2` |
| 5 | `VMAC` | **reserved and unmapped** — every encoding faults |
| 6 | `VMOV` | `op` — scalar/vector moves and reductions |
| 7 | `VPRM` | `op<<3 \| idx` — permute: slide, pack, unpack |

| `funct3` | custom-1 group | contains |
|---:|---|---|
| 0 | `FMAC` | the rotating accumulator and everything that touches it |
| 1 | `FRED` | cross-slot reduction. **Encoded, not built** — it faults |
| 2 | `FCVT` | binary32 ↔ int32 |
| 3 | `FALU` | multiply, add, subtract, fused multiply-add, min, max, compare |
| 4 | `FSFU` | the four seeds |

`vst` uses the I-type layout with its data register in the `rd` position. RV32's
S-format exists to keep `rs1` and `rs2` in place for the scalar register read; a
vector store's data comes from the *vector* file, so that constraint does not
apply and a single 12-bit immediate field is the simpler encoding.

The permute group spends three `funct7` bits on a lane index instead of two on
an element type, because `vsldw` needs somewhere to put one and an R-type has no
field left.

### One table, four consumers

The encoding is defined once, in a field table, and four consumers are generated
from or checked against it: the assembler, the golden model, the RTL decode
(`khs_isa.vh`) and the C intrinsic header (`khs_intrin.h`). A test encodes and
decodes every instruction through all four and fails on any disagreement, which
is what makes "one source of truth" a property rather than an intention.

> **An encoding test proves nothing about execution.** An instruction can
> round-trip perfectly through all four consumers, set a write enable, and have
> no datapath behind it. What catches that is running each instruction on the
> RTL and comparing the result against the model —
> [gates](gates.md#decode-without-datapath) has the detection rule for the class.

## The integer instructions

**Memory.** Line-aligned by contract — an address that is not a multiple of the
vector width faults.

| | |
|---|---|
| `vld vd, imm(xs1)` | `vd` ← the vector at `xs1 + imm` |
| `vst vs, imm(xs1)` | the vector at `xs1 + imm` ← `vs` |

**Packed integer**, each in `.s8`, `.s16` and `.s32` forms:

| | |
|---|---|
| `vadd` / `vsub` | element-wise, wrapping |
| `vsadd` / `vssub` | element-wise, signed saturating |
| `vmin` / `vmax` | element-wise signed minimum / maximum |
| `vmul` | element-wise product, low half kept — **`.s8` and `.s16` only** |

**Bitwise**, untyped: `vand`, `vor`, `vxor`, `vandn` (`vs1 & ~vs2`).

**Immediate shifts**, each in three element widths:

| | |
|---|---|
| `vslli` / `vsrli` / `vsrai` | shift left, right logical, right arithmetic |
| `vsrari` | right arithmetic, **rounding** — the requantise primitive |

**Moves and reductions**, the only vector instructions that write a scalar
register:

| | |
|---|---|
| `vsplat vd, xs1` | every 32-bit slot of `vd` ← `xs1` |
| `vextr xd, vs1, k` | `xd` ← 32-bit slot `k`. A `k` at or above `SIMD` **faults** |
| `vredsum xd, vs1` | `xd` ← the sum of the 32-bit slots |
| `vredmax xd, vs1` | `xd` ← the signed maximum of the 32-bit slots |

**Permute** — [lanes](lanes.md#when-lanes-must-talk):

| | |
|---|---|
| `vsldw0..7 vd, vs1, vs2` | slot *i* ← slot `(k+i)` of `{vs2, vs1}` |
| `vpack.s16` / `vpack.s32` | two vectors narrowed to one, signed saturating |
| `vunpkl.s8` / `vunpkh.s8` | the low or high int8s of `vs1`, widened to int16 |
| `vunpkl.s16` / `vunpkh.s16` | likewise int16 → int32 |

## The float instructions

The compute format is IEEE binary32 in every build and there is no parameter
anywhere that changes it. A vector register holds **one binary32 element per
32-bit slot**, so at `SIMD` 8 it is eight elements. **How many float units
compute them is a separate parameter** — `FLOAT_LANES` — and an instruction
takes `elements / units` cycles rather than one
([float](float.md#elements-units-and-passes)).

| | |
|---|---|
| `vfmul.f32` `vfadd.f32` `vfsub.f32` `vfma.f32` | elementwise |
| `vfmin.f32` `vfmax.f32` | IEEE minNum / maxNum; a NaN operand loses |
| `vfcmplt.f32` `vfcmpgt.f32` `vfcmpeq.f32` | write **all ones or all zeros per element** — blend with `vand` / `vandn` / `vor` |
| `vfexp2.f32` `vflog2.f32` `vfrcp.f32` `vfrsqrt.f32` | the seeds. Newton refinement is an instruction sequence, deliberately |
| `vfcvt.f2i.f32` `vfcvt.i2f.f32` | binary32 ↔ int32 |
| `vfaccz ad` | `facc[ad] ← 0`, every slot. Untyped: zero is zero |
| `vfmacc.f32 ad, vs1, vs2` | `facc[ad][i] += vs1[i] * vs2[i]`, elementwise |
| `vfmsac.f32 ad, vs1, vs2` | the same, subtracted |
| `vfaccwr.f32 ad, vs1` | `facc[ad] ← vs1` — how a bias vector seeds an accumulation |
| `vfaccrd.f32 vd, as1` | fold the partials and return one binary32 per element |
| `vfredsum.f32 xd, as1` | **encoded and not built; it faults** |

```
        vfaccz    facc0                 clear all eight slots -- NPART cycles
   loop:
        vld       v0, 0(s0)             32 bytes = 8 binary32 activations
        vld       v1, 0(s1)             8 binary32 weights
        vfmacc.f32 facc0, v0, v1        8 fused multiply-adds; 2 cycles at four
                                        float units, 1 at eight
        addi      s0, s0, 32
        addi      s1, s1, 32
        addi      s2, s2, -1
        bnez      s2, loop
        vfaccrd.f32 v2, facc0           fold: 112 + passes cycles at the defaults
        ...                             cross the slots in integer or scalar code
```

Four properties of that loop are the tier and are worth reading before writing
one:

- **A float unit never stalls, including back to back into the same
  accumulator.** The unit is several cycles deep and the accumulator rotates
  underneath it. What a unit count costs the *instruction* is the pass walk:
  `vfmacc` issues `elements / units` operations, one per cycle, holds the memory
  stage until the last has gone, and **retires once**. Nothing in the program
  sees the passes.
- **The accumulation order is contract, not an implementation detail.** Element
  *i*, on the *n*th accumulate since `vfaccz`, lands on partial
  `(n × passes + i / units) mod NPART`. Float addition does not associate, so a
  build with a different `NPART` **or a different `FLOAT_LANES`** computes
  different answers on the same program, and the golden model takes both numbers.
- **Everything that observes or disturbs an accumulator waits.** `vfaccz` and
  `vfaccwr` sweep a one-write-port memory, `vfaccrd` folds, and all three stall
  in decode until the shadow of the last `vfmacc` is clear. It is once per
  reduction against a kernel of thousands.
- **There is no cross-slot reduction.** `vfaccrd` gives one binary32 per element
  chain; summing them is scalar code. `vfredsum` faults rather than returning
  slot 0 alone.

**The elementwise groups issue one instruction at a time**, at an interval of
`ALAT + passes` rather than `passes` — a known limit of the shared staging
register, named in
[configurations](configurations.md#the-one-rate-limit-the-float-tier-still-has).
The accumulator group does not have it.

## The assembly form

The assembler that ships with the PE test tooling accepts these mnemonics
directly, with `v0..v7` for vector registers and `facc0..facc1` for
accumulators. Accumulators are deliberately *not* spelled `a0` — that is a
scalar ABI name, and a program that meant one and wrote the other should not
assemble.

```
        li      s0, 0x40000000          the vector scratchpad
        vld     v0, 0(s0)
        vld     v1, 32(s0)
        vadd.s16 v2, v0, v1
        vst     v2, 64(s0)
```

An immediate is a byte offset, as in RV32. A vector is 32 bytes at `SIMD` 8, so
consecutive vectors are 32 apart — and an offset that is not a multiple of that
faults rather than silently reading across a row boundary.

## The C form

Every instruction has a macro in the generated header, written as a `.insn`
statement so that a stock RISC-V GCC assembles it with no knowledge of this
extension:

```c
#include "khs_intrin.h"

int32_t *a = (int32_t *)KHS_VSPAD_BASE;

khs_vld(0, a, 0);                   /* v0 <- a[0..7]  */
khs_vld(1, a, 32);                  /* v1 <- a[8..15] */
khs_vadd_s32(2, 0, 1);              /* v2 <- v0 + v1  */
khs_vst(2, a, 64);
```

Register numbers are macro arguments because they are immediates in the
encoding. **Every intrinsic is `volatile`, and that is a real cost, not
caution**: the compiler cannot see the vector state at all — two identical calls
are not one value when they accumulate — so it may not reorder, hoist or common
them, and it cannot software-pipeline the vector datapath.

On an in-order single-issue core whose multi-cycle operations stall in the
existing hazard unit, that costs much less than it would on a machine with a
scheduler to lose. It is the honest price of not forking a compiler, and it is
the concrete argument for a real back end if scheduling ever turns out to matter.

## A requantise epilogue, worked

The kernel a quantised network ends every layer with: add a bias, apply ReLU,
shift back down with rounding, saturate to int8, pack. On the scalar core it is
fifteen instructions per element. Here it is five vector instructions per eight
elements, plus the packing:

```
        li      t1, 12345
        vsplat  v7, t1                  the bias, broadcast to every slot
        li      t1, 0
        vsplat  v6, t1                  zero, for the ReLU

   loop:
        vld     v0, 0(s0)               8 int32 accumulators
        vld     v1, 32(s0)              8 more
        vadd.s32   v0, v0, v7           bias
        vadd.s32   v1, v1, v7
        vmax.s32   v0, v0, v6           ReLU: max against zero
        vmax.s32   v1, v1, v6
        vsrari.s32 v0, v0, 11           requantise, ROUNDING
        vsrari.s32 v1, v1, 11
        vpack.s32  v2, v0, v1           16 int32 -> 16 int16, saturating
        ...                             the same for the next 16
        vpack.s16  v4, v2, v3           32 int16 -> 32 int8, saturating
        vst     v4, 0(s1)
```

Three things in that sequence are the reason the tier includes what it does.
`vmax` against a broadcast zero is a branchless ReLU — no mask, no predicate.
`vsrari` rounds rather than truncates, which is what a real requantise does and
what a plain `vsrai` gets subtly wrong. And `vpack` saturates on the way down,
replacing the six-instruction branchless clamp the scalar form needs per element.

The whole epilogue over 256 elements is **242 cycles against the scalar form's
8,025** — [performance](performance.md).

## Rules a kernel must respect

- **Vector addresses are aligned** to the vector width, or they fault.
- **The vector scratchpad is store-only from the scalar side.** Stage data with
  `sw`, read it with `vld`.
- **`vsldw` slides by 32-bit slots, not elements.** Sliding int16 data by one
  element means widening it to int32 first — or laying the data out so the slide
  is a slot slide.
- **Reductions cross slots; arithmetic does not.** Keep `SIMD` running totals in
  a vector register and reduce once at the end, rather than reducing inside a
  loop.
- **A build that lacks a feature faults on its encodings.** A kernel written for
  one configuration and run on a narrower one halts with an illegal instruction
  at the offending program counter rather than computing something else. A build
  that merely has a feature *narrower* runs the same kernel with the same
  answers and more cycles.
- **`NPART` and `FLOAT_LANES` change float answers** when the accumulator is
  used. Neither is a tuning knob there: the accumulation order is architectural,
  and a kernel validated at one value is not validated at another. The
  elementwise groups carry no such contract.
- **A float accumulator must be zeroed before it is used.** The partials are a
  memory and a memory has no reset — `vfaccz` is what makes them zero, and a
  kernel that skips it reads whatever the RAM powered up holding.
- **Scalar arithmetic is RV32IM.** `mul` and its three high halves are there;
  divide, remainder and float in `x0..x31` are not, and a C expression that
  divides calls libgcc. Move the work into a vector instruction, or let the
  compiler strength-reduce a constant divisor to `mulhu`.
