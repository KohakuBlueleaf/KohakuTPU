---
title: The transform slot
summary: One shared transform bank on the memory mover's read-return path, reached only through descriptor mode 5, occupant selected by an id. The port contract, the geometry contract, the selection encoding and the default occupant.
tags:
  - spec
  - sysnode
  - transform
  - addon
---

# The transform slot

> **Kind: Fixed interface, Addon occupant.** Where the slot sits, how an occupant
> is selected, the port it presents, the geometry it must declare and the three
> hard rules are all **fixed protocol** — an occupant that breaks one of them
> stalls a move or delivers the wrong bytes. What the occupant *computes* is a
> **customizable addon**: the framework carries its mode bits without reading
> them and is named after no number format.

A transform converts data between the format memory holds and the format a
compute unit wants. The **memory mover** — the descriptor engine inside the
system node that walks addresses and moves bytes without any compute unit being
involved — is the only thing that drives it.

The framework fixes where the slot sits, how it is selected and how it is driven;
what goes in it belongs to the accelerator. The shipping example is KohakuTPU's
FP16→MXFP7 quantiser, in `src/kohakutpu/transform/xform_bank.v`; the empty
default is `src/templates/transform/xform_bank.v`.

## Position

**One bank per memory agent**, on the memory mover's read-return path — not one
per port, and not on a compute unit's fetch path.

```
   mem / L2  --> [ slot ] --> mem / L2      pre-convert on card, once
   mem / L2  ------------->   port --> NoC --> compute unit
```

Only the **memory mover** drives the slot. A compute unit's fetch is never
transformed: it reads operands that are already in their final format, whether
the host wrote them that way or the mover converted them in place.

**The slot is ON the mover's datapath, not beside it.** There is one walker in
the system and the mover owns it; a transform is pre- or post-processing on a
move, never an engine that traverses memory for itself. It sits on the
**read-return path**, between R and the mover's FIFO — the slot's input is pushed
at line rate and never handshaken, which is what an in-order R return already is,
and the FIFO holds converted words rather than source ones. A converting move is
mover mode 5.

The invariant this placement breaks, and which the mover therefore does not
have, is *one word in per word out*: a transform consumes `IN_BITS` and produces
`OUT_WORDS`, so the mover's read-side reservation counts `OUT_WORDS` per entry
rather than one per beat. That reservation is still taken before the AR and is
still static. [arch/sysnode/simd-model](../arch/sysnode/simd-model.md) has the
argument for the arrangement.

### Why one is enough

This is structural, not a workload measurement. A per-port transform is fed from
that port's AXI R channel; `mag_1m` converges every port master onto **one**
`M_AXI_DRAM`; and a staged read never transforms, because staging holds operand
words verbatim. So every transformed byte comes from a single converged master,
and N transforms could consume one beat per cycle between them — N−1 idle by
construction, and each of them carrying the occupant's DSPs.
[arch/sysnode/simd-model](../arch/sysnode/simd-model.md) has what one costs on
the reference part.

### Why the mover, and not the requester

A transform on the fetch path is paid **once per read**. A transform on the
mover path is paid **once per tensor**. Hidden state is written back as FP16 by
the units and then re-read; converting it on every read is the expensive
arrangement, and converting it once into staging or back into memory is not.

The cost of this choice is that a **single-use** operand pays more, not the same.
Converting on the fetch read it once; converting on the mover reads the source,
writes the converted copy, and the fetch then reads that. At the reference
occupant's 2:1 geometry that is 256 + 128 + 128 bytes against 256, plus a pass
of latency; the ratio follows from `IN_BITS`/`OUT_WORDS` and is whatever the
occupant declares. The trade is deliberate: an
operand read more than once wins immediately, and an operand read once is the
case the compiler should not be generating.

## Selection

Selection is an **id**, never a bitmask. Occupants are all resident in fabric;
the id routes one request to one of them.

| value | meaning |
|---|---|
| `0` | no transform — bypass |
| `1` | slot 1 (KohakuTPU: the MXFP7 quantiser) |
| `n` | slot n |

Two fields travel together:

| field | width | who reads it |
|---|---|---|
| `XFORM_ID` | `ID_W` | the agent — routes to an occupant |
| `XFORM_MODE` | `MODE_W` | **the occupant only** — the agent carries it and never interprets it |

`XFORM_MODE` is opaque. KohakuTPU's occupant uses `mode[0]` as its A/B operand
packing select — what the protocol used to call `BLAYOUT`. Nothing in the
framework is named after a number format.

### Where the fields ride

On the **source walker's header**, mover register `0x10`, in bits the header
already left free. A transform applies to the read side of a move, so they are
written with `sel = 0` and ignored on the destination's header.

| bits | field |
|---|---|
| `[50:47]` | `XFORM_ID` |
| `[58:55]` | `XFORM_MODE` |

No register was spent on them. Of the sixteen 8-byte slots in `0x00`–`0x7F`,
`mm_mover` decodes nine, and the retired engine's four came back with it, so
`0x08, 0x48, 0x58, 0x60, 0x68, 0x70, 0x78` are all free.

### What the memory request no longer does

`MEM_RD_REQ` `flags[4]` and `flags[5]` were `QUANT` and `BLAYOUT`. **Both are
now reserved and ignored.** A requester that still sets them gets an
untransformed fetch, which is the correct answer for every operand now that
conversion happens before the fetch.

## Where a transform's data comes from

Three sources, and which one applies decides whether an occupant needs registers
at all:

| kind | example | mechanism |
|---|---|---|
| per-**move** selector | A vs B operand packing | `mode`, opaque, rides the descriptor |
| per-**configuration** | coefficient table, LUT, palette, bias | **registers**, written before the move |
| per-**entry** derived | the quantiser's block scale | the occupant buffers and computes internally |

KohakuTPU's quantiser uses only the first and third, which is why a zero-register
occupant is a complete one. `mode` is kept alongside registers rather than
replaced by them: it is per-move and free, where a register write is per-move
cost if you use it that way.

## The port contract

An occupant bank presents:

| port | dir | width | contract |
|---|---|---|---|
| `clk`, `rst` | in | 1 | `rst` active-high, synchronous, the agent's domain |
| `start` | in | 1 | one-cycle pulse opening an entry; `id` and `mode` are valid during `start` |
| `id` | in | `ID_W` | which occupant; 0 is bypass |
| `mode` | in | `MODE_W` | opaque configuration, captured at `start` |
| `beat` | in | `DATA_W` | one source beat, already registered by the agent |
| `beat_valid` | in | 1 | qualifies `beat`; beats are pushed at line rate, never handshaken |
| `need_beat` | out | 1 | for an occupant that cannot take line rate; the agent ignores it today, so tie it high or drive it truthfully |
| `done` | out | 1 | one-cycle pulse: outputs are final |
| `word0..word3` | out | `DATA_W` each | the transformed entry, stable from `done` until the next `start` |
| `cfg_en` | in | 1 | write strobe for the register at `cfg_addr` |
| `cfg_id` | in | `ID_W` | which occupant the register access names |
| `cfg_addr`, `cfg_data` | in | 8, 32 | byte offset and value; registers are 4 bytes |
| `cfg_rdata` | out | 32 | **combinational** read of `cfg_addr` — so there is no write-enable: a write is `cfg_en`, a read is always available |
| `fault` | out | 4 | sticky, cleared by any write to register `0x00` |

> The shipping occupant needs no registers of its own — its `mode` picks its
> packing and its scale is derived per entry — and that a complete occupant needs
> zero registers is exactly what keeps them optional. The **bank** still uses the
> space, for status: see §Bank registers.

### Bank registers

Two are defined for every bank, and a project's own occupants may add more:

| offset | R | W |
|---|---|---|
| `0x00` | `{28'd0, fault}` | any write clears the fault |
| `0x04` | `{8'd0, OUT_WORDS, IN_BITS}` of `cfg_id`, or zero if that id names no occupant | — |

**`fault[0]` means an entry was started with an id that names no occupant.** It
is the one fault a bank can detect by itself, and it matters because the demux
answers an unknown id with the bypass path: without it the move completes,
reports success, and delivers an unconverted operand. Geometry is readable so a
driver discovers what a slot holds rather than being told.

*A shipping occupant may have no fault of its own; KohakuTPU's quantiser has
none, so on that bank `fault` is `[0]` and nothing else.*

### How a register is reached

By ordinary load and store from the control processor's **node range**:

    0xF001_0000 | (id << 8) | reg

A register the processor can read and one it can write are not different things,
and whether a write is followed by a move is the program's business.

The **host** has no path to them. The host talks to the processor for work.

> **This holds only for the RV32 control complex** — `sysnode`'s `CPU_RV64 = 0`,
> the default. `rv_mag_pe` decodes that range and drives `mag_xform`'s
> `cfg_en / cfg_id / cfg_addr / cfg_data` from it, and returns `cfg_rdata`.
>
> **With `CPU_RV64` non-zero the register port is tied off.** `rv64_mag_pe`
> instantiates `mag_xform` with `cfg_en` at zero and `cfg_rdata` unconnected, and
> the RV64 control region carries no occupant window, so **an occupant's
> registers are unreachable in that configuration** — the bank's own `0x00` fault
> and `0x04` geometry included. An occupant with no registers of its own is
> unaffected, which is the case the shipping bank is in; one that needs
> configuration cannot be driven there.
>
> Because `0x00` cannot be written, `fault` is also **unclearable** in that
> configuration: it is sticky, and any write to register `0x00` is the only thing
> that clears it. See [parameters.md](parameters.md) §5.1.

### Configuration is only legal while ungranted

Grant is held for a whole run (§Arbitration), so the ordering above is safe by
construction and reconfiguring mid-run is unrepresentable. An occupant may latch
its registers at `start` and needs no further guard.

### A fault aborts the run, and the run still completes

An occupant raising `fault` stops the move: the agent issues no further reads,
`busy` falls normally, and the mover reports a fault code meaning *the occupant
faulted*. The occupant's own sticky `fault` says which.

The completion still arrives, so nothing above has to learn a new wait. The
destination is left **partially written**, which is the deliberate trade: a
destination that is definitely incomplete is safer than one that is plausibly
wrong.

### The three hard rules

1. **Fixed output shape, and four is the ceiling.** An entry yields `OUT_WORDS`
   words whatever the source length, and the bank presents exactly
   `word0..word3`, so **`OUT_WORDS` is at most 4**. An expanding transform
   shrinks its entry rather than growing its output. The bypass occupant obeys
   the same rule — four beats in, four words out — so a requester naming id 0
   gets the same shape as any other.
2. **The whole entry may be needed before anything is emitted.** The quantiser's
   block scale is shared along K. `done` may come any number of cycles after the
   last beat.
3. **Input is push-only.** A transform needing backpressure buffers internally.

## The geometry contract

The agent's address arithmetic needs the occupant's shape *before* the occupant
runs — a transform does not only change data, it changes how many bytes a read
must fetch and how far apart entries sit.

    parameter integer IN_BITS     // source bits consumed per entry
    parameter integer OUT_WORDS   // words produced per entry, at most 4

KohakuTPU's quantiser declares `IN_BITS = 2048`, `OUT_WORDS = 4` — eight source
beats in, four words out, the 2:1 ratio the mover needs to size a converting
move. These replace the `Q_ENTRY_BITS` / `P_ENTRY_BITS` literals that used to
live in the framework.

`IN_BITS` is free; `OUT_WORDS` is bounded by the port list above. A 1:2
expansion is `IN_BITS 512 / OUT_WORDS 4`, not `1024 / 8`.

## Arbitration

Requesters contend for the bank through `mag_xform.v`. **Grant is held for a
whole run**, and a requester **MUST NOT** issue its read until it holds one —
that is what makes it impossible for a beat to arrive with nowhere to go.
Per-entry grant would be finer-grained but is unsafe: a requester issues the next
entry's read while the current entry is still in the occupant, so its beats can
land before it could re-acquire.

A beat presented without a grant is **dropped**, and reported by a simulation
`$display` only.

> **There is one requester today.** `mag_xform`'s `NREQ` defaults to 2, and both
> instantiations in the tree pass 1: the memory mover is the only thing that
> drives the slot. The arbiter is built and never arbitrates. An occupant author
> gains nothing from that — the grant discipline above is what the port contract
> is written against, and a second requester may be added without the occupant
> changing — but a reader comparing this page against a netlist should expect to
> find the arbitration folded away.

## Timing

Two register stages are part of the contract, not an implementation choice:

- **The agent registers the beat before the bank.** An occupant may therefore
  treat `beat` as arriving from a register, and the path from whatever memory
  feeds the read return to the occupant's first stage of logic is broken.
- **The agent registers again after the requester mux.** This costs one cycle
  **per entry**, not per beat, which is what makes the mux affordable.

An occupant that adds combinational depth in front of its own first register is
extending a path the agent has already broken once, and gets no third stage.
What either stage is worth on a given part is in [projects/](../projects/).

## The default occupant

A project with no transform compiles `src/templates/transform/xform_bank.v`
instead of its own: every id is bypass, `IN_BITS = 4 × DATA_W`, `OUT_WORDS = 4`,
so a transform move through it is a copy. A slot whose empty state costs nothing
is a slot people leave in — the same property `noc_l2_adapter`'s `PASS=1` has.

**This is what keeps the framework free of any project.** `mag_xform`
instantiates `xform_bank` by name and that is the one module name the framework
fixes; if the only bank in the tree were a project's, no framework-only build
could elaborate. `tests/sysnode/xform_identity_tb.v` builds exactly that —
`kohakuaccel`, `templates` and `verif`, nothing else — and would fail to compile
if the rule were broken.

## Padding

**A bound axis is not available in a transform move.** A padded element issues no
read, and the occupant is fed a fixed `IN_BITS` off the read return, so a bound
axis would leave an entry a beat short forever. The mover raises fault 7 rather
than converting the wrong bytes.

This costs nothing in practice: a transform descriptor tiles to whole entries.
Padding remains available on every other mover mode, where `src_valid` low
injects the immediate.

## Related

- [arch/sysnode/transform-stage](../arch/sysnode/transform-stage.md) — what the stage
  is for, and the addon/fixture distinction
- [integrate/addon-slots](../integrate/addon-slots.md) — the four obligations a
  slot has to meet
- [spec/memory-protocol](memory-protocol.md) §10 — the retired request flags
- [projects/kohakutpu/number-format](../projects/kohakutpu/number-format.md) —
  the occupant, as one project's answer
