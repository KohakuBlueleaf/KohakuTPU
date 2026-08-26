---
title: The processor, the mover and the slot
summary: The scalar/SIMD/extension model of the system node — one front door, one walker, and a transform slot on the mover's read-return path. What each layer owns, how they are commanded, what a converting move costs, and how it is verified.
tags:
  - architecture
  - sysnode
---

# The processor, the mover and the slot

| layer | is | owns |
|---|---|---|
| control processor | the node's scalar processor | **what** and **where** — descriptors, control flow, the irregular cases |
| memory mover | its SIMD unit | **when** — the walk, bursting, ordering, backpressure, padding |
| transform slot | that SIMD unit's extension | **what shape** — the byte envelope of a stream |

There is **one walker in the system and the mover owns it**. Anything that must
traverse memory does so by being a transform on a move, never by walking for
itself.

**The bottom two layers belong to the node, not to the processor.** The node is
built with one of two control complexes, chosen by `CPU_RV64` — the default RV32
one, or an RV64 one. Both assemble the same mover and the same slot, unchanged,
and only the top layer differs — [control-processor](control-processor.md).

## One front door

The mover is an **executor of the processor, not a peer with a doorbell**. The
descriptor is architectural state, program order is the queue, and the mover has
no fabric endpoint of its own — the host talks to the processor for work.

So a move is commanded one way: **a store into an address range the processor
decodes**, uncached and not reorderable against the move it commands. The two
complexes place that range differently and hand the mover the same nine
registers either way.

| | the RV64 complex | the RV32 complex |
|---|---|---|
| where the range is | the control region at `0x0002_0000` | the node range at `0xF000_0000` |
| how a descriptor is delivered | one store per mover register, straight through | one store of a **pointer**; `mv_exec` fetches the register list from the scratchpad and replays it |
| what `busy` spans | the move | the descriptor fetch **and** the move, so one poll covers both |

The pointer form buys a seven-register move for one store, at the cost of a
small fetch engine and a scratchpad port; the direct form costs seven stores and
no engine. Both leave the mover's interface identical, which is the point — the
mover does not know which processor is in front of it.

The **host's** config window is a different thing and it does not disappear.
Issuing a move register-by-register from the host is the transport cost this
design exists to delete, but bring-up needs a path that works before any program
runs. When the processor and the host both write in one cycle, **the processor
wins.**

## Registers are registers

Nothing about a control range is special: a register the processor can read and
one it can write are the same mechanism, and whether a given write is followed
by a move is the program's business.

**Space is not a constraint.** `mm_mover` decodes `reg_sel = {cfg_addr[7:3],
3'b000}` at `0x00, 0x10, 0x18, 0x20, 0x28, 0x30, 0x38, 0x40, 0x50` — nine of the
sixteen 8-byte slots in the client range, so **seven are free: `0x08, 0x48,
0x58, 0x60, 0x68, 0x70, 0x78`**. The transform's id and mode needed none of
them: they ride the source walker header's free upper bits, `[50:47]` and
`[58:55]`, because a transform applies to the read side.

> **A register range must be reached by an early read and a registered write.**
> On both complexes the range is decoded ahead of the L1, and on both the read
> has to arrive in the cycle the L1 would have answered — a combinational read
> is sampled with the request already low and the cache array's word is returned
> in its place, so a status load reports zero however the mover is doing. The
> write must not be combinational either: driven off the address adder it lands
> on a register's clock enable and puts the adder in a 15-level chain. Read
> early, write registered. This is the same rule as
> [control-processor](control-processor.md#one-handshake-for-every-access-and-it-costs-a-cycle)'s.

### Status and faults

A load in the range returns `busy` and the mover's fault code as **disjoint
fields**. The two complexes place them differently, and both keep them apart:

| | busy | mover fault | retired-move count | bank fault |
|---|---|---|---|---|
| RV64, control region `0x20` | `[32]` | `[31:28]` | `[27:0]` | — |
| RV32, node range `0xF000_0000` | `[0]` | `[7:4]` | — | `[11:8]` |

**Disjoint is the load-bearing part, not the positions.** Merged into one word,
bit 0 reads `fault[0] | busy`; a poll loop on bit 0 then spins forever on fault
code 1, and no code can tell a fault from a move in flight. **A status word
whose fields overlap is not a status word.**

The transform bank's own sticky fault sits beside the mover's rather than merged
into it, and is **per bank, not per occupant** — the one condition a bank can
detect for itself is an id naming no occupant, which is a property of the demux
rather than of any occupant. **The RV64 complex has no column for it above,
because the bank's register port is tied off there, so neither the bank's fault
nor any occupant register is reachable today** —
[control-processor](control-processor.md#where-todays-source-disagrees).

**A fault aborts the run and the run still completes.** The mover stops issuing,
`busy` falls normally, the fault field is non-zero — the existing poll is
unchanged. The destination is left partially written, which is deliberate:
definitely incomplete beats plausibly wrong.

## The slot

One bank per memory agent, selected by an **id** — `0` bypass, `1` slot 1, `n`
slot n. Occupants are all resident in fabric, so more slots cost area and buy a
*choice*, not concurrency. The framework names exactly one module, `xform_bank`,
which holds the project's occupants and demuxes the id internally.

`mag_xform.v` arbitrates: round-robin across `NREQ` requesters with the **grant
held for a whole run**, and a requester must not issue its read until it holds
one — that is what makes it impossible for a beat to arrive with nowhere to go.

Properties of the contract that follow from the RTL, each of which an occupant
author has to design around:

- **Beats are pushed at line rate and never handshaken.** `need_beat` is left
  unconnected; an occupant that cannot take line rate buffers internally.
- **The occupant is not double-buffered.** `start` resets the pipeline, so
  starting entry N+1 while N is still packing *aborts N*. The mover therefore
  keeps **one entry in the slot**: the next entry's first read is held until
  `done`. Entry N's *write* still overlaps N+1's reads, because the command FIFO
  decoupled those before the slot was ever on this path.
- **`start` leads the first beat by a cycle.** `mx_quant`'s control is
  `if (start) ... else if (filling && beat_valid)`, so a beat presented *with*
  start is silently dropped. The mover's beat path is two registers and start is
  one.
- **The four output words are serialised into the FIFO.** The occupant emits
  `word0..word3` in parallel and the FIFO takes one a cycle, so `done` starts a
  four-cycle push rather than writing directly.
- **Geometry is declared, not discovered.** `IN_BITS` and `OUT_WORDS` are
  parameters because the agent sizes both walks before the occupant has run.

## Where the slot sits

**On the mover's read-return path**, between R and the FIFO. `MODE_XFORM` is
mover mode 5; there is no second engine and no mux.

> It used to be a separate engine, `mm_xfer.v`, sharing the AXI requester channel
> through a mux in `mag.v`, split out because the mover's flow control was one
> 32-byte word in per word out and a 2:1 transform breaks that. That engine is
> deleted. What replaced the invariant is below.

```
src walker ─► issue engine ─► AR ─┐
                                  │  R returns, in order (m_arid = 0)
                                  ▼
                              [ SLOT ]
                                  ▼
                                FIFO ─► write engine ─► AW/W ─► dst walker
```

Three things drove it, and the third is the one that forced it:

**The slot's input contract is what an in-order R return already is.** Reads all
issue under `m_arid = 0`, and the mover already depends on ordered returns — its
FIFO is a plain queue drained in destination order.

**The arbiter argument runs the other way.** The split avoided a second requester
on the converged arbiter by muxing two engines. The fold leaves **one** engine
and no mux — one requester fewer, and Gate 0 measured that direction as worth
`+0.088 → −0.372` for one *extra* requester at two ports.

**`mm_xfer` had no walker.** Source and destination were contiguous runs, so a
strided source needed a gather into staging first — **28 word transfers per entry
against 12**, the source crossing the DRAM boundary twice on the one converged
master. Its FSM was fully serial (`X_AR → X_FILL → X_WAIT → X_AW → X_W → X_B`),
so entry N's write never overlapped entry N+1's read either.

**Measured, `tests/sysnode/mm_xform_tb.v`:** a 3-entry move from a source strided
64 bytes within the entry issues **24 individual reads and no staging pass at
all**, against 3 folded bursts for the contiguous case — 27 ARs across both. The
gather the old engine needed is gone, not cheaper.

**Measured, out-of-context synthesis on `xcvu13p-fhgb2104-2L-e`, Vivado 2024.2,
at 3.333 ns, `PORTS=2`, `sysnode` whole** — the hierarchical report of the run
each column names:

| instance | | LUT, RV64 node | LUT, RV32 node | DSP |
|---|---|---|---|---|
| `u_xform` | `mag_xform` + the bank + its occupant | 4,499 | 4,356 | 32 |
| `u_mover` | `mm_mover`, the slot folded onto its read path | 4,651 | 4,601 | 3 |
| `u_mag` | MAG, without the mover or the slot | 19,047 | 18,924 | 0 |

Produced by `scripts/tcl/ooc_sysnode_rv64.tcl` and `scripts/tcl/ooc_sysnode.tcl`
respectively. The two runs differ in more than the processor — the RV64 one also
moves staging out of the memory ports — so read the pair as *the mover and the
slot do not move with the processor*, which is what "they belong to the node"
means as a measurement, and not as a difference of anything else.

The node's DSP total is **39 with either processor** — 32 for one transform
bank, 3 for the mover, 4 for the core's multiplier. A figure that does not scale
with the port count is what says there is one bank rather than one per port, and
`ooc_sysnode.tcl` errors above 48 to keep it that way.

> **Hierarchical rows here come from a `rebuilt` netlist**, so a leaf may be
> charged to the instance it was re-parented into. The top-line node totals are
> exact; treat the breakdown as attribution.

### What the mover does with it

| copy | transform |
|---|---|
| dst walker defines the iteration space, src follows 1:1 | **src** defines it; dst steps once per entry, so a dst descriptor counts ENTRIES |
| one FIFO word reserved per read element | `OUT_WORDS` reserved per entry, at its first source word |
| the write-run accumulator folds consecutive writes | one burst of `OUT_WORDS` per entry, named when the entry opens |

**The reservation invariant is unchanged.** `m_rready` is tied `1'b1`; the
reservation exists so a read return can never be refused. Folded, the rule reads
"do not issue an entry's ARs without room for its `OUT_WORDS`" — still a static
count, still known before the AR.

Two invariants hold the folded path together, and each fails silently:

- **The read run must close at the entry boundary.** Held open across the stall
  that waits for `done`, its AR never goes out and the wait is permanent.
- **The dst walker runs one element AHEAD of the element latch**, like every
  other walker here. Stepping it on the entry's last element instead of the one
  before puts every entry's words at the *previous* entry's address.

**Bound-axis padding is not available in a transform move.** A padded element
issues no read, and the occupant is fed a fixed `IN_BEATS` off the read return,
so a bound axis would leave an entry a beat short forever. The mover raises
fault 7 rather than converting the wrong bytes. A transform descriptor tiles to
whole entries, which is what the compiler emits anyway.

## Occupant registers

`cfg_en / cfg_id / cfg_addr / cfg_data / cfg_rdata / fault` on the bank, reached
from the processor's control range and indexed by occupant id. `cfg_rdata` is a
combinational read of `cfg_addr`, so there is no write-enable: a write is
`cfg_en`, a read is always available.

> **This is wired on the RV32 complex and tied off on the RV64 one.**
> `rv64_mag_pe` drives the bank's `cfg_en` to zero and leaves `cfg_rdata` and
> `fault` unread, so **in that configuration no occupant register is readable or
> writable and the bank's fault is not observable**. Everything below describes
> the contract, which the RTL implements and the default RV32 configuration
> reaches; what is missing is the connection inside the RV64 complex.

The shipping occupant still needs none — `mode` picks its packing and its scale
is derived per entry — and that a complete occupant needs zero registers is what
keeps them optional. What the *bank* uses them for is status:

| offset | R | W |
|---|---|---|
| `0x00` | `{28'd0, fault}` | any write clears the fault |
| `0x04` | `{8'd0, OUT_WORDS, IN_BITS}` of `cfg_id` — zero if the id names no occupant | — |

**The one fault a bank can detect by itself is an id that names no occupant.**
The demux answers such an id with the bypass path, so without this the move
completes, reports success, and delivers an unconverted operand. Geometry is
readable for the same reason: a driver discovers what a slot holds rather than
being told.

Configuration is legal only while the occupant is ungranted, which the whole-run
grant already guarantees.

### Where a transform's data comes from

| kind | example | mechanism |
|---|---|---|
| per-**move** selector | A vs B operand packing | `mode`, opaque, rides the descriptor |
| per-**configuration** | palette, coefficient table | registers |
| per-**entry** derived | a block scale | the occupant buffers and computes |

## Four transforms

The framework does not know what the bytes mean.

**Quantise — 2:1, arithmetic, entry-granular.** `IN_BITS 2048 / OUT_WORDS 4`. The
whole entry is needed before anything is emitted because the scale is shared
along K. Zero registers.

**Dequantise — 1:2.** `IN_BITS 512 / OUT_WORDS 4` — two beats in, four words out.
Proves the mover must handle **expansion**: the destination walk is twice the
source and the reservation is 4 per entry against 2 beats read.

> Not `IN_BITS 1024 / OUT_WORDS 8`. `xform_bank` presents exactly `word0..word3`,
> so **`OUT_WORDS > 4` is not expressible** — the mover would name an
> `OUT_WORDS`-beat burst and serialise four registers into it. An expanding
> transform shrinks its entry instead of growing its output. Going past four
> means widening the port list, which is a protocol change, not a parameter.

**Tile ↔ linear swizzle — 1:1, permutation only.** A render target is stored
tiled; scanout wants linear. No arithmetic, no registers, and it belongs on bytes
that were already moving.

**Palette or format conversion — register-fed.** RGBA8 → FP16 per channel, or a
paletted source through a lookup table. The palette is written once and many
moves use it; without registers this cannot exist in the slot at all.

## How this is verified

Every row runs in `scripts/py/check.py blocks`.

| bench | what it holds |
|---|---|
| `mm_xform` | the mover and the slot against a reference occupant, contiguous **and strided within an entry** |
| `xform_identity` | the framework alone — `kohakuaccel`, `templates`, `verif` and no project source — so the `xform_bank` dependency rule cannot rot |
| `mm_mover` | every other mode |
| `mag_system` | the converting move reaching real memory through the agent, with two compute units and the NoC live |
| `rv_mag_pe` | the RV32 complex: the node-range decode, a slot register written and one read back |
| `rv64_mag_pe` | the RV64 complex: the processor with the mover and the bank instantiated |
| `ctrlpe_mesh` | **a full mesh**, RV32 — the processor runs assembly that programs a mode-5 move, driven only through the station bus |
| `ctrlpe_mesh2` | **two meshes**, RV32 — mesh 0 converts and the result lands in mesh 1 over the interlink; one header field decides local or remote |

The last two are the ones that matter for "does software drive it": nothing is
poked hierarchically, the descriptor is staged as `CU_DATA` and the processor
executes ordinary loads and stores exactly as a compiled program would.
**Neither has an RV64 counterpart yet** — there is no whole-node simulation with
`CPU_RV64=1`, so the mesh-level "software drives it" evidence is the RV32
complex's.

## What does not fit

**A variable-ratio transform.** `OUT_WORDS` is read before the transform runs,
because the mover sizes the destination walk from it. Data-dependent compression
needs a transform that *writes back* a descriptor — a different architecture.

**A second data stream.** Registers carry configuration, not a second operand. A
transform combining two tensors is a two-source move, and the mover has one
source walker.
