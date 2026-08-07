# MAS specification

The NoC↔AXI data plane: address translation, caching, and DRAM access.

Wire protocol is fixed by [`../noc/spec.md`](../noc/spec.md) §5.1/§5.2 and is
**not** restated here — MAS is the other end of a protocol that already exists
and already has a working stub (`tests/noc/noc_fake_mem.v`).

Status: **partly built.** The adapter itself exists — `src/kohakumas/mag.v` is an
AXI slave with an address decode, **several independent memory ports** (§2.4),
a quantising host-upload path, and the dispatch agent **sharing those same ports**
rather than holding a mesh attachment of its own (§2.5); it runs end to end
([`README.md`](README.md)). What is **still design only** is everything to do
with translation and caching: the TLB, address slicing across MAS instances,
and the bandwidth sizing for a populated partition. Sections below are marked
where they diverge from what is built.

The instruction set MAG answers on the NoC side —
`MEM_RD_REQ` / `MEM_RD_RESP` / `MEM_WR_REQ`, and the quantiser's output format —
is [`../isa/memory.md`](../isa/memory.md), written against the RTL.

---

## 1. What MAS is

**An AXI adapter that owns a piece of memory**, with NoC ports on the side.

```
                  AXI slave IO  (from the main SmartConnect)
                         │
                    address decode
       memory range ┌────┴────┐ control range
                    ▼         ▼
              upload FSM    agent  ── dispatch, NODE_STATUS,
              + quantiser     │      NoC packet injection
                    │         │      (NO NoC port of its own)
                    │    ┌────┴────┐
                    │    │  share  │  in : demux by FLIT TYPE
                    │    │  layer  │  out: by DESTINATION ROW
                    │    └─┬──┬──┬─┘
                    │      │  │  │
   NoC mem port 0 ──┼──────┴──┼──┼──► memory port 0 ──► AXI master 0 ─┐
   NoC mem port 1 ──┼─────────┴──┼──► memory port 1 ──► AXI master 1 ─┼─► DDR4 / URAM
        ...         └────────────┴──►      ...      ──► AXI master N ─┘
```

Three interfaces, and the shape is the design:

- **AXI slave**, facing the host and the main orchestrator. Writes into the
  memory range go to memory through the upload path, optionally quantised on the
  way in; writes into the control range become NoC packets or agent commands.
- **AXI masters**, facing the memory bound to this instance — one per memory
  port plus one for the upload, not one shared channel (§2.4).
- **NoC ports, and nothing else.** They are **MAG ports rather than memory
  ports**: each carries `MEM_RD_REQ`/`MEM_WR_REQ` *and* the agent's control
  traffic, told apart by flit type inside MAG (§2.5). One per mesh row, all on
  the west edge, and a port attaches to the **NoC** rather than to any
  particular cluster (§2.4).

**MAS is a slave on the main interconnect, not a master.** The SmartConnect's
slave list previously held DDR controllers; it now holds MAS instances, each
owning a DDR controller. Same port count, same crossbar — the memory moved one
level down, behind an adapter. Nothing was added to the interconnect.

**The requester names itself as `src`.** A cluster's read reply comes straight
back to the cluster. The orchestrator never touches operand data.

### 1.1 Why the control range matters

A write to the control range **becomes a NoC packet**. That one decode gives:

| writer | what it gets |
|---|---|
| main orchestrator | dispatch and status without being a NoC node |
| JTAG / XDMA | packet injection for bring-up and debug, via `jaxi::write` |
| host | TLB and cache control through the same path as everything else |

No private control fabric, no second interface to keep consistent, and the
AXI↔NoC bridge that was a separate planned item is now a side effect of the
address map.

### 1.2 What MAS is not

It is **not** a coherence point, because there is nothing to be coherent with:
CU-side memory is explicitly managed (see [cache.md](cache.md)), so software
already knows when its copy is stale. No snooping, no directory.

---

## 2. Shape: narrow NoC ports, wide AXI masters

The memory controller decides this, so start there. §2.1–§2.3 are the sizing
argument for a populated partition and remain design; **§2.4 is what is built**,
and it arrived at the same shape from the other end — measurement rather than
bandwidth arithmetic.

**MIG moves bandwidth by being wide and slow.** The UltraScale+ DDR4 controller
in 4:1 mode presents a user interface at `memory_clock / 4` and 8× the DQ width.
For DDR4-2400 on x64 that is:

```
   512 bits @ 300 MHz  =  19.2 GB/s  =  2400 MT/s x 8 B     per channel
   DDR4 BL8 on x64     =  8 x 64 b   =  64 B                minimum access
```

Two facts fall out of that, and both matter more than they look.

**The user clock is 300 MHz — our clock.** No CDC on the AXI side for DDR4-2400
at 4:1. §5 previously assumed a crossing; against this configuration there is
none, and MAS stays single-domain. That is worth checking per part and per IP
configuration rather than assuming, but it is the common case.

**One channel is exactly two NoC ports.** A 256-bit flit payload at 300 MHz is
9.6 GB/s, so 19.2 GB/s of DDR is 2.0 NoC ports.

That sets the shape of a slice: **2 NoC ports in, 1 AXI master out.**

```
   MAS slice                     MAS slice                  MAS slice ...
   ┌──────────────────┐          ┌──────────────────┐
   │ NoC port ─┐      │          │ NoC port ─┐      │
   │           ├ TLB  │          │           ├ TLB  │
   │ NoC port ─┘ cache│          │ NoC port ─┘ cache│
   │           512 b  │          │           512 b  │
   │        AXI master│          │        AXI master│
   └───────────┬──────┘          └───────────┬──────┘
             DDR ch0                       DDR ch1
```

**There is no crossbar, and that is the point.** Because addresses are sliced
(§2.1), a line has exactly one home and a request goes straight to the slice
that owns it. Slices never talk to each other — no arbitration between them, no
shared cache array, no 8-way fan-in of 288-bit buses converging on one block.

The mesh already *is* the crossbar. Building a second one inside MAS would
duplicate the network's whole job and put the result in one physical place,
which on a multi-die part is exactly where it cannot go (§2.2).

### 2.1 Address slicing

Within that, addresses are still **sliced** so each line has one home:

```
   slice = addr[SLICE_HI:SLICE_LO]   ->   which cache bank and AXI master
```

**Why slicing rather than replication.** A line lives in exactly one slice, so
two clusters reading the same address reach the same cache — the sharing in §6
is captured, and the cache is coherent with itself *by construction* with no
protocol. Replicated caches would need invalidation between them for the same
benefit.

**Bursts must not cross a slice boundary.** With slice granularity ≥ the largest
burst this is free: a 4-flit L1-entry fetch is 128 B, and slicing at ≥4 KB
leaves three orders of magnitude of headroom. Splitting a burst across slices
and reassembling would need reorder buffering at the requester and is not worth
it.

> `SLICE_LO` sits **above** the cache line and **below** the page, so a line is
> never split and a page maps to one slice. Finer interleaving spreads load
> better but breaks the "one page, one TLB entry, one home" property.

### 2.2 Port budget, and why it survives the floorplan

Proposed: **8 NoC ports for MAS, 4 for the orchestrator** — 12 between them.

```
   8 MAS ports  x 9.6 GB/s  =  76.8 GB/s  =  exactly 4 x DDR4-2400
```

Nothing is stranded at either end: eight ports is precisely what four channels
can absorb, so the mesh side and the memory side are matched.

**Whether 8 is enough depends on the cluster's L2, not on MAS.** Operand demand
is `16(Ma+Nb)/(Ma·Nb)` words/cycle for an L2-resident `Ma × Nb` output tile:

| L2-resident tile | words/cycle | 32 clusters | URAM/cluster | ports needed |
|---|---|---|---|---|
| 64 × 128 | 0.375 | 115 GB/s | ~6 | 12 |
| **128 × 256** | **0.188** | **57.6 GB/s** | **~10** | **6** |
| 256 × 256 | 0.125 | 38.4 GB/s | ~13 | 4 |

So **8 ports is comfortable once the cluster holds a 128×256 tile**, which costs
about 10 URAM per cluster — 320 of 1,280 across the machine. That is the trade
worth taking: URAM is cheap and local, NoC ports are expensive and global.

> **What was actually built is the 64×128 row, and it spends no URAM.** The
> resident tile is 5 BRAM36 at any depth up to 512, so `Gm = 16, Gn = 32` uses
> depth that was already paid for. A 128×256 tile is a 4x larger output block,
> which every problem then pads up to — bandwidth bought by spending output
> granularity, charged back on every shape that is not enormous.
>
> The demand it was sized against has also moved. Measured at the 256-cube on
> 2 clusters, memory is **idle 88%** of the run: operands are pre-quantised
> (half the bytes, no quantiser pass) and B is resident across the m loop
> (a quarter of all traffic gone). Re-derive this table from beats before
> spending URAM on it — `.plans/CONTEXT.md` §2 and §3, and §2.4 for what the
> port count actually became and why.

> Sizing MAS in isolation gets this wrong in both directions. Ports are not set
> by DRAM bandwidth (the cache hides most of it) and not by raw cluster demand
> (L2 hides most of *that*). They are set by what neither level captures.

The orchestrator's 4 ports are **not** about bandwidth — a program is a handful
of flits per pass and completion signals are one each. They are about **fan-out
and distance**: one port is a single place through which dispatch to 64
endpoints and status from all of them must funnel, and on a large mesh that is
a latency and congestion problem long before it is a throughput one.

> **As built the agent gets that fan-out for nothing, and the 12 becomes 8.**
> The argument above is right about what the agent needs and wrong about how to
> buy it: dispatch spreads across all `MEM_PORTS` attachments without any of them
> being the agent's, because the two consumers are told apart by what a flit *is*
> rather than by which node it went to (§2.5). Control traffic is a handful of
> flits against a stream of operand words, so it costs the memory side nothing
> measurable — and the ports it borrows are already one per mesh row, which is
> exactly the distribution the 4-port proposal was trying to arrange.

### 2.3 The floorplan this implies

The VU13P is a **4-SLR** device, and crossing an SLR boundary costs a dedicated
register stage. A design with one big central block fights that; a sliced one
falls into it naturally:

```
   SLR 3   8 clusters   1 MAS slice (2 ports, agent shares them)   DDR ch3
   SLR 2   8 clusters   1 MAS slice (2 ports, agent shares them)   DDR ch2
   SLR 1   8 clusters   1 MAS slice (2 ports, agent shares them)   DDR ch1
   SLR 0   8 clusters   1 MAS slice (2 ports, agent shares them)   DDR ch0
```

The agent is **not** a separate line in that floorplan any more. It sits inside
the slice and rides the slice's ports (§2.5), so an SLR's mesh attachments are
exactly its memory ports.

Per SLR that is roughly 111 kLUT of 432 k (26%), 2,048 DSP of 3,072 (67%), and
~80–140 URAM of 320 — every dimension comfortable, with the DSPs correctly the
tightest.

The only nets that cross SLRs are **mesh links**, which are point-to-point and
already registered at every router. Nothing about MAS or the orchestrator needs
to span a die.

> This is the concrete reason to prefer address slicing over a shared cache with
> a crossbar. The crossbar version is not merely more logic — it is logic that
> cannot be placed, because 8 × 288-bit buses plus 4 × 512-bit AXI converging on
> one block will not sit inside one SLR and will not make 300 MHz across four.

### 2.4 As built: `MEM_PORTS` independent ports, one per mesh row

**A memory port is the unit the machine grows by, and that is architecture
rather than an option.** `src/kohakumas/mag_mem_port.v` is a whole memory port —
intake queues, read engine, its own quantiser, write slots, emitter, and its own
AXI master channel — and `src/kohakumas/mag.v` generates `MEM_PORTS` of them.
Nothing is shared between ports except the address space on the far side of AXI.

**What forced it was not bandwidth.** MAG used to be one instance of that body
serving every cluster in the partition, and the read engine fetches one entry at
a time, so every cluster queued behind one FSM and one emit buffer. With
per-cluster work held identical and everything pre-quantised:

| | `fetch` cyc/entry (floor 4) | busiest data path |
|---|---|---|
| 2 CU | 7.6 | — |
| 4 CU | 25.6 | — |
| 8 CU | 93.3 | `noc_out` 60.2%, `mem_rd` 55.6% |

Latency tracked the cluster count exactly while **nothing was saturated**. It
was one *server*, not one wire — the failure a bandwidth argument cannot see,
and the reason §2.2's port budget is necessary but not sufficient.

**A port attaches to the NoC, not to a cluster.** Routing is X-then-Y on clamped
coordinates ([`../noc/spec.md`](../noc/spec.md) §2), so **every cluster can reach
every port through the mesh**, and which port a cluster's memory traffic leaves
by is a routing choice rather than a wiring one. A cluster spans two adjacent
rows — manager on its band's outer row, accumulator on the inner one
([`../system.md`](../system.md) §2.3) — so the choice is between those two:
**columns nearer MAG exit by their manager's row, the farther half by their
accumulator's.** That keeps both of a band's ports carrying instead of leaving
one idle, and it suits the directions, because the links are **full duplex** and
a manager mostly *receives* fill responses while an accumulator mostly *sends*
results — the two rows load opposite directions rather than competing for one.
Putting two ports on one router would split the *server* and leave the funnel,
which is why the ports sit at different mesh nodes; that works because the
destination is clamped into the grid and the outward hop is taken only on
arrival.

> **That split is measured, and it is not free.** Sending a band's two column
> groups out by different rows costs about **three points of peak at 8 CU** —
> `flops` 72.7% against 75.7% when both of a cluster's endpoints shared one row.
> It is not the memory service giving way: `fetch` held at 7.1 → 7.2
> cycles/entry and `wslot_full` *fell* 5.5% → 2.1%. The cost is the routing
> itself. It is accepted for the physical locality the column layout buys, and
> **tuning it is deferred until the vector and general cores exist** — see
> [`../perf.md`](../perf.md) §0.1.

**The ports are MAG ports, not memory ports.** Each carries operand traffic and
the agent's control traffic on the same wires, demuxed by flit type (§2.5), so
the agent has no node of its own and the north, south and east edges stay free
for whatever attaches next.

**Every one of those nodes is on the west edge, and no other edge is used.** The
agent briefly had a node of its own on the *east* edge of row 1, which made MAG a
single module attached at two opposite edges of the mesh; it no longer has one
(§2.5). North, south and east are therefore free for whatever attaches next — a
vector unit and a general core are what the plan owes.

**The host upload is its own path, on its own channel.** It has its own FSM and
its own `mx_quant` instance, so an upload and a fetch no longer contend — the
read path used to hold the single quantiser under a mutex. Uploads are bursty
and rare against a steady state that is neither, so they get a channel rather
than a share of one.

**The memory behind them is multi-channel to match.** `src/kohakumas/axi_ram.v`
takes a `PORTS` parameter — independent AW/W/B and AR/R state per port over one
array, flattened so that `PORTS = 1` is bit-identical to the single-port module
it replaced. That matters because a single 256-bit port at 300 MHz is 9.6 GB/s
against DDR4-2400's 19.2 GB/s per channel: with eight clusters behind it, a
one-channel stub stops being scenery and becomes the answer.

Measured, all pre-quantised, per-cluster work identical:

| | one port | per-row ports | `fetch` cyc/entry |
|---|---|---|---|
| 2 CU 256³ | 20,293 cyc · 80.7% of peak | **18,701 · 87.6%** | 7.6 → **5.4** |
| 8 CU 256×1024×256 | 44,193 cyc · 37.1% | **24,115 · 67.9%** | 93.3 → **29.9** |

Eight clusters got 1.83× faster with the arithmetic bit-unchanged. What limits
them now is instruction dispatch, not memory.

> **`mem_rd_count` and `mem_wr_count` are sums over ports.** MAG adds the
> per-port counters so the AXI-level totals stay one number whatever
> `MEM_PORTS` is. Anything deriving a *utilisation* from them must divide by the
> port count as well as by the run — see [`../perf.md`](../perf.md) §2.2, where
> not doing so reported 101.9% and looked exactly like a saturated bus.

> **Synthesised since.** `mag_mem_port` closes **330.0 MHz** out of context
> (WNS +0.303 ns at the 3.333 ns target) at 7,592 LUT, 7,650 FF, 32 DSP and
> **0 BRAM / 0 URAM**, with `mx_quant` inside it at 400.6 MHz — the binding path
> is now the write-request FIFO into the slot-ready flags, not the quantiser.
> [quantiser-timing.md](quantiser-timing.md) §4 has the breakdown. The multi-port
> `mag` wrapper and the changed NoC output port are still verified in cycles
> only, and every OOC number is unplaced and therefore an upper bound.

### 2.5 As built: the agent shares the memory ports

**MAG presents `MEM_PORTS` NoC attachments and no more.** The agent — the
`noc_orchestrator` instance inside MAG — has no node of its own. `agt_in_*` /
`agt_out_*` are gone from `mag`'s port list and the `AGT_X` / `AGT_Y` parameters
are deleted; there is no separate coordinate left to place.

**What was wrong with the old shape** is not that it cost a port. It is that one
module was physically attached at two opposite edges of the mesh, and that *every
dispatch in the machine left through that single east link*. Dispatch is what the
eight-cluster case is short of (§0 of [`../perf.md`](../perf.md)), so the one
resource it had was the one that could not be widened.

**Inbound is a demux by flit TYPE, per port.** `T_MEM_RD_REQ` (0),
`T_MEM_WR_REQ` (1) and `T_MEM_WR_DATA` (4) are the engine's; every other type is
the agent's.

```
   mem_in_valid[p] && type is a memory type   ->  this port's mag_mem_port
   mem_in_valid[p] && anything else           ->  the agent
```

The agent has one input, so the ports **round-robin** into it, and the pointer
advances only on an accepted flit — moving it every cycle would let a port lose
its turn to one that had nothing to offer. A port that is not granted simply
holds `mem_in_busy`, and its sender holds the flit: that is not a new
requirement, it is the link's existing retry contract
([`../noc/spec.md`](../noc/spec.md) §2.1), which every sender on this mesh
already obeys.

**Outbound is steered by DESTINATION ROW.** A flit for row `y` leaves from the
port that sits on row `y`; a destination row with no port on it falls back to
port 0. That is the point of the change — dispatch now spreads across every
attachment instead of queueing behind one.

**The agent wins outbound arbitration against the memory engine.** Its traffic is
a handful of control flits against a stream of operand words, so the cost to
memory is negligible, and the engine already holds `valid` and `data` until a
cycle with `busy` low, so being held off is a case it handles by construction.
Engine priority would be the wrong way round: it can starve dispatch exactly when
the machine is busiest, which is the state dispatch most needs to keep up in.

**The agent answers at port 0's coordinate, `(MEM_X, MEM_Y)`.** A CU replying to
the source of its `CU_INST` addresses that node; the flit arrives at port 0; the
demux hands it to the agent because its type is not a memory type. A
`MEM_RD_REQ` to the same coordinate still reaches the engine. **One address, two
consumers, told apart by what the flit is rather than by where it went.**

Measured at every cluster count, both operands pre-quantised:

| | before | after |
|---|---|---|
| 2 CU 256×256×256 | 18,701 cyc · 538.3 · 87.6% | **identical** |
| 4 CU 256×512×256 | `flops` 79.4% | **79.6%** |
| 8 CU 512×1024×256 | 43,382 cyc · 1856.3 · 75.5% | **43,315 · 1859.2 · 75.7%** |

**Free, and fractionally better.** The reason is the shorter path rather than the
wider one: an agent flit no longer crosses the mesh from the east edge to reach a
cluster, because it now leaves from a west-edge port on the destination's own
row. That is 67 cycles in 43,382 — **0.15%, which is inside what any topology
change can move either way**, and it is not evidence that dispatch stopped being
the eight-cluster limiter. It did not: the cause is a serial staging cursor,
upstream of the link and untouched by this
([`../optimization.md`](../optimization.md) §J4).

Error profiles are unchanged at every count:

| | p50 | worst | outliers |
|---|---|---|---|
| 2 CU | 1.70e-04 | — | 4 of 65,536 over 10% |
| 4 CU | 1.70e-04 | 2.43e+00 | 7 of 131,072 |
| 8 CU | 1.71e-04 | 2.43e+00 | 159 of 524,288, 49 of them over 10% |

`mag_system` passes its 257 checks and `mag_wslot` passes.

> **`in_bp` changed meaning, and nothing got slower.** The bench's `in_bp`
> counter — cycles in which MAG asserts `mem_in_busy`, i.e. refuses NoC traffic —
> went from **0.0% to 39.8%** at 2 CU and **0.0% to 22.4%** at 8 CU, on runs whose
> cycle counts did not move. A port now asserts busy on any cycle whose offered
> flit belongs to the *other* consumer, so refusal is **routine demultiplexing,
> not congestion**. It no longer counts blocked memory traffic and it is no longer
> evidence of pressure; it is read as **"port declined a flit (incl. not-mine)"**.
> **No time transferred anywhere** — the rates and error profiles above are the
> proof. See [`../perf.md`](../perf.md) §1.1.
>
> This project has twice drawn a false conclusion from a percentage whose
> denominator changed underneath its own name (§2.4's counter note, and
> [`../perf.md`](../perf.md) §2.2). This is the third such rename, recorded
> before anyone reads it as a regression.

---

## 3. Translation

A **TLB**, not a cache tag array — it answers *where does this page live*, which
is a different question from *do I have this line*.

```
   TLB entry
     valid       1
     vpn         34 - log2(PAGE)      virtual page number
     ppn         34 - log2(PAGE)      physical page number in the backing store
     backing     2    0 = DDR4, 1 = on-chip URAM scratch, 2 = unmapped/fault
     flags       4    cacheable, read-only, ...
```

Three things this buys, in order of importance:

**The backing store becomes a property of the page, not of the design.** A
tensor can live in on-chip URAM scratch today and DDR4 tomorrow with no change
anywhere above MAS. That is what makes "bring the system up against AXI URAM,
move to DRAM later" a configuration change rather than a port — the two are
logically identical and differ only in latency and capacity.

**The host owns placement.** Which tensor is resident on chip is a scheduling
decision the compiler makes, and the TLB is how it expresses that.

**Isolation.** An unmapped access faults instead of corrupting another tensor.

### 3.1 Filled by a memory write, not walked by hardware

**There is no hardware page walker.** Allocation here is static and known at
compile time, tensors are few and large, and a walk would add DRAM round-trips
to the latency of a miss for no benefit. A walker is what you build when
allocation is dynamic and the working set is unknown — neither is true here.

Entries are written with an ordinary **`MEM_WR_REQ`** aimed at a reserved
region, not through a private register port:

```
   0x3_FFFF_0000 .. 0x3_FFFF_FFFF     MAS control aperture, NOT translated
       + 0x0000   TLB entries, one per 32 B word
       + 0x8000   cache control: invalidate, flush
       + 0xC000   counters: hits, misses, faults
```

**MAS speaks one protocol.** The host programs the TLB by having the
orchestrator inject a `MEM_WR_REQ` — which the orchestrator can already do, it
is a raw flit through the mailbox — so there is no second control path to build,
arbitrate, or keep consistent with the first. A CU can do the same if a workload
ever wants to stage its own translations.

The aperture is **untranslated**, which resolves the obvious chicken-and-egg:
programming the TLB must not itself require a translation. Reserving a fixed
window at the top of the map costs nothing, since nothing else may use it.

The cost of no walker is that a program touching more than §3.2's mapped
capacity needs entries rewritten between phases. That is a scheduling problem,
and the compiler already knows the phase boundaries.

### 3.2 Page size — two of them, and it is a property of the backing store

DRAM and on-chip scratch want different sizes, so the TLB supports both:

```
   PAGE_SHIFT_LARGE = 21    2 MB     DRAM        VPN = addr[33:21], 13 bits
   PAGE_SHIFT_SMALL = 16    64 KB    URAM        VPN = addr[33:16], 18 bits
```

The reasoning is capacity against granularity. DRAM is 16 GB and holds whole
tensors, so 2 MB pages map a lot with few entries. URAM scratch is a few MB
total, so 64 KB pages let the compiler place individual tiles without wasting a
2 MB entry on a 200 KB tensor.

**One entry, one size bit.** Each entry carries a `large` flag, and the CAM
masks its compare accordingly — bits `[33:21]` for a large page, `[33:16]` for a
small one. Standard, and cheap at this scale: a 64-entry maskable CAM on an
18-bit key is single-cycle.

```
   64 x 2 MB   = 128 MB DRAM mapped at once
   64 x 64 KB  =   4 MB URAM mapped at once
```

Both shifts are **parameters**, not constants, because the right pair depends on
the part and the workload — a device with more URAM, or a model with many small
tensors, moves them.

> The alternative is two separate TLBs looked up in parallel, one per size. That
> is simpler to build and wastes entries when a program is lopsided. The masked
> CAM shares one pool between both sizes, which is what you want when you do not
> know the mix in advance.

**128 MB of DRAM mapped at once is the number to watch.** A model whose working
set exceeds it needs entries reprogrammed between phases — see §7.

---

## 4. Ordering

The requester assembles a burst **positionally** — it counts arriving flits and
places them by index. That imposes exactly one rule:

> **A response burst is atomic per destination.** MAS must not interleave flits
> of two responses heading to the same node.

Everything else is free. Responses to *different* nodes may interleave, requests
may complete out of order, and no global ordering is implied. Two requests from
one CU may return in either order because each carries its own `txn_id`.

The simplest implementation that satisfies this is a per-destination response
queue that is drained a whole burst at a time.

---

## 4a. Writes are first-class

Reads dominate the traffic, so it is easy to write a memory-system document that
quietly assumes them. Every NoC member can write, and three distinct kinds of
write reach MAS:

| writer | what | notes |
|---|---|---|
| **cluster accumulator** | result tiles, one 256-bit word per sub-tile | the common case: 64 per output block |
| **host, via the orchestrator** | TLB entries, cache control, initial tensor upload | untranslated aperture (§3.1) |
| **any CU** | spill, scratch, CU→CU staging through memory | protocol allows it; nothing does it yet |

`MEM_WR_REQ` carries a descriptor flit then `len+1` data flits
([`../noc/spec.md`](../noc/spec.md) §5.1). MAS must therefore hold per-source
write state — a descriptor arriving from cluster A can be followed by data flits
that interleave, at the router, with a descriptor from cluster B.

> **This is a real requirement, not a detail.** A single global "current write"
> register works with one writer and corrupts silently with two. MAS needs write
> reassembly keyed by `src`, sized for as many writers as can be in flight.

### 4a.1 Acks are fire-and-forget

`MEM_WR_ACK` is a completion the requester may ignore — today's matmul CU does,
retiring on send. MAS must not depend on acks being consumed: a design that
stalls waiting for ack credit deadlocks against a CU that never reads them.

The corollary is that **a write is not ordered against a later read by
hardware.** A cluster that writes a tile and another that reads it must be
ordered by the program. See [cache.md](cache.md) §4.

---

## 5. AXI side

One AXI4 master per slice. Nothing exotic: `INCR` bursts, one outstanding
transaction per cache miss, `awsize`/`arsize` at the full data width.

> **As built there are `MEM_PORTS + 1` masters, not one** (§2.4): a channel per
> memory port, plus one for the host upload. Sharing them would put every
> cluster's read beats back on one AR/R pair, which is the constraint the ports
> exist to remove.

**Clock domain — probably none.** DDR4-2400 at 4:1 puts the MIG user interface
at 300 MHz, the same as the NoC (§2), so MAS can be single-domain. That is a
configuration-dependent gift rather than a design property: a different speed
grade or clock ratio reintroduces the crossing, and if it does, it belongs on
the AXI side so NoC-side timing is unchanged.

**URAM scratch** is an AXI slave like any other, and the TLB is what decides a
page lives there. No special path, which is the point — bringing the system up
against on-chip memory and moving to DRAM later is a mapping change.

### 5.1 Beats and lines

```
   AXI beat    512 b = 64 B   = one DDR4 BL8 access on x64
   cache line  128 B          = 2 beats     (matches one L1-entry fetch)
   or          256 B          = 4 beats     (better row locality)
```

64 B is the atom; anything smaller wastes a DDR burst. The line-size choice
between 128 and 256 B is in §9.

---

## 6. Bandwidth: how many slices

This is the sizing question, so it gets arithmetic rather than a guess.

```
   one NoC port          256 b x 300 MHz   =   9.6 GB/s
   4 x DDR4-2400                           =  76.8 GB/s
```

**Demand at the NoC.** A cluster fetching operands needs
`4(Gm+Gn)/(Gm·Gn)` words/cycle — 0.375 at the balanced 16×32 tiling:

```
   per cluster   0.375 words/cyc x 32 B x 300 MHz  =  3.6 GB/s
   32 clusters                                     =  115 GB/s
```

**Demand at DRAM is much lower**, because clusters share operands. With output
tiling on an 8×4 cluster grid, each A tile is read by the 4 clusters in its row
band and each B tile by the 8 in its column band:

```
   read by all clusters   32 x 192 words per K block  =  6,144
   distinct words         8x16x4 (A) + 4x32x4 (B)     =  1,024
   reuse factor                                       =  6x
   DRAM traffic           115 / 6                     =  ~19 GB/s
```

So **DRAM is not the constraint — NoC attach points are.** At 0.375 words/cycle
the mesh side wants 12 ports while DRAM sees only ~19 GB/s, one channel's worth.

That inverts the naive reading of the spec, which sized MAS against DRAM
bandwidth. But it is only half the correction, because the 0.375 figure is
itself a function of how much the cluster holds — see §2.2. Enlarging the
cluster's L2 from a 64×128 tile to 128×256 halves NoC demand to 57.6 GB/s, and
**8 ports then covers it with room to spare**.

Three levels each hide a different part of the traffic, and sizing any one of
them without the other two gets the wrong answer:

```
   cluster L2   115 -> 58 GB/s at the mesh    reuse across a cluster's own tiles
   MAS cache     58 -> ~10 GB/s at DRAM       sharing between clusters, 6x
   DDR4 x4                76.8 GB/s available
```

> Sanity check on the whole machine: `C[512,512] = A[512,1024]·B[1024,512]` is
> 268 M MACs, 54.6 µs at 9.83 TFLOPS, and touches 2.6 MB of FP16 operands — 48
> GB/s of DRAM, within four channels. The arithmetic holds at both ends.

---

## 7. Faults

An unmapped page, a write to a read-only page, or an AXI error must not hang the
requester. Both things happen:

```
   1  MAS returns the burst anyway, zero-filled, with status != 0
      -> the CU's positional assembly completes and it never stalls
   2  MAS sends CU_SIGNAL / SIG_FAULT to the orchestrator, carrying the
      offending address and the requester's coordinates
      -> the orchestrator records it in NODE_STATUS and can abort the program
```

MAS is a NoC node, so signalling the orchestrator costs nothing new — it is the
same path a CU uses to report completion. Routing faults through the
orchestrator rather than back to the CU is deliberate: **the CU has no way to
act on a fault**, having already issued the request and committed to consuming
a response. The orchestrator can stop dispatching, and it is the only component
with a view of the whole program.

Returning zeros rather than nothing is the part that is easy to get wrong. A
faulting read that simply never replies leaves the requester waiting forever,
and the failure then presents as a hang with no diagnosis — which is exactly the
class of bug that cost this project a day when the orchestrator's staging window
silently truncated a program.

---

## 7a. Recommended v1: the cheapest thing that works

There is one lever that removes most of the memory system, and it is not in the
memory system at all.

### The accumulator tile depth sets the operand bandwidth

They are the same number. A cluster whose resident tile holds `Gm × Gn`
sub-tiles computes an `Ma × Nb = 4Gm × 4Gn` output block, and

```
   ACU DEPTH        =  Ma · Nb / 16
   words/cycle      =  16 (Ma + Nb) / (Ma · Nb)      =  32/G   for square G
```

so **making the resident tile deeper directly reduces NoC traffic**:

| ACU DEPTH | output block | primitive | words/cyc | 8 clusters |
|---|---|---|---|---|
| 512 | 64 × 128 | 5 BRAM36 | 0.375 | 28.8 GB/s |
| 2048 | 128 × 256 | 5 URAM288 | 0.188 | 14.4 GB/s |
| **4096** | **256 × 256** | **5 URAM288** | **0.125** | **9.6 GB/s** |

URAM gives depth 4096 for the same **five** primitives that BRAM spends on 512
(measured: `.plan/measurements/memory-primitives.md`). Eight times the depth,
same primitive count, and it is exactly the shape URAM is for.

### What that buys

At DEPTH=4096, eight clusters demand **9.6 GB/s against a 19.2 GB/s channel** —
50% utilisation, comfortable against real DRAM efficiency. And then:

```
   no MAS cache      8 clusters already fit one channel; the cache exists to
                     close a gap that no longer exists
   no TLB            flat 34-bit physical addressing, with a range decode to
                     pick URAM scratch vs DDR. That was the TLB's main job.
   no cross-SLR NoC  each SLR is self-contained, so no SLL crossings on the
                     mesh, no skid buffers, no busy/valid round-trip problem
   no NUMA question  s9's slice granularity does not arise
   4 small meshes    ~19 endpoints each (5x4) instead of one 76-endpoint mesh:
                     shorter paths, lower latency, easier timing
```

MAS reduces to **2 NoC ports, an AXI master, and a range decode**. That is a
weekend module rather than a subsystem.

### The recommended configuration

```
   per cluster   ACU tile   5 URAM  (DEPTH 4096, 256x256 output block)
                 L2        13 URAM  ((256+256) x 1024 x 7 bit = 3.67 Mbit)
                 L1                  LUTRAM, unchanged
                          = 18 URAM

   per SLR       8 clusters              144 URAM of 320
                 1 MAS slice, 2 ports    no cache, no TLB
                 the dispatch agent      SHARES those 2 ports, s2.5
                 1 DDR4 channel          2x1 SmartConnect
                 mesh entirely local     no cross-SLR links

   machine       576 URAM (45%), and nothing else wants URAM
```

Read-only operands shared between SLRs are **replicated per channel**. For
matmul the shared operand is the weight matrix, and weights are small against
16 GB of DRAM — a 1024×4096 FP16 layer is 8 MB, so four copies is 32 MB. The
cost is upload time, once.

### What is given up

```
   cross-SLR cooperation   none. Output tiling never needed it -- splitting K
                           across clusters was already rejected because the
                           reduction dominates the compute.
   dynamic allocation      no TLB means static placement. Fine for v1; the TLB
                           is additive when multi-tenancy matters.
   minimum problem size    a 256x256 block per cluster means 2M output elements
                           before the machine is full. Small matmuls waste it.
   drain latency           4096 sub-tiles to emit per block, ~10 cycles each.
                           Throughput is unaffected; time-to-first-result is not.
```

The last two are the real cost, and both are consequences of the same choice:
the deep tile that buys the bandwidth also coarsens the granularity.

### What to keep from the full design

Everything above is **additive later**. The TLB slots in front of a flat address
map; the cache slots behind the port; cross-SLR links are new mesh edges. None
of them require rework of what v1 builds — which is the point of recommending
this order rather than treating it as a lesser design.

> The one thing worth measuring before committing: **does the ACU still close
> timing with a URAM tile?** The probe says 352×4096 in URAM runs at 585 MHz
> standalone, and `READ_LAT=2` hands the align stage a register — but the ACU is
> what the whole cluster closes on, and URAM's clock-to-out is worse than BRAM's.
> That measurement decides whether DEPTH 4096 is free or costs a pipeline stage.

---

## 8. Settled

```
   write policy      WRITE-THROUGH. DRAM stays authoritative, no dirty bits, no
                     eviction path. Write-back would coalesce output-tensor
                     traffic but it is internal to the cache and can change
                     later behind the same interface. Simple first.

   page sizes        2 MB (DRAM) and 64 KB (URAM), both parameters, selected
                     per entry by a size bit -- s3.2.

   TLB programming   ordinary MEM_WR_REQ to an untranslated aperture, so MAS
                     has exactly one interface -- s3.1.

   fault reporting   zero-filled response to the requester, SIG_FAULT to the
                     orchestrator -- s7.
```

## 9. Open questions

```
   NoC port count    8 for MAS + 4 for the orchestrator is the proposal, and
                     the arithmetic supports it PROVIDED the cluster L2 holds a
                     128x256 tile (~10 URAM/cluster). At the smaller 64x128
                     tiling, 8 ports throttles the machine to ~67% of peak.
                     The port count and the L2 size are one decision, not two.
                     PARTLY ANSWERED by s2.4, and from the other direction: a
                     port turned out to be set by how many clusters one read
                     ENGINE can serve, not by how much bandwidth one wire can
                     carry. One per mesh row, and a band's two ports divide its
                     columns between them -- ~2 clusters' worth each at 8 CU.
                     The orchestrator's 4 became 0: the agent shares those same
                     ports and has no attachment of its own -- s2.5.

   burst length      DDR4 BL8 makes 64 B the atom, so a 128 B line is 2 beats
                     of 512-bit AXI and a 256 B line is 4. Longer amortises row
                     activation, which favours 256 B; 128 B matches the CU's
                     L1-entry fetch exactly, so a miss is one line. Needs the
                     access stream to decide -- see cache.md s3.

   TLB reload rate   how often does a real model exceed 128 MB of mapped DRAM,
                     and does reprogramming stall dispatch?

   slice granularity the NUMA question, and the biggest one left. Fine
                     interleaving balances load but makes cross-SLR hops the
                     norm; coarse per-SLR slicing keeps reads local but turns a
                     machine-wide shared tensor into a hotspot. With output
                     tiling one operand is ALWAYS shared across SLRs, so no
                     partitioning makes both private. Replicate, interleave, or
                     phase the schedule -- needs a real workload.
                     See ../arch-design.md s6.5.

   AXI clock         4:1 DDR4-2400 puts the user interface at 300 MHz, so no
                     crossing. Confirm per part and IP configuration; a
                     different speed grade or ratio reintroduces it.
```

---

## 8. Build order

```
   1  MAS with no cache and no TLB: NoC <-> AXI, one slice, flat addressing.
      DONE, and it since grew MEM_PORTS independent ports -- s2.4 -- which the
      dispatch agent then joined rather than adding an attachment -- s2.5.
   2  TLB, with the backing store pointed at URAM scratch.
   3  Cache (cache.md), measured against the reuse factor in s6.
   4  Slicing to k nodes.
```

Step 1 was deliberately the whole bridge with none of the cleverness, and it is
built: `tests/mas/mag_system_tb.v` and `tests/mas/mag_driver_tb.v` drive the
real MAG end to end and pass, so steps 2–4 have a known-good reference to be
wrong against.

> This step used to be justified by "`mx_mesh2x2_tb` already passes against a
> stub with exactly that interface". That was wrong twice over and the claim has
> been removed: the bench has been **deleted**, and by the end it was not
> passing — it hung in `S_FILL` while reporting wrong answers, because the stub
> wrote zeros where the response word index belongs.
> [`../system.md`](../system.md) §2.3 has the full account. `noc_fake_mem` is
> still the stub the *NoC-level* benches use, and it is no longer a reference
> for anything MAS-shaped.
