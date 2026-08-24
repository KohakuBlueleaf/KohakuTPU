---
title: Memory protocol
summary: Requests, responses, write descriptors, streaming fetches and unit-to-unit transfers against the memory agent — every flit type, who may send it, and what is and is not ordered.
tags:
  - spec
  - normative
  - memory
  - sysnode
---

# Memory protocol

> **Kind: mixed, and marked per section.**
>
> | Section | Kind |
> |---|---|
> | §1–§5, §7–§9 — the encoding, the framing rules, the ordering guarantees | **Fixed** |
> | §3.2.3 — what the agent's shape forces on a receiver | **Convention, but binding.** Nothing checks it; the agent will deliver in this shape regardless. |
> | §6.0 — the payload inside an intra-mesh unit-to-unit burst | **Convention, free.** |
> | §9.3 — a burst that crosses a mesh boundary | **Fixed.** |
> | §10 — the read-path transform | **Addon.** The slot is Fixed; what you put in it is yours. |

The memory agent is the endpoint that turns mesh traffic into DRAM traffic. This
document specifies the wire protocol against it: what a requester may send, what
comes back, and what may be assumed about the order in which it arrives.

Flit field positions are in [flit-format.md](flit-format.md) §4. The link
handshake is in [compute-unit-port.md](compute-unit-port.md) §2. The
architectural argument for why the agent is shaped this way is in
[arch/sysnode/](../arch/sysnode/).

Source of truth: `src/kohakuaccel/sysnode/core/mag.v` and
`src/kohakuaccel/sysnode/core/mag_mem_port.v`.

## 1. What the agent presents to the mesh

The agent presents `MEM_PORTS` independent NoC endpoints, each at its own
coordinate. Each port owns its intake queues, read engine, write slots and AXI
channel. Nothing is shared between ports except the address space on the far side
of AXI.

Consequences that are protocol, not implementation:

- **A port is a server, not a route.** Two requesters behind one port queue for
  the same read engine. Two requesters at different ports do not.
- **Ports MUST be placed at different mesh nodes.** Routing is XY on clamped
  coordinates, so a port at `(0, y)` draws traffic to router `(1, y)`. Two ports
  on one router split the server without splitting the funnel.
- **A memory port coordinate is also the control agent's address.** The
  orchestrator has no endpoint of its own; it answers at port 0's coordinate.
  Inbound flits at a port are demuxed **by type**: memory types go to that port's
  engine, everything else goes to the agent. One address, two consumers, told
  apart by what the flit is.

That last point is what lets a compute unit reply to whoever sent its
instruction without being configured: the completion is addressed at the
orchestrator's coordinate, arrives at port 0, and is handed to the agent because
`CU_SIGNAL` is not a memory type.

### 1.1 Inbound classification at a memory port

| Flit | Goes to | Backpressure |
|---|---|---|
| `MEM_RD_REQ`, `MEM_WR_REQ`, `MEM_WR_DATA` | that port's engine | The engine's intake queues. |
| any type with `rsvd[2]` set | the interlink encapsulator | Round-robin across ports; a port waiting its turn holds `busy`, bounded by `MEM_PORTS` cycles. |
| everything else | the control agent | Round-robin. **If the agent cannot take it at all, the flit is accepted and dropped.** |

The last row is deliberate and it is a protocol guarantee running the other way:

> **Control-plane traffic addressed at a memory port is best-effort.** A flit the
> agent cannot accept is discarded rather than held.

The agent raises its own `busy` when its receive mailbox is full, and a host that
never drains the mailbox leaves it full indefinitely. Holding the port for that
would stall the *memory* flits behind it on the same link, for good, because
nothing clears the condition. The framework trades a loss on a path nothing uses
in steady state — completions bypass the mailbox entirely, and the driver does
not read it — for removing an unbounded stall on the path everything uses.

Waiting one's *turn* is different and does hold the port. That is bounded by the
port count.

### 1.2 Outbound arbitration at a memory port

Priority: **control agent, then interlink, then the read/write engine.** A flit
leaves from the port on its destination's row.

Within the engine, the read-response emitter outranks the plain-read and
write-ack path. A `MEM_WR_ACK` can therefore be delayed for the whole duration of
a streaming fetch. It cannot be starved indefinitely — four response flits per
entry against a fetch of at least four AXI beats leaves cycles free — but no
latency bound is offered.

### 1.3 Intake backpressure

Each port keeps **two** intake queues, demuxed by type: one for read requests,
one for write descriptors and write data.

- With one queue, a read request at the head that cannot be served blocks the
  write data behind it — and that data is what lets a drain finish and frees the
  resource the read was waiting for.
- `mem_in_busy` is `either queue is near full`, which depends only on the port's
  own state. It is **not** a function of the arriving flit's type. See
  [compute-unit-port.md](compute-unit-port.md) §2 for why that rule is absolute.
- The margin is explicit: `Q_MARGIN` entries of `Q_DEPTH`. The port counts for
  itself rather than relying on the FIFO's `almost` flag, which is not a margin.

## 2. Message types

| Code | Name | Sent by | Consumed by | Flits |
|---|---|---|---|---|
| `0x0` | `MEM_RD_REQ` | any endpoint | the agent | 1 |
| `0x1` | `MEM_WR_REQ` | any endpoint | the agent | 1, followed by data |
| `0x4` | `MEM_WR_DATA` | the same endpoint | the agent | `len + 1` |
| `0x2` | `MEM_RD_RESP` | the agent | the requester and any listed peers | 1 per word |
| `0x3` | `MEM_WR_ACK` | the agent | **nobody** | 1 |

A requester **MUST NOT** send `MEM_RD_RESP` or `MEM_WR_ACK`. The agent **MUST
NOT** be sent any other type expecting memory service; another type arriving at a
port coordinate is handed to the control agent, and if the agent cannot take it,
dropped.

## 3. Reads

There are two request forms, distinguished by `flags[6]` (`STREAM`). They run in
**separate state machines** and can be in flight at the same time, over the one
AXI read channel.

### 3.1 Plain read

`STREAM = 0`.

- One AXI burst of `len + 1` beats from `addr`.
- Each AXI beat becomes one `MEM_RD_RESP` flit, verbatim.
- `txn` is echoed unchanged on every response flit.
- `last` is set on the flit carrying the final beat.
- `rsvd[1:0]` is 0.

This path exists for benches and bring-up. It occupies the port's shared FSM, so
it excludes a write from being issued while it runs.

### 3.2 Entry read and streaming fetch

`STREAM = 1`. Served by the port's own read engine.

An **entry** is the unit this path works in, and its size is stated by the
request: `entry_words × DATA_W/8` bytes, giving `entry_words` AXI beats and
`entry_words` response flits.

`entry_words` is `[165:158]` of the descriptor. **0, or any value above 4, means
4** — which is what every existing requester sends, so the field is backward
compatible by construction.

There is no second geometry. A fetch is never transformed, so no request can
imply a source entry of a different size from the one it names — see §10.

A **streaming** request covers `count` consecutive entries:

- Entry `i` is read from `addr + i × entry_bytes`. Entries **MUST** be contiguous
  in memory; the engine accumulates the address and offers no stride.
- `count` is 8 bits. 0 means 1. The maximum run is 255 entries.
- Entries are fetched and emitted in ascending order, and the next entry's AXI
  address is issued the moment the previous entry's last beat lands, so the
  request-to-first-beat latency is paid once per run rather than once per entry.

#### 3.2.1 How a response names its slot

This is the mechanism that makes streaming possible, and a requester **MUST**
use it rather than keeping a cursor.

| Header field | Carries |
|---|---|
| `txn` | The requester's own `txn`, **plus this entry's index within the run**, as an 8-bit sum. |
| `rsvd[1:0]` | The word's index within the entry, 0–3. |
| `last` | Set on the final word **of each entry**, not only of the run. |

A requester **MUST** size its own tag space so that `txn + count - 1` does not
exceed 255. The addition is 8-bit and wraps silently; a run that wraps aliases
two entries onto one slot.

Because every flit names its exact destination slot, **arrival order is not load
bearing** and a requester needs no per-entry state. A requester that assembles an
entry from consecutive flits into one register is relying on a property of the
*server* — that one agent finishes an entry's words before starting the next —
and MUST assert it rather than assume it. A second server, a reordering fetch
engine, or two senders into one receiver would interleave two entries into one
and produce a plausible wrong result.

#### 3.2.2 Multicast to peers

A read request MAY name up to three **extra destinations** in `peer[23:0]`, one
`{y, x}` byte each, with `n_peer` saying how many are present.

- The requester is always destination 0. The listed peers follow.
- The entry is read **once**; the same latched words are re-sent with a different
  header per destination.
- Every destination receives the same `txn` and the same word indices.
- Destinations are served one entry at a time: all of entry *i*'s words to
  destination 0, then all of entry *i*'s words to destination 1, and so on.

This exists because a set of units frequently sweeps the same operand: served
separately, that is one DRAM read **per consumer** for a bit-identical result.

The decision of *who issues the request* is not made by the framework. A sharing
set must arrive at one issuer by some rule its own driver enforces; the framework
neither elects one nor detects two.

#### 3.2.3 What the agent's shape forces on you

> **Kind: Convention — but binding in practice.** Nothing checks any of this. The
> agent will deliver in this shape whether or not your unit was designed for it,
> so a unit that ignores these has to convert, and conversion at the receiver is
> the thing entry tagging exists to avoid.

Your unit's local memory is entirely yours (see
[compute-unit-port.md](compute-unit-port.md), "What this document does not
constrain"). The agent has no idea what it looks like. But the agent *does* have
a shape of its own, and these five properties of it reach across the boundary:

| The agent will… | So a receiver should… |
|---|---|
| deliver in **whole entries**, never partial ones | make the entry a natural write unit — an integer number of entries per buffer slot, not a fractional one |
| deliver one entry as **`entry_words` consecutive flits**, `last` on the final word | assemble by word index rather than by counting arrivals |
| put the **destination slot in the header** — `txn` plus the entry index, and `rsvd[1:0]` for the word | derive the write address from the flit, not from a cursor. A cursor is correct only for as long as there is exactly one server |
| **interleave** other traffic between an entry's words | frame by type and tolerate gaps |
| deliver **the same words to every peer destination** of a multicast | not assume it is the only recipient, and not assume its copy differs from anyone else's |

Two idioms follow, both used by the reference units and neither required:

- **Assemble an entry in one register and commit on the last word.** Cheap, and
  correct only because a single agent finishes an entry before starting the next.
  A unit that does this **SHOULD** assert it in simulation rather than assume it:
  a second server, a multicast source, or a reordering fetch engine would
  interleave two entries into one and produce a plausible wrong result.
- **Use the request's `txn` as the first destination slot.** The agent adds the
  entry index, so a run of entries lands in a run of slots and the receiver needs
  no arithmetic at all.

A unit whose buffer geometry does not match the agent's entry can still request
plain reads (§3.1) and place beats itself. It gives up streaming and multicast to
do so.

## 4. Writes

A write is a `MEM_WR_REQ` descriptor followed by `len + 1` `MEM_WR_DATA` flits.

### 4.1 The reassembly rule

The mesh interleaves. Another node's flit can land between a descriptor and its
data, so the agent gives each **source coordinate** its own reassembly slot and
matches data to it by `src_x`, `src_y`.

The obligations that follow are absolute:

- A source **MUST NOT** have more than one write open at a time. "Open" means the
  descriptor has been sent and its data is incomplete. Slots are matched by source
  coordinate alone, so two open writes from one source cannot be told apart, and
  which one the data binds to is undefined.
- A source **MUST** send exactly `len + 1` data flits per descriptor. The burst
  ends on the agent's **beat counter**, not on the flit stream, so a requester
  that miscounts its own data does not desynchronise the response — but it does
  leave a slot that never completes, and that slot is never freed.
- A source **MUST** set `last` on the final data flit and clear it on the others.
- `src_*` on the data flits **MUST** match the descriptor's. This is the only
  binding; a wrong source coordinate stores the bytes into another node's write.

### 4.2 Burst length limit

**`len` MUST be at most 7** — that is, at most 8 beats per write, at the default
build.

The limit is `WBURST`, a fixed constant of 8 in `mag_mem_port.v`, not a
parameter. A slot holds `WBURST` beats and the descriptor's `len` field is
truncated to `$clog2(WBURST) + 1` bits on capture. A descriptor with `len > 7`
therefore has undefined behaviour: the count wraps and the data buffer aliases.

`len = 0` reduces exactly to one beat per descriptor, which is what makes the
burst form safe to turn off.

### 4.3 Slot count

`WR_SLOTS` **MUST** be at least **two per node that can have a write in flight**,
not one.

A compute unit discards its `MEM_WR_ACK` and does not wait for it, so its next
descriptor arrives while the previous burst is still on the AXI bus. A slot is
held from descriptor until its ack is sent, so the previous write's slot is still
allocated. With one slot per node the second descriptor finds nothing free, is
never popped, and blocks the data flits behind it — which are what would have
freed one.

Under-sizing does not corrupt anything. It deadlocks.

## 5. Acknowledgements

`MEM_WR_ACK` is a single flit, `txn` echoed, `last` set, **payload all zero**.

- It carries **no status**. Success and failure are indistinguishable on the mesh.
- It is sent when the AXI slave's write response has been received, so it does
  mean the data reached memory rather than a queue.
- **Nothing consumes it.** Every compute unit in the tree drops it, and a unit
  that does not drop it wedges — see [compute-unit-port.md](compute-unit-port.md)
  §5. Acks are fire-and-forget by design.

A program that must read what it wrote therefore **MUST NOT** sequence on the
ack. It sequences at an instruction boundary the host can observe: the writing
instruction's completion, seen through the orchestrator's status mirror.

## 6. Unit-to-unit transfers (`CU_DATA`)

The framework's second data-movement path: one endpoint writes directly into
another's local memory, without going through DRAM. It does not involve the
memory agent at all except as a router of last resort for remote destinations.

### 6.0 Where the contract stops

Read this before the rest of the section, because the two halves have different
force.

| Layer | Status | Who agrees on it |
|---|---|---|
| **The envelope** — flit type, descriptor-then-data framing, `buf_id`, `offset`, `len`, `flags`, `ack`, destination coordinates, `last` | **Fixed contract.** MUST. | The framework. |
| **The payload of a data flit, within one mesh** | **Recommendation.** Not specified, not checked. | The two units, by being the same design. |
| **Everything about a transfer that crosses a mesh boundary** | **Fixed contract.** MUST. §9.3. | The framework. |

Inside a mesh, the network does not care what two compute units say to each
other. A router reads `dst_x` and `dst_y`; the memory agent does not know
unit-to-unit interconnection exists at all. So the framework specifies the
envelope and stops: **the 256 bits of a `CU_DATA` data flit are not specified,
and a unit may put whatever it likes there.** Two units of the same design agree
by construction, and two units of different designs agree by publishing what
their `buf_id`s hold — which they must do anyway (see
[flit-format.md](flit-format.md) §4.7.1).

The framework's recommendation, which nothing enforces:

- **Send whole granules.** `offset` counts 32-byte granules and advances one per
  flit, so a payload that is not a whole number of granules needs a length the
  descriptor cannot express.
- **Put a multi-flit item's low half first.** A receiver that reassembles pairs
  can then complete on the odd granule with one comparison, and a burst that
  starts on an odd granule is detectably malformed.
- **Make the format a property of `buf_id`, not of a bit in the payload.** One
  field naming the destination buffer already names the format; a second bit that
  could disagree with it is a type error waiting to happen.

The moment a transfer crosses a mesh boundary the recommendation becomes a
requirement, because the interlink has to encapsulate and route it. §9.3.

### 6.1 Framing

Fixed contract.

- One descriptor flit, then `len + 1` data flits, all typed `CU_DATA`.
- `offset` in the descriptor is the start granule; it advances by one per data
  flit. A burst is a **run**, so nothing needs a per-buffer cursor.
- `last` is set on the final data flit.
- `buf_id` names the destination buffer and is drawn from a **framework
  namespace**, not chosen by the unit. The allocation, including the index
  reserved for the staging adapter, is in
  [flit-format.md](flit-format.md) §4.7.1.

### 6.2 Obligations

On the **sender**:

- **MUST** send the descriptor and its data as one uninterrupted sequence from
  that endpoint.
- **MUST** set `flags[0]` (`signal_on_complete`) if it intends to wait for the
  transfer. Without it the transfer is unobservable.
- **SHOULD** name an explicit `{ack_y, ack_x}` when the receiver is another
  compute unit, because a completion sent back to a compute unit is consumed by
  nobody. **MUST** name one if the burst crosses a mesh boundary.
- **MUST NOT** begin a second burst into the same receiver before the first has
  completed, unless that receiver publishes that it can reassemble more than one.

On the **receiver**:

- **MUST** frame by descriptor-then-count, and **MUST** check each data flit's
  source coordinate against the open burst's. Two senders interleaving is
  otherwise indistinguishable from corruption: the second sender's descriptor is
  consumed as the first's data and both buffers fill with nonsense.
- **SHOULD** check `last` against the descriptor's own count. They disagree
  exactly when interleaving has happened.
- **MUST** range-check `offset + len` against the named buffer, and **MUST**
  reject rather than wrap.
- **MUST** count a rejected burst out to its `last` flit anyway, or the next data
  flit is read as a descriptor.
- **SHOULD** acknowledge a rejected burst, and **SHOULD** raise `exec_fault` on
  the next retirement, so the sender is released and the error is reported once
  rather than forever.

## 7. Ordering

### 7.1 Guaranteed

| Guarantee | Basis |
|---|---|
| Flits between one `(src, dst)` pair arrive in the order sent. | One path per pair, XY dimension-order. |
| A write's data flits are applied in the order they were sent. | Beat counter within the slot. |
| Entries of a streaming run are emitted in ascending order. | The engine walks the run. |
| One entry's words are emitted consecutively to a given destination. | The emitter finishes an entry per destination before advancing. |
| A `MEM_WR_ACK` implies the write reached the memory's response channel. | It is issued on `BVALID`. |

### 7.2 Not guaranteed — and these are the ones that bite

| Non-guarantee | What a requester must do |
|---|---|
| **No ordering between flits from different sources.** | Frame by type and check `src`. Never by position. |
| **No ordering between a read and a write**, even from the same requester to the same port. They are separate machines on separate AXI channels. | Sequence in the program, not in the protocol. |
| **No ordering between two memory ports.** Different ports are different AXI masters with no barrier between them. | Do not let two ports touch the same words. The framework does not enforce disjointness. |
| **No ordering between a `MEM_WR_ACK` and a later read of the same address.** | See §5: sequence at an instruction boundary. |
| **No latency bound on anything.** Nothing retries at the message level. | Do not build a timeout into the datapath; report a fault and let the host decide. |
| **No guarantee a response arrives at all** if the request was malformed. | See §8. |

## 8. Faults

The memory path reports almost nothing to hardware. This is a deliberate v1
scope, and a requester **MUST NOT** build a recovery mechanism on top of a report
that does not exist.

| Condition | What happens | Reported |
|---|---|---|
| Input flit dropped because backpressure was late | The flit is lost. | Simulation `$display` only. |
| `MEM_WR_DATA` with no matching open write | Dropped. | Simulation `$display` only. |
| Write descriptor arrives with no free slot | Not popped. Blocks the write queue. | Nothing. Presents as a hang. |
| Read or write naming a mesh other than the agent's own, in `addr[37:36]`, on a flit **not** marked remote | **Not forwarded.** The access aliases to local memory with the mesh field ignored. | `mag.v:455` raises `bad_remote_req` into the interlink's status when `ILINK != 0`. A build without an interlink reports nothing. |
| AXI slave error response (`BRESP`/`RRESP` non-OKAY) | Ignored. | Nothing. `MEM_WR_ACK` carries no status. |

**A memory request MUST address the local mesh.** There is no remote read and no
remote compute-unit write in v1; only the mover's writes and encapsulated
`CU_DATA` cross a boundary.

## 9. Host and inter-mesh paths

These are framework services rather than mesh protocol, and are specified here
only far enough to state their contracts. The mechanism is in
[arch/sysnode/](../arch/sysnode/) and [arch/axi.md](../arch/axi.md).

### 9.1 The memory window

The agent is an AXI4 slave `DATA_W` bits wide, through which the host uploads and
reads back memory. **An upload is written verbatim** — the whole address space is
addressable and no address bit carries a marker.

**Retired:** `addr[ADDR_W-1]` and `addr[ADDR_W-2]` used to mean `QUANT` and
`BLAYOUT`, converting an upload on the way in, and the window was 32 of 34
address bits for that reason. That path is gone with the rest of the per-request
transform selection (§10). A host that wants converted bytes in memory either
converts them itself or uploads raw and has the mover convert on card, which is
one descriptor and no host round trip.

### 9.2 The control window

A separate 64-bit AXI4 slave, wired straight to the orchestrator. Its register
map is in [control-registers.md](control-registers.md) §2.

### 9.3 Crossing a mesh boundary

**This is fixed contract, and it is the one place unit-to-unit transfer stops
being a local agreement between two units.** The interlink has to encapsulate and
route the burst, so it has to know its shape.

A remote burst is an ordinary `CU_DATA` burst whose header is filled in
differently. Every field below is framework-owned on this path.

| Header field | Value on a remote burst | Why |
|---|---|---|
| `dst_x`, `dst_y` | **The local memory-agent port's coordinates**, not the destination endpoint's. | The routers must deliver it to the local agent, which demuxes it to the encapsulator. The mesh never learns another mesh exists. |
| `rsvd[2]` | `1`. The remote marker. | The agent's inbound demux tests this bit. |
| `rsvd[1:0]` | The destination **mesh id**, 0–3. | Routed by the inter-mesh switch, XY on mesh coordinates. |
| `txn` | `{fin_y[3:0], fin_x[3:0]}` — the **final endpoint's coordinate in the destination mesh**. MUST be nonzero. | `CU_DATA` has no other use for `txn`, and the encapsulator needs the final coordinate on the wire. |
| `src_x`, `src_y` | The sending endpoint's own coordinates, unchanged. | Preserved across the crossing; see below. |

Rules:

- **`rsvd[2]`, `rsvd[1:0]` and `txn` MUST be present on every flit of the burst**,
  descriptor and data alike — not only on the descriptor. The encapsulator is
  stateless, and the routers interleave bursts from different senders at its
  port; a stateful encapsulator would need a CAM keyed on source to reunite them.
- **`txn` MUST be nonzero on a remote burst**, and zero is what marks a burst
  local. `(0,0)` is safe as the "not remote" sentinel because it is a mesh
  corner, which touches no router and can hold no endpoint. A burst encoded
  before the interlink existed has an all-zero tail and therefore reads as local,
  which is what it was.
- **A remote burst MUST name its ack destination explicitly** in
  `{ack_y, ack_x}`. Source coordinates are preserved across the crossing — which
  is what keeps two remote bursts arriving at one node distinguishable by the
  receiver's source check — so the "answer the sender" sentinel would resolve to
  a node in the *wrong* mesh.
- A unit **MUST NOT** use `txn` for its own purposes on any `CU_DATA` flit that
  might cross a boundary.
- On a **local** `CU_DATA` burst `txn` is not read by anything. The two reference
  units send different constants there, and both are correct.

Memory requests are **not** forwarded across a boundary. `MEM_RD_REQ` and
`MEM_WR_REQ` naming a mesh other than the local one alias to local memory with
the top two address bits ignored; see §8. Only the mover's writes and
encapsulated `CU_DATA` cross.

## 10. The read-path transform

> **Kind: Addon.** A slot that ships working and exists to be swapped. The
> interface around it is Fixed; the transform inside it is yours.

**A read request no longer selects a transform.** `flags[4]` (`QUANT`) and
`flags[5]` (`BLAYOUT`) are **reserved and ignored**; a requester that sets them
gets an untransformed fetch. The host upload window's `QUANT`/`BLAYOUT` address
markers (§9.1) are retired with them.

The transform slot moved off the fetch path and belongs to the **memory mover**:

```
   mem / L2 --> [ slot ] --> mem / L2      the mover, once per tensor
   mem / L2 -----------------> port --> unit
```

A compute unit reads operands already in their final format. Conversion happens
before the fetch, not during it — so it is paid once per tensor rather than once
per read, which is what hidden state re-read across passes needs.

Two consequences for a requester:

- **An entry is `entry_words × DATA_W/8` bytes, always.** The old table where
  `QUANT = 1` implied a 2048-bit source entry is gone; §3.2's `entry_words`
  rule is now the only one.
- **A single-use operand costs an explicit mover pass.** DRAM traffic is
  unchanged — the same bytes are read once either way — but the conversion is a
  separate move the compiler or the control processor has to schedule.

The slot's own contract — the id-based selection, the port and geometry
contracts, arbitration and the default occupant — is
[spec/transform-slot](transform-slot.md). Nothing in this protocol names a
number format, and software never sees one.

There is **no postprocess hook on the write path**. A drain is written to memory
verbatim. An accelerator that needs one is adding a framework feature, not
configuring an existing one.
