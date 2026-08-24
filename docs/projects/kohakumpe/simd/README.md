---
title: SIMD PE
summary: The controller PE with a wide datapath behind it — one program counter, one instruction, eight integer lanes and four float ones. What it is, what is a parameter and what is not, what it costs, and where to read about each part.
tags:
  - architecture
  - pe
  - rv32
  - dsp
  - simd
---

# SIMD PE

`src/kohakumpe/simd/` — the [controller PE](../../../arch/pe/README.md) with a vector
unit attached at its EX stage. Everything the base core is stays true: RV32I, one
port on the fabric, the same kick and the same completion. What is added is a
second register file, a second scratchpad, and an array of lanes that all execute
**the same instruction at the same time**.

## The reference configuration

```
SIMD PE, 8 integer lanes + 4 float lanes

   13,772 LUT  ·  10,126 FF  ·  13 BRAM  ·  72 DSP48  ·  353.4 MHz
```

MEASURED — assembled `rv_pe` on `xcvu13p-fhgb2104-2L-e`, out-of-context
synthesis at the **2.857 ns ask (350 MHz)**, Vivado 2024.2, synthesis only.

That is the build. It is not a point on a menu: eight integer lanes are fixed by
the memory granule, four float lanes are the chosen width, and the float
datapath has exactly one arithmetic format. What a build *may* vary is set out
in [what is a parameter](#what-is-a-parameter-and-what-is-not) below, and it is
a shorter list than the old documentation implied.

## Two halves, and only one of them is a choice

```
   8 lanes x 32 bit  =  256 bit  =  one native memory entry  =  one flit
```

**The integer lanes are the address path.** A contiguous 32-bit load by eight
lanes is exactly one `MEM_RD_REQ`, and that is the strongest machine-level
alignment in the design. Narrow the integer side and the alignment breaks
permanently: every coalesced load becomes two or more requests, for every
kernel, forever. The lane is 32 bits wide for a second reason that points the
same way — `vdot` reduces *within* a lane into one int32, so an accumulator is
`SIMD × int32` and is exactly one vector register wide, which is what makes
`vaccrd` a move rather than a narrowing.

**The float lanes are pure arithmetic and have no such tie.** A vector register
holds sixteen FP16 elements at SIMD 8 regardless of how many float lanes exist;
fewer lanes cost an **issue interval** of `elements / lanes` — four cycles per
vector at four lanes — and latency is the cheapest thing to trade in a datapath
that is already fifteen cycles deep.

```
   integer lanes    the memory granule                      8, FIXED
   float elements   register width / element width          16, DERIVED
   float lanes      arithmetic demand (throughput vs LUT)   4, A KNOB
```

That asymmetry is the whole justification for the shape. It is why `int 4 /
float 4` is rejected — it halves the memory alignment to buy what the float knob
already buys — and why `int < float` is rejected outright: it starves addressing
to feed arithmetic, so every memory operation serialises while the float lanes
wait.

**Rendering is genuinely mixed, and the integer half is there for correctness**:
rasterisation and depth are integer *because float gets them wrong* — a
watertight edge test on a subpixel grid and a non-z-fighting 24-bit depth buffer
are exactness requirements, not performance ones — while shading, filter weights
and vertex transform are the float half. The stage-by-stage table is on the
[SIMT PE](../simt/README.md) page and applies to both classes.

## There is one float format, and it is not a setting

```
   FP32 or FP16 operands in   ->   E8M15 compute   ->   FP32 or FP16 out
```

**Operand width is a property of an instruction, not of a build.** The lane,
`khs_float_lane`, carries one operand port and both converters, unconditionally
— its own header says "BOTH INPUT FORMATS AND THE ONE COMPUTE FORMAT ARE THE
CONTRACT, not options: there is no parameter here that removes either edge." The
compute format is **always E8M15** and there is no build in which it is anything
else.

So there is no dtype axis anywhere in this PE, and a reader should not go
looking for one.

**What this tier currently reaches of that contract is narrower than the lane
offers, and the gap is not cosmetic.** `khs_unit` ties every lane's width bit low
and faults every `.f32` encoding, so:

> **The SIMD PE carries the FP32 converters in its 13,772 LUT and cannot issue an
> FP32 instruction.**

That is a half-finished transition, not a capability and not a planned one — and
the reason it is not one wire away is a correctness constraint rather than an
unfinished chore, because the operand width changes the accumulator's fold order
and **float addition does not associate**.
[float](float.md#what-is-not-built) states it in full,
including the two decisions anyone finishing it has to make.

**E8M15 is not a compromise.** It carries 1.5e-5 relative error — 32× better
than the FP16 that mobile fragment shaders run at — with an 8-bit exponent,
which is more range than the FP24 of the DX9 era had. A dot product of FP16
inputs therefore accumulates 32× more accurately than its own operands, in a
format whose range covers FP32's verbatim.

## What is a parameter, and what is not

| Thing | Category |
|---|---|
| everything the base PE fixes — regions, ordering, halting, kick and completion | **fixed protocol** of the base unit: [architecture](../../../arch/pe/architecture.md) |
| the vector scratchpad region and its store-only rule from the scalar side | **fixed protocol** of this unit — [memory](memory.md) |
| the instruction encoding: custom-0 integer, custom-1 float | **fixed protocol** — [programming](programming.md) |
| **the compute format** | **not a parameter at all.** E8M15, always, in every build |
| **operand width** | **not a parameter** — it is a field of the instruction |
| `SIMD_FLOAT` | a **presence** switch: float tier, or no float tier and custom-1 unmapped. It does not select a format |
| `SIMD_FLOAT_LANES` | a **width** knob: how many float lanes serve the sixteen elements. Also **architectural** — see below |
| the accumulator rotation, `SIMD_NPART` | **fixed protocol** — float addition does not associate, so the count changes the answers: [float](float.md) |
| `SIMD_LANES`, `SIMD_VREGS`, `SIMD_NACC`, `SIMD_MULS`, `SIMD_SHIFT`, `SIMD_PERM`, `SIMD_VSPAD` | **parameters**, each measured as itself: [performance](performance.md) |
| `SIMD_DOTDSP`, `SIMD_WB` | **parameters, both defaulting to 1** on `rv_pe` — they only pay at a binding constraint, and each changes a latency: [performance](performance.md#the-two-knobs-the-tighter-ask-turned-on) |
| `SIMD_EN = 0` | a parameter too: the unit disappears — generate, not zero-width — and the PE is the base core bit for bit |
| what a kernel computes | **yours** |

**`SIMD_FLOAT_LANES` is architectural, not just an area knob.** With fewer lanes
an element's accumulate chain is a shorter, strided subset of the partials, so
the accumulation order changes — and float addition does not associate. *A build
with a different lane count computes different answers on the same program.* The
golden model takes the lane count, and `khs_unit_tb` carries it in its
configuration guard (`meta[12]`) so a vector/build mismatch names itself instead
of failing as arithmetic.

## The shape

```
                          ONE program counter
                          ONE instruction stream
                                   |
        +--------------------------+---------------------------+
        |                                                      |
   +----v---------------------+          +---------------------v----------+
   |   the base RV32I core    |          |   the vector unit  (khs_unit)  |
   |                          |          |                                |
   |  x0..x31, 32 bits        |          |  v0..v7, 256 bits              |
   |  scratchpad, 32-bit face |          |  acc0..acc1, 8 x int32         |
   |  branches, loads, loop   |          |  facc0..facc1, 16 E8M15 slots  |
   |  counters, addresses     |          |    over 16 rotating partials   |
   |  NO multiply, NO float   |          |  vector scratchpad, 256-bit    |
   |                          |          |                                |
   |                          |          |   lane0 lane1 ... lane7        |
   |                          |          |   [32b] [32b]     [32b]        |
   |                          |          |   flane0 flane1 flane2 flane3  |
   +--------------------------+          +--------------------------------+
        the address, the trip count           the elements, all of them
```

**The float accumulator is sized in ELEMENTS and the lane array in LANES**, and
at the reference they are different numbers: each of `facc0`/`facc1` holds
sixteen E8M15 slots because a 256-bit register holds sixteen FP16 elements,
while four lanes compute them in four passes. Nothing in the program sees the
passes — the instruction retires once.

The scalar core keeps doing what it is good at: addresses, trip counts,
branches. The vector unit never computes an address and never takes a branch. A
loop is a scalar loop whose body happens to move 32 bytes at a time.

**The scalar half has no arithmetic beyond RV32I** — no multiply, no divide, no
float — so every product in a kernel, integer or floating, is a vector
instruction. That is a deliberate split and it is costed in
[the base PE's microarchitecture](../../../arch/pe/microarchitecture.md#the-arithmetic-the-ex-stage-does-not-have).

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
248** cycles ([performance](performance.md) states which build those were taken
on).

That is the whole thesis, and it is also the boundary: this design goes wide on
work that is *uniform*. When lanes need to take different paths, or fetch from
different addresses, the answer is a different machine and not a wider one — see
[what this PE does not own](#what-this-pe-does-not-own).

## What a mesh holds

The SIMD PE is a replicated unit, so its LUT count is machine capacity rather
than a line in a report. Against roughly **350,000 usable LUT** per mesh:

```
   8 SIMD PEs    8 x 13,772  =  110,176
   4 SIMT PEs    4 x 21,586  =   86,344
                              --------
                               196,520      the PE array
   2 controllers                  4,954
                              --------
                               201,474      against a ~350k budget
```

PROJECTED — arithmetic over per-PE measurements, not a placed mesh. The float
throughput of that array is `8 × 4 + 4 × 8 = 64` fused multiply-adds per clock,
which is **exactly one Mali-G610 shader core**.

## The pages

| Page | What is in it |
|---|---|
| [lanes](lanes.md) | what a lane **is**, how one instruction drives eight of them, how four int8 elements share one carry chain, and why the float lane count is a different number |
| [accumulator](accumulator.md) | `vdot` cycle by cycle, why a stream of them never stalls, the four multipliers, and what `SIMD_DOTDSP` does to the latency contract |
| [memory](memory.md) | the vector scratchpad's banks and two faces, the vector register file, and the float partial store |
| [pipeline](pipeline.md) | where the unit sits among the base pipeline's six register boundaries, every hazard it adds, and why the scalar critical path is untouched |
| [**float**](float.md) | one compute format and two operand widths, what this tier reaches of that, lanes against elements, the rotating accumulator, and **what is not built** |
| [programming](programming.md) | the instruction set, the encoding, the C intrinsics, and a kernel written twice |
| [performance](performance.md) | what the reference costs, which knobs are worth turning, **what each feature costs against the kernel that uses it**, and which figures were not carried forward and why |
| [gates](gates.md) | the benches that must pass before a number is quotable, what the list does **not** cover, and the stale-artifact incident that made two of them pass by finding nothing to run |

If you have never read a SIMD datapath before, read [lanes](lanes.md) and then
[accumulator](accumulator.md); those two are the machine.

## What this PE does not own

| Concern | Whose |
|---|---|
| per-lane branching, per-lane addresses, masks and predication | a SIMT core's. Nothing here anticipates them, and adding them would cost every uniform kernel |
| elementwise float, float compare and min/max | not encoded at all — they write a vector register from a fifteen-cycle datapath, which needs a scoreboard. An accumulating instruction needs only the accumulator's own busy shadow ([float](float.md#what-is-not-built)) |
| transcendentals | **they do not exist here**, and the cause is the same one that makes the tier cheap: `vec_alu` computes `exp2`, `log2`, `inv` and `rsqrt` at full rate, and the float lane ties its operation to FMA, so constant propagation removes them before anything is placed |
| where operands come from before the scratchpad | [sysnode](../../../arch/sysnode/) — the memory agent fills the vector scratchpad the way it fills any window |
| the flit, the router, the port | [noc](../../../arch/noc/) |
