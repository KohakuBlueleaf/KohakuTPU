# KohakuNoC Specification

**Status: DRAFT, revision 2.** Open questions are marked ❓.

KohakuNoC is a 2D-mesh packet network carrying three kinds of traffic:

1. **Memory access** — a node reads/writes DRAM via MAS
2. **CU ↔ CU data** — bulk transfer from one unit's L1 into another's L1
3. **CU ↔ CU control** — instructions and completion signals

The network deliberately knows nothing about what a compute unit *is*. It defines
how to address a node, how to frame a message, and what it guarantees. Everything
above that is the CU's business.

Two modules connect the mesh to the AXI world (§10): the **Global Orchestrator**
carries the control plane and the **MAS** carries the data plane.

> **As built there is one, and it is MAG.** The orchestrator's mesh-facing half
> became the *agent* inside MAG, and since it shares MAG's memory ports it is no
> longer a node in its own right — §10.2, §10.5, and
> [`../mas/spec.md`](../mas/spec.md) §2.5. The two planes still exist and are
> still told apart; what tells them apart is the flit's **type** rather than
> which attachment it arrived at.

---

## 1. Topology and addressing

The coordinate space is `2^POS_WIDTH` square. At `POS_WIDTH = 4` that is 16×16.

- **Routers** occupy the inner grid, coordinates `1..14` → **14×14 = 196 routers**
- **PEs** occupy either a router's local port, or a position in the **border ring**
  (coordinate 0 or 15 in one axis), adjacent to the router it attaches to
- **The four corners** `(0,0) (0,15) (15,0) (15,15)` are invalid — no router is
  adjacent to them

Border ring = `4·16 − 4 = 60` cells, minus the 4 corners = **56 border PEs**.
Total addressable PEs = `196 + 56 = 252` = `n·m + 2(n+m)` for `n=m=14`.

```
      x=0   x=1   x=2  ...
y=0          P
y=1    P     R     R
y=2          R     R
```
`R` = router, `P` = border PE. A border PE hangs off the router's unused
directional port.

---

## 2. Routing: XY dimension-order, on clamped coordinates

**Route X until the column matches, then Y, then one final hop out to a border PE.**

Border PEs live outside the router grid, so a packet destined for `(0, y)` cannot
literally "complete X" — X terminates at a PE, not a router. Naively it would exit
west from whatever row it happened to be in. Routing Y-first for those
destinations would mix XY and YX in one network and reintroduce the cycles XY
exists to prevent.

The fix is to route toward the **clamped** destination — the router adjacent to
the target — and only take the outward hop on arrival:

```verilog
// clamp the destination into the router grid
wire [POS_WIDTH-1:0] r_dst_x = (dst_x < GRID_LO) ? GRID_LO :
                               (dst_x > GRID_HI) ? GRID_HI : dst_x;
wire [POS_WIDTH-1:0] r_dst_y = (dst_y < GRID_LO) ? GRID_LO :
                               (dst_y > GRID_HI) ? GRID_HI : dst_y;

wire x_done    = (r_dst_x == POS_X);
wire y_done    = (r_dst_y == POS_Y);
wire at_router = x_done && y_done;

wire [4:0] port_choice = {
    at_router &&  (dst_x == POS_X) && (dst_y == POS_Y),          // local
    (!x_done  &&  (r_dst_x < POS_X)) || (at_router && dst_x < POS_X),  // west
    ( x_done  && !y_done && (r_dst_y > POS_Y)) || (at_router && dst_y > POS_Y), // south
    (!x_done  &&  (r_dst_x > POS_X)) || (at_router && dst_x > POS_X),  // east
    ( x_done  && !y_done && (r_dst_y < POS_Y)) || (at_router && dst_y < POS_Y)  // north
};
```

`port_choice` is **one-hot**, so the round-robin between candidate directions in
the current `noc_inport.v` disappears and the inport gets *smaller*.

Why this matters beyond tidiness:

- **Deadlock-free by construction.** The current implementation is *adaptive* —
  a north-east packet sets both bits and takes whichever is free. That permits
  every turn, hence cyclic channel dependencies, hence deadlock. Prohibiting the
  Y→X turn breaks every cycle. The final outward hop is terminal (border PEs never
  forward), so it adds no cycle. **Deeper FIFOs do not fix deadlock** — they delay
  it and make the failure rarer and harder to find.
- **In-order per source→destination pair.** The path is deterministic, so two
  packets from A to B cannot reorder. This is what lets the memory system work
  without reorder buffers, and lets multi-flit messages reassemble by counting.

Buffers therefore only need to cover the backpressure round-trip — **8 to 32
flits in LUTRAM/SRL**, not URAM. Measured on the current router: 0 URAM instead of
15 per node, returning 960 URAM to the caches on a 196-node mesh.

Each input port holds **one** flit beyond its queue, offered to a single output.
An input therefore has one flit outstanding rather than one per direction, so a
flit waiting on a congested output blocks the flit behind it even when that one
would have gone elsewhere — ordinary head-of-line blocking, the price of not
keeping a `FLIT_WIDTH` register per (input, output) pair. Virtual channels are the
standard remedy if profiling ever shows it costs more than it saves.

Measured cost per router is **4,050 LUT, 5,960 FF, 0 BRAM, 0 URAM**, and the
2×2 tile it was measured in closes 400.8 MHz against a 300 MHz target. Those
figures live in one place — [`resource-budget.md`](resource-budget.md) §1, which
names the instrument — and are not restated elsewhere; this section had a second
copy that had drifted. At the 14×14 maximum they come to 46% of the device's
LUTs and 34% of its registers, with the entire BRAM and URAM budget left for
caches.

### 2.1 The link handshake: `valid` is held until `busy` is low

A link is a `data / valid / busy` triple, and the rule is **exactly two lines**:

- **Sender:** assert `valid` and hold both `valid` and `data` unchanged until a
  cycle in which `busy` is low. That cycle is the transfer.
- **Receiver:** accept **iff** `valid && !busy`. Accept once, and never on a
  cycle when its own `busy` is high.

Nothing else is legal, and the two halves are not independently choosable.

**Why it must retry.** The obvious alternative — sample `busy`, commit a flit
one cycle later, no retry — is what this NoC used to do, and it is unsound
because `busy` is a *prediction*. The sender decides at T against `busy(T)`; the
flit is presented at T+1; the receiver may raise `busy` at T+1. That flit is
then destroyed. It requires a receiver that is full *and* an upstream that
committed while it was not, so it appears only under sustained congestion:
**two clusters never triggered it, four did**, and what it destroyed most often
was a `MEM_WR_DATA` — leaving MAG's write slot permanently short of `ws_len`, so
the slot never became ready, the source's next descriptor opened a second slot,
and a later data flit matched nothing. The machine stopped with every write slot
free and nothing reported lost. See `.plans/optimization-log.md`.

**Why the receiver must not accept unconditionally.** The mirror-image fix —
"receiver takes whatever is valid, `busy` is only advisory" — fails against the
*same* senders. Because they hold, an accept-on-room receiver enqueues the same
flit once per cycle of backpressure. A duplicated `MEM_WR_DATA` overruns its
slot's `ws_len`, and the surplus flit then matches nothing: identical symptom,
opposite cause.

So there is no tolerant middle setting. Every endpoint obeys both lines:

| module | sender | receiver |
|---|---|---|
| `noc_outport.v` | holds; `grants` withheld while holding | — |
| `noc_inport.v` | — | `wr_en = valid && !port_busy` |
| `noc_cu_base.v` | `tx_free = !out_valid \|\| !out_busy` | `wr_en = valid && !noc_in_busy` |
| `noc_orchestrator.v` | `tx_pop` gated on the same term | `wr_en = valid && !rx_full` |
| `mag.v` | `out_free = !out_valid \|\| !out_busy` | `wr_en = valid && !mem_in_busy` |

> **`mag.v` has two senders and two receivers behind one port**, and the retry
> contract is what lets them share it. Inbound, `mem_in_busy` is the memory
> engine's occupancy for a memory flit and "the agent is not taking it this
> cycle" for anything else, so an ungranted port refuses and its sender holds —
> which is this section's rule, not an extra requirement. Outbound, the agent
> pre-empts the engine on the port its destination row selects; the engine is
> already obliged to hold `valid` and `data`, so it simply does. §10.5 and
> [`../mas/spec.md`](../mas/spec.md) §2.5.

With retry, `busy` may safely be plain `full` — the margin that a
commit-one-cycle-ahead link needs does not apply. That matters here because
`sync_fifo`'s `wr_almost` is **not** the margin it appears to be:
`USE_ADV_FEATURES` is zero, so XPM ties `prog_full` low and `wr_almost` reduces
to `wr_busy`. Anything that still wants a real margin must count for itself, as
MAG does with `Q_MARGIN`.

`noc_inport.v` carries the assertion that enforces the sender's half: a flit
offered while `busy` must still be offered next cycle, unchanged. Testing *that*
rather than "was a flit offered into a full buffer" is what separates a sender
that retries — the normal steady state under backpressure — from one that gave
up.

---

## 3. Flit format

One flit = one cycle on a link. `FLIT_WIDTH = 288` → 32-bit header, **256-bit
(32-byte) payload**. A power-of-two payload divides cache lines cleanly; 216 would
leave an awkward 184.

| Bits | Field | Width | Meaning |
|---|---|---|---|
| `[287:284]` | `dst_x` | 4 | destination column |
| `[283:280]` | `dst_y` | 4 | destination row |
| `[279:276]` | `src_x` | 4 | source column — **required for replies** |
| `[275:272]` | `src_y` | 4 | source row |
| `[271:268]` | `type` | 4 | message class, §4 |
| `[267:260]` | `txn_id` | 8 | matches replies to requests; groups multi-flit messages |
| `[259]` | `last` | 1 | final flit of this message |
| `[258:256]` | `rsvd` | 3 | reserved, must be 0 |
| `[255:0]` | `payload` | 256 | §5 |

The router reads **only** `dst_x`/`dst_y`. Everything else is opaque to it, which
keeps the router small and lets the protocol evolve without touching it.

---

## 4. Message types

| `type` | Name | Direction | Meaning |
|---|---|---|---|
| `0x0` | `MEM_RD_REQ` | CU → MAS | read request |
| `0x1` | `MEM_WR_REQ` | CU → MAS | write request; data in following flits |
| `0x2` | `MEM_RD_RESP` | MAS → CU | read data |
| `0x3` | `MEM_WR_ACK` | MAS → CU | write completion |
| `0x4` | `MEM_WR_DATA` | CU → MAS | write payload following a `MEM_WR_REQ` |
| `0x5` | `CU_INST` | any → CU | instruction delivery |
| `0x6` | `CU_SIGNAL` | CU → any | completion / synchronisation |
| `0x7` | `CU_CTRL` | any → CU | control-register access |
| `0x8` | `CU_DATA` | CU → CU | bulk L1→L1 transfer |
| `0xF` | `ERROR` | any | unroutable / malformed / faulted |

`0x9`–`0xE` reserved.

**`CU_DATA` IS `0x8`, AND `type[3]` NO LONGER PARTITIONS ANYTHING.** This table
printed `CU_DATA` at `0x4` while `mag_mem_port.v` and `vec_cu.v` had used `0x4`
for `MEM_WR_DATA` since write bursts gained their own type — so a `CU_DATA` flit
reaching MAG would have entered the write queue as data. Resolved in favour of
the silicon, because the collision was only latent while nothing sent `CU_DATA`.

The old rule was that bit 3 separated memory from CU traffic so a cache could
filter on it. Five memory messages do not fit in four codes, and no cache exists
to filter on the bit. `NOC_T_IS_MEM(t)` is now `(t) <= 4'h4`.

---

## 5. Message layouts

Multi-flit messages use a **descriptor flit followed by pure data flits**. Flit 0
carries the descriptor; flits 1..n carry 256 bits of data each; the final flit
sets `last`. One extra flit per message, in exchange for trivial parsing at both
ends.

### 5.1 `MEM_RD_REQ` / `MEM_WR_REQ` (descriptor flit)

| Payload bits | Field | Meaning |
|---|---|---|
| `[255:222]` | `addr` (34) | byte address — exactly the 16 GB physical map |
| `[221:216]` | `addr_spare` (6) | **reserved**, must be 0; room for future control/behaviour bits |
| `[215:208]` | `len` (8) | payload flits requested/supplied, minus 1 (1–256) |
| `[207:200]` | `flags` (8) | bit0 `cacheable`, bit1 `invalidate`, bit2 `flush`, rest reserved |
| `[199:0]` | reserved | |

`addr` is kept at exactly 34 bits to match the physical map rather than inflated
to 40 — the extra 6 bits are named `addr_spare` so their eventual use is a
deliberate decision, not an accidental address-width change.

`MEM_WR_REQ` is followed by `len+1` data flits. `MEM_RD_REQ` is a single flit with
`last = 1`.

### 5.2 `MEM_RD_RESP` / `MEM_WR_ACK`

`MEM_RD_RESP` returns `len+1` data flits with `txn_id` echoing the request; no
descriptor flit, since the requester already knows the shape from its own `txn_id`
bookkeeping. `MEM_WR_ACK` is a single flit, payload `[7:0]` = status (0 = OK).

### 5.3 `CU_DATA` — L1 → L1 (descriptor flit)

This format must survive **not knowing what a CU's L1 looks like**. A TPU-style CU
has 3 operand buffers + 1 output; a CPU-style CU may have 2. The abstraction that
holds for both is *(which buffer, where in it, how much)*:

| Payload bits | Field | Meaning |
|---|---|---|
| `[255:248]` | `buf_id` (8) | destination buffer index, CU-defined |
| `[247:232]` | `offset` (16) | destination start offset, in 32-byte granules |
| `[231:224]` | `len` (8) | data flits following, minus 1 |
| `[223:216]` | `flags` (8) | bit0 `signal_on_complete` |
| `[215:0]` | reserved | |

The network never interprets `buf_id` or `offset`. What a CU's buffer indices mean
is a CU-level contract published by that CU, not a network concern.

### 5.4 `CU_INST` — instruction delivery

**We do not define your instruction encoding. We define how to ship one.**

| Payload bits | Field | Meaning |
|---|---|---|
| `[255:248]` | `inst_len` (8) | instruction body flits, minus 1 |
| `[247:240]` | `inst_class` (8) | opaque to the network; CU-defined |
| `[239:0]` | `inst_body` (240) | first 240 bits of the instruction |

240 bits covers every CU instruction in a single flit with room to spare — the
cluster ISA's widest, `FILL`, uses well under half of it
([`../isa/cluster.md`](../isa/cluster.md) §2). Longer encodings continue in
following flits, and nothing built so far needs to.

### 5.5 `CU_SIGNAL`

Single flit.

| Payload bits | Field | Meaning |
|---|---|---|
| `[255:248]` | `code` (8) | **centrally allocated** below `0x40`, CU-defined at `0x40`+ |
| `[247:216]` | `arg` (32) | **always CU-defined content** |
| `[215:0]` | reserved | |

Codes are centrally allocated so the global controller can act on any CU's signals
without knowing what that CU is; the argument stays CU-defined so a unit can
attach whatever it wants to the event.

| `code` | Name | Meaning |
|---|---|---|
| `0x00` | `INST_COMPLETE` | one instruction retired; `arg` = instruction tag |
| `0x01` | `BATCH_COMPLETE` | instruction FIFO drained |
| `0x02` | `BARRIER_REACHED` | `arg` = barrier id |
| `0x03` | `DATA_RECEIVED` | `CU_DATA` with `signal_on_complete` landed; `arg` = `buf_id` |
| `0x04` | `FAULT` | `arg` = error code, mirrors `CU_ERROR` |
| `0x05`–`0x3F` | reserved | |
| `0x40`–`0xFF` | CU-defined | |

---

## 6. The mandatory CU interface

Every CU, whatever it computes, **must** expose the same two things. This is what
makes units pluggable — the global controller can drive, discover and health-check
any CU without knowing its internals.

`src/kohakunoc/noc_cu_base.v` implements this section in full, so a CU conforms by
construction rather than by remembering to. It queues completions rather than
holding one, because a CU that retires faster than a congested link drains would
otherwise overwrite -- and therefore lose -- the signals that return credits. A CU author writes a datapath and
nothing else; see [`cu-framework.md`](cu-framework.md).

### 6.1 Instruction FIFO (`CU_INST`)

A FIFO, because instruction streams are ordered and every CU ISA in this design
executes blocking and in order — a cluster runs one `FILL`/`GEMM`/`DRAIN` to
completion before starting the next ([`../isa/cluster.md`](../isa/cluster.md)).

- **Entries are whole 288-bit flits**, not extracted instruction bodies. Storing
  the flit needs no field extraction on the write side, and costs nothing: a
  240-bit body occupies the same number of BRAMs as the full 288.
- **Depth 512, in BRAM.** A RAMB36E2's widest shape is 512×72, so a 288-bit entry
  is 4 BRAM36 in parallel and 512 is the depth that fills them exactly. 64 CUs →
  256 BRAM36 of 2688.
- **Shape is fixed, depth is a parameter** (`INST_FIFO_DEPTH`, default 512).
- The CU must expose **space-available** to its NIC. This feeds the credit scheme
  in §7 — a sender must never be able to overflow it, because backpressuring an
  instruction into the network is what causes protocol deadlock.
- Draining is the CU's business; the network only guarantees ordered arrival.

> **Why BRAM and not SRL here**, when §2 argues the opposite for router buffers:
> the constraint differs. Router FIFOs are numerous (5 per router × 196) and need
> only enough depth to cover the backpressure round-trip, so SRL at depth 32 is
> both sufficient and cheap. Instruction FIFOs are one per CU, and on this device
> LUTs are the binding resource — 64 CUs at 22–25k LUT each already consume
> 81–93% of 1.728M — while BRAM sits at roughly half utilisation. Trading 256 BRAM
> for ~18k LUT is the right direction *for this budget*, and once a BRAM is
> committed the 512 depth is free. Re-evaluate if the CU count or LUT budget
> changes.

Implementation: the existing `src/common/uram_fifo.v` with
`FIFO_MEMORY_TYPE("block")` and `FIFO_DEPTH(512)` — no new RTL needed.

### 6.2 Control registers (`CU_CTRL`)

Random-access, not FIFO — so it is a separate mechanism from the instruction path.
The first four words are **mandatory and identical across all CUs**; everything
above `0x10` is CU-defined.

| Offset | Name | Access | Contents |
|---|---|---|---|
| `0x00` | `CU_CAPS` | RO | `cu_type[15:0]`, `version[7:0]`, `n_buffers[3:0]`, `inst_fifo_depth_log2[3:0]` |
| `0x04` | `CU_STATUS` | RO | `busy`, `error`, `inst_fifo_space[15:0]` |
| `0x08` | `CU_CONTROL` | RW | `enable`, `soft_reset`, `halt` |
| `0x0C` | `CU_ERROR` | RO | error code, sticky until cleared |
| `0x10+` | — | — | CU-defined |

`CU_CAPS` is deliberately **one word**: enough for the controller to enumerate a
mesh and learn each unit's type, version and buffer count without a hardcoded map.
If richer self-description is ever needed (per-buffer sizes, supported ops), the
clean extension is a descriptor block pointed at from CU-defined space — not a
wider `CU_CAPS`, so the mandatory region stays fixed forever.

---

## 7. Flow control

**Hop-by-hop** (exists): a full input FIFO raises `busy`, the upstream output port
stalls and **holds its flit** until the link clears — §2.1. Prevents buffer
overflow. Stalling without holding does not: it prevents the overflow by
discarding the flit that would have caused it.

**End-to-end** (to add): hop-by-hop alone does **not** prevent protocol deadlock.
If a node's input fills with requests and it cannot inject the response that would
drain them, the network locks. That is a dependency between message *classes*,
which XY routing does not address.

The rule this spec adopts: **a requester may not issue a request whose response it
cannot absorb.** Each requester holds `MAX_OUTSTANDING` credits, decremented on
issue, returned on response. At zero it stalls locally — which is safe — instead of
stalling the network, which is not. The same applies to `CU_INST`: a sender must
hold a credit reflecting the target's instruction-FIFO space.

`MAX_OUTSTANDING` is a **parameter, default 8**. It sets the reassembly buffer at
each requester: 8 × 256 B = 2 KB, comfortably one BRAM. Larger hides more DRAM
latency at linear cost.

Cost: one counter per endpoint, nothing in the router.

---

## 8. Reassembly

The receiver demuxes on `(src_x, src_y, txn_id)`. XY routing guarantees in-order
delivery per source→destination pair, so reassembly is: append payload flits until
`last`. No reorder buffer, no sequence numbers.

Flits from *different* sources interleave freely in the network; they land in
different reassembly slots, so this is harmless.

---

## 9. Caching

> **Superseded by [`../mas/cache.md`](../mas/cache.md).** The scheme below makes
> L2 and L3 the same parameterised cache module. That is no longer the plan: the
> CU-side level is **not a cache** — it has no tags, because the compute unit
> states its access pattern in the instruction itself, so there is nothing to
> guess. Only the MAS-side level is tagged. The wire protocol in §5.1/§5.2 is
> unaffected. Kept here for the reasoning about write policy and storage
> primitives, which carries over.
>
> As built, only L1 exists: `FILL` names a base address and an entry count and
> the manager writes the result into a dedicated memory. Neither cache level has
> RTL, so a cluster re-reads its operands from DRAM every pass — which is why the
> machine is currently fill-bound ([`../arch-design.md`](../arch-design.md) §4.2).

Three levels, all optional except L3:

| Level | Where | Storage | Scope | Write policy |
|---|---|---|---|---|
| L1 | inside the CU | BRAM | the CU's own buffers | n/a |
| **L2** | CU-side, between NIC and CU | BRAM **or** URAM (parameter) | private to one CU | **parameter** |
| **L3** | MAS-side | URAM | shared, memory-side | **write-through** |

L2 and L3 are **the same parameterised module**, differing in storage primitive,
capacity, and backing port (L2 backs to the NoC, L3 backs to AXI). A CU that
streams predictably omits L2 entirely; a CU with irregular access instantiates it.

Storage primitive is a parameter, not hardcoded: with 4 MAS nodes at 64–128 URAM
each, only 256–512 of 1280 URAM are committed, so CU-side L2 may use URAM too.

**L2 write policy is the CU's choice** (`WRITE_THROUGH` / `WRITE_AROUND`), because
it depends entirely on whether that unit re-reads what it writes — a
read-modify-write CU wants the line kept, a streaming-output CU does not want its
cache polluted.

**L3 is write-through.** Simple, keeps DRAM authoritative, no dirty-line tracking
or eviction logic.

> Worth revisiting once you can measure: a TPU writes an output tensor on every
> layer, and write-through sends all of it to DRAM. Write-back would coalesce those
> and save real DRAM bandwidth. It costs dirty bits and eviction logic. Since this
> is entirely internal to the cache module, starting write-through and changing
> later is cheap — just don't let the *protocol* assume DRAM is always current.

**No coherence.** Two CUs privately caching the same line, one writing, is the
entire difficulty of multiprocessor design. L2 is **read-only for shared data**,
writes go through to L3, and the `invalidate`/`flush` flags in `MEM_*_REQ` give
software explicit control. Decide this now — retrofitting non-coherence is easy,
retrofitting coherence is not.

---

## 10. AXI interworking

The mesh carries flits; the outside world speaks AXI4. Two modules bridge them,
and they are deliberately separate because they serve different planes:

| Module | AXI role | NoC role | Plane |
|---|---|---|---|
| **Global Orchestrator** | **slave** only | one NoC node | control |
| **MAS** | **master** only | one NoC node per instance | data |

Splitting them matters. Mixing bulk DRAM traffic with control traffic through one
attachment point makes the control path's latency hostage to data congestion —
and the host polls control constantly while touching bulk data rarely.

> **As built the table is one row, and the paragraph above was tested and found
> too strong.** MAG is a slave to the host, a master to its memory, and presents
> `MEM_PORTS` NoC attachments which **both** planes use. The control path is not
> hostage to data congestion, for a reason the original argument did not consider:
> the agent **wins** outbound arbitration against the memory engine on whichever
> port its destination row selects, so a busy memory path can never delay a
> dispatch off the port. Measured, the change was free — 2 CU 256³ unchanged at
> 18,701 cycles and 87.6% of peak ([`../mas/spec.md`](../mas/spec.md) §2.5).
>
> Inbound is weaker and honestly so: a port's link is in-order, so whichever
> consumer's flit is at the head delays the other's behind it until the
> round-robin grants it. That is head-of-line blocking, bounded and paid in
> latency — **except when the agent's RX FIFO is full**, in which case a
> non-signal control flit is never accepted and the memory traffic behind it on
> that link stops permanently. Latent rather than live today, and stated in full
> in [`../isa/memory.md`](../isa/memory.md) §3.2.1. It is a real instance of the
> §7 class of hazard: a dependency between message *classes*, which sharing an
> attachment makes possible where two wires did not.
>
> The separation the paragraph really wanted was **priority**, not a second wire.
> Buying it with a wire also bought a defect: MAG hung off two opposite edges of
> the mesh and every dispatch in the machine left through that one link.

### 10.1 Flit / AXI word packing

A flit is 288 bits, which is not a multiple of any AXI data width in use:

| AXI width | Beats per flit | Bits used |
|---|---|---|
| 64 (`jtag_axi`) | 5 | 288 of 320 |
| 512 (XDMA `M_AXI`) | 1 | 288 of 512 |

Word `k` carries flit bits `[64k+63 : 64k]`, little-endian, so word 0 holds
`payload[63:0]` and word 4 holds the header in its upper bits. The unused top bits
read as zero and are ignored on write. Sending one flit from Tcl is therefore a
single 5-beat `jaxi::write`, and receiving one a single 5-beat `jaxi::read`.

### 10.2 Global Orchestrator — AXI slave map

> **This block is now the agent inside MAG**, not a standalone NoC node. The
> registers below are unchanged and still decoded exactly this way; what moved is
> where they live and how they are reached — `src/kohakumas/mag.v` instantiates
> the agent and maps this window into its control range at
> `MAG_BASE = 0x1000_0000`, and `src/kohakuaxi/main_orch.v` is the separate
> host-facing command-RAM machine that drives them. The authoritative,
> against-the-RTL version of this table, including the two registers this one
> predates, is [`../isa/agent.md`](../isa/agent.md) §2.
>
> **It has no mesh attachment of its own either.** It shares MAG's memory ports:
> inbound each port demuxes by flit type, outbound a flit leaves from the port on
> its destination's row, and the agent answers at port 0's coordinate. So the
> "one NoC node" this section was written around is now **zero** —
> [`../isa/agent.md`](../isa/agent.md) §1.1 and
> [`../mas/spec.md`](../mas/spec.md) §2.5.

64-bit registers. This is the window the host polls; **none of these addresses are
NoC coordinates**.

| Offset | Name | Access | Contents |
|---|---|---|---|
| `0x0000` | `CTRL` | RW | `enable`, `mesh_reset`, `halt` |
| `0x0008` | `STATUS` | RO | `busy`, `error`, `mesh_ready` |
| `0x0010` | `CAPS` | RO | `flit_width`, `grid_lo`, `grid_hi`, `n_nodes` |
| `0x0018` | `IRQ_STATUS` | RW1C | pending event bits |
| `0x0020` | `IRQ_ENABLE` | RW | mask |
| `0x0040` | `PROG_DST` | RW | `{dst_y, dst_x}` for the next dispatch |
| `0x0048` | `PROG_LEN` | RW | flits to dispatch |
| `0x0050` | `PROG_KICK` | WO | begin dispatch |
| `0x0058` | `PROG_STATUS` | RO | `running`, `flits_left`, `credit` |
| `0x0060` | `PROG_CREDIT` | RW | seed the instruction-FIFO credit count |
| `0x0068` | `PROG_BASE` | RW | first staging slot of this program — added since, and it is what lets N programs be resident at once |
| `0x0070` | `SIG_DONE` | RO/W | completions from **every** node in one counter; any write clears it |
| `0x0100`–`0x0120` | `TX_FLIT[0..4]` | RW | one outgoing flit |
| `0x0140` | `TX_KICK` | WO | inject `TX_FLIT` |
| `0x0148` | `TX_STATUS` | RO | `space`, `full` |
| `0x0180`–`0x01A0` | `RX_FLIT[0..4]` | RO | head of the receive FIFO |
| `0x01C0` | `RX_POP` | WO | advance the receive FIFO |
| `0x01C8` | `RX_STATUS` | RO | `count`, `empty`, `overflow` |
| `0x1000`–`0x1FFF` | `NODE_STATUS[0..255]` | RO | status mirror, §10.4 |
| `0x2000`+ | `STAGE[]` | RW | instruction staging buffer, §10.3 |

The `TX_*`/`RX_*` window is a **raw flit mailbox**: it injects and receives
arbitrary flits with no interpretation. That is deliberate — an address-mapped
bridge could only ever emit `MEM_RD_REQ`/`MEM_WR_REQ`, and could not produce
`CU_INST`, `CU_DATA`, `CU_SIGNAL`, or a deliberately malformed flit. As the
bring-up and test instrument it has to be able to say anything the protocol can.

The mailbox is also the *first* thing to build: it is independently useful before
the orchestrator's program-fetch logic exists, and it is what makes the mesh
reachable from `jaxi::write` / `jaxi::read` on real hardware.

### 10.3 Global Orchestrator — instruction dispatch

**The orchestrator has no AXI master.** Programs are staged in a local buffer that
the host writes through the same AXI slave, at `0x2000+`. The host
then sets `PROG_DST`, `PROG_LEN`, `PROG_CREDIT` and kicks; the dispatcher walks
the buffer, rewrites each flit's destination to `PROG_DST`, and injects.

This works because **programs here are small** — a KohakuTPU CU instruction is
one flit, and one flit carries 240 bits of body, so a few hundred flits of
staging covers a dispatch batch comfortably. A local buffer is therefore
sufficient, and an entire AXI master implementation disappears with it. As built,
`STAGE_FLITS = 128`; a GEMM that needs more is cut into rounds by the driver
([`../isa/kernel.md`](../isa/kernel.md) §4).

Because destination is applied at dispatch rather than baked into the staged
flits, one staged program can be sent to any node, or to several in turn.

**The staging buffer is single-use while a dispatch is running.** The dispatcher
streams out of it as it goes, so the host must wait for `PROG_STATUS.running` to
clear before refilling. That is *not* the same as waiting for the target CU to
finish executing -- the CU keeps working while the next program is staged and
dispatched, which is where overlap between programs comes from.

> `PROG_BASE` softened this considerably after the fact. Because a program names
> its own first slot, several programs live in the window at once and all of them
> run concurrently — measured at 1.97x on `C[32,32]` across two clusters. Without
> it, a second cluster's flits cannot be staged until the first has consumed its
> own, which serialises clusters that share no data at all.

Credits (§6.1, §7): `PROG_CREDIT` seeds the count, each dispatched flit consumes
one, and `CU_SIGNAL`/`INST_COMPLETE` from the target returns one. The dispatcher
stalls locally at zero rather than backpressuring `CU_INST` into the mesh, which
is the protocol deadlock §7 exists to prevent.

> A later option, if programs ever do outgrow the buffer: the orchestrator issues
> a `MEM_RD_REQ` naming the **CU** as `src`, so MAS delivers instruction data
> straight to the CU and the orchestrator never touches it. That needs two
> additions — credit delegation (the issuer holds the credit, not the node named
> in `src`) and a response-type override so MAS replies `CU_INST` rather than
> `MEM_RD_RESP`, for which `addr_spare` is the natural home. Deliberately not in
> v1.

### 10.4 Status mirror

`NODE_STATUS[n]` is a BRAM-backed word per node, updated whenever a `CU_SIGNAL`
arrives from that node. The host polls **this**, not DRAM:

| Bits | Field |
|---|---|
| `[63:56]` | `last_code` — the `CU_SIGNAL` code |
| `[55:24]` | `last_arg` |
| `[23:8]` | `signal_count` — increments per signal, so the host can detect an event it did not read |
| `[7:1]` | reserved |
| `[0]` | `valid` |

252 nodes x 8 B = 2 KB. The point is latency and isolation: polling DDR4
across PCIe costs hundreds of nanoseconds per read *and* injects traffic into the
memory system the CUs are trying to use. Polling a BRAM behind the AXI slave costs
a BAR read and disturbs nothing.

`signal_count` rather than a sticky flag because the host may poll slower than
events arrive; a counter tells it how many it missed, a flag does not.

> **Both of the orchestrator's arrays land in LUTRAM, not BRAM** (measured, not
> assumed: `tests/run_synth_check.ps1`). The staging buffer maps to `RAM64M8 x100`
> at `STAGE_FLITS = 128` and `NODE_STATUS` to `RAM256X1D x64`; a
> `ram_style = "block"` attribute on either is rejected as *"Infeasible"* and
> silently downgraded. `NODE_STATUS` cannot be BRAM as written because
> `signal_count` is a same-cycle read-modify-write. Together they cost roughly
> 2.7k LUTs, and there is one orchestrator per system, so this is recorded rather
> than fixed. It matters only if `STAGE_FLITS` grows by an order of magnitude.

**`CU_SIGNAL` never enters the RX FIFO.** It updates this mirror and is then
dropped. Queuing it would make an unread RX FIFO fatal: once full, the
orchestrator raises `noc_in_busy` and stops accepting *any* inbound flit,
including the `INST_COMPLETE` signals that return dispatch credits (§6.1, §7).
The machine then stalls after exactly `RX_DEPTH` completions with nothing
reported anywhere. Signals are therefore always absorbed, and `RX_FLIT` carries
only traffic that has no other home -- `CU_CTRL` replies, `CU_DATA` addressed to
the orchestrator, and anything injected during bring-up.

### 10.5 MAS

> As built it is **MAG**: an AXI *slave* facing the host with one AXI master per
> memory port facing its own memory, plus `MEM_PORTS` NoC attachments — one per
> mesh row — and the dispatch agent of §10.2–4, which **shares those same
> attachments** rather than holding one of its own. The wire protocol below is
> unchanged and is what `src/kohakumas/mag.v` implements. One addition the text
> below predates: **a read request can ask for quantisation**, and MAG then
> converts FP16 to int7 + E5M3 on the way out, so the response carries a
> different element format from the memory it came from. The RTL-accurate
> version of this section is [`../isa/memory.md`](../isa/memory.md), and §1.1
> there is the type demux that lets one coordinate serve two consumers.

MAS is a NoC node with an AXI master. It accepts `MEM_RD_REQ`/`MEM_WR_REQ`,
issues AXI transactions, and returns `MEM_RD_RESP`/`MEM_WR_ACK` to `src_x`/`src_y`
with `txn_id` echoed.

`MEM_*.addr[33:0]` maps **directly** onto the physical DDR4 map — no translation.
Which of the four DDR4 channels serves an address is MAS-internal; the protocol
never exposes it.

Bandwidth is the constraint that decides how many MAS instances you need. One NoC
link carries `FLIT_W x f`: at 288 bits and 300 MHz that is **10.8 GB/s**. Four
DDR4-2400 channels supply **76.8 GB/s**. A single MAS node is therefore a ~7x
bottleneck, and full DRAM bandwidth needs roughly **8 injection points** — either
several MAS nodes at different mesh positions, or a wider/faster NoC. Size this
deliberately rather than discovering it later.

### 10.6 Clock domains

The NoC, the orchestrator's AXI slave, and `jtag_axi` should start on **one
clock** (the 100 MHz `clk_wiz_0` domain on the current carrier). CDC is a
parameter, default off.

Async FIFOs bring their own class of bug, and debugging them at the same time as a
new protocol is a false economy. `docs/noc/README.md` already assumes CUs run at
the same frequency or a power-of-two division, so a single domain is the natural
starting point. MAS is the first module with a genuine reason to cross domains,
since the DDR4 user interface runs at 300 MHz.

Note also that anything on the XDMA `axi_aclk` domain is unreachable whenever the
PCIe link is down — so the orchestrator's slave port belongs on the always-running
fabric clock, not on XDMA's.

---

## 11. Guarantees

Provided:

- **Lossless delivery** — backpressure and retry, never drop and never
  duplicate; both halves of §2.1, not either one
- **In-order per (source, destination) pair** — from XY routing
- **No routing deadlock** — from XY routing
- **No protocol deadlock** — provided endpoints honour §7 credits

Not provided:

- ordering between *different* source/destination pairs
- atomicity across multiple messages
- coherence between caches
- multicast

---

## 12. Deferred

**Multicast.** The dominant reuse pattern in a TPU is one weight tensor reaching
many CUs. Multicast — one flit replicated at mesh branch points — turns N reads
into 1, a larger win than any cache hit-rate improvement. The existing
`port_out`/`port_valid`/`clear` structure suits it well: let one flit assert valid
on several outputs and clear the slot only when all have taken it. Deferred because
it complicates credits (one request, many responses).

**Virtual channels.** Would permit adaptive routing and per-class separation. Not
needed once XY routing and end-to-end credits are in place, and expensive in
buffers. Revisit only if measurement demands it.

---

## Decisions taken

| Question | Decision |
|---|---|
| `POS_WIDTH` | **4** — 16×16 space, 14×14 routers, 56 border PEs, 4 corners invalid |
| Border-PE routing | **clamp to adjacent router, then one outward hop** — keeps pure XY |
| Link handshake | **busy/valid WITH RETRY** — sender holds until `busy` is low, receiver accepts iff `valid && !busy`. Both halves, no tolerant middle setting — §2.1 |
| Receiver `busy` | **plain `full`** — retry removes the need for a margin, and `sync_fifo`'s `wr_almost` is not one (`USE_ADV_FEATURES` is zero) |
| `MAX_OUTSTANDING` | **parameter, default 8** |
| L2 write policy | **parameter** — the CU decides |
| L3 write policy | **write-through** |
| `addr` width | **34 bits** + 6 explicitly named `addr_spare` |
| CU instruction buffer | **mandatory FIFO, whole flits, BRAM, depth 512** |
| `CU_CTRL` register map | **first 4 words mandatory**, rest CU-defined |
| CU discovery | **one `CU_CAPS` word** for now |
| `CU_SIGNAL` codes | **centrally allocated** `<0x40`, CU-defined above; `arg` always CU-defined |
| AXI attachment | **two modules**: orchestrator (control, **slave only**) and MAS (data, master). **As built, one**: MAG, with the agent sharing its memory ports and the planes told apart by flit type — §10, §10.5 |
| Instruction source | **staged in orchestrator BRAM**, not fetched from DRAM -- programs are small |
| Host control window | **register-mapped mailbox + BRAM status mirror**, not address-mapped |
| Host status polling | **`NODE_STATUS` BRAM**, never DDR4 |
| Clock domains | **single clock to start**, CDC a parameter defaulting off |
