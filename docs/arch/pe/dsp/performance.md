---
title: DSP PE performance
summary: What every configuration costs in LUT, FF, DSP, BRAM and frequency, what each packed instruction buys against the scalar sequence it replaces, and which knobs are worth turning.
tags:
  - architecture
  - pe
  - dsp
  - performance
---

# DSP PE performance

Two independent questions, and this page keeps them apart: **what the vector
unit costs**, which is a synthesis result, and **what it buys**, which is a
cycle count on real programs.

Resource and frequency figures are out-of-context synthesis on
`xcvu13p-fhgb2104-2L-e` (Vivado 2024.2, synth only — pre-placement, so a routed
result will be somewhat worse), at a 3.333 ns request. Cycle figures are read
from the PE's own `CTL_CYCLE` counter on the full system: one PE, real routers,
the real memory agent, RAM behind it. Every configuration reported here passed
the component test **as itself**; a variant is never assumed from the default
build.

The reference to hold beside all of it: the shipped controller PE is
**2,491 LUT, 5 BRAM, 410.8 MHz**, a quarter of which is the framework attach
every compute unit carries.

## The whole PE

The number a mesh has to accept is not the vector unit's — it is the assembled
PE's, because integration adds the vector stall into the MEM stage's own and
the reduction's word into the writeback mux. Both ends measured through one
script, so they are comparable to each other:

| | LUT | FF | BRAM | DSP | Fmax |
|---|---:|---:|---:|---:|---:|
| `DSP_EN = 0` | 2,477 | 4,140 | 5 | 0 | 377.9 MHz |
| `DSP_EN = 1` | 10,430 | 5,789 | 13 | 32 | **361.9 MHz** |

**The extension costs area, not frequency.** The assembled PE closes within
16 MHz of the same PE with the extension switched off, and its critical path
runs from the vector unit's own destination register to the instruction
window's address — 8 logic levels. What the extension costs is about 7,950 LUT
and 8 BRAM on top of a controller PE.

Against a 300 MHz mesh clock that is 21 % margin. (These two rows are
synthesised with hierarchy preserved, for the per-unit accounting; the
410.8 MHz quoted for the base PE elsewhere comes from a flattened run. Compare
within a flow, never across one.)

## The configuration matrix

The vector unit alone — the marginal cost of making a controller PE a DSP PE.

| config | LUT | FF | DSP | BRAM | Fmax | vs the default |
|---|---:|---:|---:|---:|---:|---|
| **`s8` (shipped)** | **7,961** | 1,617 | 32 | 8 | **368.7** | — |
| `s4` | 4,138 | 966 | 16 | 4 | 367.4 | −3,823 LUT, −1.3 MHz |
| `s2` | 2,091 | 512 | 8 | 2 | 402.1 | −5,870 LUT, +33.4 MHz |
| `s8` no shifter | 6,448 | 1,551 | 32 | 8 | 393.1 | −1,513 LUT, +24.4 MHz |
| `s8` no permute | 6,327 | 1,615 | 32 | 8 | 402.6 | −1,634 LUT, +33.9 MHz |
| `s8` 2 multipliers | 7,330 | 1,621 | 16 | 8 | 369.0 | −631 LUT, +0.3 MHz |
| `s8` multipliers in fabric | 15,068 | 2,437 | **0** | 8 | 295.3 **✗** | +7,107 LUT, −73.4 MHz |
| `s8` 32 vector registers | 7,853 | 1,621 | 32 | 8 | 368.9 | −108 LUT, +0.2 MHz |
| `s8` 4 vector registers | 7,965 | 1,617 | 32 | 8 | 368.7 | +4 LUT, +0.0 MHz |
| `s8` 1 accumulator | 7,575 | 1,357 | 32 | 8 | 368.9 | −386 LUT, +0.2 MHz |
| `s8` 4 accumulators | 10,164 | 2,142 | 32 | 8 | 368.6 | +2,203 LUT, −0.1 MHz |
| `s8` block-RAM vector file | 8,158 | 1,107 | 32 | **16** | 270.8 **✗** | +197 LUT, −97.9 MHz |
| `s8` write in WB | 7,905 | 1,881 | 32 | 8 | **408.0** | −56 LUT, +39.3 MHz |
| `s8` everything optional off | 4,010 | 1,281 | 16 | 8 | 399.4 | −3,951 LUT, +30.7 MHz |

**✗** marks a configuration that does not meet the 3.333 ns request. Both are
priced for the comparison rather than offered: fabric multipliers and a
block-RAM register file are the two ways to build this unit that are worse on
every axis at once.

### How many fit in one mesh

The DSP PE is the **replicated** unit, so its LUT count is the machine's
capacity rather than a line in a report. A mesh has roughly **350,000 usable
LUT** once the fabric and the memory agent are paid for, and the question every
configuration has to answer is how many of itself fit in that.

The assembled PE is the vector unit plus the base core and its integration —
measured at **10,430 LUT for the shipped `s8`**, of which 2,469 is everything
that is not the vector unit. Adding that constant to each unit figure gives the
count per mesh:

| config | unit LUT | assembled PE | per mesh |
|---|---:|---:|---:|
| `s2` | 2,091 | 4,560 | **76** |
| `s8` everything optional off | 4,010 | 6,479 | **54** |
| `s4` | 4,138 | 6,607 | **52** |
| `s8` no permute | 6,327 | 8,796 | **39** |
| `s8` no shifter | 6,448 | 8,917 | **39** |
| `s8` 2 multipliers | 7,330 | 9,799 | **35** |
| `s8` 1 accumulator | 7,575 | 10,044 | **34** |
| **`s8` (shipped)** | **7,961** | **10,430** | **33** |
| `s8` write in WB | 7,905 | 10,374 | 33 |
| `s8` 32 vector registers | 7,853 | 10,322 | 33 |
| `s8` 4 accumulators | 10,164 | 12,633 | **27** |
| `s8` multipliers in fabric ✗ | 15,068 | 17,537 | 19 |

Only the `s8` row is a measured assembled PE; the rest add the measured 2,469
to a measured unit, which moves by a few LUT with placement but not by enough
to change a count.

**The integer tier at full width leaves 33 PEs per mesh**, and the eight-lane
build costs 6 of those against four lanes for roughly double the per-PE
throughput — which is the trade to make deliberately rather than by default.

### What the rows say

**Width is a clean area-versus-throughput trade.** LUT scales with `SIMD` —
about 1.9× per doubling — and frequency barely does: four lanes and eight land
within 1.3 MHz of each other. Two lanes is genuinely faster, and for a
structural reason rather than a lucky one — the binding path ends at the vector
register file's write port, and at two lanes that file is a quarter as wide.

**Everything on the critical path is the register file's read-to-write loop**,
so what moves the frequency is what sits *in* that loop. Removing the permute
network buys 33.9 MHz, removing the shifter 24.4 — both shorten the result mux
that feeds the write port. Replacing the hard multipliers with fabric costs
73.4 MHz and replacing the register file with block RAM costs 97.9, because both
put something slower *into* the loop. The knobs that touch neither — register
count, accumulator count, multipliers per lane — move it by less than half a
megahertz.

**The permute network costs 1,634 LUT and 33.9 MHz** — the largest optional
block on both counts. It buys `vsldw` (a stencil's or a filter's misaligned
neighbour), the saturating `vpack` an epilogue ends with, and the widening
`vunpk`. It is also the one structure whose cost grows with lane count, since
each output lane selects from `2 × SIMD` inputs. A build that only computes dot
products should not carry it.

**The packed shifter costs 1,513 LUT and 24.4 MHz.** Both optional blocks are
therefore real frequency decisions and not only area ones — which was not true
of an earlier datapath, where a longer path upstream hid them.

**A DSP column is worth about 230 LUT.** Moving the 32 multipliers into fabric
costs 7,275 LUT to save 32 DSP, and loses 44 MHz. On this device LUT is the
binding resource and DSP is not, so the hard multipliers stay.

**Two multipliers per lane saves 16 DSP and costs int8 entirely.** `vdot.s8`
and `vmul.s8` become illegal encodings on that build — the honest outcome,
because there is no way to get two independent int8 multiplies from one
DSP48E2 ([accumulator](accumulator.md#the-multipliers-and-why-there-are-four-per-lane)).

**The vector register count is free in both directions.** Thirty-two registers
cost 16 LUT more than eight, and four save two. A distributed-RAM primitive is
32 entries deep, so a small file wastes the depth it does not use — which is
the mirror image of the scalar core's result, where a 32-entry file in a
1024-deep block RAM was 3.1 % depth-utilised and the LUTRAM form shipped.

**Accumulators are the one structure that grows badly**: the second costs
374 LUT, and going from two to four costs 2,238 more, because the read mux in
front of a flat register array grows with the count and the width. Two is the
shipped number.

**A block-RAM vector register file is worse on every axis**: 324 LUT more,
8 BRAM more, and 70 MHz slower. Distributed RAM ships.

### The critical path

```
   m_alu_op_reg/C  ->  u_vrf/.../mem_reg/RAMF_D1/I        12 levels, 2.860 ns
```

The registered decode bit that selects the lane's operation, through the packed
adder, to the vector register file's write port. Its shape is the design's, and
worth reading rather than counting:

| segment | cells | cumulative |
|---|---|---:|
| decode register to the adder's `sub` input | FDRE, LUT4 | 0.409 ns |
| into the carry chain | LUT3 | 0.705 ns |
| **the packed adder** | **CARRY8 × 4** | 1.121 ns |
| the signed-compare spread | LUT4, LUT6 | 1.741 ns |
| min/max select, then the operation mux | LUT6, LUT4 | 2.215 ns |
| the result mux | LUT6 × 2 | 2.661 ns |
| the register file's write port | RAMD32 | 2.888 ns |

**Four of the twelve levels are the carry chain, and they cost 0.23 ns between
them** — 0.027 ns per level against 0.038–0.090 ns for every LUT on the path.
Counting levels without reading them would call this path half again as deep as
it is, and would point at the one structure that made the datapath fast: a
32-bit SWAR add is a single native carry chain precisely so that it is *not*
seven gated ones ([lanes](lanes.md#the-packed-adder)).

The other reading is that **77 % of the delay is interconnect**, concentrated on
the broadcast of the decode bits to every lane. That is the shape of a wide
uniform-control datapath: one decode, many consumers.

## The constraint to ask

**Constrain the unit at 3.333 ns**, the same request the base core settles on.
The same three-point curve, on the shipped configuration:

| Request | Fmax | LUT |
|---|---:|---:|
| 5.0 ns | 368.7 MHz | 7,905 |
| **3.333 ns** | **368.7 MHz** | **7,961** |
| 2.5 ns | 368.7 MHz | 8,636 |

The ceiling does not move at any of the three, and the tighter ask spends
731 LUT — 9 % of the unit — answering a question synthesis cannot answer.
This is the base core's result repeated on wider logic, and it has the same
cause: a memory's clock-to-out is not something a constraint can shorten.

At the mesh ship clock of 300 MHz the shipped configuration carries 13 %
margin.

### The one knob where cycles and clock disagree

`DSP_WB = 1` writes the vector register file a stage later. It reaches
**408.0 MHz and is 56 LUT smaller** — so on the synthesis report it is better on
every axis — and it costs a second stall on every back-to-back dependency
([pipeline](pipeline.md#the-vector-file-is-written-in-mem-not-wb)). That is
exactly the case where a resource table decides wrongly, and the comparison has
to be in time:

| kernel | at 368.7 MHz | at 408.0 MHz | faster |
|---|---:|---:|---|
| int8 dot, two accumulators | 184.4 ns | 181.4 ns | write in WB, by 1.6 % |
| int8 dot | 141.0 ns | 139.7 ns | write in WB, by 0.9 % |
| int32 sum and max | 672.6 ns | 686.3 ns | write in MEM, by 2.0 % |
| 256-word copy | 640.1 ns | 656.9 ns | write in MEM, by 2.6 % |
| requantise epilogue | 656.4 ns | 789.2 ns | write in MEM, by 20.2 % |
| 8-tap FIR | 1,508.0 ns | 1,818.6 ns | write in MEM, by 20.6 % |

**Writing in MEM ships**, despite losing on both LUT and megahertz. The extra
clock buys about 1 % on the two kernels that are streams of independent
operations and loses 20 % on the two that are chains of dependent ones — an
epilogue and a filter — because every link in such a chain pays the second
stall. A configuration that wins the datasheet and loses the workload is the
reason cycles are measured at all.

## What the instructions buy

Kernel-only cycles, same PE, same data, same independently computed reference
for both forms.

| kernel | scalar | vector | speedup | against |
|---|---:|---:|---:|---|
| int8 dot, 128 elements | 8,221 | 52 | **158.1×** | the core as it ships |
| int8 dot, 128 elements | 1,297 | 52 | **24.9×** | a scalar core that has a multiplier |
| requantise epilogue, 256 elements | 8,025 | 242 | **33.2×** | — |
| int32 sum and signed max, 256 | 3,090 | 248 | **12.5×** | — |
| 8-tap int16 FIR, constant taps | 3,404 | 556 | **6.1×** | — |
| 256-word copy | 780 | 236 | **3.3×** | — |

### Width, in cycles and in time

Halving the lane count does not double the cycles, because the part of a kernel
that is loop control and reduction does not shrink with the datapath:

| kernel | 8 lanes | 4 lanes | 2 lanes |
|---|---:|---:|---:|
| requantise epilogue | 242 | 466 | 914 |
| int32 sum and max | 248 | 472 | 918 |
| 256-word copy | 236 | 460 | 908 |
| int8 dot | 52 | 84 | 147 |
| int8 dot, two accumulators | 68 | 108 | 186 |

Halving the width costs about 1.95× on the streaming kernels and only about
1.6× on the dot products, whose fixed prologue and final cross-lane reduction do
not shrink with the datapath.

**At two lanes the vector copy loses to the scalar one** — 908 cycles against
780. The scalar copy is unrolled by four, the two-lane vector loop moves two
words per iteration, and the same loop overhead is then amortised over less
work. A wide datapath does not help a kernel whose cost is the loop, and two
lanes is where that crosses over.

Three readings matter more than the numbers.

**The multiplier and the width are separate purchases.** The base core is RV32I
and has no multiplier, so an int8 dot's scalar loop spends 84 % of its cycles
in a software multiply — eight unrolled shift-add steps per element. Quoting
158× would be mostly a statement that the base core cannot multiply. Every
multiplying kernel therefore carries a twin whose multiply is costed at one
instruction, and **24.9× is the honest SIMD number**.

**Loop overhead bounds everything at short vectors.** The copy moves eight
times the data per instruction and measures 3.3×, because the two pointer
bumps, the counter and the branch do not shrink. That is the Amdahl ceiling for
any kernel on this machine, and it is a property of the loop rather than of the
datapath.

**A kernel whose scalar form is already good wins the least.** The FIR's taps
are compile-time constants, so its scalar form strength-reduces to two
instructions per tap with no software multiply to remove. 6.1× is width alone,
and it is the narrowest frontier in the suite.

## Instruction timing

| Event | Cost |
|---|---|
| ALU, logic, shift, permute, moves, `vld`, `vst` | 1 cycle |
| `vdot`, including back to back | 1 cycle, accumulate lands 2 cycles later |
| `vmul` | 2 cycles |
| `vredsum` / `vredmax` at more than two lanes | 2 cycles |
| RAW on a vector register, distance 1 | 1 stall |
| `vld` behind a `vst` | 1 stall |
| `vaccrd` / `vaccz` / `vaccwr` behind a dot in flight | up to 2 stalls |

The measured CPI of the vector kernels is 1.17 to 1.53 — the stalls above,
plus the loop's own mispredicted exit.
</content>
