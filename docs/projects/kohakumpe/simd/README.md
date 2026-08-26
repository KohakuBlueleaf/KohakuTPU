---
title: SIMD PE
summary: The controller PE with a wide uniform datapath attached to its execute stage — what it is, where it sits, what is a parameter and what is not, and what it deliberately does not do.
tags:
  - architecture
  - pe
  - rv32
  - simd
---

# SIMD PE

`src/kohakumpe/simd/` — the framework's
[controller PE](../../../arch/cpu/rv32-pe/README.md) with a **vector unit** attached at
its execute stage. It is a compute unit like any other: one port on the mesh,
the same kick that starts it and the same completion that retires it.

Everything the base core is stays true. It is RV32IM, in order, single issue.
What is added is a second register file, a second scratchpad, and an array of
**lanes** — copies of the arithmetic — that all execute **the same instruction
at the same time**.

```
                          ONE program counter
                          ONE instruction stream
                                   |
        +--------------------------+---------------------------+
        |                                                      |
   +----v---------------------+          +---------------------v----------+
   |   the base RV32IM core   |          |   the vector unit  (khs_unit)  |
   |                          |          |                                |
   |  x0..x31, 32 bits        |          |  v0..v7, 32 x SIMD bits        |
   |  scratchpad, 32-bit face |          |  facc0..facc1, one binary32    |
   |  branches, loads, loop   |          |    slot per element, over      |
   |  counters, addresses     |          |    NPART rotating partials     |
   |  mul/mulh/mulhsu/mulhu   |          |  vector scratchpad, 256-bit    |
   |  NO divide, NO float     |          |                                |
   |                          |          |   lane0 lane1 ... lane7        |
   |                          |          |   [32b] [32b]     [32b]        |
   |                          |          |   float units, a separate count|
   +--------------------------+          +--------------------------------+
        the address, the trip count           the elements, all of them
```

The scalar half keeps doing what it is good at: addresses, trip counts,
branches. **The vector unit never computes an address and never takes a branch.**
A loop is a scalar loop whose body happens to move 32 bytes at a time.

## Where it sits

Above it is the mesh: instructions arrive as a kick through the compute-unit
port, operands arrive as bursts written into its windows, and completion goes
back the same way. None of that is this unit's design — it is the framework's,
and it is [arch/noc/](../../../arch/noc/) and
[arch/sysnode/](../../../arch/sysnode/).

Below it is nothing. This is a leaf: it computes and it stores, and it has no
subordinate units.

Beside it is the [SIMT PE](../simt/README.md), which answers the case this one
cannot — lanes that need to disagree.

## The problem it solves

A controller PE is fast at deciding and slow at arithmetic, and the arithmetic
it is slow at is the regular kind. Two of its own kernels say it plainly:

| what the base core spends | on | the same work, vectorised |
|---:|---|---:|
| 8,025 cycles | a 256-element requantise epilogue: bias, ReLU, rounding shift, saturate, pack | **242** |
| 3,090 cycles | summing and max-ing 256 int32 | **248** |

None of that work is serial. Every element is independent, every element gets
the same treatment, and the core is executing one 32-bit operation per cycle
because that is the only shape it has. [performance](performance.md) states the
build both columns were taken on, and carries the rest of the suite.

That is the whole thesis, and it is also the boundary: this design goes wide on
work that is *uniform*. When lanes need to take different paths, or fetch from
different addresses, the answer is a different machine and not a wider one.

## Two halves, and only one of them is a free choice

```
   8 lanes x 32 bit  =  256 bit  =  one native memory entry  =  one flit payload
```

**The integer lanes are the address path.** A contiguous 32-bit load by eight
lanes is exactly one memory read request, and that is the strongest
machine-level alignment in the design. Narrow the integer side and the alignment
breaks permanently: every coalesced load becomes two or more requests, for every
kernel, forever.

**The float lanes are pure arithmetic and have no such tie.** A vector register
holds one binary32 element per 32-bit slot regardless of how many float units
exist; fewer units cost an **issue interval** of `elements / units`, and latency
is the cheapest thing to trade in a datapath that is already six cycles deep.

```
   register width     32 x SIMD bits                  256 at SIMD 8
   elements           register width / 32              8, DERIVED
   integer lanes      ILANES, the memory granule       8, effectively FIXED
   float units        FLOAT_LANES, arithmetic demand    a knob
```

That asymmetry is the whole justification for the shape. It is why *four integer
lanes and four float lanes* is rejected — it halves the memory alignment to buy
what the float knob already buys — and why *fewer integer lanes than float
lanes* is rejected outright: it starves addressing to feed arithmetic, so every
memory operation serialises while the float units wait.

**`ILANES` is nonetheless a real width**, and narrowing it costs cycles rather
than alignment: it narrows the **ALU** and not the multipliers, and the
register width does not move with it. What is fixed is `SIMD`, the register
width; what is configurable is how many lanes serve it per pass.

## There is one float format, and it is not a setting

```
   IEEE binary32 in   ->   binary32 compute   ->   binary32 out
```

There is no second format, no conversion at the operand edge, and no parameter
anywhere in this PE that changes what the arithmetic is done in. **Denormals
flush to sign-preserved zero** on input and output, which is D3D11's functional
requirement.

`funct7[1:0]` carries the element type where the integer tier's does, and `f32`
is the only value a build accepts — every other value is an unmapped encoding
rather than a silent reinterpretation. So there is no dtype axis anywhere in
this PE, and a reader should not go looking for one.

**KohakuMPE holds its own float units.** `rv_fpu.v` is in the framework, because
RV32F is a standard extension over IEEE binary32 and binary32 is nobody's
private format; everything above it — `khs_fp32_alu.v`, `khs_fp32_sfu.v`,
`khs_fcvt.v` — is this project's. KohakuTPU's vector core keeps computing in
E8M15 with its own modules and is not touched by any of it.

[float](float.md) states the tier in full: the groups, the pass walk, the
rotating accumulator, the four seeds and the rounding property.

## What is a parameter, and what is not

| thing | kind |
|---|---|
| everything the base PE fixes — regions, ordering, halting, kick and completion | **fixed protocol** of the base unit: [architecture](../../../arch/cpu/rv32-pe/architecture.md) |
| the vector scratchpad region and its store-only rule from the scalar side | **fixed protocol** of this unit — [memory](memory.md) |
| the instruction encoding: custom-0 integer, custom-1 float | **fixed protocol** — [programming](programming.md) |
| **the compute format** | **not a parameter at all.** IEEE binary32, always, in every build |
| `NPART`, the accumulator's partial count | **fixed protocol** — float addition does not associate, so the count changes the answers: [float](float.md) |
| `SIMD`, `ILANES`, `SHIFT_UNITS`, `PERM_UNITS`, `RED_UNITS`, `FLOAT_LANES`, `FSFU_UNITS`, `FCVT_UNITS`, `HAS_SHROUND` | **widths.** Each is a unit count, 0 means not built and its encodings fault: [configurable-widths](../configurable-widths.md) |
| `HAS_FALU`, `HAS_FACC` | **groups.** Each names a set of float opcodes rather than an array that can be narrowed |
| `VREGS`, `NACC`, `VSPAD_ENTRIES`, `WB_STAGE`, `RED_PIPE`, `VREG_PRIM`, `MEM_PRIM`, `USE_DSP` | **structural parameters**, each measured as itself: [performance](performance.md) |
| `SIMD_EN = 0` | a parameter too: the unit disappears — a generate, not a zero width — and the PE is the base core bit for bit |
| what a kernel computes | **yours** |

**`FLOAT_LANES` is architectural when the accumulator is built, not only an area
knob.** With fewer units an element's accumulate chain is a shorter, strided
subset of the partials, so the accumulation order changes — and float addition
does not associate. *A build with a different unit count computes different
answers on the same program.* The golden model takes the unit count, and the
component bench carries it in its configuration guard so a vector/build mismatch
names itself instead of failing as arithmetic. The elementwise groups carry no
such contract: every instruction is one pass of independent elements, and its
answer does not depend on how many units were built.

## The pages

| page | what is in it |
|---|---|
| [lanes](lanes.md) | what a lane **is**, how one instruction drives eight of them, how four int8 elements share one carry chain, and the three operations that cross lanes |
| [memory](memory.md) | the vector scratchpad's banks and two faces, the vector register file, and the float partial store |
| [pipeline](pipeline.md) | where the unit sits among the base pipeline's register boundaries, every hazard it adds, and why the scalar critical path is untouched |
| [float](float.md) | one compute format, the groups, elements against units against passes, the rotating accumulator, the four seeds, and **what is not built** |
| [programming](programming.md) | the instruction set, the encoding, the C intrinsics, and a kernel written twice |
| [configurations](configurations.md) | which feature mixes are worth building, and why the SIMD PEs of one mesh need not be the same build |
| [performance](performance.md) | what the unit costs, what it buys in cycles, and which published figures were withdrawn and why |
| [gates](gates.md) | the benches that must pass before a number is quotable, and the ways a number here can look like evidence without being any |
| [accumulator](accumulator.md) | the integer dot unit and its accumulator, **removed** — what they were and what replaced them |

If you have never read a SIMD datapath before, read [lanes](lanes.md) first;
that page is the machine.

## What this PE does not own

| concern | whose |
|---|---|
| per-lane branching, per-lane addresses, masks and predication | a SIMT core's. Nothing here anticipates them, and adding them would cost every uniform kernel — [simt/](../simt/README.md) |
| per-lane float masks | there are none: this unit has one program counter and no waves, so a deep result is tracked by a one-bit-per-register scoreboard instead ([float](float.md#what-waits-and-what-does-not)) |
| integer dot products with a dedicated accumulator | nobody's. A dot product is `vmul` then `vredsum`; a part that needs a high rate carries matrix units — [accumulator](accumulator.md) |
| integer multiply | the **base core's**, not this unit's — `mul`, `mulh`, `mulhsu` and `mulhu` are built into the scalar pipeline ([the multiplier](../../../arch/cpu/rv32-pe/microarchitecture.md#the-multiplier)), so a scalar product does not have to become a vector instruction |
| divide and remainder | nobody's. They fault, and divide-by-a-constant strength-reduces to `mulhu` |
| where operands come from before the scratchpad | [sysnode](../../../arch/sysnode/) — the memory agent fills the vector scratchpad the way it fills any window |
| the flit, the router, the port | [noc](../../../arch/noc/) |
| where the PE lands on the die and at what clock | [physical](../../../arch/physical/) |
