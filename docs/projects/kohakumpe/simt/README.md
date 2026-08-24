---
title: SIMT PE
summary: The SIMT PE — one instruction stream, many threads, each with its own address and its own path through a branch. What it is, what it costs, and where to read about each part.
tags:
  - architecture
  - pe
  - rv32
  - gpu
  - simt
---

# SIMT PE

`src/kohakumpe/simt/` — the [controller PE](../../../arch/pe/README.md) rebuilt so
that an ordinary RV32I opcode addresses a **per-thread** register file. It is
KohakuMPE's third PE class, alongside the scalar controller and the
[SIMD tier](../../../arch/pe/simd/). Everything the base unit is stays true: one port on the
fabric, the same kick, the same completion.

**Current state is on one page: [status](status.md).** It lists what exists,
what has been run, and what has not been built — with the commands. Read it
before quoting anything from here.

## The arithmetic: 8 integer lanes, 8 float lanes

**The configuration of record for this PE class is 8 integer lanes and 8 float
lanes, with RV32M integer multiply.** It is stated here because a baseline
nobody names gets silently assumed to be something else.

**All of it is built and measured**, `kht_pe` at 8 lanes / 16 waves,
`xcvu13p-fhgb2104-2L-e`, out-of-context synthesis at the **2.857 ns ask
(350 MHz)**:

```
   20,086 LUT   17,282 FF   30.5 BRAM   48 DSP48   392.0 MHz   slack +0.306
```

At `-flatten_hierarchy rebuilt`, which is what the ship's synthesis run takes;
the same design at `none` reads 636 LUT high. It was **21,586 / 365.6** before a
shrink campaign took −1,745 LUT out of the lane ALU, the reduction tree and the
writeback mux, and a required float-decode fix gave +210 back —
[ladder](ladder.md#shrinking-it-ten-attempts-three-that-paid) carries the three
that paid, the one that cost, and the seven that were reverted.

An earlier revision of this page said the reference was **8 integer / 4 float**
and that neither number was built. Both halves of that are false now: the float
tier and the integer multiplier are in the RTL and in the ISA, and the float
lane count moved from 4 to 8 because the rendering target requires it —
`8 DSP × 4 + 4 GPU × 8 = 64` FP FMA per clock is one Mali-G610 shader core, and
at 4 float lanes the same mesh is 48 and short. See
[comparison](comparison.md#2-what-this-machine-has).

### Why the two numbers are equal now, and what still pins the integer one

```
   8 lanes x 32 bit  =  256 bit  =  one native memory entry  =  one flit payload
```

**The integer lanes are the address path.** A contiguous 32-bit load by eight
threads is exactly one `MEM_RD_REQ`, and that is the strongest machine-level
alignment in the design. Narrow the integer side and the alignment breaks
permanently: every coalesced load becomes two or more requests, for every
kernel, forever.

Float has no such constraint. It is pure arithmetic, deeply pipelined at
II = 1, and a core with sixteen resident wave contexts hides its **15-cycle
latency** rather than stalling on it — latency is the cheapest thing to trade in
a throughput machine, which is why the float lane count was free to be chosen
rather than forced.

```
   integer lanes  <-  the memory granule  (256-bit entry / flit)   8, fixed
   float lanes    <-  arithmetic demand   (throughput vs LUT)      a knob
```

That asymmetry is the whole justification, and it survives the two numbers
becoming equal. **Eight integer lanes is a constraint; eight float lanes is a
choice.** `int 4 / float 4` is still rejected — it halves the memory alignment to
buy what the float knob already buys — and `int < float` is rejected outright: it
starves addressing to feed arithmetic, so every memory operation serialises while
the float lanes wait.

What the knob was turned *to* is set by the mesh, not by this PE:

```
   8 int + 4 float:   8x4 + 4x4 = 48 FMA/clk     short of a G610 shader core
   8 int + 8 float:   8x4 + 4x8 = 64 FMA/clk     parity, and what is built
```

**`FLANES < LANES` is a parameter but not yet a working configuration.**
`kht_fpu` ties the lanes above `FLANES` to zero rather than sequencing them, and
reaching a real reduced build needs the lane/interval walk sequencer that belongs
to the DSP realm. Four float lanes is therefore the *intended* reduced
configuration for area-constrained meshes and not one that can be measured today.

### The float tier takes both operand widths, and that is not a knob

```
   FP32 or FP16 operands in  ->  E8M15 compute  ->  FP32 or FP16 out
```

**Operand width is a property of the instruction, not of the build.** The
funct7 bit that distinguishes `vfma` from `vfma_h` drives `half` in `kht_fpu`,
which drives `wide(!half)` into the lane; `wide` is a *port* on
`khs_float_lane`, not a parameter, and that lane's own header states the
contract:

> BOTH INPUT FORMATS AND THE ONE COMPUTE FORMAT ARE THE CONTRACT, not options:
> there is no parameter here that removes either edge.

So there is no build of this PE that has the float tier and refuses one of the
two widths, and no name for "the tier that does one of them". What *is* a
parameter is whether a float tier exists at all (`HAS_FLT`) and how many lanes
it has (`FLANES`). Neither selects a format.

The three conversions are worth naming individually, because they are not
symmetric and the asymmetry is what decides which format a shader should reach
for:

| conversion | property |
|---|---|
| `FP16 → E8M15` | **exact** — nothing is lost, subnormals normalise |
| `FP32 → E8M15` | the exponent field is kept **verbatim**; mantissa below bit 8 is rounded off |
| `E8M15 → FP16` | the one direction that is both lossy **and** range-limited — a finite overflow **saturates silently** to the largest finite FP16 |

Those are statements about formats. Everything else in these pages says "the
float tier".

### Rendering is genuinely mixed

Float is **not optional** for this PE — rendering needs it. But the integer half
is not a leftover from a compute machine either: rasterisation and depth are
integer **because float gets them wrong**.

| stage | needs | kind |
|---|---|---|
| rasterisation, edge equations | exact, watertight edge functions on a subpixel grid | integer |
| depth interpolation and buffer | 24-bit fixed point; M15 **z-fights** | integer / fixed |
| texture addressing | wrap, clamp, mip select, Morton swizzle | integer / bitwise |
| texture filtering | fixed-point or E8M15 weights | float-ish |
| fragment / colour shading | mediump; E8M15 exceeds fp16 in range and mantissa | float |
| vertex transform | E8M15 products into an FP32 accumulator | float |

Those are exactness requirements, not performance ones.

**Two of those integer rows are why RV32M is built.** A pixel index is
`y * width + x` and a mip or Morton address is a multiply, and until the
multiplier landed each of those was a software shift-add chain running on every
lane of every fragment. `mul`, `mulh`, `mulhsu` and `mulhu` are one 33×33 signed
product per lane at the float tier's exact latency; divide and remainder are
deliberately absent, because divide-by-a-constant strength-reduces to `mulhu`.

### Where the lane machinery lives

Arithmetic lanes are a **separate purchase from thread count**, with issue
interval `threads / lanes`. At the configuration of record that interval is
**1** — eight threads over eight float lanes — so no walk sequencer is built and
none is needed. It becomes needed only at `FLANES < LANES`, and that sequencer
belongs to the **DSP realm and would be instantiated here — never forked into
this PE.**

The same single-sourcing rule already holds for the arithmetic itself: every
float lane is one `khs_float_lane`, which is operands → `vec_cvt` → `vec_alu`'s
FMA → converted back. `kht_fpu` selects operands and converts the result; it
does not compute. That is what makes a GPU float number comparable to a DSP
float number, and what keeps `cost(SIMT) = G8 − G0` meaning anything at all.

## The problem it solves

The [SIMD PE](../../../arch/pe/simd/) goes wide on work that is *uniform*: every lane does the
same thing to a different element at a stride the scalar side computed. That
covers dense linear algebra and it covers it well. It does not cover the case
where lanes need to **disagree** — where lane 3 takes the `if` and lane 4 takes
the `else`, or where the address lane 5 wants is in a table rather than at a
stride.

That is not a wider machine, it is a different one, and the SIMD tier's own
documentation says so:

> per-lane branching, per-lane addresses, masks and predication — a SIMT core's.
> Nothing here anticipates them, and adding them here would cost every uniform
> kernel.

This PE is that SIMT core. The whole question it exists to answer is **what
that capability costs**, in LUT, on this fabric, against a lane array that is
otherwise identical — and the design is arranged so the answer is a measurement
rather than an estimate. See [ladder](ladder.md).

## The shape

The base ISA slot is spent on the **per-thread** side. That is AMD GCN's split
with the polarity inverted, and it is deliberate: a shader is mostly per-thread
work, so the per-thread half should be the cheap encoding.

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
 |  base pointers, uniform    |        |  active mask + IPDOM stack    |
 |  branches, trip counts     |        |  8 int lanes + 8 float lanes  |
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

Three things follow from that picture, and they are the three things this PE
has that the SIMD tier does not:

1. **A mask, not a predicate on the datapath.** An inactive lane computes
   whatever it computes and its *write* is dropped. Masking costs one enable per
   bank and nothing on the arithmetic path.
2. **An IPDOM stack**, so `split` and `join` implement structured divergence
   exactly, without the compiler proving anything about uniformity.
3. **An address per lane**, with three addressing tiers already distinguished in
   the encoding so a coalescer can replace the current serial walk without the
   ISA moving.

## The pages

| Page | What is in it |
|---|---|
| [status](status.md) | **what exists and what is measured, right now** — the ground truth for progress |
| [isa](isa.md) | the instruction set: the two custom opcodes, the six groups, the float tier and RV32M, divergence, subgroup ops, the three memory tiers — and the one field table all four consumers are generated from |
| [microarchitecture](microarchitecture.md) | how it is built: the pipeline, the mask and its stack, the lane-serialising LSU, the shared shadow pipe the float tier and the multiplier retire through, the halt-and-flush, and the traps each shape was chosen to avoid |
| [ladder](ladder.md) | the method — why the design is parameters rather than branches, and what each gate measured |
| [comparison](comparison.md) | what the measured numbers are worth against shipped mobile GPUs, and the fixed function that decides a frame rather than the arithmetic |

If you want the number, [ladder](ladder.md). If you want what the number is
*worth*, [comparison](comparison.md). If you are writing a shader,
[isa](isa.md). If you are changing the RTL,
[microarchitecture](microarchitecture.md) first — several of its shapes exist to
avoid a specific failure that has already happened once.

## Fixed protocol, parameter, or yours

| Thing | Category |
|---|---|
| everything the base PE fixes — regions, ordering, halting, kick and completion | **fixed protocol** of the base unit: [architecture](../../../arch/pe/architecture.md) |
| the instruction encoding: custom-2 for the R-type groups, custom-3 for the I-type ones | **fixed protocol** of this unit — [isa](isa.md), and [opcode-map](../../../arch/pe/opcode-map.md) is the authority on who owns which major |
| RV32M at its **standard** encoding — the existing OP group, `funct7 = 0000001` — and the float group at custom-2 `funct3 = 5` | **fixed protocol**. No new opcode major was spent on either; all four customs were already claimed |
| the float port takes **both** operand widths, selected per instruction by funct7[2]; the compute format is E8M15 | **fixed protocol** of this unit, and of `khs_float_lane` above it. There is no parameter that removes either edge |
| the float register layout: an FP32 element fills `vreg[31:0]`; an FP16 element sits in `vreg[15:0]` with the upper half **RESERVED and written zero** | **fixed protocol** of this unit — it is what lets packed 2×FP16 later become an opcode addition rather than a migration |
| a per-thread conditional branch is **not encodable** | **fixed protocol** — a per-thread condition reaching one PC is undefined, so the encoding refuses it rather than trusting a proof of uniformity |
| a `split` pushes **two** entries and a `join` pops **one**; depth D permits D/2 nested levels, and overflow is a **fault** | **fixed protocol** of this unit — [isa](isa.md) |
| a halt **flushes** before it completes | **fixed protocol** — the completion means the stores are in memory, not merely issued |
| `LANES`, `WAVES`, `HAS_MASK`, `HAS_IPDOM`, `HAS_LDSBANK`, `HAS_SHFL`, `HAS_FLT`, `FLANES`, `IPDOM_D`, `VREG_PRIM`, `MEM_PRIM` | **parameters**, and each is measured as itself: [ladder](ladder.md) |
| a gate that is off **faults** rather than computing something plausible — `HAS_SHFL = 0` faults `shflxor`/`bcast`, `HAS_FLT = 0` faults every float op *and* every RV32M op | **fixed protocol** of this unit. A plausible-looking wrong number is worse than a halt, and the point of building a narrow configuration is to know |
| what a shader computes | **yours** |

## What this PE does not own

| Concern | Whose |
|---|---|
| uniform wide arithmetic, packed int8 `vdot`, the float **accumulator** | the [SIMD PE](../../../arch/pe/simd/)'s. This one does not anticipate them. The float *lane* is a different matter — it is inherited from that tier verbatim and never forked |
| the arithmetic inside a float lane, and both of its operand edges | the [SIMD PE](../../../arch/pe/simd/)'s `khs_float_lane`. This PE selects operands and converts formats; it does not compute |
| the lane/interval walk sequencer that `FLANES < LANES` would need | the [SIMD PE](../../../arch/pe/simd/)'s, by ruling. Instantiated here if it is ever built, never written here |
| where operands come from before the scratchpad | [sysnode](../../../arch/sysnode/) |
| the flit, the router, the port | [noc](../../../arch/noc/) |
| which custom opcode major belongs to which PE class | [opcode-map](../../../arch/pe/opcode-map.md) — a table one tier owns is not something another can check itself against |
| where the PE lands on the die and at what clock | [physical](../../../arch/physical/) |
