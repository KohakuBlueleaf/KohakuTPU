---
title: Flit format
summary: The 288-bit flit — header fields and their bit positions, the message type codes, every per-type payload layout, and which fields the framework owns.
tags:
  - spec
  - normative
  - noc
  - flit
---

# Flit format

> **Kind: Fixed.** Every field, width and bit position below is protocol. The
> one exception is marked: §4.7.1's table of what each buffer index *typically
> holds* is Convention; the namespace itself and the reservation of index 3 are
> Fixed.

Source of truth: `src/kohakunoc/noc_pkt.vh` for the declared layout, and the
`HDR_*` macros in `src/kohakunoc/noc_cu_base.v` for the parameterised form the
RTL actually uses. Where the two differ, §7 records it.

The link handshake that carries a flit is specified in
[compute-unit-port.md](compute-unit-port.md) §2. This document covers only what
is inside one.

## 1. Geometry

A flit is one indivisible unit of transfer. There is no sub-flit granularity and
no flit spans two cycles.

| Quantity | Expression | Default value |
|---|---|---|
| Flit width | `FLIT_WIDTH` | 288 |
| Coordinate width | `POS_WIDTH` | 4 |
| Header width | `4*POS_WIDTH + 16` | 32 |
| Payload width | `FLIT_WIDTH - 4*POS_WIDTH - 16` | 256 |

The header is the top `4*POS_WIDTH + 16` bits; the payload is everything below
it. The router reads `dst_x` and `dst_y` and nothing else — every other bit of a
flit is opaque to the fabric.

**`POS_WIDTH = 4` caps a mesh at 16×16 coordinates**, edge endpoints included.
The mesh generator's clamped-coordinate scheme places routers at `1..N` and
endpoints just outside, so the usable router grid is at most 14×14.

## 2. Header fields

Positions are given MSB-first. The general expression is what the RTL computes;
the concrete column is the value at `FLIT_WIDTH = 288`, `POS_WIDTH = 4`.

| Field | Width | General position | At the defaults | Owner |
|---|---|---|---|---|
| `dst_x` | `POS_WIDTH` | `[FLIT_WIDTH-1 -: POS_WIDTH]` | `[287:284]` | framework |
| `dst_y` | `POS_WIDTH` | `[FLIT_WIDTH-POS_WIDTH-1 -: POS_WIDTH]` | `[283:280]` | framework |
| `src_x` | `POS_WIDTH` | `[FLIT_WIDTH-2*POS_WIDTH-1 -: POS_WIDTH]` | `[279:276]` | framework |
| `src_y` | `POS_WIDTH` | `[FLIT_WIDTH-3*POS_WIDTH-1 -: POS_WIDTH]` | `[275:272]` | framework |
| `type` | 4 | `[FLIT_WIDTH-4*POS_WIDTH-1 -: 4]` | `[271:268]` | framework |
| `txn` | 8 | `[FLIT_WIDTH-4*POS_WIDTH-5 -: 8]` | `[267:260]` | per type, see §2.3 |
| `last` | 1 | `[FLIT_WIDTH-4*POS_WIDTH-13]` | `[259]` | framework |
| `rsvd` | 3 | `[FLIT_WIDTH-4*POS_WIDTH-14 -: 3]` | `[258:256]` | framework, see §2.4 |
| `payload` | rest | `[FLIT_WIDTH-4*POS_WIDTH-17 : 0]` | `[255:0]` | per type, see §4 |

### 2.1 `dst_x`, `dst_y`, `src_x`, `src_y`

- `dst_*` is the only thing the router inspects. Routing is XY dimension-order on
  coordinates **clamped** into the router grid: a destination outside the grid
  (an edge endpoint) routes toward the nearest router and takes the outward hop
  on arrival. This keeps the channel dependency graph acyclic, which is the whole
  deadlock argument.
- `src_*` MUST be the sender's own coordinates. Three mechanisms depend on it and
  all three fail silently if it is wrong:
  1. `noc_cu_base` addresses an instruction's completion to the `src` of that
     instruction's flit. A wrong source sends the completion somewhere else, and
     the dispatch credit is never returned.
  2. `mag_mem_port` matches a write's data flits to its descriptor **by source
     coordinate alone**. A wrong source binds data to another node's write.
  3. A multi-flit receiver distinguishes two interleaved senders by source. A
     wrong source is indistinguishable from stream corruption.
- The interlink preserves `src_*` across a mesh boundary. A flit that arrives
  from another mesh carries the coordinates of the endpoint that sent it, not of
  the local memory agent that injected it. The consequence is that an
  "answer the sender" sentinel is meaningless on a remote burst; see §4.7.

### 2.2 `type`

The four-bit message class. Codes are centrally allocated; see §3.

### 2.3 `txn`

Eight bits whose meaning depends on `type`. It is **not** a globally unique
transaction identifier and there is no mechanism that allocates one.

| On | Meaning | Owner |
|---|---|---|
| `CU_INST` | Program identifier. The framework echoes it as the argument of `SIG_BATCH_COMPLETE`. | framework |
| `MEM_RD_REQ` | The requester's tag. The memory agent echoes it, **plus the entry's index within a streaming run**, on every response. | unit, with a framework-defined transformation |
| `MEM_RD_RESP` | `txn` of the request plus the entry index. | framework |
| `MEM_WR_REQ` | The requester's tag. Echoed unchanged on the `MEM_WR_ACK`. | unit |
| `MEM_WR_DATA` | Not read. | reserved |
| `MEM_WR_ACK` | `txn` of the request. | framework |
| `CU_SIGNAL` | For a framework-generated completion, the `txn` of the instruction being reported. | framework |
| `CU_CTRL` | Echoed on the reply, so a controller can match request to answer. | framework |
| `CU_DATA` | Unread by the framework in a single-mesh build. **When `rsvd[2]` is set it carries the destination coordinate in the far mesh** and is framework-owned. | unit, unless remote |

A unit **MUST NOT** use `txn` on a `CU_DATA` flit for its own purposes if that
flit may cross a mesh boundary.

### 2.4 `last`

Framework-owned on every type.

| On | Meaning |
|---|---|
| `CU_INST` | This is the final instruction of a program. The framework reports its completion as `SIG_BATCH_COMPLETE` rather than `SIG_INST_COMPLETE`. |
| `MEM_WR_REQ` / `CU_DATA` descriptor | 0. The burst continues. |
| `MEM_WR_DATA` / `CU_DATA` data | 1 on the final data flit of the burst, 0 otherwise. |
| `MEM_RD_RESP` | 1 on the final word of an entry, 0 otherwise. |
| `MEM_WR_ACK`, `CU_SIGNAL`, `CU_CTRL` | 1. Single-flit messages. |

A receiver **SHOULD** check `last` against its own descriptor's count. They
disagree exactly when two senders have interleaved into one receiver, which is
otherwise indistinguishable from data corruption.

### 2.5 `rsvd`

Three bits. All three are framework-owned. A unit **MUST** transmit `3'b000`
unless a rule below says otherwise.

| Bit | Meaning | Set by |
|---|---|---|
| `rsvd[2]` | **Remote-mesh marker.** This flit is destined for another mesh; the memory agent's inbound demux hands it to the interlink encapsulator instead of to the agent. Zero on every flit a single-mesh build produces. | a unit sending across a mesh boundary |
| `rsvd[1:0]` when `rsvd[2]` is set | The destination **mesh id**, 0–3. | the same sender |
| `rsvd[1:0]` on `MEM_RD_RESP` | **Word index within the entry**, 0–3. Combined with `txn`, this tells the receiver exactly which slot the word belongs in, so arrival order stops being load-bearing. | the memory agent |
| `rsvd[1:0]` otherwise | Reserved. MUST be zero. | — |

The two uses of `rsvd[1:0]` never collide: a `MEM_RD_RESP` never sets `rsvd[2]`,
and a remote flit is `CU_DATA` or `MEM_WR_*`.

## 3. Message types

| Code | Name | May be sent by | Consumed by |
|---|---|---|---|
| `0x0` | `MEM_RD_REQ` | any endpoint | the memory agent |
| `0x1` | `MEM_WR_REQ` | any endpoint | the memory agent |
| `0x2` | `MEM_RD_RESP` | the memory agent | the requester, or a listed peer |
| `0x3` | `MEM_WR_ACK` | the memory agent | nobody — see §4.4 |
| `0x4` | `MEM_WR_DATA` | any endpoint | the memory agent |
| `0x5` | `CU_INST` | the orchestrator | a compute unit's instruction FIFO |
| `0x6` | `CU_SIGNAL` | a compute unit | the orchestrator's status mirror |
| `0x7` | `CU_CTRL` | any controller | answered inside `noc_cu_base` |
| `0x8` | `CU_DATA` | any endpoint | a compute unit's receive path |
| `0x9`–`0xE` | unallocated | — | — |
| `0xF` | `ERROR` | — | — |

`NOC_T_IS_MEM(t)` is `t <= 4'h4`.

Two facts about this table that are easy to get wrong:

- **`CU_DATA` is `0x8`, not `0x4`.** `0x4` is `MEM_WR_DATA`. The two collided in
  an earlier revision, and a `CU_DATA` flit reaching the memory agent entered its
  write queue as data — a silent wrong-bytes store. Bit 3 no longer partitions
  memory traffic from unit traffic: five memory messages do not fit in four
  codes.
- **`ERROR` (`0xF`) is declared and unimplemented.** No module produces it and no
  module consumes it. A unit MUST NOT send it and MUST NOT expect one.

Codes `0x9`–`0xE` are **reserved to the framework**. A unit that needs a private
message class MUST use `CU_DATA` with a unit-defined `buf_id`, not an
unallocated type code.

## 4. Payload layouts

All positions below are for the default 256-bit payload. A payload field's
position is absolute within the payload, so a build with a different
`FLIT_WIDTH` or `POS_WIDTH` changes the payload width and these tables do not
carry over unchanged — see §7.

### 4.1 `MEM_RD_REQ` (`0x0`) and `MEM_WR_REQ` (`0x1`) — descriptor flit

| Bits | Field | Width | Owner | Meaning |
|---|---|---|---|---|
| `[255:222]` | `addr` | 34 | framework | Byte address. Exactly the 16 GB physical map, not widened. |
| `[221:216]` | `addr_spare` | 6 | reserved | MUST be 0. |
| `[215:208]` | `len` | 8 | framework | Beats minus one. |
| `[207:200]` | `flags` | 8 | framework | See §4.1.1. |
| `[199:192]` | `count` | 8 | framework | Entries in a streaming fetch. Read only when `flags[6]`. 0 is treated as 1. |
| `[191:168]` | `peer` | 24 | framework | Up to three extra destinations for a read response, `{y,x}` per byte, lowest byte first. |
| `[167:166]` | `n_peer` | 2 | framework | How many of `peer` are present, 0–3. |
| `[165:158]` | `entry_words` | 8 | framework | Words per entry on a streaming fetch. 0, or any value above 4, means 4. Ignored when `flags[4]` is set. |
| `[157:0]` | reserved | 158 | reserved | MUST be 0. |

On a `MEM_WR_REQ` only `addr` and `len` are read. `flags`, `count`, `peer`,
`n_peer` and `entry_words` are read on `MEM_RD_REQ` only.

`addr[33:32]` additionally names the **mesh** a request is aimed at, which the
memory agent compares against its own id; see [memory-protocol.md](memory-protocol.md) §8.

#### 4.1.1 `flags`

| Bit | Name | Status |
|---|---|---|
| 0 | `cacheable` | Declared in `noc_pkt.vh`. **No RTL reads it.** |
| 1 | `invalidate` | Declared. No RTL reads it. |
| 2 | `flush` | Declared. No RTL reads it. |
| 3 | — | Unallocated. MUST be 0. |
| 4 | `QUANT` | The source in memory is FP16 and MUST be converted on the way out. |
| 5 | `BLAYOUT` | Pack the converted result for a B operand rather than an A operand. |
| 6 | `STREAM` | This descriptor covers `count` consecutive entries, not one fetch. |
| 7 | — | Unallocated. MUST be 0. |

Bits 4 and 5 name a **format conversion in the memory agent**. The conversion
itself (FP16 to MXFP7 with an E5M3 block scale) is a KohakuTPU choice that
currently lives in a framework module; see
[memory-protocol.md](memory-protocol.md) §9.

### 4.2 `MEM_WR_DATA` (`0x4`)

The entire payload is data. No fields.

| Bits | Field | Owner |
|---|---|---|
| `[255:0]` | one beat | unit |

### 4.3 `MEM_RD_RESP` (`0x2`)

The entire payload is data. Placement information is in the **header**: `txn`
carries the requester's tag plus the entry index, and `rsvd[1:0]` the word index
within the entry.

| Bits | Field | Owner |
|---|---|---|
| `[255:0]` | one word | framework (it is what was read) |

There is no descriptor flit on a read response. The requester already knows the
shape from its own request.

### 4.4 `MEM_WR_ACK` (`0x3`)

| Bits | Field | Owner |
|---|---|---|
| `[255:0]` | zero | reserved |

The payload is transmitted as all zeros. **There is no status field.** The
pre-reframing snapshot documents `payload[7:0]` as a status byte; no RTL writes
or reads it. A write's success or failure is not reported on the mesh.

### 4.5 `CU_INST` (`0x5`)

The payload is unit-defined in its entirety. See
[instruction-encoding.md](instruction-encoding.md), which exists because
`noc_pkt.vh` declares a split here that no implementation honours.

### 4.6 `CU_SIGNAL` (`0x6`)

| Bits | Field | Width | Owner | Meaning |
|---|---|---|---|---|
| `[255:248]` | `code` | 8 | framework below `0x40` | The event. See §5. |
| `[247:216]` | `arg` | 32 | unit | Always unit-defined content, whatever the code. |
| `[215:0]` | reserved | 216 | reserved | MUST be 0. |

### 4.7 `CU_DATA` (`0x8`) — descriptor flit

A `CU_DATA` burst is **one descriptor flit followed by `len + 1` pure data
flits**. Only the descriptor carries these fields; the data flits are 256 bits
of payload each.

The descriptor is fixed contract. **The 256 bits of a data flit are not
specified inside one mesh** — see [memory-protocol.md](memory-protocol.md) §6.0.
A burst that crosses a mesh boundary is fixed contract in full; see §9.3 of the
same document.

| Bits | Field | Width | Owner | Meaning |
|---|---|---|---|---|
| `[255:248]` | `buf_id` | 8 | **framework namespace** | Which buffer of the destination unit. See §4.7.1. |
| `[247:232]` | `offset` | 16 | framework | Start offset **in 32-byte granules**, and it advances by one per data flit. |
| `[231:224]` | `len` | 8 | framework | Data flits following, minus one. A burst is therefore 1–256 flits. |
| `[223:216]` | `flags` | 8 | framework, bit 0 only | Bit 0 `signal_on_complete`. Bits 7:1 unallocated, MUST be 0. |
| `[215:212]` | `ack_y` | 4 | framework | Where the completion goes. |
| `[211:208]` | `ack_x` | 4 | framework | |
| `[207:0]` | reserved | 208 | reserved | MUST be 0. |

- `buf_id` is the abstraction that survives not knowing what a unit's local
  memory looks like: *(which buffer, where in it, how much)*.
- `flags[0]` set makes the receiver emit `SIG_DATA_RECEIVED` when the burst
  completes. Without it a unit-to-unit transfer is unobservable: the framework
  signals on instruction retirement and a burst is not an instruction, so a
  sender that waits would wait forever.
- `{ack_y, ack_x} == 0` means **send the completion to the descriptor's source**.
  `(0,0)` is a safe sentinel because it is a mesh corner, which touches no router
  and can hold no endpoint — the mesh generator rejects a map that puts anything
  there.
- A completion addressed at the sender is useless when the sender is another
  compute unit: nothing there consumes it, so nothing can sequence a reader
  behind a writer. A unit-to-unit transfer **SHOULD** point `ack` at the
  orchestrator instead. A burst that crosses a mesh boundary **MUST** name an
  explicit `ack`, because the source coordinate is preserved and the sentinel
  would resolve to a node in the wrong mesh.
- A receiver **MUST** range-check `offset + len` against the named buffer and
  **MUST NOT** wrap. A rejected burst **MUST** still be counted out to its `last`
  flit — otherwise the next data flit is read as a descriptor and the damage
  spreads — and **SHOULD** still be acknowledged, or the sender waits forever.

#### 4.7.1 `buf_id` is a framework namespace

`buf_id` is **not** a free field. A unit does not pick its own numbering.

The routers never interpret it, but the framework does allocate it: a sender
naming a destination buffer has to mean the same thing the receiver does, and
framework services — the staging adapter, and anything later that writes into an
endpoint — need indices they can rely on across unit types.

| `buf_id` | Allocation | Kind |
|---|---|---|
| `0` | First operand buffer, by convention. | Convention |
| `1` | Second operand buffer, by convention. | Convention |
| `2` | Accumulator / result buffer, in the unit's internal accumulation format, by convention. | Convention |
| `3` | **Reserved: the staging adapter.** A unit MUST NOT claim it. | **Fixed** |
| `4`–`255` | Unallocated. A unit MAY use one, but MUST publish what it means, and MUST expect a future framework allocation to take it. | **Fixed** |

The three Convention rows say what indices 0–2 hold in practice. **A unit is not
obliged to have those buffers, or that many.** A unit with one flat buffer
answers at `0` and nothing else; a unit with five may number them 0–2 and 4–5.
Nothing in the framework reads a buffer's contents, so nothing enforces the
meaning — the value is that a sender written against one unit is more likely to
be right against another.

The reservation of index 3 is Fixed and does not depend on how many buffers the
unit has.

Two rules follow, and both are absolute:

- A unit with fewer buffers than the table has entries **MUST** map its buffers
  onto the low indices in order and **MUST** reject every other index, rather
  than aliasing an unallocated index onto something it does have. A unit with one
  flat buffer answers at `0` and faults on everything else.
- A unit **MUST** publish, in its own documentation, which indices it accepts and
  what each holds. Index `2` in particular carries the unit's *internal*
  accumulation format, which differs between units by construction — one field
  names it, and there is deliberately no second bit that could disagree.

The current numbering is visible today only as local parameters inside a
KohakuTPU compute unit. It is framework-owned regardless; see §7.

### 4.8 `CU_CTRL` (`0x7`)

Request:

| Bits | Field | Width | Owner | Meaning |
|---|---|---|---|---|
| `[255:248]` | `op` | 8 | reserved | Present in the driver's encoder as 0. **`noc_cu_base` does not read it** — it answers the index regardless. MUST be 0. |
| `[247:240]` | `index` | 8 | framework | Which control register. |
| `[239:0]` | reserved | 240 | reserved | MUST be 0. |

Reply:

| Bits | Field | Width | Owner | Meaning |
|---|---|---|---|---|
| `[255:248]` | `op` | 8 | framework | Always `0x02`, read response. |
| `[247:240]` | `index` | 8 | framework | Echoed. |
| `[239:176]` | `value` | 64 | framework | The register. |
| `[175:0]` | reserved | 176 | reserved | Zero. |

Register contents are in [control-registers.md](control-registers.md) §1.

## 5. `CU_SIGNAL` code allocation

| Code | Name | Emitted by | `arg` |
|---|---|---|---|
| `0x00` | `INST_COMPLETE` | the framework, on retirement | `exec_result` |
| `0x01` | `BATCH_COMPLETE` | the framework, on retiring an instruction with `last` set | `{24'd0, txn}` |
| `0x02` | `BARRIER_REACHED` | **nothing.** Allocated, unimplemented. | barrier id |
| `0x03` | `DATA_RECEIVED` | the unit, on completing a `CU_DATA` burst whose `flags[0]` was set | `{24'd0, buf_id}` by convention |
| `0x04` | `FAULT` | the framework, when `exec_fault` is set at `exec_done` | `exec_result` |
| `0x05`–`0x3F` | reserved to the framework | — | — |
| `0x40`–`0xFF` | **unit-defined** | the unit | unit-defined |

Codes below `0x40` are centrally allocated so a controller can act on any unit's
signals without knowing what that unit is. The argument stays unit-defined at
every code, so a unit can attach whatever it wants to an event.

A unit **MUST NOT** emit `0x00`, `0x01` or `0x04`: the framework emits those, and
a duplicate returns a dispatch credit that was never spent.

## 6. Multi-flit framing

The rule, and the reason it is a rule:

> A multi-flit message is a descriptor flit followed by pure data flits. Data
> flits **MUST** be identifiable **by their `type` code**, never by their
> position after a descriptor.

The mesh interleaves. Another node's flit can land between a descriptor and its
data at any point, and there is no mechanism that prevents it. A receiver that
collects "the next flit" into the open message stores the wrong bytes the moment
two nodes write at once, and does it silently.

The cost is one type code per multi-flit class — `MEM_WR_REQ`/`MEM_WR_DATA` is
the pair — in exchange for framing that cannot be broken by arbitration.

`CU_DATA` is the exception that proves the rule: descriptor and data share the
type code, so a receiver frames by **count** (`len` from the descriptor, checked
against `last`) and disambiguates senders by **source coordinate**. A unit
implementing `CU_DATA` reception MUST do both checks, and MUST publish that it
can only reassemble one burst at a time if that is the case.

## 7. Known divergences

| Divergence | Detail |
|---|---|
| `noc_pkt.vh` is included by nothing | Every module re-declares the type codes as local parameters. A divergence between two of them is silent, and one has already happened (`CU_DATA` versus `MEM_WR_DATA`). The header is documentation with a `.vh` extension. |
| Absolute versus parameterised positions | `noc_pkt.vh` writes header positions as literals (`287:284`, …) and payload positions as literals (`255:222`, …), so the file is only correct at `FLIT_WIDTH = 288`, `POS_WIDTH = 4`. The RTL computes header positions from the parameters. Payload field positions are literal everywhere and do **not** track `FLIT_WIDTH`. |
| Descriptor fields declared outside `noc_pkt.vh` | `count`, `peer`, `n_peer` and `entry_words` on `MEM_RD_REQ`, and `ack_y`/`ack_x` on `CU_DATA`, are framework fields with no macro in `noc_pkt.vh`. They exist only as literal part-selects in `mag_mem_port.v` and in the compute units. |
| `buf_id` allocation lives in an instance | The namespace in §4.7.1 exists as `BUF_L1A` / `BUF_L1B` / `BUF_PEER` localparams inside `src/kohakutpu/matmul/mx_cluster_cu.v`, and as a bare `!= 0` rejection inside `src/kohakutpu/vector/vec_cu.v`. Neither the allocation nor the reservation of index 3 is stated anywhere a second accelerator would look. |
| `rsvd` semantics undeclared | The remote-mesh marker, the mesh id and the read-response word index all live in `rsvd` and none is declared in `noc_pkt.vh`. |
| `NOC_MEM_LEN` comment | `noc_pkt.vh` describes `len` as "payload flits minus 1". On a `MEM_RD_REQ` served by the entry read engine it is not read at all. |
| `MEM_WR_ACK` status byte | Documented in the snapshot, absent from the RTL. |
