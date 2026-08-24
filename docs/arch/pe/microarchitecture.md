---
title: PE microarchitecture
summary: How the core is built and why — the six register boundaries, the arithmetic EX does not have and what each option would cost, the hazard rules, the predictor, the two L1s, and the NoC requestor.
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

The EX stage is one adder, one subtractor and one shifter, not three of each.
`SLL`, `SRL` and `SRA` share a single 33-bit arithmetic right shifter between two
bit reversals — a left shift is a right shift on the reversed word, and reversals
are wiring. The subtractor is shared three ways: `SUB`, the `SLT`/`SLTU` results,
and every branch comparison come off it.

**The adder is not shared with it, and that is structural rather than an
oversight.** `sum = op1 + op2` is also `ex_addr`, which leaves the stage
combinationally to the data arrays' address pins; anything muxed into its inputs
lands in front of that path. It is the one adder in this core that cannot be
borrowed — see [below](#the-arithmetic-the-ex-stage-does-not-have).

## The arithmetic the EX stage does not have

**There is no multiplier, no divider and no floating point in this core, and
none is planned into it by default.** `mul` faults: `rv_id.v` accepts `funct7`
of `0000000` and `0100000` on the register-register group and nothing else, so
RV32M's `0000001` raises an illegal-instruction halt at the offending PC. There
is no float register file to name, no `fcsr`, and no CSR file to hold one.

Nor is the arithmetic somewhere else on the same core. The SIMD PE's multiply and
its measured [float tier](../../projects/kohakumpe/simd/float.md) live in the *vector* register file behind
custom-0 and custom-1.

**A scalar register-register multiply exists nowhere in this machine — but a
multiply does.** The SIMT PE builds `RV32M` on its *per-thread* register file:
`mul`, `mulh`, `mulhsu` and `mulhu`, one product per lane, on the standard `OP`
encoding. Its *uniform* ALU on custom-2 is still the same ten register-register
operations RV32I has and has no multiply, and `div`/`rem` fault on every class.
So a C expression that multiplies calls libgcc on this core and on the SIMD PE's
scalar half — and compiles to one instruction in a shader.

**That the multiplier landed there rather than here is the finding, not an
accident** — [why the GPU was the cheap place](#why-the-gpu-was-the-cheap-place-for-a-multiplier-and-this-core-is-not).

What that costs is measured rather than assumed. The base core's 128-element
int8 dot product is **8,221 cycles**, and the same kernel with its multiply
costed at one instruction is **1,297** — so 128 software multiplies account for
6,924 cycles, **about 54 cycles each**, and that is the *easy* case: int8
operands unroll to eight shift-add steps, where a general 32×32 `__mulsi3`
unrolls to far more ([dsp/performance](../../projects/kohakumpe/simd/performance.md#what-the-instructions-buy)).

### What a minimal multiplier would cost

Everything in this subsection is **ESTIMATE** — reasoned from measured
neighbours on `xcvu13p-fhgb2104-2L-e`, not synthesised. Nothing here has been
built.

| shape | DSP | LUT | issue | latency |
|---|---:|---:|---|---|
| `mul` alone, pipelined | **3** | ~20 + registers | 1 | 3–4 |
| the whole family — `mul`, `mulh`, `mulhsu`, `mulhu` | **4** | **~200** | 1 | 3–4 |
| one DSP, multi-cycle over four passes | **1** | ~150 | 1 per 4–5 cycles | 4–5 |

A DSP48E2 is 27×18 signed, so a 32×32 product is four 17-bit partial products
and a small carry network; `mul` needs only three of them, because the
high-times-high term contributes nothing below bit 34. The LUT is the merge
network and the operand registers, not the multiply.

**Take the pipelined form, not the multi-cycle one.** The multi-cycle form saves
three DSP columns, and on this device a DSP column is worth about **230 LUT**
([dsp/performance](../../projects/kohakumpe/simd/performance.md#what-the-rows-say)) — so it trades ~690
LUT of nominal value for real sequencing logic on a resource the design is
explicitly *not* short of. LUT is the binding resource here and DSP is not.

**The real cost is not the multiplier — it is where its result joins.** Two
things, in order of risk:

1. **The result mux is on the distance-1 forwarding path.** `ex_alu` feeds both
   `m_val` and `fwd_x_val`, and `fwd_x_val` is an input to `x_op1_reg` — the
   register the core's critical path already ends at. Adding a case to that mux
   is one more LUT level in exactly the wrong place. The SIMD PE measures this
   shape one level up: its own binding path ends at a result mux feeding a write
   port, and removing one block from that mux buys **33.9 MHz**.
2. **The stall term widens, and stall terms fan out.** A 3-cycle multiply in an
   in-order core with three positional forwarding sources must **stall EX**, the
   same shape `khs_unit` uses to stretch `vmul`. That is a new term in
   `stall_d`, and the `FWD_X` measurement is the warning: widening a stall term
   that was already there cost **5 MHz** and saved 2 LUT.

   The alternative is a scoreboard, letting the multiply retire out of order.
   **Do not.** The hazard unit is the whole of this core's complexity budget —
   three sources by position, one stall rule, nothing else — and a scoreboard
   ends that invariant for one instruction.

ESTIMATE: **−5 to −15 MHz** on a 410.8 MHz core, and the mux is what to measure
first. If it lands worse than that, retire the product through MEM on its own
writeback port instead of through `ex_alu`, which leaves the distance-1 forward
untouched at the price of one more stall cycle.

Even at four stall cycles, `mul` replaces a ~54-cycle software multiply.

### Why the GPU was the cheap place for a multiplier, and this core is not

The costing above is still the costing for **this** core. What has happened
since is that the machine got its multiplier somewhere else, and the reason is
exactly the two risks named above — the SIMT PE does not have either of them.

| the cost, on this core | the same thing on the SIMT PE |
|---|---|
| **the result mux lengthens the distance-1 forwarding path.** `ex_alu` feeds `fwd_x_val`, which is an input to `x_op1_reg` — the register the critical path already ends at | **that path does not exist.** Barrel scheduling deleted it: with as many resident waves as the pipeline is deep, no two in-flight instructions share a wave, so the design carries no forwarding network and no interlock — there is nothing there to lengthen |
| **a multi-cycle result needs a new stall term**, and stall terms fan out — the `FWD_X` measurement priced widening one at 5 MHz | **the flag was already there.** A wave with a float in flight is skipped by the scheduler via one pending bit per wave. A multiply sets and clears the *same* bit and retires through the *same* slot |

The multiplier is built at the float tier's own latency **on purpose**: equal
latency makes a collision between a float result and a multiply result
*structurally* impossible rather than arbitrated, because one instruction issues
per cycle and two results can only want the write port on the same cycle if they
were issued on the same cycle. It is therefore an increment on machinery that
already existed for float, not a second mechanism — and it ships with that tier
rather than separately from it.

That is also the general rule this core's costing was pointing at, now with a
built example behind it: **a multi-cycle unit is cheap in a machine that already
has a way to park an instruction, and expensive in one whose whole complexity
budget is three positional forwards and one stall rule.**

### Why `div` and `rem` are a different answer

An iterative divider is ~35 cycles and a **33-bit subtract per cycle**, and it
cannot borrow the one in EX: `sum` is `ex_addr`. Muxing a divider's operands
into the EX adder puts a mux in front of the effective address, which is the
one path the whole six-boundary arrangement exists to keep short — the address
leaves EX combinationally precisely because the data arrays register their
address input. **So a divider carries its own 33-bit subtractor, its own
remainder shift register, its own quotient register, the sign fixups `div`'s
truncate-toward-zero needs, and the two mandated special cases** (÷0, and
−2³¹ ÷ −1). ESTIMATE **200–300 LUT** — most of the EX stage's own 418 again,
for an instruction a controller issues approximately never.

And it is 35 cycles against libgcc's ~60–80. That is a 2× on a rare instruction,
where `mul` is an 8–13× on a common one. **Iterative division is a trap at this
size** — not because it is hard, but because its cost is a fixed structure and
its benefit is a small multiple on a rare event.

If `mul` is built, divide-by-a-constant strength-reduces to `mulhu` and covers
the case a controller actually meets: turning a linear index into mesh
coordinates.

### Why minimal scalar float is the wrong purchase

The tempting shape is to reuse what is already measured: `khs_f16_lane` is
**609 LUT and 2 DSP**, fifteen cycles deep, II = 1, and its FMA is verified
bit-for-bit against a golden model. One of those, a 32-entry float register file
(the same LUTRAM shape as `rv_regfile`'s 129 LUT), and the two FP32 converters —
one of which is pure wiring, because E8 *is* FP32's exponent field — is roughly
**900–1,100 LUT and 2 DSP, ESTIMATE**.

Two things make it the wrong purchase anyway, and neither is about the LUT:

- **Fifteen cycles into a three-source in-order forwarding network.** `fadd`
  would stall EX for fifteen cycles or need the scoreboard the paragraph above
  refuses. The SIMD tier does not have this problem because an *accumulating*
  instruction needs only the accumulator's own busy shadow, where an instruction
  that writes a register needs to be tracked.
- **It would not be `F`, so no compiler would emit it.** E8M15 is 24 bits with
  no subnormals, one rounding mode, and a documented one-ulp deviation on
  subtractive alignment. RISC-V's `F` is IEEE-754 binary32 with subnormals, five
  rounding modes and `fcsr` — and this core has no CSR file at all. A
  non-conforming float behind a custom major also has nowhere to live: all four
  custom majors are spoken for ([opcode-map](opcode-map.md)). The core's first
  design objective is that ordinary compilers work unmodified; a float
  extension a compiler cannot target defeats it. **`RV32M` is the contrast that
  makes the point**: it needed no custom major at all, because RISC-V had
  already standardised the encoding, so the SIMT PE's multiply is something a
  compiler emits with `-march=rv32im` and nothing else.

### The recommendation

**Build `mul`/`mulh` on four DSPs. Do not build `div`/`rem`. Do not build scalar
float.**

`mul` is the only one of the three that is standard encoding space, that GCC
emits with nothing more than `-march=rv32im`, and that removes a measured
54-cycle tax from every index computation — and from every benchmark baseline
this project publishes, which today has to carry a twin whose multiply is costed
by hand to stay honest. Four DSP columns on a device where DSP is not the
binding resource is the cheapest thing this core could spend.

The other two belong where the arithmetic already is. Both wide classes carry a
float tier that is built and measured — the format, the lane counts and the
accuracy are [KohakuMPE's](../../projects/kohakumpe/README.md) to state. That is
where a kernel needing float should be, not in this core's EX stage.

**What would change this answer:**

| if | then |
|---|---|
| a controller kernel profile shows >5 % of cycles in `__divsi3` — not `__mulsi3` | `div`/`rem` earns its ~250 LUT; measure before building |
| the EX result mux measures worse than −15 MHz with the multiply input | keep `mul` but retire it through MEM on its own writeback port, not through `ex_alu` |
| **this** core gains a way to park an instruction — a per-thread pending flag, a scoreboard, anything | every multi-cycle unit reprices at once, `mul` first. This is what happened on the SIMT PE and it is why the multiply is there and not here — [above](#why-the-gpu-was-the-cheap-place-for-a-multiplier-and-this-core-is-not) |
| something needs a scalar **transcendental** (rendering will) | the answer is still not scalar float. Untie `op` in the float lane: ESTIMATE +640 LUT, +1 DSP and +1 BRAM **per lane** for `exp2`, `log2`, `inv` and `rsqrt` at II = 1 — and the lane count is a build parameter, so that cost scales with it — [dsp/float](../../projects/kohakumpe/simd/float.md#the-transcendentals-are-in-the-source-and-not-in-the-machine) |

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
