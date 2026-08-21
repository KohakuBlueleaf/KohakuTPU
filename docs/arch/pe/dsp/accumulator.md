---
title: The dot product and the accumulator
summary: What vdot computes, why a multiply-accumulate needs an accumulator instead of a third register read port, and the dataflow cycle by cycle from operands to accumulated sum.
tags:
  - architecture
  - pe
  - dsp
  - simd
---

# The dot product and the accumulator

Multiply-accumulate is the operation the whole datapath exists for, and it is
the one operation that does not fit the shape of every other instruction. Two
sources in, one destination out, is what a register file with two read ports
and one write port supports. A MAC wants **three** sources — the two factors
and the running total — and it wants the total back where it came from.

The answer is an accumulator: a small state that lives outside the register
file, is read and written by the multiplier's own datapath, and never makes a
round trip through the registers.

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
which is what makes `vaccrd` (accumulator to vector register) a move rather
than a narrowing.

Within-lane reduction is the ARM `SDOT` and x86 `VPDPBUSD` shape rather than a
whole-vector dot. The difference matters when laying out data: a dot product of
two long arrays falls out naturally (stream both, one `vdot` per 32 elements,
one `vredsum` at the end), while an 8-tap FIR does not, because its taps are
adjacent elements of one array rather than paired elements of two.

`vdot.s16` is the same instruction over two int16 pairs per lane. `vdotn`
subtracts instead of adding.

## Why not a third read port

The alternative is a register-to-register MAC — read the total from a vector
register, add, write it back. Two things go wrong, and the second is the one
that decides.

A third read port on a 256-bit register file means a **third mirrored array**,
because no FPGA memory primitive offers three independent read ports: the file
grows by half again for one instruction's benefit.

More importantly, the accumulate becomes a **read-modify-write of the register
file through the whole datapath**: file read, multiply, add, file write, all
inside one cycle, and the register file is the widest structure in the unit.
That loop is what sets the clock, and it is the loop the DSP48's own P-register
idiom exists to avoid — a hardware multiplier accumulates into its own output
register, never through the machine's registers.

The accumulators here are a small fabric array rather than the DSP48's
P register, for two reasons that are worth the fabric: the **count** is a
parameter (`DSP_NACC`), and `vaccwr` can seed an accumulation with a bias
vector as a plain write. What is kept from the idiom is the part that matters —
the running total never enters the register file, and its recurrence is one
32-bit add.

## The dataflow, cycle by cycle

The instruction occupies MEM for one cycle and its arithmetic finishes after
it, in the background. Three stages, one register boundary each:

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

At `T` the instruction is in MEM: operands arrive from the register file and
the four multipliers compute. Their outputs register at the end of that cycle —
which is what a DSP48 does anyway, and taking the free register is why the
multiply is not in series with the adder tree.

At `T+1` the four products are registered values, and the small adder tree that
sums them runs. At `T+2` the sum is registered, and the accumulator's own
one-cycle add takes it. At `T+3` the accumulator holds the new total.

### Why a stream of them never stalls

Each stage is a register, so the three stages are a pipeline and not a latency
to wait out:

```
   vdot #1    T: mul    T+1: sum    T+2: acc +=
   vdot #2            T+1: mul    T+2: sum    T+3: acc +=
   vdot #3                      T+2: mul    T+3: sum    T+4: acc +=
```

`vdot` therefore issues at **one per cycle**, back to back, including into the
same accumulator. Correctness comes from ordering rather than from waiting: the
accumulator index travels down the pipeline beside the sum, each sum reaches
the accumulate stage in issue order, and the accumulate itself is a one-cycle
recurrence, so a new sum can arrive every cycle.

That is the whole point of an accumulator. A dot product's arithmetic is a
dependent chain — every product must be added to the same total — and the
pipeline makes it a chain of one-cycle adds rather than a chain of full
multiply-add latencies.

### What does wait

Three instructions **disturb or observe** the accumulator rather than feeding
it: `vaccrd` reads one, `vaccz` clears one, `vaccwr` seeds one. Each of them
waits for the pipeline to drain — at most two cycles — because an accumulate
still in flight belongs to an older instruction and must land first.

That is the only stall the accumulator can cause, and it costs nothing in a
real kernel: draining happens once at the end of a reduction, not inside it.

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

A 128-element int8 dot product this way is **52 cycles**, against 8,221 on the
scalar core — [performance](performance.md) separates how much of that is
width and how much is simply owning a multiplier.

## The multipliers, and why there are four per lane

A 4-way int8 dot needs four products; a 2-way int16 dot needs two, and those
two need to be 16×16. So each lane carries **two 16×16 multipliers with muxed
operands, and two 8×8 multipliers that exist only for int8**.

```
   et = s16   m0: a[15:0] * b[15:0]      m1: a[31:16] * b[31:16]
              m2, m3 idle

   et = s8    m0: a[7:0]  * b[7:0]       m1: a[15:8]  * b[15:8]
              m2: a[23:16]* b[23:16]     m3: a[31:24] * b[31:24]
```

There is no cheaper arrangement on this device, and the reason is specific
enough to be worth stating: a DSP48E2's B port is 18 bits **signed**, which
holds one int8 operand and not two. The well-known trick of packing two int8
MACs into one DSP48 requires the two products to **share an operand** — and a
dot product's operands both vary, so they cannot.

`DSP_MULS = 2` builds only the two wide multipliers. It saves 16 DSP at eight
lanes and removes int8 multiplication entirely: `vdot.s8` and `vmul.s8` become
illegal encodings on that build rather than instructions that quietly return
the wrong answer for two of every four elements.

`vmul` reads the same four products one cycle earlier than the dot sum and
keeps the low half of each — an element-wise product, not a reduction. It is
the one instruction that takes an extra cycle in MEM, because its result is the
product register itself.
</content>
