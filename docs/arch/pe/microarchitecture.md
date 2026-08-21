---
title: PE microarchitecture
summary: How the core is built and why — the six register boundaries, the hazard rules, the predictor, the two L1s, and the NoC requestor.
tags:
  - architecture
  - pe
  - rv32
---

# PE microarchitecture

How the [architecture](architecture.md) is implemented, and why each piece
has the shape it has. The recurring theme: the core spends FF and BRAM
freely, prefers a pipeline stage over a bypass, and registers anything whose
combinational form would fan out — because its objectives are LUT and
frequency, never latency.

## The pipeline

Five architectural stages, **six register boundaries**. The extra boundary
exists because the instruction window and the register file are synchronous
arrays: each costs a cycle between presenting an address and receiving data,
and counting those honestly is what lets the fetch loop close.

```
    IF1   next-PC select                    -> the instruction window's address register
    IF2   instruction out, decode           -> the register file's address register
    ID    operands out, forwarding          -> EX
    EX    ALU, branch resolve, address      -> MEM
    MEM   array address and write enables   -> WB
    WB    array data out, commit
```

The address path in fetch is `PC -> mux -> RAM address register` and nothing
else. Two consequences are structure, not detail:

- **Decode is combinational on the fetched word**, inside IF2. The
  register-file address leaves at the same edge as the control bits, buying
  the operand-fetch cycle instead of costing a seventh boundary.
- **The effective address leaves EX combinationally** — it is the ALU's own
  adder output — because the data arrays register their address input.

A branch or jump target is computed in ID rather than EX: PC and immediate
are both registered by then, the adder is off every critical path in that
stage, and carrying the target instead of the immediate keeps the EX register
the same width.

The EX stage carries one adder and one shifter, not three of each. Subtract
and compare share the adder; `SLL`, `SRL` and `SRA` share a single 33-bit
arithmetic right shifter between two bit reversals — a left shift is a right
shift on the reversed word, and reversals are wiring.

## Hazards

The forwarding network is the whole of the complexity budget: three sources
by position, one stall rule, nothing else.

| Producer is in | Distance | What happens |
|---|---|---|
| EX | 1 | forward the ALU output, or stall — see `FWD_X` |
| MEM | 2 | forward the EX result register; **stall if it is a load** |
| WB | 3 | forward the writeback value, loads included |
| — | 4 | the register file's own write-through bypass |

A load's data does not exist until WB, which is why distances 1 and 2 stall
on a load and distance 3 does not. That is the load-use penalty: two cycles
back to back, one at a spacing of one.

**`FWD_X = 1` is the default because the mux was never the expensive part.**
Removing the distance-1 bypass looks like it should trade a cycle for
frequency; measured, it saves about 2 LUT and *loses* 5 MHz, because without
the bypass the stall term widens from `hz1 && x_load` to `hz1` and that term
fans out across the whole front end. The 0 form stays built and verified so
the claim survives re-measurement.

**The distance-4 write-through is not optional.** A write lands at the same
edge that captures a read address four instructions behind it, and a
synchronous array returns the pre-write value for that read. Without the
bypass the core is wrong for exactly that one spacing — the kind of bug that
survives a casual test suite, which is why the co-simulation covers every
producer-to-consumer distance by construction.

## Branch prediction

A small BTB plus a 2-bit saturating table, read with the **same address as
the instruction window**, so the prediction is available in the cycle the
instruction's bits are and a correctly predicted taken branch costs no
bubble.

Its job is to remove the taken-branch penalty of a loop, not to be accurate.
Nothing in it is speculative state needing repair: EX resolves every branch
against the architectural answer, so a wrong prediction costs the redirect
penalty and never correctness — which is why the tag can be short and the
table can alias.

Tag, target, valid and counter all ride in one LUTRAM entry, so the entry
count buys memory depth rather than logic. Two registrations bound its
timing:

- **The update lands one cycle after the resolve.** EX's comparator driving
  a read-modify-write is a long path for something non-architectural, and a
  cycle of staleness can only cost a prediction.
- **A redirect is registered.** Steering fetch in the resolve cycle would
  put the ALU output into the next-PC mux; one more cycle costs a third
  bubble on a mispredict and keeps the ALU output going nowhere but a flop.

`BTB_ENTRIES = 0` removes the structure entirely (a generate, not a
zero-sized array); the shipped configuration carries the 32-entry table.

## The two L1s

The I/D split is recast as **external L1 and internal L1**, split by *who
writes*, not by what is stored. This is the single idea that removes
coherence from the design.

| | external L1 | internal L1 |
|---|---|---|
| what it is | real SRAM windows mapped into the global address space | a tagged cache over global DRAM |
| who writes it | the NoC, and this core | this core only, plus fills |
| tags | none — the address-region decode **is** the lookup | yes, direct mapped |
| holds | program text (`rv_imem`) and data (`rv_spad`) | copies of DRAM lines |
| coherence case | none: it is the **home** of its addresses, never a copy | none: **never externally written** |

There is no external-write-versus-dirty-line case anywhere in this PE
because there is no way to construct one. The instruction window is not
reachable from the data side, which keeps the fetch port exclusive — fetch
never contends with a load.

### The write both ports can make at once

A window written by the NoC and read by its owner has one hard case: the
push lands in the very word a poll loop is reading — **and on a doorbell
that is the common case**, because the peer pushes exactly the word the
consumer polls. A true-dual-port array returns undefined data for that
collision in silicon, and per-port reasoning ("neither port reads what it
writes") is true per port and false across them.

The scratchpad therefore carries a **byte-wise cross-port bypass**: when the
NoC port writes the word the core is reading, the core receives the written
bytes — correct rather than merely defined, and byte-wise because a peer's
`sb` is as legal as a local one. It costs 38 LUT and sits on the critical
path, which is the price of the doorbell being right.

The internal L1's fill collides too — a fill writes the word a stalled
access is presenting — and answers differently: the colliding read is
discarded and re-issued after the fill. Which answer is right belongs to the
caller, not the array. The RAM wrapper (`rv_ram_be`) makes the choice
explicit: an array that does not declare how it handles the collision
asserts the moment one happens.

### The cache, and why it exists even if it never hits

Two jobs, and the second does not depend on hit rate: **protocol
adaptation**. A 32-byte line is exactly one NoC/MAG payload, so a fill is
one request and one response and a writeback is one descriptor and one beat.
Ordinary `lb`/`lh`/`lw` and `sb`/`sh`/`sw` are presented to software while
the upstream protocol stays line-oriented.

It is direct mapped and blocking, with **one outstanding miss**. No MSHRs,
no hit-under-miss, no load/store queue: latency tolerance in this machine
comes from having many independent PEs, and whether per-core miss
concurrency beats instantiating more cores is a later measurement, not an
assumption.

Per-line `valid` and `dirty` ride in the tag LUTRAM beside the tag rather
than in flop arrays — indexed flop arrays cost LUT twice, once as flops and
once as the read mux in front of the tag compare, and moving them is what
makes the line count nearly free. The consequence is that invalidate-all is
a one-line-per-cycle sweep rather than a broadcast, which is why `CTL_INVAL`
blocks ([architecture](architecture.md#ordering)).

### Why the arrays are 32 bits, and the rotate

A flit carries 256 bits, so a 256-bit array port looks natural. It is not: a
`RAMB36E2` in true-dual-port mode is 36 bits per port, so a 256-bit TDP
array is eight BRAMs whose 32-bit face is the only one the CPU uses — and
every read needs an 8:1 32-bit mux on the load path. Walking a line as eight
32-bit words costs 8 cycles per fill against a DRAM latency of hundreds, and
zero LUT.

The 256-bit line buffer between array and fabric is a **rotate**, not an
indexed register: a fill walks words out in order and an eviction walks them
in, so always taking the bottom word removes the mux and the demux for
wiring. The limit of the trick is worth stating: a rotate needs a 2:1 mux on
every bit, so it pays only where the register was already written
word-at-a-time and that mux already existed — applied to a register loaded
whole, the same construction *adds* logic.

**Primitives are named, never inferred.** Left to inference, both the
resource and the read latency can move between tool versions, and read
latency here is pipeline structure.

## The NoC requestor

Everything about the framework memory protocol that RV32 software must never
see: transaction tags, descriptor legality, response matching, write
ordering, backpressure. `lw` and `sw` are the whole interface software gets.

Three properties are contracts rather than conveniences:

- **A fill is an entry read, not a plain read.** `entry_words = 1` with
  `STREAM` set asks the memory agent's read engine for one 32-byte entry; a
  plain read would occupy the agent's shared read/write FSM and exclude a
  write for its whole duration.
- **One write outstanding, acknowledged before the next** (`WR_MAX = 1`).
  The protocol already forbids two *open* writes from one source — agent
  write slots are matched by source coordinate alone — and the PE bounds
  un-acknowledged writes too, which is what gives
  [architecture](architecture.md#ordering) its rules 2 and 3. It is free in
  steady state because a blocking cache never asks for a second writeback
  while one is open; the one place it costs is a flush-all against slow
  acknowledgements ([performance](performance.md#memory-timing)).
- **The push handshake is a register, not a wire.** The completion FIFO's
  state reaching the MEM stall combinationally would tie the front end's
  timing to the fabric's; a one-deep holding register cuts that path for one
  cycle of push latency.
