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
| control processor | a scalar RV32 with a scratchpad | **what** and **where** — descriptors, control flow, the irregular cases |
| memory mover | its SIMD unit | **when** — the walk, bursting, ordering, backpressure, padding |
| transform slot | that SIMD unit's extension | **what shape** — the byte envelope of a stream |

There is **one walker in the system and the mover owns it**. Anything that must
traverse memory does so by being a transform on a move, never by walking for
itself.

Everything here is built and simulated. The two shape changes this page used to
carry as in-flight — the slot folding onto the mover's datapath, and occupant
registers — landed on 2026-08-24; where a decision was reversed, the reason it
was made the first way is kept, because that is the useful half.

## One front door

The mover is an **executor of the processor, not a peer with a doorbell**. The
descriptor is architectural state, program order is the queue, and the mover
exposes nothing externally — the host talks to the processor for work.

So a move is commanded one way. The processor builds a descriptor in its
scratchpad with ordinary stores and stores the pointer to `MVGO`:

```
word 0        : n, the number of register writes
then n times  : {24'b0, offset[7:0]}, value[31:0], value[63:32]
```

`mv_exec.v` fetches it and drives `mm_mover`'s `cfg` port offset for offset.
`busy` spans descriptor fetch **and** the move, so one poll covers both, and two
descriptors from different pointers are verified independent — the walk carries
no state.

## Registers are registers

The processor reaches the mover's and each occupant's control and status through
its **node range**, by ordinary load and store. That range is already carved out
ahead of the L1 (`l1_req = l1_req_core && !is_node`), which is what such a window
needs: uncached, and not reorderable against `MVGO`.

| address | | |
|---|---|---|
| `0xF000_0000` | W | `MVGO` — the descriptor pointer, and the go |
| `0xF000_0000` | R | status: `[0]` busy, `[7:4]` mover fault, `[11:8]` occupant fault |
| `0xF001_0000 \| (id << 8) \| reg` | RW | occupant `id`'s register `reg` |

Bit 16 splits the range. Nothing about this is special: a register the processor
can read and one it can write are the same mechanism, and whether a given write
is followed by a move is the program's business.

The mover's own nine registers are not in this window — they are written by the
descriptor, which is already a stream of `{offset, value}` register writes, so a
seven-write move costs the program one store rather than seven.

> **A node read is held one cycle.** `rv_l1` answers in WB, one cycle after the
> request, and the node path has to arrive with it. Combinational, it is sampled
> with `l1_req` already low and the L1 array's word is returned instead — which
> is why a status load read zero however the mover was doing. That was latent
> before the slot registers gave it a second reader.

The **host-facing** config window is a different thing and it disappears: issuing
a move register-by-register from the host is the transport cost the executor
design exists to delete.

**Space is not a constraint.** `mm_mover` decodes `reg_sel = {cfg_addr[7:3],
3'b000}` at `0x00, 0x10, 0x18, 0x20, 0x28, 0x30, 0x38, 0x40, 0x50` — nine of the
sixteen 8-byte slots in the client range. The fold returned `mm_xfer`'s four, so
**seven are free: `0x08, 0x48, 0x58, 0x60, 0x68, 0x70, 0x78`**. The transform's
id and mode needed none of them: they ride the source walker header's free upper
bits, `[50:47]` and `[58:55]`, because a transform applies to the read side.

### Status and faults

A load in the node range returns `busy` and the mover's fault code, with the
bank's own sticky fault beside it at `[11:8]` rather than merged into it. The
bank's fault is **per bank, not per occupant** — the one condition a bank can
detect for itself is an id naming no occupant, and that is a property of the
demux rather than of any occupant.

> These were OR-ed into one word, so bit 0 read `fault[0] | busy` and a poll on
> bit 0 spun forever on fault code 1. Now disjoint: `[0]` busy, `[7:4]` mover
> fault.

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

Properties of the contract that follow from the RTL and are easy to get wrong —
the middle three each cost a debugging pass:

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

**Measured, OOC on `xcvu13p` at 3.333 ns, `PORTS=2`** — hierarchical,
`-flatten_hierarchy none`, so a leaf is charged where it lives:

| instance | | LUT | DSP |
|---|---|---|---|
| `u_xform` | `mag_xform` + the bank + its occupant | 4,491 | 32 |
| `u_mover` | `mm_mover`, the slot folded onto its read path | 4,655 | 3 |
| `u_mag` | the whole agent | 28,418 | 35 |

`u_xform` is the **same 4,491 LUT and 32 DSP** the slot cost as a separate stage:
the fold moved who drives it, not what it is. The node's DSP total is 35 at any
port count, which is the figure that says there is one bank rather than one per
port — `ooc_sysnode.tcl` errors above 48 to keep it that way.

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

Two things the fold had to get right, both of which failed first:

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
from the processor's node range and indexed by occupant id. `cfg_rdata` is a
combinational read of `cfg_addr`, so there is no write-enable: a write is
`cfg_en`, a read is always available.

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
| `mm_mover` | every other mode, unchanged by the fold |
| `mag_system` | the converting move reaching real memory through the agent, with two compute units and the NoC live |
| `rv_mag_pe` | the node-range decode, a slot register written and one read back |
| `ctrlpe_mesh` | **a full mesh** — the processor runs RV32 assembly that programs a mode-5 move, driven only through the station bus |
| `ctrlpe_mesh2` | **two meshes** — mesh 0 converts and the result lands in mesh 1 over the interlink; one header field decides local or remote |

The last two are the ones that matter for "does software drive it": nothing is
poked hierarchically, the descriptor is staged as `CU_DATA` and the processor
executes `lui` / `addi` / `sw` to `MVGO` exactly as a compiled program would.

## What does not fit

**A variable-ratio transform.** `OUT_WORDS` is read before the transform runs,
because the mover sizes the destination walk from it. Data-dependent compression
needs a transform that *writes back* a descriptor — a different architecture.

**A second data stream.** Registers carry configuration, not a second operand. A
transform combining two tensors is a two-source move, and the mover has one
source walker.
