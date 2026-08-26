---
title: The memory port
summary: The unit the machine grows by — intake, the read engine and its self-describing responses, write slots matched by source, and what a port costs.
tags:
  - architecture
  - sysnode
  - memory
---

# The memory port

The server behind the instruction set, and the thing you add more of when the
machine stops scaling.

Two words this page leans on. An **entry** is the unit a read is expressed in: a
fixed number of consecutive data words, four by default, which is what one
request fetches and what one response delivers. A **run** is a sequence of
consecutive entries named by a single request — one flit that means several
hundred cycles of traffic. Everything else the page defines where it appears;
the node's shared vocabulary is in [the README](README.md#the-vocabulary-once).

**A port carries no transform.** It used to carry one each; the slot now sits on
the mover's read-return path and belongs to the mover —
[transform-stage](transform-stage.md).
What a port serves is operands already in their final format.

## A port is the unit the machine grows by

The read engine fetches one entry at a time. With a single engine, every compute
unit in the mesh queues behind one state machine and one emit buffer. That was
the constraint that stopped the reference machine scaling — and it stopped while
nothing was saturated, which is the diagnostic: the limit was the *server*, not
the bandwidth.

So a **memory port** is a whole server: its own intake queues, read engine,
write slots, response emitter, and its own AXI master channel. `PORTS` of them
are instantiated, and adding one adds all of it. Nothing is shared between ports
except the address space on the far side of AXI.

What a port is *attached to* is not the engine's, though. The node's `PORTS`
fabric attachments belong to `sn_hub`, and the engines are one of four kinds of
client on them — the control agent, the interlink and the control processor are
the others. An engine sees the flits the hub's demux already qualified as its
own, and never learns the others exist.

The ports sit at **different mesh nodes**, and that is not a placement
preference. Routing is X-then-Y on clamped coordinates, so a port at `(0, y)`
draws traffic to router `(GRID_LO, y)` and to no other. Two ports on the same
router would split the server and leave the funnel — the link into that router —
exactly as narrow as before.

## Intake: backpressure must not depend on content

`mem_in_busy` is computed from this port's own queue occupancy and nothing else.
It never depends on what the arriving flit is.

The reason is that the mesh is in-order behind a busy signal. If a port decides
"busy" because it cannot classify or accept *this particular* flit, everything
behind that flit stops too — including, in the general case, the flit that would
have freed the resource. Deciding from local state only makes the condition
self-clearing.

Two queues sit behind that one busy signal, demultiplexed by type: reads in one,
write descriptors and write data in the other. With a single queue, a read
request at the head that cannot be taken blocks the write data behind it — and
that data is exactly what lets a drain finish. Busy is still "is there room in
both", which is still local state, so the hazard above does not come back.

## Reads: the response says where it belongs

The engine turns a request into consecutive AXI reads. When a **run** is
requested, the next entry's address is issued the moment the current entry's
last beat lands, not after that entry has finished leaving — that overlaps the
address-to-first-beat latency, which would otherwise be paid once per entry. The
address is accumulated rather than computed as `base + n * size`, because a
runtime multiply lands directly in the address path.

A finished entry is latched into an **emit buffer** before it is sent, so the
next entry's AXI read can start immediately. Without the buffer, the fetch's own
capture registers *are* the emit source, so fetch and emit exclude each other and
two independent interfaces run at the sum of their times instead of the larger.

Every response flit is self-describing. Its transaction tag is the requester's
own tag plus this entry's position in the run, and the word index within the
entry rides in the header's spare bits. The receiver therefore needs no cursor,
and arrival order stops being load-bearing. **That is what makes a streaming
fetch possible at all** — one request, hundreds of cycles of traffic, and a
receiver that can bin every flit it gets without tracking where it is.

**Extra destinations** exist because many compute units frequently want the same
bytes. Without them the same entry is fetched from DRAM once per consumer for a
bit-identical result. With them, the same latched words are re-sent with a
different header: no second AXI read at all.

## Writes: slots matched by source, not by arrival order

A write is a descriptor flit and then data flits, and the mesh may put another
node's flit between them. Collecting "the next flit" into the open write is
therefore wrong the moment two units write at once.

Each source gets a **slot**, matched by source coordinate. A slot holds a whole
burst, because one burst's beats must be contiguous on AXI while the mesh
interleaves data flits freely. A slot walks `val -> rdy -> iss -> free`, and all
three bits are needed: with only `val` and `rdy`, a slot whose write is on the
bus is indistinguishable from one still waiting for its data, and the next data
flit from that source binds to the in-flight slot.

Slot count is a correctness parameter, not a performance one. A unit that
discards its write ack — which it should, they are fire-and-forget — sends its
next descriptor while the previous burst is still on the bus. With one slot per
unit, the second descriptor finds nothing free, is never popped, and blocks the
data flits behind it that would have freed one. **Under-sizing does not corrupt
anything; it deadlocks.**

## Reads and writes run alongside each other

A streaming fetch occupies the read path for its entire run. If it ran inside
the same state machine as the write path, that machine never returns to idle, so
no write slot can be issued: the slots fill, intake jams on a write descriptor
nothing will accept, and the data flit behind it reports "no open write".
Lengthening one transaction starves the other. The read engine therefore has its
own state and its own return context, and shares only the single output register
— where the emitter wins, and cannot starve the write path because a few
response flits per entry against a fetch of several beats leaves most cycles
free.

## Conventions

This is the system that forces the most on a compute unit, because **the memory
agent hands you data in a shape whether you like it or not.** Four of these are
not really optional; the rest are advice with a reason.

**Accept words in the shape they arrive.** *(Forced.)* A response is N words per
entry, each carrying its entry's index within the run and its word index within
the entry. Your fill logic has to consume that. You are free to store it however
you like — the reference project's two units store it into memories of 928 and
256 bits respectively — but the *arrival* shape is not yours to choose.

**Bin by tag; do not build a cursor.** *(Forced.)* Responses are self-describing
precisely so that arrival order need not be tracked, and a streaming fetch does
not guarantee the order a cursor would assume. A receiver written against
arrival order works until the first time two runs overlap.

**Fetches are entry-granular.** *(Forced.)* A run is consecutive entries at a
fixed stride. If your natural line size is not the entry size, either set the
entry-words field or rearrange the region with the mover — but do not expect the
memory agent to slice differently per request.

**Discard write acks.** *(Forced.)* Slot sizing assumes you do not wait. A unit
that waits for its ack before sending the next descriptor is correct but slow;
a unit that waits *and* the slot count was sized for a unit that does not is how
you get an under-sized slot array and a deadlock.

**Store operands so that a pass is one contiguous run.** *(Free, but streaming
only pays off if you follow it.)* The reference driver stores tile-major for
exactly this reason. Scattered entries turn one streaming request into many
single-entry ones, and the per-request overhead is then paid per entry.

**Name extra destinations rather than issuing identical requests.** *(Free.)* If
several units want the same bytes, the fetch happens once instead of once per
consumer. Ignoring this is correct and wasteful, and the waste scales with unit
count.

## What a port costs

**Per memory port.** Two flit-wide intake FIFOs. The write slot array — a small
register file of per-slot state indexed by source, plus a data array of
`WR_SLOTS x WBURST` beats, which is a **block RAM**. The read engine's emit
buffer, a few beat-wide registers. One AXI master channel.

**Measured, out-of-context synthesis on `xcvu13p-fhgb2104-2L-e`, Vivado 2024.2,
at 3.333 ns, design state Synthesized, `sysnode` whole at `PORTS=2`** —
hierarchical rows, from the run each one names. Nothing here is routed.

| the run | script | `mag_mem_port`, each | block RAM in it | URAM in it |
|---|---|---|---|---|
| RV64 complex, `STAGE_AT_PORT=1`, 2026-08-26 23:46 | `ooc_sysnode_rv64.tcl 2` | **2,064** and **2,032** LUT | **4 RAMB36 each** | 0 |
| RV32 complex, `STAGE_AT_PORT=0`, 2026-08-26 09:04 | `ooc_sysnode.tcl` | 5,423 and 5,418 LUT | 0 | 64 each |

**The two rows differ in two things, and neither is a port getting cheaper.**

The larger difference is the staging store moving out of the port and onto the
converged path, where one instance serves every requester instead of one per
port — [edge-and-control](edge-and-control.md#staging-inside-the-memory-agent).
That is what the URAM column shows. Wherever that store sits, a write into it
changes **only the lanes its strobes name** — the bank memory is byte-enabled —
so a narrow store does not clear the rest of the 32-byte word;
[staging honours byte
strobes](edge-and-control.md#staging-honours-byte-strobes) has why that is a
correctness property rather than a convenience.

The second is that the RV32 row **predates the write-slot data array becoming a
block RAM.** In that run the array is still distributed RAM, and the report
charges the port 1,220 LUTRAM for it. The RV32 configuration has not been
re-synthesised since; read its row as the last measurement of that
configuration, not as its current cost. The array is shared, so the change
applies there too.

Neither row is a *marginal* figure: a hierarchical row is what an instance
charges, not what adding one costs, and the two differ wherever a shared arbiter
widens.

> **A marginal per-port figure of +6,557 LUT and +12,916 FF** (21,459 → 28,016
> for the whole node at one port and two) is carried in this tree from
> `ooc_sysnode.tcl` runs of 2026-08-24, in the two-module, RV32,
> `STAGE_AT_PORT=0` shape. **It has not been re-measured against the node as it
> ships**, and the shape it was taken in is the one where each port carried its
> own staging store. Treat it as historical: the direction is right, the value
> belongs to a configuration the node no longer builds in.

**A port uses no DSP, and the node's DSP count does not move with the port
count** — 47 for the whole RV64 node, of which 32 is one transform bank, 4 the
processor's multiplier, and 11 the mover: 3 in the mover proper and 8 in its
PRNG's constant multiplies. A per-port transform would show up there and does
not, which is what "one bank per node" means as a measurement rather than a
claim; `ooc_sysnode.tcl` errors above 48 DSP to keep it that way.

### The slot data array is a block RAM, read one beat ahead

The array is one burst per slot: `WR_SLOTS` slots by `WBURST` beats of `DATA_W`,
which is 16 × 8 × 256 bits — 32 Kbit at the shipped sizing. Both factors are set
for correctness rather than tuned, so this is the part of a port that grows
fastest. Written as a plain register array it inferred distributed RAM read at
three different indices, and the RV32 run above charges each port **1,220
LUTRAM** for it — on a device carrying 2,688 block RAM tiles, of which the whole
node uses 57.5. It is now one simple dual-port block RAM, **4 RAMB36 per
port**.

**Read the memory columns of a synthesis report, not only the logic ones.** An
array whose width and depth are both fixed by correctness parameters is exactly
the shape that ought to be a memory, and nothing in the tool's output says it
has become logic instead.

The write side is unchanged: an arriving data flit writes at `{matched slot,
that slot's beat count}`, one beat per flit, and the two indices are already
registered state.

The read side is the part worth copying. A block RAM answers a cycle after its
address is presented, and the AXI write channel can stall for any number of
cycles, so "read the beat you are about to drive" does not work. Three pieces
make it work:

1. **A current-beat register.** One register holds the beat on the bus; the
   RAM's output holds the one behind it. A beat can therefore leave every cycle,
   because the next one is already out of the array when it is needed.
2. **The read enable *is* the advance.** The array is read only on the cycle the
   current-beat register takes the RAM's output — never freely. A block RAM's
   output holds its value while its enable is low, so the next beat survives a
   stall of any length. A free-running enable instead walks the address on
   through the stall, and the data arrives one beat behind for the rest of the
   burst.
3. **One priming cycle per burst.** Beat 0's address is presented while the slot
   is still being picked, in the idle cycle before the burst starts, so the
   first cycle of the transfer already has beat 0 at the RAM's output and spends
   itself taking it into the register. That one cycle is the entire cost; every
   cycle after it moves a beat.

Two structural choices keep the logic *around* the array cheap: a per-slot "the
next beat is the last" term is precomputed from registered state only, so the
ready decision is a 1-bit select rather than mux-then-add-then-compare; and
free / match / pick are three separate priority scans over the slot array rather
than one scan with conditions, so none of them lands in the other's path.

Intake FIFO primitive choice is a genuine trade, not a default. Block RAM saves
LUTs and costs frequency, because the worst path already starts at that FIFO's
output and a block RAM's clock-to-out is far slower than a LUTRAM's. Which side
to take depends on which resource the instance is short of.

What the rest of the system costs — the control agent's RAMs and the interlink —
is in [edge-and-control](edge-and-control.md#what-the-rest-costs).
