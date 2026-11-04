---
title: RV32 PE microarchitecture
summary: How the core is built and why — the six register boundaries, the multiplier, the arithmetic EX does not have, the hazard rules, the predictor, the two L1s, and the NoC requestor.
tags:
  - architecture
  - cpu
  - rv32
---

# RV32 PE microarchitecture

The [RV32 PE](README.md) is a small in-order RISC-V core packaged as a compute
unit; [architecture](architecture.md) is the contract it presents. This page is
the implementation behind that contract, and why each piece has the shape it
has.

The recurring theme: the core spends flip-flops and block RAM freely, prefers a
pipeline stage over a bypass, and registers anything whose combinational form
would fan out — because its objectives are LUT and frequency, never latency.

Resource and frequency figures on this page are **out-of-context synthesis, not
routed**, on `xcvu13p-fhgb2104-2L-e` under Vivado 2024.2, produced by
`scripts/tcl/ooc_rv_pe.tcl` unless another source is named. Synthesis slack is
optimistic; [performance](performance.md) states the request each figure was
taken at, and [measurement](../../physical/measurement.md) states what a figure
from this tree means in general.

## The pipeline

Five architectural stages, **six register boundaries**. The extra boundary
exists because the instruction window and the register file are synchronous
arrays: each costs a cycle between presenting an address and receiving data,
and counting those honestly is what lets the fetch loop close.

```
    IF1   next-PC select                    -> the instruction window's address register
    IF2   instruction out, decode           -> the register file's address register
    ID    operands out, forwarding          -> EX
    EX    ALU, multiply, branch resolve,    -> MEM
          effective address
    MEM   array address and write enables   -> WB
    WB    array data out, commit
```

The address path in fetch is `PC -> mux -> RAM address register` and nothing
else. Two consequences are structure, not detail:

- **Decode is combinational on the fetched word**, inside IF2. The
  register-file address leaves at the same edge as the control bits, buying the
  operand-fetch cycle instead of costing a seventh boundary.
- **The effective address leaves EX combinationally** — it is the ALU's own
  adder output — because the data arrays register their address input.

A branch or jump target is computed in ID rather than EX: PC and immediate are
both registered by then, the adder is off every critical path in that stage,
and carrying the target instead of the immediate keeps the EX register the same
width.

The EX stage is one adder, one subtractor and one shifter, not three of each.
`SLL`, `SRL` and `SRA` share a single 33-bit arithmetic right shifter between
two bit reversals — a left shift is a right shift on the reversed word, and
reversals are wiring. The subtractor is shared three ways: `SUB`, the
`SLT`/`SLTU` results, and every branch comparison come off it.

**The adder is not shared with it, and that is structural rather than an
oversight.** `sum = op1 + op2` is also `ex_addr`, which leaves the stage
combinationally to the data arrays' address pins; anything muxed into its
inputs lands in front of that path. It is the one adder in this core that
cannot be borrowed.

## The multiplier

EX carries `mul`, `mulh`, `mulhsu` and `mulhu`. What it does not carry is a
divider or any floating point.

**One 33 × 33 signed product serves all four forms** — only the operand
extension and which half is returned differ — in three pipeline stages. The
multiplier is free-running: the operands are frozen for the whole hold, so
every capture takes the same value and gating would buy nothing but a mux. A
multiply costs **three stall cycles**.

Two implementation properties are load-bearing:

- **The hold stops EX alone and bubbles MEM.** This is deliberately *not* what
  the memory stall does: the memory stall stops EX and MEM together, so
  freezing the MEM-valid bit under it is safe. Freezing it under the multiply
  hold instead would retire the instruction sitting in MEM once per held cycle.
- **The counter resets on advance.** Back-to-back multiplies hold the
  multiply-in condition across the stage boundary, so without the reset the
  second would retire carrying the first one's product.

`rv_ex.v` records this form as measured at **365.6 MHz** `[unverified]` — no
report in this tree backs that figure, and nothing here measures the multiplier
in isolation. What *is* measured is the assembled unit with the multiplier
built: **2,586 LUT and 4 DSP48 at 363.5 MHz against a 3.333 ns request**, OOC
synthesis, not routed — [performance](performance.md).

### Where the result joins

The multiply result joins the pipeline through `ex_alu`, which feeds both the
MEM-stage value and the distance-1 forwarding path, and that path ends at
`x_op1_reg`. Adding a case to that mux adds a LUT level on the forwarding path.
On the shipped RTL at a 3.333 ns request the binding path runs from the memory
stage's address register into the ID stage's register control, and the
forwarding network is not on it ([performance](performance.md#the-binding-path-and-the-forwarding-mux)).
**The design rule: do not add a level to a mux that feeds the register a
critical path terminates at.**

## Hazards

The forwarding network is the whole of the complexity budget: three sources by
position, one stall rule, nothing else.

| Producer is in | Distance | What happens |
|---|---|---|
| EX | 1 | forward the ALU output, or stall — see `FWD_X` |
| MEM | 2 | forward the EX result register; **stall if it is a load** |
| WB | 3 | forward the writeback value, loads included |
| — | 4 | the register file's own write-through bypass |

A load's data does not exist until WB, which is why distances 1 and 2 stall on
a load and distance 3 does not. That is the load-use penalty: two cycles back
to back, one at a spacing of one.

**`FWD_X = 1` is the default because the mux was never the expensive part.**
Removing the distance-1 bypass looks like it should trade a cycle for
frequency. Measured — the same OOC synthesis flow, run with `fwd_x 0` — it
saves about **2 LUT and loses about 5 MHz**, because without the bypass the
stall term widens from "hazard at distance 1 **and** the producer is a load" to
"hazard at distance 1", and that term fans out across the whole front end. The
`0` form stays built and verified so the claim survives re-measurement.

**The distance-4 write-through is not optional.** A write lands at the same
edge that captures a read address four instructions behind it, and a
synchronous array returns the pre-write value for that read. Without the bypass
the core is wrong for exactly that one spacing — the kind of bug that survives
a casual test suite, which is why the co-simulation covers every
producer-to-consumer distance by construction.

## Branch prediction

A small branch-target buffer plus a 2-bit saturating table, read with the
**same address as the instruction window**, so the prediction is available in
the cycle the instruction's bits are and a correctly predicted taken branch
costs no bubble.

Its job is to remove the taken-branch penalty of a loop, not to be accurate.
Nothing in it is speculative state needing repair: EX resolves every branch
against the architectural answer, so a wrong prediction costs the redirect
penalty and never correctness — which is why the tag can be short and the table
can alias.

Tag, target, valid and counter all ride in one LUTRAM entry, so the entry count
buys memory depth rather than logic. Two registrations bound its timing:

- **The update lands one cycle after the resolve.** EX's comparator driving a
  read-modify-write is a long path for something non-architectural, and a cycle
  of staleness can only cost a prediction.
- **A redirect is registered.** Steering fetch in the resolve cycle would put
  the ALU output into the next-PC mux; one more cycle costs a third bubble on a
  mispredict and keeps the ALU output going nowhere but a flop.

Resolve, predictor update and halt are all qualified by "EX is not held".
Without that, a stalled memory stage would let the same branch resolve every
cycle and walk its saturating counter to a value it never earned.

`BTB_ENTRIES = 0` removes the structure entirely (a generate, not a zero-sized
array); the shipped configuration carries the 32-entry table.

## The two L1s

The instruction/data split is recast as **external L1 and internal L1**, split
by *who writes*, not by what is stored. This is the single idea that removes
coherence from the design.

| | external L1 | internal L1 |
|---|---|---|
| what it is | real SRAM windows mapped into the global address space | a tagged cache over global DRAM |
| who writes it | the fabric, and this core | this core only, plus fills |
| tags | none — the address-region decode **is** the lookup | yes, direct mapped |
| holds | program text and data | copies of DRAM lines |
| coherence case | none: it is the **home** of its addresses, never a copy | none: **never externally written** |

There is no external-write-versus-dirty-line case anywhere in this PE because
there is no way to construct one. The instruction window is not reachable from
the data side, which keeps the fetch port exclusive — fetch never contends with
a load.

### The write both ports can make at once

A window written by the fabric and read by its owner has one hard case: the
push lands in the very word a poll loop is reading — **and on a doorbell that
is the common case**, because the peer pushes exactly the word the consumer
polls. A true-dual-port array returns undefined data for that collision in
silicon, and per-port reasoning ("neither port reads what it writes") is true
per port and false across them.

The scratchpad therefore carries a **byte-wise cross-port bypass**: when the
fabric port writes the word the core is reading, the core receives the written
bytes — correct rather than merely defined, and byte-wise because a peer's `sb`
is as legal as a local one. It is most of the scratchpad's **46 LUT** — a plain
array of that shape is about seven — and it sits on the critical path, which is
the price of the doorbell being right.

The internal L1's fill collides too — a fill writes the word a stalled access
is presenting — and answers differently: the colliding read is discarded and
re-issued after the fill. Which answer is right belongs to the caller, not the
array. The RAM wrapper makes the choice explicit: an array that does not
declare how it handles the collision asserts the moment one happens.

### The cache, and why it exists even if it never hits

Two jobs, and the second does not depend on hit rate: **protocol adaptation**.
A 32-byte line is exactly one fabric/agent payload, so a fill is one request
and one response and a writeback is one descriptor and one beat. Ordinary
`lb`/`lh`/`lw` and `sb`/`sh`/`sw` are presented to software while the upstream
protocol stays line-oriented.

It is direct mapped and blocking, with **one outstanding miss**. No miss-status
registers, no hit-under-miss, no load/store queue: latency tolerance in this
machine comes from having many independent units, and whether per-core miss
concurrency beats instantiating more units is a later measurement, not an
assumption.

Per-line `valid` and `dirty` ride in the tag LUTRAM beside the tag rather than
in flop arrays — indexed flop arrays cost LUT twice, once as flops and once as
the read mux in front of the tag compare, and moving them is what makes the
line count nearly free. The consequence is that invalidate-all is a
one-line-per-cycle sweep rather than a broadcast, which is why `CTL_INVAL`
blocks ([architecture](architecture.md#ordering)).

### Why the arrays are 32 bits, and the rotate

A flit carries 256 bits of payload, so a 256-bit array port looks natural. It
is not: a `RAMB36E2` in true-dual-port mode is 36 bits per port, so a 256-bit
true-dual-port array is eight block RAMs whose 32-bit face is the only one the
CPU uses — and every read needs an 8:1 32-bit mux on the load path. Walking a
line as eight 32-bit words costs 8 cycles per fill against a DRAM latency of
hundreds, and zero LUT.

**A block-RAM port is 36 bits wide at that aspect, and a wider array silently
becomes something else without the tool warning about it.** That is a durable
property of the primitive, not of this design.

The 256-bit line buffer between array and fabric is a **rotate**, not an
indexed register: a fill walks words out in order and an eviction walks them
in, so always taking the bottom word removes the mux and the demux for wiring.
The limit of the trick is worth stating: a rotate needs a 2:1 mux on every bit,
so it pays only where the register was already written word-at-a-time and that
mux already existed — applied to a register loaded whole, the same construction
*adds* logic.

**Primitives are named, never inferred.** Left to inference, both the resource
and the read latency can move between tool versions, and read latency here is
pipeline structure.

## The NoC requestor

Everything about the framework memory protocol that RV32 software must never
see: transaction tags, descriptor legality, response matching, write ordering,
backpressure. `lw` and `sw` are the whole interface software gets. It also
constructs the outbound `CU_INST` for
[dispatch](architecture.md#the-pe-as-a-dispatcher) and holds the inbound
completion queue.

Four properties are contracts rather than conveniences:

- **A fill is an entry read, not a plain read.** Asking the memory agent's read
  engine for one 32-byte entry with the stream flag set keeps the request off
  the port's shared read/write state machine; a plain read would occupy that
  machine and exclude a write for its whole duration.
- **One write outstanding, acknowledged before the next** (`WR_MAX = 1`). The
  protocol already forbids two *open* writes from one source — agent write
  slots are matched by source coordinate alone — and the PE bounds
  un-acknowledged writes too, which is what gives
  [architecture](architecture.md#ordering) its rules 2 and 3. It is free in
  steady state because a blocking cache never asks for a second writeback while
  one is open; the one place it costs is a flush-all against slow
  acknowledgements ([performance](performance.md#memory-timing)).
- **Write acknowledgements are counted, then dropped.** Nothing in the
  framework consumes them and a unit that holds one wedges the mesh — but
  flush-all needs to know when a writeback is actually in memory, and the
  acknowledgement is the only thing that says so.
- **The push handshake is a register, not a wire.** The completion FIFO's state
  reaching the memory-stage stall combinationally would tie the front end's
  timing to the fabric's; a one-deep holding register cuts that path for one
  cycle of push latency.

The inbound completion queue is **8 entries deep with a sticky overflow bit**.
A queue that silently dropped a completion would be indistinguishable from a
unit that never finished; a count plus an overflow flag makes the loss
detectable, where a memory location would not.

## What the microarchitecture deliberately does not do

| Not built | Consequence |
|---|---|
| a scoreboard or any out-of-order retire | every multi-cycle unit must stall EX, which is why the multiplier costs three cycles and the divider was refused |
| speculative state needing repair | a mispredict costs the redirect penalty and nothing else; the predictor can alias freely |
| hit-under-miss or an MSHR table | the internal L1 is blocking; concurrency comes from more units, not from a deeper one |
| a second clock domain | the whole unit is on the fabric clock, so there is no clock-domain crossing anywhere in it |
| an inferred memory primitive | every array names its primitive, because read latency here is pipeline structure |
