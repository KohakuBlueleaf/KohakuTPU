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
bound on the host and patched into the words before submission. The dispatcher
refuses any package carrying relocation entries (`BAD_RELOC`, detail 0, value
the relocation count). One compile therefore serves every call of a kernel at
a shape; the relocation table is host metadata, not firmware work.

The writer is `software/rust/crates/ktpu-l0/src/` (`format/`, `build/`); the reader that runs on the node is
`software/firmware/kohakuaccel/package/interp.c`, with the offsets in
`software/firmware/kohakuaccel/include/ka/package/format.h`; a reference
interpreter for package construction is
`software/simulation/kohakuaccel/simulation/package.py`. How a package reaches a node is
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
| | `[47:32]` | version, `2` |
| | `[63:48]` | header bytes, `96` |
| 1 | `[63:0]` | machine signature (§3), `0` = any machine |
| 2 | `[31:0]` | total bytes |
| | `[63:32]` | flags: `[0]` checksum present, `[1]` the package `RECV`s (§4.13): flits in the node's RX queue are its own, not strays to drop |
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
| | `[15:8]` | flags (§4) |
| | `[31:16]` | unit index, a mesh, a slot, a register or a step, by opcode (§4) |
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
| 5 | `RING` | mesh | slot | amount `[15:0]` (0 counts 1), fence `[16]` | rings that mesh: its signal slot gains the amount and its doorbell count from this mesh one. Fenced, the ring leaves only once this node's mover is idle and its outbound writes and processor stores are on the link -- the release for them; it does not cover a compute unit's flits still inside a mesh. The node keeps at most four rings queued (a fifth is dropped by the interlink) |
| 6 | `WAIT_BELL` | mesh | n | | waits until n doorbells from that mesh have arrived that no earlier `WAIT_BELL` consumed, and consumes them; the count runs from the firmware's start, so a ring that lands before the waiting package begins is still found |
| 7 | `SIGNAL` | | value | value | posts a progress completion to the host now ([node-queue.md](node-queue.md) §4) |
| 8 | `SETTLE` | | cycles | | holds that many cycles |
| 9 | `REPEAT` | unit | n | first payload, repeats, increments (§4.6) | sends the n template payloads `repeats` times, each as `DISPATCH` would; repetition r adds r × delta to every increment's field |
| 10 | `MWAIT` | | n | | waits until n of the package's moves (its GOs, counted from its start) are done |
| 11 | `ENGINE` | | n | first payload | copies n dispatch-engine entries into the engine (§4.8) |
| 12 | `FETCH` | unit | n | absolute address | the unit's fetch port reads n words from the address, in requests of at most 255 within its credit; they count as sent to it. Words a unit wrote while the package ran. A unit without a fetch port fails the package with `BAD_STEP` |
| 13 | `WAIT_SLOT` | slot | n | | waits until signal slot `slot` (0-15) holds n; `CONSUME` then subtracts n (§4.9) |
| 14 | `STORE` | | n | first payload | n payloads of (address, value) pairs, as `MOVER`'s: 64-bit stores in order (§4.10) |
| 15 | `LOAD` | register | index register | payload | register := the 64 bits at the address in the payload's `[63:0]` (§4.11) |
| 16 | `ALU` | rd | op `[7:0]`, ra `[11:8]`, rb `[15:12]` | immediate | rd := ra op b (§4.11) |
| 17 | `JUMP` | target step | cond `[7:0]`, ra `[11:8]`, rb `[15:12]` | immediate | the next step is `target` when ra cond b (§4.11) |
| 18 | `PATCH` | register | word index | where (§4.12) | a field of a payload or a step := the register, before a later step reads it |
| 19 | `SEND` | unit | n | first payload `[31:0]`, flit type `[35:32]` | n flits of that type to the unit, outside credit and completions (§4.13) |
| 20 | `RECV` | unit, or `0xFFFF` any | n | payload | n flits from the node's RX queue, each payload to the address in the payload's `[63:0]` + 32 i (§4.13) |
| 21 | `CTRL` | | n | first payload | n payloads of (offset, value) pairs: control-region writes (§4.14) |

Step flags are `w0[15:8]`:

| flag | bit | on |
|---|---|---|
| `POSTED` | 0 | `MOVER`: start the moves and go on; an `MWAIT`, a `BARRIER` or the end waits for them |
| `IMM` | 1 | `ALU`, `JUMP`: operand b is the argument, not register rb |
| `REG` | 2 | `STORE`, `CTRL`: a pair's value is register `value & 15` |
| `INDEXED` | 3 | `LOAD`: the address adds the register in `count[3:0]` |
| `CTRL` | 4 | `LOAD`: the address is a control-region offset (§4.14) |
| `CONSUME` | 5 | `WAIT_SLOT`: take n from the slot once reached |
| `MEM` | 6 | `SEND`: the first payload is a `CU_DATA` descriptor, then n words read from the address in the next payload's `[63:0]` |
| `ACK` | 7 | `SEND`: wait for the unit's `SIG_DATA_RECEIVED` |

The mover takes its registers into a configuration queue, so a later move's
registers wait behind an earlier move's GO.

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
package into `ENGINE` steps (`ktpu_l0::engine::lower`): it
tracks each unit's words sent, credit and `AWAIT`s exactly
as the firmware would, spells `REPEAT` out into payloads it fetches, and ends
the stream with the end's barrier. The node then decodes nothing; measured on
the v9 card, a firmware-translated `DISPATCH` costs it ~360 cycles, an `AWAIT`
~150, a `MOVER` of 11 registers ~1,380.

A package holding a `JUMP` or a `PATCH` is not lowered: its steps may run
again or read words the node rewrites, so the counts an `ENGINE` entry fixes
would be wrong. The node queues its steps into the engine as it reaches them,
from counts it keeps itself. `ALU`, `JUMP` and `PATCH` run without waiting for
the engine; every other step past the engine's reach (`WAIT_BELL`, `SIGNAL`,
`SETTLE`, `WAIT_SLOT`, `STORE`, `LOAD`, `SEND`, `RECV`, `CTRL`) lets it drain
first, so it acts after everything queued before it.

### 4.9 Signal slots

A node's interlink holds sixteen 32-bit signal slots
([control-registers.md](control-registers.md) §4). A `RING` adds its amount to
the destination's slot; `WAIT_SLOT` selects the slot, waits until it holds n,
and with `CONSUME` subtracts n, so one slot carries a stream of releases (a
producer rings 1 a tile; the consumer waits for 1 and takes it). A slot only
grows unless consumed, so "holds n" is a monotonic test. `WAIT_BELL` remains
for the per-source doorbell count, which every ring also raises.

### 4.10 Stores

Each pair is a 64-bit store through the processor's uncached path, completed
before the next. An address naming another mesh in `[37:36]` is a posted
remote write over the interlink, landing by the remote-write rule (staging by
its full address, DRAM by its low 32 bits); a fenced `RING` after it is its
release. Byte strobes are honoured where the destination honours them:
staging does, the card's DRAM endpoint does not. Addresses relocate as a
`MOVER` payload's do, a pair's address at `[63:0]` of its half.

### 4.11 Registers

A package has sixteen 64-bit registers, zero at its start. `LOAD` reads 64
bits at an address (8-byte aligned; through the uncached path, so it sees what
a unit wrote) or, with `CTRL`, a control register. `ALU` ops: 0 `MOV` (rd :=
b), 1 `ADD`, 2 `SUB`, 3 `MUL`, 4 `AND`, 5 `OR`, 6 `XOR`, 7 `SHL`, 8 `SHR`
(logical), 9 `MIN`, 10 `MAX` (unsigned), 11 `DIVU` (by zero: all ones), 12
`REMU` (by zero: ra). `JUMP` conditions: 0 always, 1 `EQ`, 2 `NE`, 3 `LTU`, 4
`GEU`, 5 `LT`, 6 `GE` (signed). A target is a step index, at most the step
count (which ends the package). `AWAIT` counts are increments, so a loop's
waits stay right however many times it runs; an `MWAIT`'s count is absolute
and does not belong inside a loop.

### 4.12 `PATCH`

The argument says where: `[7:0]` bit, `[15:8]` width (1-64), `[23:16]` shift,
`[24]` section (0 a payload, 1 a step), `[26]` add. The field
`[bit, bit+width)` of word `count` of that section becomes `register >> shift`
mod 2^width, or that plus its old value with add. A split field is one
`PATCH` a segment. The node writes the package where later steps read it: its
local copy, and the package in memory, which a fetch port reads. A field a
relocation sets is not patched.

A `DISPATCH` to a fetching unit is read by its port after the node has queued
it, so a word patched again in a loop must not still be on its way: the
writer waits for the unit before re-patching its words.

### 4.13 `SEND` and `RECV`

`SEND` puts raw flits on the mesh through the node's mailbox: a `CU_CTRL`
request (index at `[247:240]`; the reply comes back as an RX flit), a
`CU_DATA` burst (a descriptor and its data words, or with `MEM` a descriptor
and n words read from memory), or any other type. None counts against credit
or as a completion; with `ACK` the step waits for the unit's
`SIG_DATA_RECEIVED`, which a burst whose ack is `(0, 0)` sends to its
source, the node. The mailbox sets `last` on every flit it sends.

`RECV` takes flits a unit sent to the node's coordinate `(0, 0)` -- a peer
drain, a `CU_CTRL` reply -- writing each one's 256-bit payload at
`address + 32 i`. With `unit` named, a flit from anywhere else fails the
package. The RX queue is 8 deep and holds the node's inbound link while
full, so a package keeps its `RECV` close behind the unit that sends.

### 4.14 Control-region access

`CTRL` writes the host interrupt (`0x38`), the interlink window
(`0xC0`-`0xF8`: enable and clears, mesh id, ring, consume, slot select), the
D-cache (`0x1C8`, waited until done) and the transform bank (`0x1D0` select,
`0x1D8` data); any other offset fails `NO_REACH`. A `LOAD` with `CTRL` reads
any control register (`0x20` mover status, `0x28` doorbell counts, `0xE0` the
selected slot's signal word, `0x1D8` the transform bank's selected register).

## 5. What a project adds

### 5.1 The words

Everything inside a payload is the unit's own encoding. The framework moves it
and patches the fields §2 names; it reads nothing else.

### 5.2 Unit classes — Convention

The node looks up each unit's `CU_TYPE` in a table of **unit classes** the
firmware image carries (`software/firmware/kohakuaccel/include/ka/unit/unit.h`):
a name, an optional credit bound, how to read a completion's code (§4.2 is the
default) and how to describe a fault. A type with no class gets the framework's
generic reading. KohakuTPU registers `MG` and `VC`
(`software/firmware/kohakutpu/units/`).

## 6. Failure

A package that fails stops at the failing step. The node then lets what is in
flight retire (bounded by the same timeout) and empties the mailbox, so the next
package starts clean, and reports the status, the step index and a detail word
([node-queue.md](node-queue.md) §4).

## 7. How a compiler builds packages

A package is an L1 program lowered ([l1-program.md](l1-program.md) §3):
`ktpu_l0::Program::build` returns a `PackageBuilder`, and
`ktpu_l0::engine::lower` turns its steps into `ENGINE` steps.
The `ktpu` pipeline builds relocatable packages; `_rs.bind` binds addresses
and optionally lowers the package for dispatch.

**Acknowledgements are awaited once per unit.** A unit both dispatched to and
acknowledging is awaited once, for its own completions plus the
acknowledgements, because `AWAIT` counts are cumulative (§4) and one total is
the same wait as two parts.

**Relocation makes packages reusable.** With `addresses` (the project's address
fields, ``f(word, unit_type)``) and `spans` (``{base: size}``), every address
field inside a span becomes a relocation (§2), and the package is built with no
defaults. Two calls of one kernel then build byte-identical packages, which a
node can keep and run again with new bindings. Without them, a package is bound
to the addresses it was built at.
