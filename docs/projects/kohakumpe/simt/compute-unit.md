---
title: SIMT PE — the compute-unit design
summary: How the SIMT PE runs standard SIMT kernels while staying an ordinary KohakuTPU compute unit — the LAUNCH word, workgroups, state that survives launches, scoreboard issue, the coalescing non-blocking L1, the stream engine, LDS atomics and barriers, the instructions they add, and every knob.
tags:
  - architecture
  - pe
  - gpu
  - simt
  - spec
---

# SIMT PE — the compute-unit design

> **Kind: Yours throughout.** Everything here sits behind the framework's
> compute-unit port ([spec/compute-unit-port](../../../spec/compute-unit-port.md))
> and the memory protocol ([spec/memory-protocol](../../../spec/memory-protocol.md)),
> which it obeys and does not change.
>
> **Design, being built.** [status](status.md) says which parts exist and which
> command exercises each. Until a row there says *built*, this page is the
> target, not the machine.

## 1. The shape in one table

| Part | Today | This design |
|---|---|---|
| dispatch | a KICK: `op` = wave count, `pc`, `arg` | a **LAUNCH**: one `CU_INST` runs this core's slice of a grid as workgroups (§2). The KICK stays legal and means a one-workgroup launch |
| program | an image pushed into the instruction window before each kick | an **instruction cache** over memory, predecoded on the line fill (§3.1); the window stays as `ICACHE_LINES = 0` |
| issue | one instruction in flight across all waves; any stall holds every wave | **scoreboard and replay** (§4): a wave whose operand is pending is put to sleep and its fetch slot goes to another wave |
| per-thread memory | serial three-phase walk through a one-miss L1 | **coalescer** and an L1 with **fill buffers** (§5): one request per touched 32-byte line, several lines in flight, the core keeps issuing |
| streams | none | **stream engine** (§6): memory ↔ LDS descriptors, `CU_DATA` into LDS with `COUNT`, credits, `wait` |
| LDS | banked, one access per lane per pass | the same, plus row writes for streams, **atomics** and an executed **barrier** (§7) |
| lanes, float units, SFU, divergence, shuffle | — | unchanged |

Every new structure is a parameter. At its zero value the core is today's
machine, and every shader in the suite runs at both settings (§10).

## 2. The LAUNCH

### 2.1 The word

`CU_INST`'s 256-bit payload is unit-defined. The SIMT PE reads it as:

| Bits | Field | Meaning |
|---|---|---|
| `[255:248]` | `op` | `0x01`–`0x7F`: a **KICK** (today's form): one workgroup of `op` waves at `pc`. `0x80` **LAUNCH**, `0x81` **LAUNCH.post**, `0x82` **SYNC**, `0x83` **CACHE**. Any other value faults. |
| `[247:216]` | `pc` | First instruction. A window address at `ICACHE_LINES = 0`; a DRAM address (`pc[31] = 1`) otherwise. |
| `[215:184]` | `arg` | The kernel argument, read by `rdctl s, 0`; by convention a pointer to the argument block. |
| `[183:168]` | `wg_base` | The first workgroup id of this core's slice. |
| `[167:152]` | `wg_count` | Workgroups in the slice; 0 means 1. |
| `[151:147]` | `wg_waves` | Waves per workgroup, 1–`WAVES`; 0 means 1. |
| `[146:136]` | `wg_lds` | LDS per workgroup, in 32-byte granules; 0 means none. |
| `[135:128]` | `policy` | §2.4. |
| `[127:0]` | — | Reserved, MUST be 0. |

A KICK is exactly a LAUNCH with `wg_base 0`, `wg_count 1`, `wg_waves = op`,
`wg_lds 0` and `policy = d_wb`, which is what every shader before this page
assumed.

### 2.2 Workgroups

A workgroup is `wg_waves` consecutive wave contexts, all starting at `pc`. The
core holds as many workgroups at once as fit:

```
slots = min( WAVES / wg_waves ,  LDS granules / wg_lds  (if wg_lds > 0) )
```

Slot *s* owns waves `s × wg_waves … s × wg_waves + wg_waves − 1` and LDS granules
`s × wg_lds … `. When every wave of a slot has ended, the next workgroup of the
slice starts in it. The launch is finished when the last workgroup's last wave
has ended.

New `rdctl` slots (0–5 keep their meaning):

| `cidx` | Reads |
|---:|---|
| 6 | workgroup id (`wg_base` + its index in the slice) |
| 7 | wave index inside the workgroup |
| 8 | `wg_count` |
| 9 | `wg_waves` |
| 10 | this workgroup's LDS base, as an `R_LDS` byte address (`0x4000_0000 + slot × wg_lds × 32`) |
| 11 | launch id: launches this core has accepted since reset, modulo 2³² |
| 16–19 | stream counter 0–3 (§6.3) |

### 2.3 Completion

| `op` | `exec_done` |
|---|---|
| KICK, LAUNCH | when the last wave has ended, the release has finished (`d_wb`) and every write is acknowledged — today's completion, unchanged |
| LAUNCH.post | when the launch's first workgroups have started. The next `CU_INST` may then be accepted while this launch runs. At most `POST_DEPTH` launches are resident; a further post waits |
| SYNC | when every posted launch has finished as a LAUNCH would |
| CACHE | when the `policy` actions are done |

A fault in any wave of any resident launch faults the instruction that is in
flight when it is seen, and every resident launch stops. Each case retires
exactly once and in bounded time, which is what [the port](../../../spec/compute-unit-port.md#4-instruction-issue-and-retirement)
requires. A *persistent* launch is not a mode: it is a LAUNCH whose waves loop on
`wait` until the stream that feeds them ends, and it is bounded because the
package that sends the stream is verified.

### 2.4 Policy: what survives a launch

Nothing a launch builds up is discarded unless a policy bit says so.

| Bit | Name | Acts | State it drops |
|---:|---|---|---|
| 0 | `i_inv` | before the first wave | instruction-cache and scalar-cache lines |
| 1 | `d_inv` | before the first wave (acquire) | L1 lines; dirty lines are written back first |
| 2 | `d_wb` | after the last wave, before `exec_done` (release) | nothing: dirty L1 lines are written back and stay valid |
| 3 | `lds_clear` | before the first wave | LDS contents, zeroed |
| 4 | `cnt_reset` | before the first wave | stream counters, zeroed |
| 5–7 | — | — | reserved, MUST be 0 |

**The coherence rule this gives**, which is the GPU's kernel-boundary rule: a
launch sees memory written by another unit before the launch's `CU_INST` was
sent if it sets `d_inv` or never cached that line; what a launch writes is in
memory at its completion if it sets `d_wb`. Inside a launch the L1 is this core's
alone. The stream engine bypasses the L1, so streamed data needs neither bit.

**Write granularity is the 32-byte line.** The memory port writes whole lines
and ignores byte strobes, so an L1 write-back carries the whole line. Two units
writing different bytes of one line inside one launch lose one side's bytes —
the same contract the host already has (`host_write_granule` 32). The compiler
keeps cores' output regions line-disjoint.

## 3. Instructions in

### 3.1 The instruction cache

`ICACHE_LINES > 0` turns the instruction window into a direct-mapped cache of
32-byte lines (8 instructions) over DRAM:

- **The window's two memories are the cache's data**: the 32-bit instruction
  array and the 60-bit predecoded control array beside it, exactly as today. A
  tag array (`{valid, tag}` per line, distributed RAM) is the only addition.
- **A miss blocks one wave.** The fetching wave sleeps (§4.2) with an
  instruction-fill wake; the other waves keep issuing. One fill is outstanding.
- **A fill is one entry read** (`STREAM`, `entry_words 1`), and its eight words
  are predecoded one per cycle on the way into the arrays — the decode stays on
  the write path, so fetch reads control out of memory as it does today.
- `CU_DATA` into the instruction window (`buf_id` 1 and 5) is refused at
  `ICACHE_LINES > 0`.

### 3.2 Arguments

`rdctl s, 0` returns `arg`; **`sld s, s1`** reads the word at DRAM address `s1`
into a scalar register through the **scalar cache** (`KCACHE_LINES` lines of
32 bytes, distributed RAM, read combinationally in EX). A hit is a one-cycle
scalar instruction; a miss puts the wave to sleep with a scalar-fill wake (§4.2)
while one line is filled. At `KCACHE_LINES = 0` `sld` faults and an argument
block is read as today: a lane-linear load and `vreadfirst`. `k_inv` drops its
lines.

## 4. Issue

### 4.1 What stays

The three-stage fetch, round-robin over ready waves, predecoded control, the
registered vector read and its `warm_q` warm-up, the scalar half and its
interlock, the in-pipe hazard against MEM and WB of the same wave. A stall that
lasts a cycle or two is still a hold.

`warm_q` counts only cycles on which the vector read port actually read: a pass
walk (`fpass_hold`) or a held machine (`base_hold`) freezes the port with another
instruction's operands in it, and the warm-up waits that out.

### 4.2 The scoreboard

A bit per vector register per wave, set when an instruction issues whose result
arrives later than the pipe (float, multiply, load, LDS atomic), cleared when
that result is written.

At EX, an instruction whose source registers — or, for an instruction that
writes one, its destination — have their bit set does not hold the machine. It
is **replayed**: the same two-fetch kill a branch uses, with its wave's pointer
rewound to its own `pc`, and the wave put to **sleep**. The fetch slot goes to
another wave the next cycle.

A wave sleeps on exactly one reason and wakes when it clears:

| Sleeps on | Wakes when |
|---|---|
| scoreboard | any pending result of that wave is written |
| instruction fill | its line has been written |
| scalar fill | the scalar cache's fill has completed |
| LSU full | an LSU entry frees |
| `wait` | its counter reaches the target (§6.3) |
| `bar` | every live wave of its workgroup has arrived (§7.2) |

`fpend` and the float's redirect to `pc + 4` are deleted: a wave with a float
in flight issues its next independent instruction.

### 4.3 The register file's write port

Three producers now write the per-thread file: the pipe (every cycle), the float
and multiply retire slot (fixed latency), and load and LDS returns (any time).

| `WB_BUF` | How |
|---|---|
| 0 | Today's arrangement. A result that will write in two cycles raises `f_soon`, the pipe drops `go` once, and the result takes the port by mux. One issue slot per long-latency result |
| 1 | Long-latency results enter a **write-back queue** and write the file on any cycle the pipe does not. A float or multiply instruction's own writeback slot is empty — its result comes later, through the queue — so every such instruction leaves behind the slot its result needs, and in steady state no issue slot is lost. `f_soon` now means "the queue plus what is still in flight would reach its depth", counted exactly, and stops issue only then |

A second register-file write port would cost a copy of every read mirror and a
per-lane live-value table; the queue costs one distributed-RAM FIFO of 16 or 32
results. The scoreboard bit of a queued result clears when it reaches the file,
not when it leaves the unit, so a dependent instruction never reads around it.
`WB_BUF = 1` requires `SCORE = 1`.

A load's return (`SCORE = 1`, §5.1) is the third producer and the lowest: it
writes on a cycle neither the long result nor the pipe takes the port. A return
that has waited a cycle raises `f_soon`, the pipe drops `go`, and the bubble
reaches writeback two cycles later. Its scoreboard bit clears on that write.

## 5. Per-thread memory

### 5.1 The LSU

`LSU_DEPTH > 0` builds `kht_lsu`. A memory instruction at EX takes an **LSU
entry** — its registered per-lane addresses (`ea_all_q`), store data, mask,
width, sign, and the destination — and is served in order with every other.

- `SCORE = 1`: the instruction **retires** on the push. A load sets its
  destination's scoreboard bit and its return writes the file (§4.3). With every
  entry busy the instruction replays and its wave sleeps until an entry leaves
  (§4.2).
- `SCORE = 0`: the instruction holds until its entry returns, as the walk did,
  and the load's words reach the file through the pipe.

**One access is all DRAM or all LDS, every active lane aligned for its width.**
All-LDS goes to the resolver as one access (as today), all-DRAM to the queue.
Anything else — lanes in two regions, a lane in no region or in the scratchpad or
control window, a misaligned lane, a vmem width of 3 — faults the instruction at
`go`, before any lane takes effect. The check is two registered stages beside the
LDS one and costs no cycle. Bytes and halves are loaded with their sign or zero
extension and stored under their byte enables, as the golden model applies them.

`LSU_DEPTH = 0` keeps the serial walk and `rv_l1`, word-only and without the
access rule. `LSU_DEPTH > 0` requires the banked LDS.

### 5.2 The coalescer

The entry at the head is served **one 32-byte line per access**: the lowest
outstanding active lane names a line, every active lane whose address falls in
that line is served by the same access, and those lanes leave the outstanding
set. Lane-linear and stride-1 accesses are one line; eight lanes on five lines
are five. `COALESCE = 0` serves one lane per access instead.

An access is three cycles from registers to registers — name the line, read its
tag and present the data address, take the line — and a store hit a fourth to
write it back. Every stage starts at a flop or at a RAM output.

The L1's data array is read and written a **whole line at a time**: a load lane
takes its word through an 8:1 select on the line, a store merges its lanes' bytes
into the line under their byte enables, higher lanes last.

### 5.3 The L1 and its fill buffers

Direct-mapped, `L1_LINES` lines of 32 bytes, tag array in distributed RAM as
`rv_l1`'s. A miss does not stall the core:

- **Fill buffers**, `MSHRS` of them, each `{state, line address, 256-bit data,
  dirty}`. A line the head entry needs that is neither in the L1 nor in a fill
  buffer gets a buffer and a one-entry streaming read with its own `txn`. A
  **scanner** walks the lanes of the head entry and the next one, a lane a cycle,
  and gives every missing line a buffer while one is free, so independent misses
  overlap; the head's own miss takes a buffer first.
- **The head is served from the L1 or from a fill buffer**, whichever holds the
  line. A buffer's line is written into the L1 once the head is not serving it,
  and the buffer frees in the same cycle the tag becomes visible, so a line is
  never in neither. A dirty victim is written back first. A line the head is
  waiting on is never installed, so two lines that conflict in the L1 cannot
  evict each other into a livelock.
- **No read overtakes a write of its line.** The memory protocol orders neither
  (memory-protocol.md §7.2), so a fill waits while its line is in the LSU's write
  register or, write-through, in the store about to be sent; `kht_req` holds it
  until the one un-acknowledged write of that line is acknowledged.
- **Stores allocate.** A store to a line not present fills it, merges, and marks
  it dirty: the memory port has no partial-line write (§2.4).
- `L1_WB = 0` makes every store also send its line (write-through); the line
  stays valid and clean. `d_wb` then has nothing to do.
- `MSHRS = 1` and `COALESCE = 0` are today's single blocking miss in effect,
  without the core stall.
- **Flush and invalidate** are latched and run once the queue, the head and every
  buffer are idle: one line every two cycles, every dirty line written back, and
  done only when every write is acknowledged. The halt raises flush behind its
  last stores, so the completion still means the data is in memory.

Outstanding responses are bounded in flits: at most `MSHRS` fills, one flit each,
plus the stream engine's own budget (§6.4), inside `RECV_DEPTH`.

## 6. The stream engine

`STREAM = 1` builds it. It moves data between memory or another unit and the
LDS without the L1 and without a wave touching it.

### 6.1 Descriptors

Issued by a wave as an instruction (§8) and queued, `STREAM_DEPTH` deep.

| Kind | From → to | Unit of work | Counter it advances |
|---|---|---|---|
| FILL | DRAM → LDS | one entry read of `n` 32-byte entries (`STREAM`, `entry_words 1`, `n` ≤ 255) | + `n` as each entry lands |
| DRAIN | LDS → DRAM | write bursts of ≤ 8 lines, one open at a time | + lines, when their ack returns |
| SEND | LDS → another unit's buffer | one `CU_DATA` burst, `COUNT` set, ≤ 256 granules | + granules, when the burst has left |

The descriptor names the LDS granule offset, the memory address or destination
`{y, x, buf_id, offset}`, the length and the counter (§8.2).

A 32-byte line at an LDS granule offset is one **row** across the eight banks,
since words interleave by bank. FILL and receive write it in one cycle, DRAIN and
SEND read it in one. An LDS row access waits for the lane resolver's current pass
and takes the next cycle; the resolver yields one cycle in every two while the
stream engine has rows queued.

### 6.2 Receiving

A `CU_DATA` burst is written into the LDS a row per flit. With `flags[1]`
(`COUNT`) set its granules advance a counter:

| `buf_id` | Lands in | Counter on `COUNT` |
|---|---|---|
| 0 | LDS (granule offset) | 0 |
| 1 | the instruction window (`ICACHE_LINES = 0` only) | — |
| 4 | LDS, one byte-enabled word (today) | — |
| 5 | the instruction window, one word (`ICACHE_LINES = 0` only) | — |
| 8–11 | LDS (granule offset) | `buf_id − 8` |

A received `CREDIT` (`CREDITS = 1` on the base) adds its amount to counter 3.

### 6.3 Counters and `wait`

Four 32-bit counters, read by `rdctl 16–19`, set by `cntset`, zeroed by
`cnt_reset`. `wait k, s` puts the wave to sleep until counter *k* ≥ the unsigned
value of scalar *s*, and retires at once if it already is. A counter only ever
advances by hardware, so a wave never waits for a value it has already passed.

### 6.4 Outstanding budget

A FILL of `n` entries is `n` response flits. The engine holds a FILL back until
its flits fit in the receive queue beside the L1's `MSHRS`:
`MSHRS + Σ outstanding FILL entries ≤ RECV_DEPTH`. That is the memory
protocol's rule ([§3.2.4](../../../spec/memory-protocol.md#324-how-much-a-requester-may-have-outstanding-and-in-what-unit)),
counted in flits.

### 6.5 Sending and credits

`credit s1, s2` posts one `CREDIT` of `s2[15:0]` to `{y, x} = s1[7:0]`
through the base's `post_*` port. SEND is the producer side: a kernel waits on
counter 3 for the consumer's credits before it sends.

## 7. LDS

### 7.1 Atomics

`vamo` (§8.1): for every active lane, `old = LDS[a]; LDS[a] = op(old, v); v = old`,
with `op` one of add, signed max, signed min, swap. The resolver already serves
one lane per bank per pass, so two lanes on one address are serialized, and the
read and the write of a pass are one bank access each. The old value is a
long-latency result (scoreboard). `LDS_ATOMICS = 0` faults the encoding.

### 7.2 The barrier

`bar` executes: the wave sleeps until every live wave of its workgroup has
reached a `bar`, then all of them wake. A wave that ends counts as arrived. The
scalar operands are unused and MUST be `s0`.

## 8. Instructions added

All on the existing two majors; the field table (`rv_simt_isa.py`) is the source
and every consumer is generated from it.

### 8.1 Lane operations

custom-2, `funct3 = 6` (**VX**), R-type, one cycle in the integer pipe:

| `funct7` | Mnemonic | Per active lane |
|---:|---|---|
| 0 | `vfmin vd, vs1, vs2` | IEEE minNum of binary32 (a NaN loses to a number) |
| 1 | `vfmax vd, vs1, vs2` | maxNum |
| 2 | `vflt vd, vs1, vs2` | 1 if vs1 < vs2, else 0 (NaN: 0) |
| 3 | `vfle vd, vs1, vs2` | 1 if vs1 ≤ vs2 |
| 4 | `vfeq vd, vs1, vs2` | 1 if vs1 = vs2 (+0 = −0) |
| 5 | `vcvtfh vd, vs1, h` | binary16 half `h` (the `rs2` field as an immediate, 0 low, 1 high) of vs1 → binary32, exact |
| 6 | `vcvthf vd, vs1, vs2` | `{f16(vs2), f16(vs1)}`: two binary32 → two binary16, round to nearest even, overflow to ±inf |

bf16 needs no instruction: it is the top half of binary32, so `slli 16` widens
it and an add-and-shift narrows it with round to nearest even.

The VMEM group's `op 6` is **`vamo`**: `funct7 = 6<<4 | scale<<2 | amo`, `amo` 0
add, 1 max, 2 min, 3 swap; address `s[ss1] + (v[vs2] << scale)` in `R_LDS`; the
`rd` field is both the value and the destination.

### 8.2 Stream and sync

custom-2, `funct3 = 7` (**STRM**), R-type:

| `funct7` | Mnemonic | Operands |
|---:|---|---|
| 0 | `ldsfill s1, s2` | s1 = DRAM byte address (32-byte aligned); s2 = descriptor |
| 1 | `ldsdrain s1, s2` | s1 = DRAM byte address; s2 = descriptor |
| 2 | `ldssend s1, s2` | s1 = `{y[3:0], x[3:0], buf_id[7:0], offset[15:0]}`; s2 = descriptor |
| 3 | `credit s1, s2` | s1 = `{y, x}` in `[7:0]`; s2 = amount in `[15:0]` |
| 4 | `wait k, s1` | `k` = counter (the `rs2` field as an immediate); s1 = target |
| 5 | `cntset k, s1` | counter `k` ← s1 |

Descriptor `s2`: `[15:0]` LDS granule offset, `[23:16]` length − 1 in granules
(1–256), `[25:24]` counter, `[31:26]` reserved and 0.

`bar` keeps its encoding (DIV, `funct7 = 3`) and now executes (§7.2).

### 8.3 Scalar load

custom-2 SMOV, `funct7 = 2`: **`sld sd, ss1`** — `sd` ← the word at DRAM address
`s[ss1]` (word aligned, `R_DRAM` only, else a fault), through the scalar cache
(§3.2).

## 9. Knobs

| Knob | Values | 0 means |
|---|---|---|
| `LANES`, `FLANES` | 8 | fixed: 8 × 32 bits a cycle |
| `WAVES` | 1–16 | — |
| `FSFU_UNITS`, `SHFL_UNITS`, `LDS_BANKS`, `IPDOM_D` | as today | as today |
| `ICACHE_LINES` | 0, 64, 128, 256 | the instruction window |
| `KCACHE_LINES` | 0, 8, 16, 32 | `sld` faults |
| `SCORE` | 0, 1 | today's one-in-flight issue with `fpend` |
| `WB_BUF` | 0, 1 | one slot per long result |
| `LSU_DEPTH` | 0–4 | the serial walk and `rv_l1` |
| `COALESCE` | 0, 1 | one lane per access |
| `L1_LINES` | 64–512 | — |
| `MSHRS` | 1–8 | — |
| `L1_WB` | 0, 1 | write-through |
| `STREAM`, `STREAM_DEPTH` | 0 / 1, 1–8 | no stream engine; STRM faults |
| `LDS_ATOMICS` | 0, 1 | `vamo` faults |
| `VX` | 0, 1 | the VX group faults |
| `POST_DEPTH` | 1, 2 | LAUNCH.post behaves as LAUNCH |

## 10. How it is checked

- **One golden model** (`rv_simt_model.py`) runs launches, workgroups, the
  barrier, counters, every new instruction and the memory semantics of §2.4. It
  is cycle-free: caches are invisible to it except through the coherence rule.
- **Every block has a unit bench and a mutation witness**: the instruction cache,
  the scoreboard issue, the coalescer and L1, the stream engine, the LDS
  additions, the launch unit. A bench that still passes with its block's defining
  line broken is not a bench.
- **Every shader in the suite runs at every knob's zero and at the shipping
  setting**, against the same golden memory, through `kht_sys`.
- **Performance is measured with the six kernels the old core was measured on**
  (stream, fma, gather, argmax, diverge, exp2) plus a cluster stream into LDS,
  top-k, MoE gather/scatter and sort, with the bench's cycle classes.
