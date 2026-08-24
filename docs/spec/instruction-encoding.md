---
title: Instruction encoding
summary: The three owners of instruction bits — the NoC header, the memory agent's instruction set, and the compute unit's payload — and which bits belong to each.
tags:
  - spec
  - normative
  - isa
  - cu-inst
---

# Instruction encoding

> **Kind: mixed, and marked per section.**
>
> | Section | Kind |
> |---|---|
> | §3 — what the mesh reads of a `CU_INST` header | **Fixed** |
> | §4 — the memory agent's request encoding and mover commands | **Fixed** |
> | §4.3 — which occupant the mover's transform id selects | **Addon** |
> | §5 — the ownership split inside the payload | **Fixed** |
> | §6 — retirement | **Fixed** |
> | §8 — how a unit spends its own 256 bits | **Convention, free.** The agent forces none of it. |

**The instruction space is shared. A compute unit does not own it; it occupies a
region of it.**

That is the single most important thing on this page, and the reason it is worth
stating before anything else is practical rather than territorial: **the memory
instructions already exist.** A compute-unit author is not designing an
instruction set from scratch. They are adding compute semantics to one that
already handles addressing, movement, layout and format conversion. Designing a
`FETCH` opcode that duplicates what a `MEM_RD_REQ` descriptor already does is the
most common way to waste a week here.

This document also exists because the split was never written down. `noc_pkt.vh`
declares one; every compute unit in the tree implements a different one; nothing
reconciles them. §5 is the reconciliation, and it is normative.

## 1. The three owners

| # | Owner | Where the bits live | What they encode |
|---|---|---|---|
| 1 | **The mesh** — `kohakuaccel/noc` | The 32-bit flit header of every message | Routing, message class, tag, framing. §3. |
| 2 | **The system node** | The payload of `MEM_RD_REQ` / `MEM_WR_REQ`, and the memory mover's command registers | Memory movement: addresses, extents, entry geometry, multicast, and the mover's transform selection. §4. |
| 3 | **The compute unit** — yours | The payload of `CU_INST` | Compute semantics. Everything the framework refuses to define, because defining it would decide what the unit computes. §5. |

Owners 1 and 2 are **fixed contract**. Owner 3 is **served**: the framework will
not read a bit of it, now or later, without a revision of this specification.

A compute unit's instruction set is therefore not "the CU payload". It is the CU
payload **plus** the memory instructions the unit issues, and a unit design that
does not say which memory instructions it emits is incomplete.

## 2. An instruction is one flit

A `CU_INST` message is exactly **one** flit. `noc_cu_base` pops one flit per
instruction and hands it to the datapath as `inst_flit`; there is no mechanism
anywhere in the framework that gathers several flits into one instruction.

| Quantity | Value at the defaults |
|---|---|
| Instruction body available to a unit | 256 bits |
| Instruction bodies per flit | 1 |
| Flits per instruction | 1 |

A unit whose instruction does not fit in 256 bits **MUST** split it into several
instructions that each retire. It **MUST NOT** attempt a continuation flit: the
second one would be delivered as a separate instruction, retire separately, and
return a separate dispatch credit.

The same holds in the other direction. A memory operation is **not** a `CU_INST`.
It is a `MEM_*` message the unit sends on its own `send_*` path, in the middle of
executing whatever `CU_INST` asked for.

## 3. Owner 1 — what the mesh reads

On a `CU_INST` flit the framework reads **the header, and nothing else**.

| Field | Read by | Used for |
|---|---|---|
| `dst_x`, `dst_y` | the routers | Delivery. Overwritten by the dispatcher from `PROG_DST`. |
| `src_x`, `src_y` | `noc_cu_base` | The address the completion is sent to. Overwritten by the dispatcher with the orchestrator's own coordinates. |
| `type` | `noc_cu_base` | Steering into the instruction FIFO rather than the receive FIFO. |
| `txn` | `noc_cu_base` | Program identifier. Returned as the argument of `SIG_BATCH_COMPLETE`. |
| `last` | `noc_cu_base` | Marks the final instruction of a program, which selects `SIG_BATCH_COMPLETE` over `SIG_INST_COMPLETE`. |
| `rsvd` | — | Reserved. MUST be 0. |
| `payload[255:0]` | **nothing** | — |

Two consequences for a compiler back end:

- **`dst` and `src` in a staged instruction are don't-care.** The orchestrator's
  dispatcher stamps the destination from `PROG_DST` and the source with its own
  coordinates before pushing the flit. Whatever the back end writes there is
  discarded. This is what lets one staged program be dispatched to several nodes.
- **`type`, `txn`, `last` and the payload pass through unchanged.** The back end
  owns all four. `txn` is the program identifier and `last` MUST be set on the
  final instruction of a program, or a host waiting for `SIG_BATCH_COMPLETE`
  waits forever.

## 4. Owner 2 — the memory agent's instruction set

These are instructions a compute-unit author **uses**, not instructions they
define. Read this section before allocating a single bit of your own.

The full protocol — ordering, backpressure, faults, the write reassembly rules —
is in [memory-protocol.md](memory-protocol.md). What follows is the encoding
surface: what can be asked for, and where it is written.

### 4.1 The read descriptor

One `MEM_RD_REQ` flit states an entire fetch. Field positions are in
[flit-format.md](flit-format.md) §4.1.

| Field | What it expresses |
|---|---|
| `addr` | Byte address, **40 bits** — the whole physical map, including the aperture bit and the mesh id. [address-map.md](../address-map.md). |
| `len` | Beats minus one, on the plain-read path. |
| `flags[6]` `STREAM` | This descriptor covers `count` consecutive **entries**, not one fetch. |
| `count` | How many, up to 255. |
| `entry_words` | Words per entry. The entry geometry is stated per request, so a client whose line is not the default size streams without changing anything in the agent. |
| `flags[4]`, `flags[5]` | **Reserved and ignored.** Were `QUANT` and `BLAYOUT`; a fetch is never transformed. §4.3. |
| `peer`, `n_peer` | Up to three **extra destinations** for the response. The entry is read once. |
| `txn` | The requester's tag. Responses carry it plus the entry index, so nothing needs a cursor. |

What this buys a unit author, stated plainly: **there is no fetch loop to write.**
One flit names a whole run, the agent streams it, and every response flit names
its own destination slot. A unit that issues one request per entry pays a mesh
round trip per entry for nothing.

### 4.2 The write descriptor

One `MEM_WR_REQ` flit followed by its data flits.

| Field | What it expresses |
|---|---|
| `addr` | Byte address of the burst. |
| `len` | Beats minus one, **at most 7**. |

Writes have no flags and no streaming form. Neither reads nor writes select a
transform — see §4.3.

### 4.3 Transform selection

> **Kind: Addon.** The selection encoding and the delivery shape are Fixed. What
> the transform *does* is yours.

**A memory request does not select a transform.** `flags[4]` and `flags[5]` used
to be `QUANT` and `BLAYOUT` and are now reserved and ignored; a requester that
sets them gets an untransformed read.

The transform slot belongs to the **memory mover**, and selection rides in its
descriptor:

| field | width | who reads it |
|---|---|---|
| `XFORM_ID` | `ID_W` | the agent — 0 is bypass, *k* routes to occupant *k* |
| `XFORM_MODE` | `MODE_W` | **the occupant only**; the agent carries it and never interprets it |

Selection is an **id, not a bit per transform**, so a design with several
occupants picks one rather than encoding a mask. The framework fixes that the
occupant declares its own geometry — `IN_BITS` and `OUT_WORDS` — because the
mover has to size both walks before the transform has run.

KohakuTPU's occupant at id 1 is a FP16-to-block-format quantiser with a shared
E5M3 scale, 2048 bits in and 1024 out. Nothing in this encoding names that
format.

There is no postprocess hook on the write path. A move converts on its read
side; a drain is written verbatim.

### 4.4 The memory mover

A second instruction set in the system node, reached by **register writes rather
than by a flit**: a layout, gather, fill and transform engine with its own AXI
master and no NoC endpoint. Two things write those registers — the host through
the control window, and the control processor through a private port of its own
— and the register map is the same either way.
[control-registers.md](control-registers.md) §3.

| Mode | | What it does |
|---|---|---|
| `COPY` | 0 | Move a region, source walker stepped in lockstep with the destination walker. |
| `TRANSPOSE` | 1 | Allocated, **faults if requested**. Not implemented, and not needed: every index permutation is affine, so a transpose is `COPY` with the two walkers in different orders. |
| `GATHER` | 2 | Indexed read, indices loaded from memory into an on-chip buffer. |
| `GENERATE` | 3 | Fill from a counter-based PRNG, so the bytes are a pure function of `(seed, destination address)`. |
| `FILL` | 4 | Fill with an immediate. |
| `XFORM` | 5 | A `COPY` whose read return passes through the transform slot. §4.3. |

Both source and destination are 6-dimensional descriptors with per-axis counts
and strides. The **destination** defines the iteration space and the source is
stepped alongside it, which is what makes a source stride of zero a broadcast
with no extra mode; a source element whose bounds check fails injects the
immediate, which is how padding works.

**`XFORM` inverts that**, and it has to: the entry size is the occupant's, so
the SOURCE walker defines the iteration space and the destination steps once per
entry. A bound axis is therefore unavailable on a transform move — a padded
element issues no read, and the occupant would wait for a beat that never comes
— so the mover raises fault 7 rather than converting the wrong bytes.

The mover is word granular: every transfer is one `DATA_W`-bit beat, so strides
must be multiples of `DATA_W/8` and a misaligned descriptor faults rather than
moving the wrong bytes.

**An ordinary compute unit cannot command the mover**, and that is deliberate:
the mover has one walker and no arbitration for it, so a unit that needs data
rearranged asks for it between programs rather than inside one.

The exception is the **control processor**, and it is an exception by wiring
rather than by protocol. It sits on the mesh as a compute unit like any other,
and it also has two private wires inward that no other unit has: the mover's
config port, and a requester slot on the agent's converged path. Nothing it does
travels as a flit, so no `CU_INST` encoding covers it and nothing in this section
changes. See [arch/sysnode/control-processor](../arch/sysnode/control-processor.md).

## 5. Owner 3 — the split inside `CU_INST`

Normative, and it resolves the conflict in §7.

| Region | Owner | Rule |
|---|---|---|
| header `dst_*`, `src_*` | mesh | A unit MUST NOT read them for semantic purposes. The dispatcher rewrites both. |
| header `type` | mesh | Fixed at `0x5`. |
| header `txn` | mesh | Program identifier. A unit MAY read it; it MUST NOT redefine it. |
| header `last` | mesh | Batch marker. A unit MUST NOT repurpose it. |
| header `rsvd` | reserved | MUST be 0. `rsvd[2]` is the remote-mesh marker and has no meaning on a `CU_INST`; setting it would divert the instruction to the interlink. |
| payload `[255:0]` | **unit** | Entirely the unit's. The framework reads none of it, and MUST NOT begin reading any of it without a revision of this specification. |

**There is no framework-reserved region inside the `CU_INST` payload.** That is
the resolution, and it is chosen deliberately over the alternative:

- It matches the silicon. No module reads a payload bit of a `CU_INST`.
- It matches where the framework's own instruction bits already live. Owners 1
  and 2 have their own encodings, in the header and in the `MEM_*` payloads. The
  framework does not need a third foothold inside the unit's word.
- The alternative — honouring `noc_pkt.vh`'s `inst_len` / `inst_class` — would
  declare all three compute units in the tree nonconformant for a field that
  exists to describe multi-flit instructions the framework cannot deliver (§2).

If multi-flit instructions are ever added, the length **MUST NOT** be carved out
of a payload byte that units have already spent. The framework has unread space
of its own — `rsvd`, and the unallocated type codes `0x9`–`0xE` — and any future
mechanism belongs there.

## 6. Retirement and what it reports

A unit's encoding choices interact with retirement in exactly three places.

| Framework signal | Unit controls | Notes |
|---|---|---|
| `SIG_INST_COMPLETE.arg` | `exec_result[31:0]` | Sampled at `exec_done`. Free for anything: a cycle count, a running total, a sequence number. |
| `SIG_BATCH_COMPLETE.arg` | nothing | The framework substitutes `{24'd0, txn}`. A unit's `exec_result` is **discarded** on the final instruction of a program. A unit that needs a value out of its last instruction MUST NOT rely on the batch completion to carry it. |
| `SIG_FAULT.arg` | `exec_result[31:0]` | Sampled at `exec_done` when `exec_fault` is high. Fault codes are unit-defined; the framework allocates none. |

There is no per-instruction status other than these. There is no mechanism by
which a unit rejects an instruction: an unrecognised opcode **MUST** still retire,
either as a fault or as a no-op. A unit that stalls on an opcode it does not
know will hold its dispatch credit forever.

## 7. Known divergence: the declared split nobody implements

`src/kohakuaccel/noc/noc_pkt.vh` declares:

```
`define NOC_INST_LEN   255:248
`define NOC_INST_CLASS 247:240
`define NOC_INST_BODY  239:0
```

Facts:

- No RTL reads any of the three macros. `noc_pkt.vh` is included by no module.
- Every compute unit in the tree reads its opcode from `inst_flit[255 -: 4]` —
  the top four bits of the field named `inst_len`.
- `inst_len` describes a continuation mechanism the framework does not implement
  (§2), and `inst_class` has no consumer.

§5 resolves this in favour of the silicon: the payload is unit-owned and the two
macros describe a reservation that has been withdrawn. They should be removed
from `noc_pkt.vh` so the header stops asserting a claim nothing enforces.

## 8. Example: how KohakuTPU spends the 256 bits

> **Kind: Convention, and free.** The memory agent forces none of this. A unit
> may lay its payload out any way it likes.

**Illustrative only.** Nothing below is part of the contract. It is one project's
use of a field the framework leaves free, and a second accelerator on this
framework will and should look nothing like it. The layouts are reproduced here
because the only place they are currently written down is a header comment
inside a compute-unit source file, which is the wrong place for anything anyone
has to look up.

Note in passing how little of each encoding is about memory: addresses appear,
but extents, entry geometry, multicast and format conversion are all expressed by
the `MEM_RD_REQ` descriptor the unit emits, not by the instruction. That is §1
working.

### 8.1 `mx_cluster_cu` — matmul cluster

Opcodes: 1 `FILL`, 2 `GEMM`, 3 `DRAIN`.

| Bits | Field | Used by | Meaning |
|---|---|---|---|
| `[255:252]` | `op` | all | The opcode. |
| `[251:218]` | `addr` | FILL, DRAIN | Operand base, or destination base — the **low 34 bits**. The other six are at `[68:63]`. |
| `[217:202]` | `n` | FILL, DRAIN | Entries, or sub-tiles. 16 bits because a resident tile holds 512. |
| `[201]` | `sel` | FILL | 0 = A operand, 1 = B. |
| `[200]` | `acc` | GEMM | Accumulate into the resident tile instead of reloading it. |
| `[199:192]` | `gm` | GEMM | Row groups. |
| `[191:184]` | `gn` | GEMM | Column groups. |
| `[183:176]` | `nk` | GEMM | K blocks. |
| `[175:168]` | `anchor` | GEMM, DRAIN | Common output exponent. |
| `[167:144]` | `peers` | FILL | Other clusters receiving this fetch, `{y,x}` each. Copied straight into the read descriptor's `peer` field. |
| `[143:142]` | `npeer` | FILL | How many are present. |
| `[141]` | `preq` | FILL | **Reserved.** Every operand is already converted in memory — the cluster drives the retired `QUANT` bit to 0 unconditionally. |
| `[140:133]` | `eoff` | FILL | First local entry to write. Becomes the request's `txn`. |
| `[132:125]` | `aoff` | GEMM | Base entry on the A side. |
| `[124:117]` | `boff` | GEMM | Base entry on the B side. |
| `[116]` | `emit` | GEMM | Hand each sub-tile out as its last K block completes it. |
| `[115]` | `fuse` | DRAIN | Wait for results the sweep already emitted rather than issuing reads. |
| `[114]` | `abank` | GEMM | Which half of the A buffer this sweep reads. |
| `[113]` | `bbank` | GEMM | …and of B. |
| `[112]` | `fbank` | FILL | Which half this fill writes. |
| `[111]` | `dnode` | DRAIN | Send to a mesh node instead of to memory. |
| `[110:107]` | `dst_x` | DRAIN | The node. |
| `[106:103]` | `dst_y` | DRAIN | |
| `[102:95]` | `dbuf` | DRAIN | The destination's `buf_id`. See [flit-format.md](flit-format.md) §4.7.1 — this is a **framework namespace**, not a free field. |
| `[94:87]` | `dflags` | DRAIN | Copied verbatim into the `CU_DATA` descriptor's flags byte. |
| `[86:83]` | `dack_y` | DRAIN | Where the receiver sends its completion; 0 means back to this cluster. |
| `[82:79]` | `dack_x` | DRAIN | |
| `[78:77]` | `dmesh` | DRAIN | Destination mesh, meaningful only when `dfin` is nonzero. |
| `[76:69]` | `dfin` | DRAIN | `{fin_y, fin_x}` in that mesh. Nonzero is what makes the drain remote. |
| `[68:63]` | `addr_hi` | FILL, DRAIN | `addr[39:34]`. See below. |
| `[62:0]` | — | | Unused. |

Two encoding decisions worth copying, both of which are about **not breaking old
programs**:

- Zero means the previous behaviour, everywhere. `dnode = 0` is the memory port;
  `abank = 0` is the lower half; `dfin = 0` is a local drain. An instruction
  written before any of these bits existed still means what it did.
- New fields are appended downward into unused space rather than overlapped onto
  an existing field's spare bits.

**THE ADDRESS IS SPLIT, AND THAT IS THE SECOND RULE APPLIED TO A WIDENING.**
Going from 34 bits to 40 could have moved every field below `addr`; instead the
six new bits went to the tail, at `[68:63]`, and `addr` stayed exactly where it
was. All-zero `addr_hi` is mesh 0, DRAM — which is what every pre-40-bit
encoding carries, so old programs still mean what they meant.

The cost is that **a reader must rejoin them**. Taking `[251:218]` alone drops
the aperture bit and the mesh id, so a staging or a remote address decodes as
local DRAM at the same offset: a legal read of the wrong window, not a fault.
`isa/cluster.py`'s `_addr` splits, `model.py`'s `full_addr` rejoins, and
`mx_cluster_cu.v:268` is `{inst_flit[68 -: 6], inst_flit[251 -: 34]}`.

### 8.2 `vec_cu` — vector core

Opcodes: 1 `IMEM`, 2 `DESC`, 3 `RUN`.

| Bits | Field | Used by | Meaning |
|---|---|---|---|
| `[255:252]` | `op` | all | The opcode. |
| `[251:243]` | `addr` | IMEM, RUN | Instruction-memory address, or the start PC. |
| `[251:249]` | `ad` | DESC | Descriptor index. Overlaps `addr` — the opcode disambiguates. |
| `[248:246]` | `fld` | DESC | Field within the descriptor. |
| `[245:212]` | `value` | DESC | The value, 34 bits. On `fld = 0` this is the **low 34** of a base address. |
| `[68:63]` | `value_hi` | DESC | `addr[39:34]`, on `fld = 0` only. A dimension never uses it. |
| `[31:0]` | `word` | IMEM | The instruction word to store. |

A base address is split here for the same reason and in the same place as the
cluster's — `isa/vector.py`'s `desc_value_hi_lsb` is 63, matching `cluster.py`'s
`addr_hi` — and it has the same trap: read `[245:212]` alone and a staging or a
remote base decodes as local DRAM at the same offset. `model.py`'s `_descriptor`
rejoins them.

This unit spends most of its 256 bits on nothing, because its real instruction
set is a kernel in its own instruction memory. `CU_INST` here is a loader plus a
start button — a legitimate shape, and a cheaper one than encoding a vector ISA
into 256 bits.

### 8.3 `mx_matmul_cu` — the earlier single-tile matmul unit

Opcodes: 1 `BLOCK`, 2 `EMIT`.

| Bits | Field | Meaning |
|---|---|---|
| `[255:252]` | `opcode` | |
| `[251:218]` | `addr_a` | BLOCK: A operand address. EMIT: destination. |
| `[217:184]` | `addr_b` | BLOCK: B operand address. |
| `[183:180]` | `tile` | Which resident sub-tile. |
| `[179]` | `first` | 1 = start the tile, 0 = accumulate into it. |
| `[178:171]` | `anchor` | Common exponent for this output tile. |

Three units, three unrelated encodings, one shared convention: the opcode is
`[255:252]`. **That convention is not part of this specification.** It is what
these three happen to do. A fourth unit may put its opcode anywhere in the
payload, and the framework will not notice.
