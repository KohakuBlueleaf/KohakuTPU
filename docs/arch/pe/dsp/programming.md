---
title: DSP programming guide
summary: The vector instruction set, how it is encoded, how to write it in assembly or C, and a requantise epilogue worked end to end.
tags:
  - architecture
  - pe
  - dsp
  - software
---

# DSP programming guide

The programmer's view is the base PE's ([programming](../programming.md)) plus
one register file, one accumulator file, one memory region, and 63
instructions. Everything else — the memory map, the control words, boot, the
push-and-doorbell idiom, the halt model — is unchanged.

## The state a program can name

| | |
|---|---|
| `x0..x31` | 32-bit scalar registers, as always. Addresses, trip counts, results of reductions |
| `v0..v7` | 256-bit vector registers. Eight in the shipped build; the count is a parameter, the encoding always allows 32 |
| `acc0`, `acc1` | accumulators, each `SIMD × int32` — one vector register wide |
| `0x4xxx_xxxx` | the vector scratchpad. `vld` and `vst` reach it; a scalar `sw` stages data into it; a scalar `lw` **faults** |

Vector register numbers are **immediates in the instruction word**, not
operands a compiler allocates. That is what lets a stock RISC-V toolchain
target this machine with no back-end fork, and it has one consequence worth
knowing before writing C: see [the C form](#the-c-form).

## The encoding

RISC-V custom-0 (`0x0B`) carries the whole integer tier. **Custom-1 (`0x2B`)
carries the floating-point tiers**, and a build with `DSP_F16 = 0` lacks those
encodings at the opcode major, so a program that uses one faults rather than
computing something plausible. The six that exist are on [float](float.md); the
rest of custom-1 is reserved and every build refuses it.

```
    31      25 24   20 19   15 14  12 11    7 6      0
   |  funct7  |  rs2  |  rs1  | fn3 |  rd   | 0001011 |    R-type
   |     imm[11:0]    |  rs1  | fn3 |  rd   | 0001011 |    I-type
```

`funct3` selects the group; `funct7` selects the operation within it, and for
every typed group its low two bits are the **element type** — 0 = int8,
1 = int16, 2 = int32. The datapath reads the element type straight off the
instruction word rather than out of a decode case.

| `funct3` | Group | `funct7` |
|---:|---|---|
| 0 | `VLD` | I-type: vector load |
| 1 | `VST` | I-type: vector store |
| 2 | `VINT` | `op<<2 \| et` — packed integer arithmetic |
| 3 | `VBIT` | `op` — bitwise, untyped |
| 4 | `VSHI` | `op<<2 \| et`, with the shift amount in `rs2` |
| 5 | `VMAC` | `op<<2 \| et` — dot product and the accumulators |
| 6 | `VMOV` | `op` — scalar/vector moves and reductions |
| 7 | `VPRM` | `op<<3 \| idx` — permute: slide, pack, unpack |

`vst` uses the I-type layout with its data register in the `rd` position. RV32's
S-format exists to keep `rs1` and `rs2` in place for the scalar register read;
a vector store's data comes from the *vector* file, so that constraint does not
apply and a single 12-bit immediate field is the simpler encoding.

The permute group spends three `funct7` bits on a lane index instead of two on
an element type, because `vsldw` needs somewhere to put one and an R-type has
no field left.

### One table, four consumers

The encoding is defined once, in a field table, and four consumers are
generated from or checked against it: the assembler, the golden model, the RTL
decode (`khd_isa.vh`), and the C intrinsic header (`khd_intrin.h`). A test
encodes and decodes every instruction through all four and fails on any
disagreement, which is what makes "one source of truth" a property rather than
an intention.

## The instructions

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
| `vmul` | element-wise product, low half kept (`.s8` and `.s16` only) |

**Bitwise**, untyped: `vand`, `vor`, `vxor`, `vandn` (`vs1 & ~vs2`).

**Immediate shifts**, each in three element widths:

| | |
|---|---|
| `vslli` / `vsrli` / `vsrai` | shift left, right logical, right arithmetic |
| `vsrari` | right arithmetic, **rounding** — the requantise primitive |

**Dot product and accumulators** — [accumulator](accumulator.md):

| | |
|---|---|
| `vdot.s8` / `vdot.s16` | `acc[ad] +=` the dot product within each 32-bit lane |
| `vdotn.s8` / `vdotn.s16` | the same, subtracted |
| `vaccz ad` | `acc[ad] ← 0` |
| `vaccwr ad, vs1` | `acc[ad] ← vs1` — how a bias vector seeds an accumulation |
| `vaccrd vd, as1` | `vd ← acc[as1]`, as int32 lanes |

**Moves and reductions**, the only vector instructions that write a scalar
register:

| | |
|---|---|
| `vsplat vd, xs1` | every 32-bit lane of `vd` ← `xs1` |
| `vextr xd, vs1, k` | `xd` ← 32-bit lane `k` |
| `vredsum xd, vs1` | `xd` ← the sum of the 32-bit lanes |
| `vredmax xd, vs1` | `xd` ← the signed maximum of the 32-bit lanes |

**Permute** — [lanes](lanes.md#when-lanes-must-talk):

| | |
|---|---|
| `vsldw0..7 vd, vs1, vs2` | lane *i* ← lane `(k+i)` of `{vs2, vs1}` |
| `vpack.s16` / `vpack.s32` | two vectors narrowed to one, signed saturating |
| `vunpkl.s8` / `vunpkh.s8` | the low or high int8s of `vs1`, widened to int16 |
| `vunpkl.s16` / `vunpkh.s16` | likewise int16 → int32 |

## The assembly form

The assembler that ships with the PE test tooling accepts these mnemonics
directly, with `v0..v7` for vector registers and `acc0..acc1` for accumulators.
Accumulators are deliberately *not* spelled `a0` — that is a scalar ABI name,
and a program that meant one and wrote the other should not assemble.

```
        li      s0, 0x40000000          the vector scratchpad
        vld     v0, 0(s0)
        vld     v1, 32(s0)
        vadd.s16 v2, v0, v1
        vst     v2, 64(s0)
```

An immediate is a byte offset, as in RV32. A vector is 32 bytes at eight lanes,
so consecutive vectors are 32 apart — and an offset that is not a multiple of
that faults rather than silently reading across a row boundary.

## The C form

Every instruction has a macro in the generated header, written as a `.insn`
statement so that a stock RISC-V GCC assembles it with no knowledge of this
extension:

```c
#include "khd_intrin.h"

int32_t *a = (int32_t *)KHD_VSPAD_BASE;

khd_vld(0, a, 0);                   /* v0 <- a[0..7]  */
khd_vld(1, a, 32);                  /* v1 <- a[8..15] */
khd_vadd_s32(2, 0, 1);              /* v2 <- v0 + v1  */
khd_vst(2, a, 64);
```

Register numbers are macro arguments because they are immediates in the
encoding. **Every intrinsic is `volatile`, and that is a real cost, not
caution**: the compiler cannot see the vector state at all — two identical
`khd_vdot` calls are not one value, they accumulate — so it may not reorder,
hoist, or common them, and it cannot software-pipeline the vector datapath.

On an in-order single-issue core whose multi-cycle operations stall in the
existing hazard unit, that costs much less than it would on a machine with a
scheduler to lose. It is the honest price of not forking a compiler, and it is
the concrete argument for a real back end if scheduling ever turns out to
matter.

## A requantise epilogue, worked

The kernel a quantised network ends every layer with: add a bias, apply ReLU,
shift back down with rounding, saturate to int8, pack. On the scalar core it is
fifteen instructions per element. Here it is five vector instructions per 8
elements, plus the packing:

```
        li      t1, 12345
        vsplat  v7, t1                  the bias, broadcast to every lane
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
replacing the six-instruction branchless min/max clamp the scalar form needs
per element.

The whole epilogue over 256 elements is **242 cycles against the scalar
form's 8,025** — [performance](performance.md).

## Rules a kernel must respect

- **Vector addresses are aligned** to the vector width, or they fault.
- **The vector scratchpad is store-only from the scalar side.** Stage data with
  `sw`, read it with `vld`.
- **`vsldw` slides by lanes, not elements.** Sliding int16 data by one element
  means widening it to int32 first — or laying the data out so the slide is a
  lane slide.
- **Reductions cross lanes; arithmetic does not.** Keep `SIMD` running totals
  in a vector register and reduce once at the end, rather than reducing inside
  a loop.
- **A build that lacks a feature faults on its encodings.** A kernel written
  for the shipped configuration and run on a narrower one halts with an illegal
  instruction at the offending PC rather than computing something else.
</content>
