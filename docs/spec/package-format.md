---
title: Package format
summary: The work package a compile stores and a node's dispatcher runs — header, unit table, buffer table, step list, relocation table and the 256-bit unit words, byte for byte, and what binding and relocation do to them.
tags:
  - spec
  - normative
  - software
  - dispatcher
---

# Package format

> **Kind: Fixed** throughout, except the step *semantics* a project adds
> through its unit classes (§5.2), which are **Convention**.

A **package** is a unit of work a node runs on its own: the 256-bit instruction
words for the compute units, which unit each goes to, the order and the waits
between them, and where inside the words the memory addresses sit. It is
**storable** — nothing in it depends on where its operands happen to be on a
given call — and **relocatable**: the addresses are supplied when the package is
submitted (*binding*) and patched into the words by the dispatcher
(*relocation*). One compile therefore serves every call of a kernel at a shape.

The writer is `compiler/kohakuaccel/package/` (`format.py`, `build.py`); the
reader that runs on the node is `firmware/kohakuaccel/package/interp.c`, with the
offsets in `firmware/kohakuaccel/include/ka/package/format.h`; a reference
interpreter with the firmware's semantics is
`compiler/kohakuaccel/package/interp.py`. How a package reaches a node is
[node-queue.md](node-queue.md).

## 1. Layout

Little-endian throughout. Every offset is in bytes from the package's first
byte. The package and every section start on a **32-byte boundary**, and the
package's length is a multiple of 32 — the host writes card memory in 32-byte
granules and a shorter write zero-fills the rest of its granule.

```
   0x00  header        96 bytes, twelve 64-bit words
         units         n_unit    x 16 bytes
         buffers       n_buffer  x 32 bytes
         steps         n_step    x 16 bytes
         relocations   n_reloc   x 16 bytes, sorted by payload index
         payloads      n_payload x 32 bytes
```

The section order above is what the writer produces; a reader MUST use the
offsets in the header and MUST NOT assume the order.

### 1.1 Header

| Word | Bits | Field |
|---|---|---|
| 0 | `[31:0]` | magic `0x4B50414B` ("KAPK") |
| | `[47:32]` | version, `1` |
| | `[63:48]` | header bytes, `96` |
| 1 | `[63:0]` | machine signature (§3), `0` = any machine |
| 2 | `[31:0]` | total bytes |
| | `[63:32]` | flags: `[0]` checksum present |
| 3 | `[31:0]` | `n_payload` |
| | `[63:32]` | `n_reloc` |
| 4 | `[15:0]` | `n_buffer` |
| | `[31:16]` | `n_step` |
| | `[47:32]` | `n_unit` |
| | `[63:48]` | `ack_reserve` (§4.3) |
| 5 | | offset of the unit table |
| 6 | | offset of the buffer table |
| 7 | | offset of the step list |
| 8 | | offset of the relocation table |
| 9 | | offset of the payload table |
| 10 | | checksum: FNV-1a 64 over the whole package with this word read as zero; `0` when flag `[0]` is clear |
| 11 | | reserved, `0` |

A reader MUST refuse a package whose magic, version or header size it does not
know, and one whose sections run past the total.

### 1.2 Unit table — 16 bytes per unit

| Word | Bits | Field |
|---|---|---|
| 0 | `[7:0]` | x |
| | `[15:8]` | y |
| | `[23:16]` | mesh |
| | `[47:32]` | `CU_TYPE` — two ASCII characters ([control-registers.md](control-registers.md) §1.3) |
| 1 | `[31:0]` | credit: instructions the unit may hold in flight, normally its `inst_depth` |
| | `[63:32]` | fetch: `1 << 16 \| y << 8 \| x` names the memory port that streams this unit's `DISPATCH` payloads (§4.7); 0 sends them through the node's mailbox |

Steps name a unit by its **index** in this table. A unit the package only waits
on — a peer that acknowledges a transfer — is in the table too, so its
completions are attributed.

### 1.3 Buffer table — 32 bytes per buffer

| Word | Field |
|---|---|
| 0 | FNV-1a 64 of the buffer's name; diagnostic only |
| 1 | size in bytes |
| 2 | default address (unit-global), used when the submission binds none |
| 3 | `[7:0]` kind — 0 input, 1 output, 2 in/out, 3 temporary, 4 constant; `[15:8]` mesh |

### 1.4 Step list — 16 bytes per step

| Word | Bits | Field |
|---|---|---|
| 0 | `[7:0]` | opcode (§4) |
| | `[15:8]` | flags, `0` |
| | `[31:16]` | unit index, or a mesh index for the doorbell steps |
| | `[63:32]` | count |
| 1 | `[63:0]` | argument |

### 1.5 Relocation table — 16 bytes per entry

| Word | Bits | Field |
|---|---|---|
| 0 | `[31:0]` | payload index |
| | `[39:32]` | bit — the field's lowest bit in the 256-bit word, 0–255 |
| | `[47:40]` | width, 1–64 |
| | `[55:48]` | shift, 0–63 |
| | `[63:56]` | buffer index |
| 1 | `[63:0]` | addend, two's complement |

Entries MUST be sorted by payload index. A reader walks them with one cursor.

### 1.6 Payload table — 32 bytes per word

One 256-bit unit word, least significant 64 bits first: bytes 0–7 are payload
`[63:0]`, which is what the RV64 dispatch mailbox calls `ARG0`, up to bytes
24–31, `ARG3`, holding the unit's opcode in `[255:252]`
([control-registers.md](control-registers.md) §7.5). The routing header is not
stored: the dispatcher's mailbox builds it.

## 2. Binding and relocation

A submission carries an array of addresses, one per buffer — the **bindings**.
A binding of zero, or a buffer past the array's end, takes the buffer's default.

Every relocation entry then sets one field of one payload before that payload is
sent:

```
   value                       = binding[buffer] + addend
   payload[bit +: width]       = (value >> shift) mod 2^width
```

A **split field** — an address whose low bits sit in one place and whose high
bits sit in another, as the KohakuTPU encodings carry a 40-bit address as 34 + 6
— is two entries with the same buffer and addend and different `bit`, `width`
and `shift`.

The writer **clears** every relocated field in the stored payload. A package
built without defaults (every default zero) therefore names no address at all,
and two compiles of one kernel whose operands landed in different places produce
**byte-identical packages** — which is what lets a host keep a package resident
on the card and submit it again with new bindings.

Which bits of a word are addresses is the project's to say: the framework asks
`Backend.addresses(word, unit_type)` for `((bit, width, shift), ...)` and a
value, and relocates only the fields whose value falls inside a buffer it knows.
A field it does not relocate keeps the absolute address it was compiled with.

## 3. The machine signature

FNV-1a 64 over the machine's units, each written as the 64-bit unit word of §1.2
word 0 (little-endian, 8 bytes), **sorted ascending**. The node computes the
same over the unit table it was given at boot
([node-queue.md](node-queue.md) §2) and refuses a package whose nonzero
signature differs. A signature of zero is accepted anywhere; a package that
dispatches nothing needs none.

## 4. Steps

Steps run in order. The dispatcher keeps, per unit, the instructions **in
flight** (sent, not yet retired), and the completions **received** and
**expected**; every count starts at zero with the package.

| Opcode | Name | Unit | Count | Argument | What the node does |
|---|---|---|---|---|---|
| 0 | `END` | | | | stops reading steps |
| 1 | `DISPATCH` | unit | n | first payload | sends payloads `[arg, arg+n)` to the unit, relocated, each when credit allows (§4.1) |
| 2 | `AWAIT` | unit | n | | expected += n; waits until received ≥ expected |
| 3 | `BARRIER` | | | | waits until nothing is in flight and every AWAIT holds (§4.4) |
| 4 | `MOVER` | | n | first payload | issues mover register writes (§4.5) and waits for the moves they start |
| 5 | `RING` | mesh | tag | | waits for the mover to be idle, then rings that mesh's doorbell |
| 6 | `WAIT_BELL` | mesh | n | | waits until n doorbells from that mesh have arrived that no earlier `WAIT_BELL` consumed, and consumes them; the count runs from the firmware's start, so a ring that lands before the waiting package begins is still found |
| 7 | `SIGNAL` | | value | value | posts a progress completion to the host now ([node-queue.md](node-queue.md) §4) |
| 8 | `SETTLE` | | cycles | | holds that many cycles |
| 9 | `REPEAT` | unit | n | first payload, repeats, increments (§4.6) | sends the n template payloads `repeats` times, each as `DISPATCH` would; repetition r adds r × delta to every increment's field |
| 10 | `MWAIT` | | n | | waits until n of the package's moves (its GOs, counted from its start) are done |
| 11 | `ENGINE` | | n | first payload | copies n dispatch-engine entries into the engine (§4.8) |
| 12 | `FETCH` | unit | n | absolute address | the unit's fetch port reads n words from the address, in requests of at most 255 within its credit; they count as sent to it. Words a unit wrote while the package ran. A unit without a fetch port fails the package with `BAD_STEP` |

Step flags are `w0[15:8]`. `POSTED` (1) on a `MOVER` step starts its moves and
goes on: an `MWAIT`, a `BARRIER` or the end waits for them. The mover keeps no
queue, so a move's registers are written only once every earlier move is done.

Reaching the end of the list is an implicit `BARRIER`. Any other opcode fails
the package.

A run of consecutive `DISPATCH` and `REPEAT` steps naming distinct units goes
out **round-robin**, one payload of each in turn, so every unit starts within
one pass rather than after the whole programs ahead of it. Each unit's own
payloads stay in order, which is the only order a unit can observe. Boot flag
`SERIAL` ([node-queue.md](node-queue.md) §2) sends each step whole instead. A
step for a unit that fetches (§4.7) ends the run and goes on its own.

### 4.1 Credit

A payload goes out only while its unit holds fewer than its **credit** in
flight. The unit credit is the table's, lowered by the unit class's own bound
if it has one (§5.2); it is the deadlock rule of
[control-registers.md](control-registers.md) §2.4. A node-wide **cap** on
outstanding completions is optional — the boot block's `cq_depth` less
`ack_reserve`, none when `cq_depth` is zero: the dispatch mailbox holds a
completion it has no room for at the hub rather than dropping it, and the node
drains completions whenever it waits. Every credit is at least 1.

### 4.2 Completions

Each instruction sent is owed one completion from its unit. A completion's
signal code is read by the unit's class (§5.2); the framework's reading is:
`SIG_FAULT` (4) fails the package, `SIG_DATA_RECEIVED` (3) is an
**acknowledgement** — counted as received, but retiring nothing in flight — and
every other code retires one instruction. A completion from a coordinate not in
the unit table is counted and otherwise ignored, unless it is a fault.

### 4.3 `ack_reserve`

The most completions any one round (the steps between two barriers) awaits from
units beyond what it dispatched to them — peers acknowledging transfers. The
writer computes it; a node running with a cap holds that much room aside for
them.

### 4.4 Ordering

A compute unit sends a completion only after every memory write it issued has
been acknowledged, so a completion means the data landed. A `BARRIER` is
therefore the whole ordering between a round that writes and a round, or a
host, that reads. `SETTLE` remains for a wait the hardware does not express.
A write the memory port refuses — a reserved aperture, another mesh's staging —
is acknowledged as refused and the unit's completion arrives as `SIG_FAULT`,
which fails the package (§4.2) rather than leaving it waiting.

### 4.5 Mover payloads

A `MOVER` step's payloads each hold two `(register, value)` pairs: bytes 0–7
register, 8–15 value, 16–23 register, 24–31 value. A register of all ones is
skipped. Registers are the mover's command offsets
([control-registers.md](control-registers.md) §3); a write to `0x00` with bit 16
set is a GO, and the step waits until as many moves as GOs have completed. The
processor reaches the mover's whole map, `0x00`–`0x7F`, in its control region
at `0x100` + offset; a register outside it, or not 8-byte aligned, fails the
package with `NO_REACH`.

### 4.6 `REPEAT`

The argument is `first | repeats << 32 | increments << 48`. Payloads
`[first, first + n)` are the template, relocated like any payload; the
`increments` entries follow it, two per payload, in template order (index
non-decreasing): bytes 0–7 `index | bit << 32 | width << 40` (index into the
template, field position and width, `width` ≤ 64 and `bit + width` ≤ 256),
bytes 8–15 the delta. Repetition r sends template
word `index` with `r × delta` added to that field modulo 2^width; the writer
guarantees no repetition wraps a field. A program whose K loop is periodic is
one template and its address steps, so its size does not grow with K.

### 4.7 Fetch

When a package carries no relocations and a unit's table entry names a fetch
port, a `DISPATCH` to it is not sent word by word. The node sends that memory
port one streamed `MEM_RD_REQ` per at most 255 payloads, flags `STREAM | INST`,
one-word entries, peer 0 the unit ([flit-format.md](flit-format.md) §4.1.1);
the port delivers each payload to the unit as a `CU_INST` whose source is the
node, so completions return to the node as if it had sent them. The payloads are
read straight out of the package in memory, which is why relocations rule it
out. The words count against the unit's credit as they would sent one at a
time, bounded further by the unit's 512-entry instruction queue: a word the unit
has no room for would hold the port's whole response stream. `REPEAT` steps are
always sent by the node, so a writer that fetches does not compress. Boot flag
`NOFETCH` ignores every fetch port. Measured on the v9 card (1024x512x1024
matmul): 400 words in 4 requests, clusters never starved, 69.9% of peak against
70.9% with every word queued before the clusters start.

### 4.8 The dispatch engine

A node with a dispatch engine ([dispatch-engine.md](dispatch-engine.md)) runs a
package through it. Every step it can carry is queued as the register writes and
`WAIT`s it stands for; the engine issues them while the node reads ahead, and
counts each unit's completions itself. `WAIT_BELL`, `SIGNAL` and `SETTLE` let
the engine drain, then run as above. Boot flag `NOENGINE` keeps every package on
the firmware path. A package with more than 16 units, or a unit class with its
own completion reading (§5.2), runs on the firmware path too.

The queueing is the firmware's work per step unless the package carries it
already: an `ENGINE` step's payloads hold three entries each, word 0 their code
bytes (`0xFF` for none) and words 1–3 their values. A code is the engine's
(0–31 a register, 32 a `WAIT`), plus `0x40` when the value is a fetch request's
`ARG3` and the node adds the package's payload address, shifted left 24, to it.
A code past `WAIT` fails the package with `BAD_STEP`, as does an `ENGINE` step on
a node that runs the package without an engine. The writer lowers a bound
package into `ENGINE` steps (`kohakuaccel/package/engine.py`, `Runtime.
engine_packages`): it tracks each unit's words sent, credit and `AWAIT`s exactly
as the firmware would, spells `REPEAT` out into payloads it fetches, and ends
the stream with the end's barrier. The node then decodes nothing; measured on
the v9 card, a firmware-translated `DISPATCH` costs it ~360 cycles, an `AWAIT`
~150, a `MOVER` of 11 registers ~1,380.

## 5. What a project adds

### 5.1 The words

Everything inside a payload is the unit's own encoding. The framework moves it
and patches the fields §2 names; it reads nothing else.

### 5.2 Unit classes — Convention

The node looks up each unit's `CU_TYPE` in a table of **unit classes** the
firmware image carries (`firmware/kohakuaccel/include/ka/unit/unit.h`): a name,
an optional credit bound, how to read a completion's code (§4.2 is the default)
and how to describe a fault. A type with no class gets the framework's generic
reading. KohakuTPU registers `MG` and `VC` (`firmware/kohakutpu/units/`).

## 6. Failure

A package that fails stops at the failing step. The node then lets what is in
flight retire (bounded by the same timeout) and empties the mailbox, so the next
package starts clean, and reports the status, the step index and a detail word
([node-queue.md](node-queue.md) §4).

## 7. How a runtime builds packages

A `Runtime` whose `node` is set dispatches nothing from the host. Each stage —
one `dispatch` call, one `move`, one `wait_bell` — is appended to an **open
package**, and the open package runs on the node at the next `read`, `write` or
`sync`. A host transfer is the only point the host and the node must agree on
memory, so a whole call between two transfers is one package and one round
trip.

**A barrier closes every stage.** The next stage reads what this one wrote, and
the runtime does not know which stages are independent. `ring` is the one step
not closed by a barrier: it already waits for the mover to be idle (§4).

**Per-call words enter the framework pipeline.** A frontend that encodes per
call (`kohakutpu.lang`) hands over `{instance: [words]}` for one stage. Each
instance becomes a pinned task, dealt round-robin over the stage's units as
`dispatch.plan` deals them, and Place, Pack, Coalesce and Emit run over it with
the `Prebuilt` backend, whose `encode` returns the words unchanged. The node
streams a unit's words under credit (§4.1), so the staging, command and FIFO
bounds that split host rounds are lifted: one stage is one round.

**Acknowledgements are awaited once per unit.** A task's `acks` names peers that
acknowledge a transfer it makes. Emit folds them into the round's `AWAIT`s: a
unit both dispatched to and acknowledging is awaited once, for its own
completions plus the acknowledgements, because `AWAIT` counts are cumulative
(§4) and one total is the same wait as two parts.

**Relocation makes packages reusable.** The runtime passes the arena's live
allocations to the builder; a field the project's `addresses` reports inside
one becomes a relocation (§2), and the package is built with no defaults. Two
calls of one kernel then build byte-identical packages, which a node can keep
and run again with new bindings. With no `fields` backend, packages are bound to
the addresses they were built at.

**Uploads come before conversions.** An upload is a host `write`, so it runs the
open package. A kernel therefore uploads every input before it issues any
on-card conversion, keeping a call's conversions and compute in one package. An
input whose order the card derives (a layout's `derived_from`, as KohakuTPU's
MXFP7 entries) is uploaded in its source order to an extra allocation, and the
conversion step writes the derived order from it.
