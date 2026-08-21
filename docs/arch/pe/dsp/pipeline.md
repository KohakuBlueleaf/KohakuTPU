---
title: The vector unit in the pipeline
summary: Where the vector unit attaches to the base core's six register boundaries, the four hazards it adds, and why the scalar critical path is unchanged.
tags:
  - architecture
  - pe
  - dsp
  - microarchitecture
---

# The vector unit in the pipeline

The vector unit attaches to the base core the way the base core attaches to
memory: **the instruction arrives in EX, the operands come out of an array in
MEM, and the result is written at the end of MEM.** No stage moves, no boundary
is added, and the scalar datapath is untouched.

That is worth stating as a design property rather than a claim of tidiness. The
base core's frequency is set by its load-return path, and a extension that put
anything new on that path would slow every program including the ones that use
no vector instructions at all.

## The overlay

The base core has five architectural stages and
[six register boundaries](../microarchitecture.md#the-pipeline). The vector
unit occupies the last three:

```
   IF1   next-PC select                       |
   IF2   instruction out, decode              |  unchanged
   ID    operands out, forwarding             |
   ------------------------------------------ + ---------------------------
   EX    ALU, branch resolve, address         |  VECTOR DECODE
         rs1 + imm  ------------------------> |  vector scratchpad address
                                              |  vector register read address
                                              |  shift masks, element mask
   ------------------------------------------ + ---------------------------
   MEM   scalar array address, write enables  |  operands OUT of the file
                                              |  the lanes compute
                                              |  the vector file is WRITTEN
   ------------------------------------------ + ---------------------------
   WB    scalar array data out, commit        |  vextr / vredsum / vredmax
                                              |  join the scalar writeback
```

Three things cross the boundary between the two halves, and only three: the
address `rs1 + imm` computed by the EX adder, a scalar operand for `vsplat`,
and — in the other direction — a stall.

## Decode happens in EX, and is registered

The vector unit decodes the instruction in EX and registers the *decode* rather
than the instruction word: about 90 flops carrying the operation select, the
element width, the shift masks and the destination.

Decoding in MEM instead — from a registered instruction word, which looks
cheaper — puts the whole control cone in series with the datapath: `funct7`
through the operation select, and the element width through the shifters that
build the masks, all ahead of the lane array and the result mux and then the
register file's write port. That is 24 logic levels, and it closes at
**172.7 MHz** against a 339.7 MHz design that is otherwise identical.

This is the same structure the base core's own decode has, for the same
measured reason: [microarchitecture](../microarchitecture.md#the-pipeline).

**The masks are built once, in EX, for every lane.** The shift amount and the
element width are identical in every lane, so the three small shifters that
build the keep/round masks are shared across the whole unit rather than
replicated `SIMD` times — and being registered, they are ready at the start of
MEM rather than part way through it.

## The vector file is written in MEM, not WB

A synchronous array returns the pre-write value for a read captured at the
write's own edge. Writing the vector file in WB would therefore leave a
dependent instruction reading a stale entry at **both** distance 1 and distance
2. Writing it a stage earlier leaves only distance 1 — the same shape as the
base core's load-use hazard — and costs one stall instead of two.

On `vld; vld; vdot`, the innermost loop of most kernels here, that is 4 cycles
per 32 multiply-accumulates instead of 5.

`DSP_WB = 1` is the other arrangement: the result is registered before the file
is written, which halves the write path and costs the second stall.
[performance](performance.md) prices both, in cycles and in megahertz, because
this is the one parameter where the two move in opposite directions.

## Hazards

Four rules, and every stall in the unit is one of them.

| Rule | Fires when | Costs |
|---|---|---|
| **RAW on a vector register** | an instruction in EX reads the register an instruction in MEM is writing | 1 cycle |
| **scratchpad port** | a `vld` in EX wants the port a `vst` in MEM is using | 1 cycle |
| **accumulator observed** | `vaccrd`, `vaccz` or `vaccwr` in EX while an accumulate is in flight | up to 2 cycles |
| **multi-cycle stretch** | `vmul`, or a pipelined reduction, holding MEM for its second cycle | 1 cycle |
| **store then vector load** | a `vld` in **decode** behind any scalar store in EX — a **bubble**, not a stall | 1 cycle |

The last of those is the one whose *mechanism* matters. A stall from the vector
unit holds the MEM stage as well as EX, so a stall waiting on something in the
MEM stage would never be released; the pair is separated in decode instead,
exactly as the base core separates a load from its use.

The RAW rule **stalls rather than forwards**, which is the opposite of the
scalar core's verdict at the same distance, and the difference is width: a
forwarding mux at 256 bits is 256 LUT on the widest path in the unit, against
32 LUT for the scalar one. A stall is one cycle on a dependency software can
usually unroll away; the mux would be on the critical path of every vector
instruction whether or not it was needed.

`vdot` is deliberately absent from that table. A stream of dot products issues
**one per cycle**, into the same accumulator or different ones, because the
accumulate pipeline takes them in issue order — [accumulator](accumulator.md).
Only observing or disturbing an accumulator waits.

## Faults join the path that already exists

The vector unit raises two refusals, and both are decoded in EX so they join
the base core's existing fault path rather than opening a second one:

- **an encoding this build does not carry** — a shift on a build without the
  shifter, `vdot.s8` on a build with two multipliers per lane, a register
  number above `DSP_VREGS`. Each faults, which is what makes "a build without
  X lacks those instructions" a checkable statement instead of a description.
- **a misaligned vector address.**

Both surface as the base core's ordinary illegal-instruction halt, with the
offending PC in the halt word ([architecture](../architecture.md#halting)).

## Why the scalar path is unchanged

Four specific things could have touched it, and each is placed so that it does
not:

| Could have | Is instead |
|---|---|
| a fifth source on the scalar load mux, for reading vector memory | the region is store-only from the scalar side; a load faults — [memory](memory.md#the-scalar-core-can-store-here-but-cannot-load) |
| a wider writeback mux for `vextr` / `vredsum` | one more input to the writeback mux that already chooses between scratchpad, control, and cache, one cycle after the result is registered |
| a new stall term fanning out across the front end | the unit's stall joins the MEM stage's existing hold, which the front end already listens to; the decode bubble is one AND of two registered bits added to the hazard term that was there |
| the fabric's state reaching the core's stall | it does not: the NoC writes the vector window through a port nothing in the core touches — [memory](memory.md#the-scalar-core-can-store-here-but-cannot-load) |
| a decode path for a new opcode major | a small predecoder beside the existing one, whose output overrides the illegal-instruction bit and nothing else |

With `DSP_EN = 0` all of it disappears — generate, not zero-width — and the PE
is the base core bit for bit.
</content>
