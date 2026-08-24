---
title: The vector unit in the pipeline
summary: Where the vector unit attaches to the base core's six register boundaries, every hazard it adds on an integer and on a float build, and why the scalar critical path is unchanged.
tags:
  - architecture
  - pe
  - simd
  - microarchitecture
---

# The vector unit in the pipeline

The vector unit attaches to the base core the way the base core attaches to
memory: **the instruction arrives in EX, the operands come out of an array in
MEM, and the result is written at the end of MEM.** No stage moves, no boundary is
added, and the scalar datapath is untouched.

That is worth stating as a design property rather than a claim of tidiness. The
base core's frequency is set by its load-return path, and an extension that put
anything new on that path would slow every program including the ones that use no
vector instructions at all.

## The overlay

The base core has five architectural stages and
[six register boundaries](../../../arch/pe/microarchitecture.md#the-pipeline). The vector unit
occupies the last three:

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
address `rs1 + imm` computed by the EX adder, a scalar operand for `vsplat`, and
— in the other direction — a stall.

## Decode happens in EX, and the DECODE is what is registered

The vector unit decodes the instruction in EX and registers the *decode* rather
than the instruction word: about 90 flops carrying the operation select, the
element width, the shift masks and the destination.

> Decoding in MEM instead — from a registered instruction word, which looks
> cheaper — puts the whole control cone in series with the datapath: `funct7`
> through the operation select, and the element width through the barrel shifters
> that build the masks, all ahead of the lane array and the result mux and then
> the register file's write port. That is **24 logic levels, and it closes at
> 172.7 MHz against a 339.7 MHz design that is otherwise identical.**

This is the same structure the base core's own decode has, for the same measured
reason: [microarchitecture](../../../arch/pe/microarchitecture.md#the-pipeline).

**The masks are built once, in EX, for every lane.** The shift amount and the
element width are identical in every lane, so the three small shifters that build
the keep/round masks are shared across the whole unit rather than replicated
`SIMD` times — and being registered, they are ready at the start of MEM rather
than part way through it.

## Where the vector file is written

A synchronous array returns the pre-write value for a read captured at the write's
own edge. Writing the vector file in WB would therefore leave a dependent
instruction reading a stale entry at **both** distance 1 and distance 2. Writing
it a stage earlier leaves only distance 1 — the same shape as the base core's
load-use hazard — and costs one stall instead of two. On `vld; vld; vdot`, the
innermost loop of most kernels here, that is 4 cycles per 32 multiply-accumulates
instead of 5.

**`rv_pe` nonetheless defaults to `SIMD_WB = 1`**, and the reason is the constraint
rather than the kernels: at the 2.857 ns ask the write stage is part of a
1,210 LUT and 31.4 MHz change and the PE does not otherwise close, where at
3.333 ns the same stage measured +89 LUT and −28 MHz. `khs_unit`'s own parameter
default is still 0, so **what a bench builds depends on which level it
instantiates** — `khs_unit_tb` defaults it to 1 to match what ships.

The consequence is a hazard that does not exist at `SIMD_WB = 0`, and it is in the
table below.

## Hazards

Every stall the unit can raise is one of these rules.

| Rule | Fires when | Costs |
|---|---|---|
| **RAW on a vector register** | an instruction in EX reads the register an instruction in MEM is writing | 1 cycle |
| **RAW at distance 2** | only at `SIMD_WB = 1`, **which is what ships**: the write lands a stage later, so a read-first array still returns the pre-write value two instructions behind | 1 more cycle |
| **scratchpad port** | a `vld` in EX wants the port a `vst` in MEM is using | 1 cycle |
| **accumulator observed** | `vaccrd`, `vaccz` or `vaccwr` in EX while a dot is in flight | up to `DOT_LAT` — **4 as shipped**, 2 at `SIMD_DOTDSP = 0` |
| **multi-cycle stretch** | `vmul`, or a pipelined reduction, holding MEM for its second cycle | 1 cycle |
| **store then vector load** | a `vld` in **decode** behind any scalar store into the vector window — a **bubble**, not a stall | 1 cycle |

Three more exist only on a float build, and all of them hold the whole MEM stage
so that the instruction retires **once** rather than per step:

| Float rule | Fires when | Costs |
|---|---|---|
| **the pass walk** | a `vfmacc` / `vfmsac` that has not issued all its passes | `passes − 1` — **3 at four float lanes**, 0 at one lane per element |
| **the sweep** | `vfaccz` or `vfaccwr` clearing or seeding a one-write-port memory | `NPART` cycles, plus the element count again for `vfaccwr`'s walked converter |
| **the fold** | `vfaccrd` combining the partials through the same lane | about 270 cycles |
| **float accumulate in flight** | `vfaccz`, `vfaccwr` or `vfaccrd` in EX within fifteen cycles of a `vfmacc` | up to 15 cycles |

**`vdot` and `vfmacc` are deliberately absent from the "in flight" rules.** Each
sum reaches its accumulate stage in issue order beside its own destination index,
so one may arrive every cycle. Only reading or disturbing an accumulator has to
wait for the pipeline behind it to drain.

**The RAW rule stalls rather than forwards**, which is the opposite of the scalar
core's verdict at the same distance, and the difference is width: a forwarding mux
at 256 bits is 256 LUT on the widest path in the unit, against 32 LUT for the
scalar one. A stall is one cycle on a dependency software can usually unroll away;
the mux would be on the critical path of every vector instruction whether or not
it was needed.

### Why the last integer rule is a bubble and not a stall

A stall from the vector unit holds the MEM stage as well as EX, so a stall waiting
on something *in* the MEM stage would never be released. The scratchpad-port rule
above is safe because a `vst` in MEM will retire on its own; a **scalar** store
into the same window is not, because it is the MEM-stage instruction. That pair is
separated in decode instead, exactly as the base core separates a load from its
use, and the bubble is one AND of two registered bits added to a hazard term that
was already there.

> The float hold terms are the same shape one level down, and they contain a trap:
> the pass-walk term must **not** read the combined hold signal, because the
> combined signal contains it and reading it back closes a combinational loop.

## Faults join the path that already exists

The vector unit raises two refusals, and both are decoded in EX so they join the
base core's existing fault path rather than opening a second one:

- **an encoding this build does not carry** — a shift on a build without the
  shifter, `vdot.s8` on a build with two multipliers per lane, a `.s32` dot, a
  `.f32` float form, a register number above `SIMD_VREGS`, a float instruction at
  all on `SIMD_FLOAT = 0`. Each faults, which is what makes "a build without X
  lacks those instructions" a checkable statement instead of a description.
- **a misaligned vector address.**

Both surface as the base core's ordinary illegal-instruction halt, with the
offending PC in the halt word ([architecture](../../../arch/pe/architecture.md#halting)).

## Why the scalar path is unchanged

Four specific things could have touched it, and each is placed so that it does
not:

| Could have | Is instead |
|---|---|
| a fifth source on the scalar load mux, for reading vector memory | the region is store-only from the scalar side; a load faults — [memory](memory.md#the-scalar-core-can-store-here-but-cannot-load) |
| a wider writeback mux for `vextr` / `vredsum` | one more input to the writeback mux that already chooses between scratchpad, control and cache, one cycle after the result is registered |
| a new stall term fanning out across the front end | the unit's stall joins the MEM stage's existing hold, which the front end already listens to |
| the fabric's state reaching the core's stall | it does not: the NoC writes the vector window through a port nothing in the core touches — [memory](memory.md#why-a-256-bit-array-is-eight-arrays) |
| a decode path for a new opcode major | a small predecoder beside the existing one, whose output overrides the illegal-instruction bit and nothing else |

With `SIMD_EN = 0` all of it disappears — generate, not zero-width — and the PE is
the base core bit for bit.
