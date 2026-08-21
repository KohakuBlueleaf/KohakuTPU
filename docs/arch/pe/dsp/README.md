---
title: DSP PE
summary: The controller PE with a wide packed-integer datapath behind it — one program counter, one instruction, many lanes. What it is, what it costs, and where to read about each part.
tags:
  - architecture
  - pe
  - rv32
  - dsp
  - simd
---

# DSP PE

`src/kohakuaccel/pe/rv32/dsp/` — the [controller PE](../README.md) with a
vector unit attached at its EX stage. Everything the base core is stays true:
RV32I, one port on the fabric, the same kick and the same completion. What is
added is a second register file, a second scratchpad, and an array of lanes
that all execute **the same instruction at the same time**.

One number locates it: the assembled PE is **10,430 LUT, 32 DSP, 13 BRAM at
361.9 MHz** with eight lanes on `xcvu13p`, against 2,477 LUT at 377.9 MHz for
the same PE with the extension switched off. The extension is area; it costs
16 MHz — [performance](performance.md).

## The problem it solves

A controller PE is fast at deciding and slow at arithmetic, and the arithmetic
it is slow at is the regular kind. Three of its own kernels say it plainly:

| What the base core spends | On |
|---:|---|
| 8,221 cycles | a 128-element int8 dot product |
| 8,025 cycles | a 256-element requantise epilogue: bias, ReLU, rounding shift, saturate, pack |
| 3,090 cycles | summing and max-ing 256 int32 |

None of that work is serial. Every element is independent, every element gets
the same treatment, and the core is executing one 32-bit operation per cycle
because that is the only shape it has. Widening the datapath and letting one
instruction drive eight of them takes the same three kernels to **52, 242 and
248** cycles.

That is the whole thesis, and it is also the boundary: this design goes wide
on work that is *uniform*. When lanes need to take different paths, or fetch
from different addresses, the answer is a different machine and not a wider
one — see [what this does not own](#what-this-pe-does-not-own).

## The shape

```
                          ONE program counter
                          ONE instruction stream
                                   |
        +--------------------------+---------------------------+
        |                                                      |
   +----v---------------------+          +---------------------v----------+
   |   the base RV32I core    |          |   the vector unit  (khd_unit)  |
   |                          |          |                                |
   |  x0..x31, 32 bits        |          |  v0..v7, 256 bits              |
   |  scratchpad, 32-bit face |          |  acc0..acc1, 8 x int32         |
   |  branches, loads, loop   |          |  vector scratchpad, 256-bit    |
   |  counters, addresses     |          |                                |
   |                          |          |   lane0 lane1 ... lane7        |
   |                          |          |   [32b] [32b]     [32b]        |
   +--------------------------+          +--------------------------------+
        the address, the trip count           the elements, all of them
```

The scalar core keeps doing what it is good at: addresses, trip counts,
branches. The vector unit never computes an address and never takes a branch.
A loop is a scalar loop whose body happens to move 32 bytes at a time.

## The pages

| Page | What is in it |
|---|---|
| [lanes](lanes.md) | what a lane **is**, how one instruction drives eight of them, and how four int8 elements share one 32-bit adder |
| [accumulator](accumulator.md) | the dot product cycle by cycle: four products per lane, one accumulator file, and why a stream of them never stalls |
| [memory](memory.md) | the vector scratchpad's banks and its two faces, the vector register file, and how a 256-bit load relates to the 32-bit face the scalar core reads |
| [pipeline](pipeline.md) | where the unit sits among the base pipeline's six register boundaries, its four hazards, and why the scalar critical path is untouched |
| [programming](programming.md) | the instruction set, the assembler, the C intrinsics, and a kernel written three ways |
| [float](float.md) | the FP16 tier: E8M15 in the datapath, an accumulator of rotating partials that breaks a fifteen-cycle recurrence, and the rounding property stated exactly |
| [performance](performance.md) | what every configuration costs, and what each packed instruction buys against the scalar sequence it replaces |

If you have never read a SIMD datapath before, read [lanes](lanes.md) and
then [accumulator](accumulator.md); those two are the machine. If you are
choosing a configuration, [performance](performance.md) is the page.

## Fixed protocol, parameter, or yours

| Thing | Category |
|---|---|
| everything the base PE fixes — regions, ordering, halting, kick and completion | **fixed protocol** of the base unit: [architecture](../architecture.md) |
| the vector scratchpad region and its store-only rule from the scalar side | **fixed protocol** of this unit — [memory](memory.md) |
| the instruction encoding: custom-0 for the integer tier, custom-1 for the float one | **fixed protocol** of this unit — [programming](programming.md) |
| the float accumulator's rotation order, and `DSP_NPART` with it | **fixed protocol** — float addition does not associate, so the count changes the answers: [float](float.md) |
| `DSP_SIMD`, `DSP_VREGS`, `DSP_NACC`, `DSP_MULS`, `DSP_SHIFT`, `DSP_PERM`, `DSP_VSPAD` | **parameters** — every one is measured as itself: [performance](performance.md) |
| `DSP_F16` | **a parameter**: at 0 the float tier is not elaborated and custom-1 is unmapped |
| `DSP_EN = 0` | **a parameter too**: the unit disappears, and the PE is bit-identical to the base core |
| what a kernel computes | **yours** |

## What this PE does not own

| Concern | Whose |
|---|---|
| per-lane branching, per-lane addresses, masks and predication | a SIMT core's. Nothing here anticipates them, and adding them here would cost every uniform kernel |
| floating point beyond the FP16 accumulator | `vfmacc` and its four companions are built ([float](float.md)); elementwise float, compare and min/max are encodings custom-1 reserves and no build carries, and FP32 lanes are priced and off |
| where operands come from before the scratchpad | [mas](../../mas/) — the memory agent fills the vector scratchpad the same way it fills any window |
| the flit, the router, the port | [noc](../../noc/) |
