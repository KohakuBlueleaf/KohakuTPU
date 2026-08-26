---
title: The vector unit in the pipeline
summary: Where the vector unit attaches to the base core's register boundaries, every hazard it adds on an integer and on a float build, and why the scalar critical path is unchanged.
tags:
  - architecture
  - pe
  - simd
  - microarchitecture
---

# The vector unit in the pipeline

> **Kind: Yours throughout.** Where the vector unit attaches to the base core's
> register boundaries, and every hazard it adds, are internal to this project's
> PE. The base core is the framework's RV32 controller, and attaching a wide
> datapath to its execute stage is exactly what the `SIMD_EN` slot exists for
> ([integrate/addon-slots](../../../integrate/addon-slots.md)).

The vector unit attaches to the base core the way the base core attaches to
memory: **the instruction arrives in EX, the operands come out of an array in
MEM, and the result is written at the end of MEM.** No stage moves, no boundary
is added, and the scalar datapath is untouched.

That is worth stating as a design property rather than a claim of tidiness. The
base core's frequency is set by its load-return path, and an extension that put
anything new on that path would slow every program including the ones that use
no vector instructions at all.

## The overlay

The base core has five architectural stages and
[six register boundaries](../../../arch/cpu/rv32-pe/microarchitecture.md#the-pipeline).
The vector unit occupies the last three:

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
> through the operation select, and the element width through the barrel
> shifters that build the masks, all ahead of the lane array and the result mux
> and then the register file's write port. That is **24 logic levels, and it
> synthesises at 172.7 MHz** against a design that is otherwise identical and
> reports above 330. Both are out-of-context synthesis estimates; the ratio is
> the finding, not either number.

This is the same structure the base core's own decode has, for the same measured
reason: [microarchitecture](../../../arch/cpu/rv32-pe/microarchitecture.md#the-pipeline).

**The masks are built once, in EX, for every lane.** The shift amount and the
element width are identical in every lane, so the three small shifters that
build the keep and round masks are shared across the whole unit rather than
replicated per lane — and being registered, they are ready at the start of MEM
rather than part way through it.

## Where the vector file is written

A synchronous array returns the pre-write value for a read captured at the
write's own edge. Writing the vector file in WB would therefore leave a
dependent instruction reading a stale entry at **both** distance 1 and distance
2. Writing it a stage earlier leaves only distance 1 — the same shape as the
base core's load-use hazard — and costs one stall instead of two.

**`rv_pe` nonetheless defaults `SIMD_WB` to 1**, registering the result before
the write, and the reason is the constraint rather than the kernels: at a tight
period the write stage buys frequency the PE does not otherwise reach, and at a
loose one it costs a small amount of area and gains nothing. `khs_unit`'s own
`WB_STAGE` default is still 0, so **what a bench builds depends on which level
it instantiates** — a component bench must set it explicitly to match what
ships.

The consequence is a hazard that does not exist at `WB_STAGE = 0`, and it is in
the table below.

## Hazards

Every stall the unit can raise is one of these rules.

| rule | fires when | costs |
|---|---|---|
| **RAW on a vector register** | an instruction in EX reads the register an instruction in MEM is writing | 1 cycle |
| **RAW at distance 2** | only at `WB_STAGE = 1`, **which is what ships**: the write lands a stage later, so a read-first array still returns the pre-write value two instructions behind | 1 more cycle |
| **scratchpad port** | a `vld` in EX wants the port a `vst` in MEM is using | 1 cycle |
| **multi-cycle stretch** | `vmul`, or a pipelined reduction, holding MEM for its second cycle | 1 cycle |
| **narrowed width** | any instruction whose feature is built below full rate, holding MEM until its last pass issues | `passes − 1` |
| **store then vector load** | a `vld` in **decode** behind any scalar store into the vector window — a **bubble**, not a stall | 1 cycle |

Three more exist only when the float accumulator is built, and all of them hold
the whole MEM stage so that the instruction retires **once** rather than per
step:

| float rule | fires when | costs |
|---|---|---|
| **the sweep** | `vfaccz` or `vfaccwr` clearing or seeding a one-write-port memory | `NPART` cycles |
| **the fold** | `vfaccrd` combining the partials through the same unit | `NPART × (ALAT+1) + passes` — **112 + passes** at `NPART` 16 with no seed units, 176 + passes with them |
| **float accumulate in flight** | `vfaccz`, `vfaccwr` or `vfaccrd` in EX within the tier's latency of a `vfmacc` | up to `ALAT` cycles — 6 with no seed units, 10 with them |

**`vfmacc` is deliberately absent from the "in flight" rules.** Each result
reaches its accumulate stage in issue order beside its own partial index, so one
may arrive every cycle. Only reading or disturbing an accumulator has to wait
for the pipeline behind it to drain, and that happens once at the end of a
reduction rather than inside it.

**The RAW rule stalls rather than forwards**, which is the opposite of the
scalar core's verdict at the same distance, and the difference is width: a
forwarding mux at 256 bits is 256 LUT on the widest path in the unit, against 32
LUT for the scalar one. A stall is one cycle on a dependency software can
usually unroll away; the mux would be on the critical path of every vector
instruction whether or not it was needed.

### Why the last integer rule is a bubble and not a stall

A stall from the vector unit holds the MEM stage as well as EX, so a stall
waiting on something *in* the MEM stage would never be released. The
scratchpad-port rule above is safe because a `vst` in MEM will retire on its
own; a **scalar** store into the same window is not, because it is the MEM-stage
instruction. That pair is separated in decode instead, exactly as the base core
separates a load from its use, and the bubble is one AND of two registered bits
added to a hazard term that was already there.

> **A hold term must not read the combined hold signal**, because the combined
> signal contains it and reading it back closes a combinational loop. Every
> width's hold is derived from registered state only, and the issue gate reads
> the other hold terms individually.

> **A hold that stops only the upper stage must send the lower stage a bubble.**
> A stall that stops two adjacent stages together may freeze the register
> between them; a hold that stops EX alone must not, because MEM continues to
> advance and would otherwise retire the instruction it holds once per held
> cycle. The multiply hold in the base core is exactly this shape.

## Faults join the path that already exists

The vector unit raises two refusals, and both are decoded in EX so they join the
base core's existing fault path rather than opening a second one:

- **an encoding this build does not carry** — a shift on a build with
  `SHIFT_UNITS = 0`, a permute on `PERM_UNITS = 0`, a reduce on `RED_UNITS = 0`,
  a converter form on `FCVT_UNITS = 0`, any float instruction on
  `FLOAT_LANES = 0`, a `vextr` lane index at or above `SIMD`, a register number
  at or above `VREGS`, or any element type but the one the tier computes in.
  Each faults, which is what makes "a build without X lacks those instructions"
  a checkable statement rather than a description.
- **a misaligned vector address.**

Both surface as the base core's ordinary illegal-instruction halt, with the
offending program counter in the halt word
([architecture](../../../arch/cpu/rv32-pe/architecture.md#halting)).

## Why the scalar path is unchanged

Four specific things could have touched it, and each is placed so that it does
not:

| could have | is instead |
|---|---|
| a fifth source on the scalar load mux, for reading vector memory | the region is store-only from the scalar side; a load faults — [memory](memory.md#the-scalar-core-can-store-here-but-cannot-load) |
| a wider writeback mux for `vextr` / `vredsum` | one more input to the writeback mux that already chooses between scratchpad, control and cache, one cycle after the result is registered |
| a new stall term fanning out across the front end | the unit's stall joins the MEM stage's existing hold, which the front end already listens to |
| the fabric's state reaching the core's stall | it does not: the network writes the vector window through a port nothing in the core touches — [memory](memory.md#why-a-256-bit-array-is-eight-arrays) |
| a decode path for a new opcode major | a small predecoder beside the existing one, whose output overrides the illegal-instruction bit and nothing else |

With `SIMD_EN = 0` all of it disappears — a generate, not a zero width — and the
PE is the base core bit for bit.
