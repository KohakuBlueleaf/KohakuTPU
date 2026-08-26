---
title: SIMT PE
summary: The controller PE rebuilt so an ordinary RV32I opcode addresses a per-thread register file — what it is, where it sits, what it is for, and what it deliberately does not do.
tags:
  - architecture
  - pe
  - rv32
  - gpu
  - simt
---

# SIMT PE

`src/kohakumpe/simt/` — the framework's
[controller PE](../../../arch/cpu/rv32-pe/README.md) rebuilt so that an ordinary RV32I
opcode addresses a **per-thread** register file. It is a compute unit like any
other: one port on the mesh, the same kick that starts it, the same completion
that retires it.

Two terms, before anything else. A **thread** is one lane's worth of
architectural state — its own `x0..x31`, its own address, its own side of a
branch. A **wave** is a group of `LANES` threads that share one program counter
and issue together; the core holds `WAVES` of them resident and picks one to
issue from every cycle.

**Current state is on one page: [status](status.md).** It says what exists, what
has been run, and what has not been built. Read it before quoting anything from
here.

## Where it sits

Above it is the mesh — instructions arrive as a kick, a shader image arrives as
a burst into its instruction window, and completion goes back the same way.
Below it are `kht_unit` (the per-thread register file, the active mask, the
divergence stack and the lane arrays) and `kht_lds` (the banked shared memory).
Beside it is the [SIMD PE](../simd/README.md), which is the same framework
controller PE with a *uniform* wide datapath instead.

## The problem it solves

The [SIMD PE](../simd/README.md) goes wide on work that is *uniform*: every lane
does the same thing to a different element at a stride the scalar side computed.
That covers dense linear algebra and it covers it well. It does not cover the
case where lanes need to **disagree** — where lane 3 takes the `if` and lane 4
takes the `else`, or where the address lane 5 wants is in a table rather than at
a stride.

That is not a wider machine, it is a different one. This PE is that machine, and
the whole question it exists to answer is **what that capability costs**, in
LUT, on this fabric, against a lane array that is otherwise identical. The
design is arranged so the answer is a measurement rather than an estimate —
[ladder](ladder.md).

## The shape

The base ISA slot is spent on the **per-thread** side. That is AMD GCN's
scalar/vector split with the polarity inverted, and it is deliberate: a shader
is mostly per-thread work, so the per-thread half should get the cheap encoding.

```
                     ONE program counter per WAVE
                     ONE instruction at a time, any wave
                                |
      +-------------------------+--------------------------+
      |                                                    |
 +----v-----------------------+        +-------------------v-----------+
 |  the scalar half           |        |  the per-thread half          |
 |  behind custom-2/-3        |        |  ORDINARY RV32I               |
 |                            |        |                               |
 |  s0..s31, per WAVE         |        |  x0..x31, per LANE per WAVE   |
 |  base pointers, uniform    |        |  active mask + divergence stk |
 |  branches, trip counts     |        |  LANES integer lanes          |
 |                            |        |  FLANES binary32 FMA units    |
 |                            |        |                               |
 |                            |        |   lane0 lane1 ... lane7       |
 |                            |        |   [32b] [32b]     [32b]       |
 +----------------------------+        +-------------------------------+
      the base, the uniform test            the threads, all of them
                                                     |
                                          +----------v----------+
                                          |  the LSU            |
                                          |  one address        |
                                          |  PER LANE           |
                                          +---------------------+
```

Three things follow from that picture, and they are the three things this PE has
that the SIMD tier does not:

1. **A mask, not a predicate on the datapath.** An inactive lane computes
   whatever it computes and its *write* is dropped. Masking costs one enable per
   bank and nothing on the arithmetic path.
2. **A divergence stack**, so `split` and `join` implement structured divergence
   exactly, without the compiler proving anything about uniformity.
3. **An address per lane**, with three addressing tiers already distinguished in
   the encoding so a coalescer can replace the current serial walk without the
   ISA moving.

## Eight integer lanes is a constraint; the float count is a choice

```
   8 lanes x 32 bit  =  256 bit  =  one native memory entry  =  one flit payload
```

**The integer lanes are the address path.** A contiguous 32-bit load by eight
threads is exactly one memory read request, and that is the strongest
machine-level alignment in the design. Narrow the integer side and the alignment
breaks permanently: every coalesced load becomes two or more requests, for every
kernel, forever. A SIMT processor is also *defined* by its threads, so `LANES`
is the one width on either PE set by definition rather than by measurement.

Float has no such constraint. It is pure arithmetic, deeply pipelined at one
instruction per cycle, and a core with sixteen resident wave contexts hides its
latency rather than stalling on it — latency is the cheapest thing to trade in a
throughput machine.

```
   integer lanes  <-  the memory granule  (256-bit entry / flit)   LANES, fixed
   float units    <-  arithmetic demand   (throughput vs LUT)      FLANES, a knob
   seed units     <-  transcendental rate                          FSFU_UNITS
```

`FLANES < LANES` is a **working configuration**: a thread count above the unit
count is served by `LANES / units` passes, one per cycle, placed by the register
file's per-lane write enable. The instruction set carries no count — the same
shader image, the same golden memory, and only the cycles change.

*Fewer integer lanes than float units* is rejected outright: it starves
addressing to feed arithmetic, so every memory operation serialises while the
float units wait.

## One float format, and it is not a setting

```
   IEEE binary32 in   ->   binary32 compute   ->   binary32 out
```

Binary32 is the only compute type, so a thread is a whole 32-bit slot: no format
bit, no conversion at either edge, no reserved half of a register. **The
arithmetic is the [SIMD tier](../simd/float.md)'s and is never forked** — every
unit is one `rv_fpu`, and a seed unit carries a `khs_fp32_sfu` beside it. That
is what makes a SIMT float result comparable to a SIMD float result element for
element, and what keeps the cost identity in [ladder](ladder.md) meaningful.

KohakuTPU's vector core keeps its own E8M15 format and its own modules and is
untouched by any of this.

## Rendering is genuinely mixed

Float is **not optional** for this PE — rendering needs it. But the integer half
is not a leftover from a compute machine either: rasterisation and depth are
integer **because float gets them wrong**.

| stage | needs | kind |
|---|---|---|
| rasterisation, edge equations | exact, watertight edge functions on a subpixel grid | integer |
| depth interpolation and buffer | 24-bit fixed point; a short significand z-fights | integer / fixed |
| texture addressing | wrap, clamp, mip select, Morton swizzle | integer / bitwise |
| texture filtering | fixed-point or float weights | float |
| fragment and colour shading | binary32 exceeds what mobile fragment shaders run at | float |
| vertex transform | products into a float accumulator | float |

Those are exactness requirements, not performance ones.

**Two of those integer rows are why RV32M is built.** A pixel index is
`y * width + x` and a mip or Morton address is a multiply, and without a
multiplier each of those is a software shift-add chain running on every lane of
every fragment. `mul`, `mulh`, `mulhsu` and `mulhu` are one 33×33 signed product
per lane; divide and remainder are deliberately absent, because
divide-by-a-constant strength-reduces to `mulhu`.

## The pages

| page | what is in it |
|---|---|
| [status](status.md) | **what exists and what is measured, as of a stated date** — the ground truth for progress |
| [isa](isa.md) | the instruction set: the two custom opcode majors, the six groups, the float tier and RV32M, divergence, subgroup operations, the three memory tiers — and the one field table all consumers are generated from |
| [microarchitecture](microarchitecture.md) | how it is built: the pipeline, the mask and its stack, the lane-serialising LSU, the shared shadow pipe both multi-cycle units retire through, the halt-and-flush, and a symptom-to-cause reference |
| [ladder](ladder.md) | the method — why the design is parameters rather than branches, and what each gate measured |
| [comparison](comparison.md) | what the arithmetic width is worth against shipped mobile GPUs, and the fixed function that decides a frame rather than the arithmetic |

If you want the number, [ladder](ladder.md). If you want what the number is
*worth*, [comparison](comparison.md). If you are writing a shader,
[isa](isa.md). If you are changing the RTL,
[microarchitecture](microarchitecture.md) first.

## Fixed protocol, parameter, or yours

| thing | kind |
|---|---|
| everything the base PE fixes — regions, ordering, halting, kick and completion | **fixed protocol** of the base unit: [architecture](../../../arch/cpu/rv32-pe/architecture.md) |
| the instruction encoding: custom-2 for the R-type groups, custom-3 for the I-type ones | **fixed protocol** of this unit — [isa](isa.md), and [opcode-map](../../../arch/cpu/rv32-pe/opcode-map.md) is the authority on who owns which major |
| RV32M at its **standard** encoding — the existing register-register group — and the float group in a custom-2 `funct3` slot | **fixed protocol**. No new opcode major was spent on either; all four customs were already claimed |
| the compute format: IEEE binary32 | **not a parameter at all** |
| a per-thread conditional branch is **not encodable** | **fixed protocol** — a per-thread condition reaching one program counter is undefined, so the encoding refuses it rather than trusting a proof of uniformity |
| a `split` pushes **two** entries and a `join` pops **one**; depth D permits D/2 nested levels, and overflow is a **fault** | **fixed protocol** of this unit — [isa](isa.md) |
| a halt **flushes** before it completes | **fixed protocol** — the completion means the stores are in memory, not merely issued |
| `LANES`, `WAVES`, `FLANES`, `FSFU_UNITS`, `SHFL_UNITS`, `LDS_BANKS`, `HAS_MASK`, `HAS_IPDOM`, `IPDOM_D`, `VREG_PRIM`, `MEM_PRIM` and the memory depths | **parameters**, each measured as itself: [ladder](ladder.md), [unit-counts](../unit-counts.md) |
| a feature at zero **faults** rather than computing something plausible | **fixed protocol** of this unit. A plausible-looking wrong number is worse than a halt, and knowing is the point of building a narrow configuration |
| what a shader computes | **yours** |

## What this PE does not own

| concern | whose |
|---|---|
| uniform wide arithmetic, packed integer element types, the float **accumulator** | the [SIMD PE](../simd/README.md)'s. This one does not anticipate them. The float *unit* is a different matter — it is single-sourced and never forked |
| the arithmetic inside a float unit | `rv_fpu` and `khs_fp32_sfu`. This PE selects operands and places results; it does not compute |
| memory-latency hiding, and coalescing | not built — [microarchitecture](microarchitecture.md#what-this-core-does-not-do) |
| where operands come from before the scratchpad | [sysnode](../../../arch/sysnode/) |
| the flit, the router, the port | [noc](../../../arch/noc/) |
| which custom opcode major belongs to which PE class | [opcode-map](../../../arch/cpu/rv32-pe/opcode-map.md) — a table one tier owns is not something another can check itself against |
| where the PE lands on the die and at what clock | [physical](../../../arch/physical/) |
