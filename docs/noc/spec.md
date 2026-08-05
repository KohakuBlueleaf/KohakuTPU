# HakuNoC Specification

**Status: DRAFT, revision 2.** Open questions are marked ❓.

HakuNoC is a 2D-mesh packet network carrying three kinds of traffic:

1. **Memory access** — a node reads/writes DRAM via MAS
2. **CU ↔ CU data** — bulk transfer from one unit's L1 into another's L1
3. **CU ↔ CU control** — instructions and completion signals

The network deliberately knows nothing about what a compute unit *is*. It defines
how to address a node, how to frame a message, and what it guarantees. Everything
above that is the CU's business.

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
| `0x4` | `CU_DATA` | CU → CU | bulk L1→L1 transfer |
| `0x5` | `CU_INST` | any → CU | instruction delivery |
| `0x6` | `CU_SIGNAL` | CU → any | completion / synchronisation |
| `0x7` | `CU_CTRL` | any → CU | control-register access |
| `0xF` | `ERROR` | any | unroutable / malformed / faulted |

`0x8`–`0xE` reserved.

`type[3]` splits the two worlds: `0x0–0x3` memory, `0x4–0x7` CU. **The cache
filters on exactly that bit** — memory messages traverse the cache, CU↔CU messages
bypass it entirely.

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

240 bits covers every instruction in `docs/controller.md` (largest is 71) in a
single flit. Longer encodings continue in following flits.

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

### 6.1 Instruction FIFO (`CU_INST`)

A FIFO, because instruction streams are ordered and `docs/controller.md` already
specifies blocking in-order execution.

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
stalls. Prevents buffer overflow.

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

## 10. Guarantees

Provided:

- **Lossless delivery** — backpressure, never drop
- **In-order per (source, destination) pair** — from XY routing
- **No routing deadlock** — from XY routing
- **No protocol deadlock** — provided endpoints honour §7 credits

Not provided:

- ordering between *different* source/destination pairs
- atomicity across multiple messages
- coherence between caches
- multicast

---

## 11. Deferred

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
| `MAX_OUTSTANDING` | **parameter, default 8** |
| L2 write policy | **parameter** — the CU decides |
| L3 write policy | **write-through** |
| `addr` width | **34 bits** + 6 explicitly named `addr_spare` |
| CU instruction buffer | **mandatory FIFO, whole flits, BRAM, depth 512** |
| `CU_CTRL` register map | **first 4 words mandatory**, rest CU-defined |
| CU discovery | **one `CU_CAPS` word** for now |
| `CU_SIGNAL` codes | **centrally allocated** `<0x40`, CU-defined above; `arg` always CU-defined |
