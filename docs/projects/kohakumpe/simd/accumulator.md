---
title: The dot product and the accumulator
summary: What vdot computes, why a multiply-accumulate needs an accumulator instead of a third register read port, the dataflow cycle by cycle, and what SIMD_DOTDSP does to both the DSP48 count and the latency contract.
tags:
  - architecture
  - pe
  - simd
---

# The dot product and the accumulator

Multiply-accumulate is the operation the whole datapath exists for, and it is
the one operation that does not fit the shape of every other instruction. Two
sources in, one destination out, is what a register file with two read ports and
one write port supports. A MAC wants **three** sources — the two factors and the
running total — and it wants the total back where it came from.

The answer is an accumulator: a small state that lives outside the register file,
is read and written by the multiplier's own datapath, and never makes a round
trip through the registers.

## What `vdot` computes

`vdot.s8 acc0, v1, v2` reduces **within each 32-bit lane**. One lane holds four
int8 elements from each source; the four products are summed to one int32, and
that int32 is added into the lane's slot of the accumulator.

```
   one lane of vdot.s8

   v1 lane  | a3 | a2 | a1 | a0 |      four int8
   v2 lane  | b3 | b2 | b1 | b0 |

              a3*b3  a2*b2  a1*b1  a0*b0        four products
                 \     |      |     /
                  +----+------+----+
                          |
                     one int32 sum
                          |
                          v
              acc0[lane]  +=  sum                32-bit, in place
```

Every lane does that independently, so at eight lanes one instruction performs
**32 int8 multiply-accumulates** and produces eight independent running totals.
An accumulator is therefore `SIMD × int32` — exactly one vector register wide,
which is what makes `vaccrd` (accumulator to vector register) a move rather than
a narrowing.

Within-lane reduction is the ARM `SDOT` and x86 `VPDPBUSD` shape rather than a
whole-vector dot. The difference matters when laying out data: a dot product of
two long arrays falls out naturally (stream both, one `vdot` per 32 elements, one
`vredsum` at the end), while an 8-tap FIR does not, because its taps are adjacent
elements of one array rather than paired elements of two.

`vdot.s16` is the same instruction over two int16 pairs per lane. `vdotn`
subtracts instead of adding. `.s32` **faults** — an int32 product does not fit a
34-bit lane sum, so the encoding is refused rather than truncated.

## Why not a third read port

The alternative is a register-to-register MAC — read the total from a vector
register, add, write it back. Two things go wrong, and the second is the one that
decides.

A third read port on a 256-bit register file means a **third mirrored array**,
because no FPGA memory primitive offers three independent read ports: the file
grows by half again for one instruction's benefit.

More importantly, the accumulate becomes a **read-modify-write of the register
file through the whole datapath**: file read, multiply, add, file write, all
inside one cycle, on the widest structure in the unit. That loop is what sets the
clock, and it is the loop the DSP48's own P-register idiom exists to avoid.

What is kept from that idiom is the part that matters — the running total never
enters the register file, and its recurrence is one 32-bit add. What is *not*
kept is the P register itself: the accumulators are fabric registers, so `NACC`
is a parameter and `vaccwr` (seeding an accumulation with a bias vector) is a
plain write.

> **Per-bank registers, not one array indexed by a runtime bank.** As an array,
> `vaccz` is a *third* input on every flop's write mux; as its own condition on a
> per-bank register it is the flop's own synchronous clear, which is free.
> And leave the accumulate a ternary — rewriting it as one adder with a
> conditionally inverted operand measured **byte-identical at 17,792 LUT**.

## The dataflow, cycle by cycle

The instruction occupies MEM for one cycle and its arithmetic finishes after it,
in the background. How many stages are behind it is `DOT_LAT`, and that number is
a **contract between `khs_unit` and `khs_lane`**: both derive it from the same
two parameters, and a disagreement would land an accumulate on the wrong
destination.

| build | `DOT_LAT` | where the sum comes from |
|---|---:|---|
| `SIMD_DOTDSP = 0` | **2** | a fabric adder tree, one register deep behind the products |
| **`SIMD_DOTDSP = 1`, `MULS ≥ 4` — what ships** | **4** | a **DSP48 PCIN cascade**: four terms, one hop per cycle |

The fabric form is drawn below because it is the shorter one and the shape is the
same. The cascade adds two more hops between the products and the accumulate, and
pipelines the operands to match so that term *k* waits *k* cycles.

```
   cycle          T                T+1              T+2              T+3
   ------------------------------------------------------------------------
   operands   v1, v2 out of
              the register file
   multiply   4 products
              (combinational)
                    |___ products registered
   sum                             p0+p1+(p2+p3)
                                        |___ sum registered
   accumulate                                       acc += sum
                                                         |___ accumulator
                                                              holds it
```

At `T` the instruction is in MEM: operands arrive from the register file and the
four multipliers compute. Their outputs register at the end of that cycle — which
is what a DSP48 does anyway, and taking the free register is why the multiply is
not in series with the adder tree.

At `T+1` the four products are registered values and the small adder tree that
sums them runs. At `T+2` the sum is registered, and the accumulator's own
one-cycle add takes it. At `T+3` the accumulator holds the new total.

### Why a stream of them never stalls

Each stage is a register, so the stages are a pipeline and not a latency to wait
out:

```
   vdot #1    T: mul    T+1: sum    T+2: acc +=
   vdot #2            T+1: mul    T+2: sum    T+3: acc +=
   vdot #3                      T+2: mul    T+3: sum    T+4: acc +=
```

`vdot` therefore issues at **one per cycle**, back to back, including into the
same accumulator. Correctness comes from ordering rather than from waiting: the
accumulator index and the negate bit travel down the pipeline beside the sum,
each sum reaches the accumulate stage in issue order, and the accumulate itself
is a one-cycle recurrence, so a new sum can arrive every cycle.

That is the whole point of an accumulator. A dot product's arithmetic is a
dependent chain — every product must be added to the same total — and the pipeline
makes it a chain of one-cycle adds rather than a chain of full multiply-add
latencies.

> **A dot does not wait for a dot**, and it is worth knowing what the alternative
> cost. Making `vdot` wait like `vaccrd`/`vaccz`/`vaccwr` cost **3 cycles per dot**
> on `dot2_i8_v` — 80 cycles for 58 instructions, where every other hazard in that
> kernel accounts for 9.

### What does wait

Three instructions **disturb or observe** the accumulator rather than feeding it:
`vaccrd` reads one combinationally, `vaccz` clears one in MEM, `vaccwr` seeds one
in MEM. Each waits for the pipeline to drain — **up to `DOT_LAT` cycles, so 4 as
shipped** and 2 at `SIMD_DOTDSP = 0` — because an older dot's add would otherwise
land on top of that write, or be read before it landed.

That is the only stall the accumulator can cause, and it costs nothing in a real
kernel: draining happens once at the end of a reduction, not inside it.

## A dot product end to end

```
        vaccz   acc0                    clear the eight running totals
   loop:
        vld     v0, 0(s0)               32 int8 activations
        vld     v1, 0(s1)               32 int8 weights
        vdot.s8 acc0, v0, v1            32 multiply-accumulates
        addi    s0, s0, 32
        addi    s1, s1, 32
        addi    s2, s2, -1
        bnez    s2, loop
        vaccrd  v2, acc0                the eight totals into a vector register
        vredsum a2, v2                  and across the lanes into one int32
```

Two instructions end it, and both are necessary: the eight lanes hold eight
independent partial sums because nothing crosses lanes inside the loop, so the
lane-crossing reduction happens exactly once, outside it.

## The multipliers, and why there are four per lane

A 4-way int8 dot needs four products; a 2-way int16 dot needs two, and those two
need to be 16×16. So each lane carries **two 16×16 multipliers with muxed
operands, and two 8×8 multipliers that exist only for int8**.

```
   et = s16   m0: a[15:0] * b[15:0]      m1: a[31:16] * b[31:16]
              m2, m3 idle -- `hi` is tied to 34'sd0

   et = s8    m0: a[7:0]  * b[7:0]       m1: a[15:8]  * b[15:8]
              m2: a[23:16]* b[23:16]     m3: a[31:24] * b[31:24]
```

There is no cheaper arrangement on this device, and the reason is specific enough
to be worth stating: **a DSP48E2's B port is 18 bits signed**, which holds one
int8 operand and not two. The well-known trick of packing two int8 MACs into one
DSP48 requires the two products to **share an operand** — and a dot product's
operands both vary, so they cannot.

`SIMD_MULS = 2` builds only the two wide multipliers. It removes int8
multiplication entirely: `vdot.s8` and `vmul.s8` become **illegal encodings** on
that build rather than instructions that quietly return the wrong answer for two
of every four elements.

`vmul` reads the same registered products one cycle earlier than the dot sum and
keeps the low half of each — an element-wise product, not a reduction. It is the
one instruction that takes an extra cycle in MEM, because its result *is* the
product register.

The primitive is **named, not inferred**: `khs_mul` exists as its own module
because `use_dsp` takes a string *literal* and not a parameter, so choosing
between a DSP48 and fabric has to be a generate somewhere. Doing it once there
keeps the choice out of the lane's datapath and makes it a configuration row
rather than a synthesis guess.

## `SIMD_DOTDSP` builds a SECOND set of multipliers, deliberately

`SIMD_DOTDSP = 1` keeps the dot sum inside the DSP48 column instead of in a fabric
adder tree — and it does that by **adding four more multipliers per lane** rather
than re-using the four above.

That is not waste, it is the constraint: **`p0..p3` must still surface for
`vmul`**, and that single extra fanout is what stops the tool folding the sum into
the DSP48's post-adder. An operand with two consumers cannot be cascaded. So the
form pays for a second set and takes the adder back.

| at eight lanes | `SIMD_DOTDSP = 0` | `SIMD_DOTDSP = 1` |
|---|---:|---:|
| DSP48 for the integer tier | 32 | **64** |
| fabric for the dot sum | 256 LUT + 32 CARRY8 | **0 + 0** |
| `DOT_LAT` | 2 | **4** |

On this device LUT is the binding resource and DSP is not, so the trade is taken:
together with `SIMD_WB` it is 1,210 LUT and 31.4 MHz at the 2.857 ns ask
([performance](performance.md#the-two-knobs-the-tighter-ask-turned-on)). The
reference build's **72 DSP48** is that 64 plus two per float lane.

> **One multiply per stage.** Folding several terms into one register makes the
> DSP48 do multiply-then-post-add *combinationally* into PREG — an unpipelined
> column, which cost **12 MHz** when it was written that way. Each PCIN hop is a
> cycle, so term *k*'s operands wait *k* cycles; flops are the cheap resource here
> and an unpipelined DSP48 is not.

> **The cascade free-runs; it is NOT `mul_en` gated.** `mul_en` is a
> per-instruction pulse, and the fabric path can gate the products with it because
> the sum register flows anyway. Gating a multi-stage cascade instead **freezes
> hops 2..N and the result never arrives.** The accumulator's own shift register
> free-runs on the same clock, which is what keeps the two aligned.
