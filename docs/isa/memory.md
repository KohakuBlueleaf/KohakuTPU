# The memory protocol

The flits a compute unit exchanges with MAG, and what MAG turns them into on
AXI.

Source: [`src/kohakumas/mag.v`](../../src/kohakumas/mag.v) (the host window and
the port array), [`mag_mem_port.v`](../../src/kohakumas/mag_mem_port.v) (intake,
read engine, write slots, emitter),
[`mx_quant.v`](../../src/kohakumas/mx_quant.v). The requester side is
[`src/kohakutpu/matmul/mx_cluster_cu.v`](../../src/kohakutpu/matmul/mx_cluster_cu.v).

**There is no "MAG" that serves this protocol — there are `MEM_PORTS` of them.**
`mag.v` is a generate loop over `mag_mem_port`, one instance per mesh row, and a
port is a whole memory server: its own intake queues, read engine, `mx_quant`,
write slots, emitter and AXI master channel. Nothing is shared between ports
except the address space on the far side of AXI. The host upload is separate
again — its own FSM, its own quantiser, its own AXI channel — so there is **no
global quantiser and no mutex** anywhere in this path. Everything below that
says "MAG does X" means one port does X, independently, `MEM_PORTS` times over.
Why one per row: [`../mas/spec.md`](../mas/spec.md) §2.4.

**Those ports are also the only way to reach the dispatch agent, which is why
they are MAG ports rather than memory ports.** MAG has no other NoC attachment.
A flit arriving at one is handed to that port's engine or to the agent according
to its **type** — §1.1 — so this protocol shares its wires with the control plane
and is unaffected by it. Why: [`../mas/spec.md`](../mas/spec.md) §2.5.

**A port attaches to the NoC, not to a cluster.** Every cluster can reach every
port through the mesh, so which one a cluster's traffic leaves by is a routing
choice: a cluster occupies one column of a *band* of two mesh rows, and columns
nearer MAG exit by their manager's row while the farther half exit by their
accumulator's ([`../system.md`](../system.md) §2.3). Nothing in this protocol
depends on that — a requester names MAG's coordinate and the mesh does the rest.

---

## 1. The flit

288 bits, 32 of header and 256 of payload. At `POS_WIDTH = 4`:

| bits | field |
|---|---|
| `[287:284]` | dst x |
| `[283:280]` | dst y |
| `[279:276]` | src x |
| `[275:272]` | src y |
| `[271:268]` | type |
| `[267:260]` | txn id |
| `[259]` | last |
| `[258:256]` | reserved |
| `[255:0]` | payload |

| type | name | direction |
|---|---|---|
| `0x0` | `MEM_RD_REQ` | CU → MAG |
| `0x1` | `MEM_WR_REQ` | CU → MAG |
| `0x2` | `MEM_RD_RESP` | MAG → CU |
| `0x3` | `MEM_WR_ACK` | MAG → CU |
| `0x4` | `MEM_WR_DATA` | CU → MAG |
| `0x5` | `CU_INST` | agent → CU |
| `0x6` | `CU_SIGNAL` | CU → agent |
| `0x7` | `CU_CTRL` | either |

### 1.1 Type decides which consumer, not merely which handler

A memory port's inbound flit is demultiplexed by `type` before anything else
looks at it:

| type | goes to |
|---|---|
| `0x0` `MEM_RD_REQ`, `0x1` `MEM_WR_REQ`, `0x4` `MEM_WR_DATA` | **this port's** `mag_mem_port` |
| everything else — `CU_SIGNAL`, `CU_CTRL`, anything injected | **the agent** |

Only those three types are memory's. The engine's `mem_in_valid` is gated on the
classification, so **the engine never has to know the agent exists** — a control
flit offered to its port is simply not valid at it.

The agent has one input and there are `MEM_PORTS` ports, so the ports round-robin
into it. A port whose flit is the agent's and has not been granted holds
`mem_in_busy`; its sender holds the flit and retries, which is the link contract
every sender on this mesh already obeys
([`../noc/spec.md`](../noc/spec.md) §2.1).

**The agent answers at port 0's coordinate**, `(MEM_X, MEM_Y)` — the same
coordinate a CU sends `MEM_RD_REQ` to. A `CU_SIGNAL` addressed there reaches the
agent and a `MEM_RD_REQ` addressed there reaches the engine, because the two are
told apart by what the flit *is* rather than by where it went. Nothing in this
protocol changes as a result: a requester still names MAG's coordinate and still
gets its `MEM_RD_RESP` back from the engine.

## 2. Request payload

`MEM_RD_REQ` and `MEM_WR_REQ` share one layout.

| bits | field | width |
|---|---|---|
| `[255:222]` | address, bytes | 34 |
| `[221:216]` | reserved | 6 |
| `[215:208]` | len — AXI beats minus one | 8 |
| `[207:200]` | flags | 8 |
| `[199:192]` | count — entries in a streaming read, **max 255** | 8 |
| `[191:168]` | peers — extra destinations, `{y,x}` each | 24 |
| `[167:166]` | npeer — how many are present | 2 |
| `[165:0]` | unused | 166 |

Flags:

| bit | name | meaning |
|---|---|---|
| `[3:0]` | cache hints | reserved by the spec, ignored here |
| `[4]` | `QUANT` | source is FP16; quantise on the way out. **Clear means the entries are already int7 + E5M3** — half the bytes, no quantiser pass |
| `[5]` | `BLAYOUT` | pack for a B operand rather than an A operand |
| `[6]` | `STREAM` | this is a **descriptor**: fetch `count` consecutive entries |

`len` is honoured on **writes** — a write burst is `len + 1` beats, and MAG
buffers the whole run before issuing one `awlen = len` transaction (§3). On an
entry read it is ignored, and correctly so: the burst length is a consequence
of the entry size and the source format, which are both stated by the flags, so
a requester cannot state it inconsistently.

`MEM_WR_DATA` and `MEM_RD_RESP` carry 256 bits of data in the payload.
`MEM_RD_RESP` also uses two header fields the other types leave alone:

| header bits | field |
|---|---|
| `[267:260]` | the requester's **entry index** — its own `txn` plus this entry's position in the run |
| `[257:256]` | which of the entry's four **words** this is |

Together they are enough for the receiver to place the flit with no cursor of
its own, which is what stops arrival order from being load-bearing and is what
makes a streaming fetch expressible at all. `MEM_WR_ACK` carries a zero
payload; only the header matters.

> **`count` is 8 bits and 256 is not expressible.** It wraps to 0, which MAG
> coerces to 1 — so a requester that asked for 256 entries waits forever for
> 255 that were never fetched, in a legal state, with nothing to assert on. It
> happened: a tile with `nk = 8` makes `FILL B` exactly 256 entries, and the
> run completed quickly with every output zero. `matmul._flit` refuses `n > 255`
> on a `FILL` and `kernel.choose_tile` caps `nk` to match, so the shape is
> rejected where the count is chosen. Widening the field is cheap in itself —
> the payload has 166 unused bits — but it also moves `rd_cnt`, `rd_ent` and
> the 8-bit response tag, so it is its own change.

### 2.1 Why a stream has a count and not a stride

Entries in a streaming fetch are contiguous by construction. The driver stores
operands tile-major precisely so that a pass's entries are one run
([kernel.md](kernel.md) §3). A stride field that is always 1 is a field to keep
correct for no benefit; a genuinely irregular walk is a tensor descriptor,
which is a different mechanism.

## 3. Pairing a write with its data

A write is a descriptor flit followed by `len + 1` data flits — up to
`WBURST = 8` of them, which is what a drain sends (`len = 7`,
[cluster.md](cluster.md) §5.2). **The mesh can put another node's flit between
any two of them.** Collecting "the next flit" into the open write is therefore
wrong the moment two nodes write at once — the data lands under someone else's
address, and the result is a plausible-looking output tile with some sub-tiles
from the wrong cluster.

Two mechanisms fix it, and both are necessary.

**Type, not position.** `MEM_WR_DATA` is its own flit type. A receiver
identifies the data flit by what it is, not by where it arrived.

**One write slot per source.** Each `mag_mem_port` holds its own table of
`WR_SLOTS = 16` entries — nothing is shared between ports:

| field | contents |
|---|---|
| `ws_val` | descriptor seen — the slot is occupied |
| `ws_rdy` | the **whole burst** has landed, ready to issue on AXI |
| `ws_iss` | issued to AXI, awaiting the `B` response |
| `x`, `y` | the source coordinate that owns this slot |
| `txn` | transaction id, echoed in the ack |
| `addr` | 34-bit byte address |
| `len` | beats expected, `len + 1` from the descriptor |
| `cnt` | beats received so far |
| `data` | `WBURST` × 256 bits |

A descriptor takes a free slot. A data flit is matched to a slot by **source
coordinate**, not by order, and lands at that slot's own `cnt`. The slot becomes
ready only when `cnt` reaches `len`, so one burst's `W` beats stay contiguous on
AXI while the mesh interleaves data flits freely.

A slot walks four states, and all three bits are needed to name them:

```
   free            !val
   awaiting data    val  !rdy  !iss     <- a WR_DATA from this source binds here
   ready            val   rdy  !iss     <- pickable by the AXI write FSM
   in flight        val         iss     <- on the bus, waiting for bvalid
```

**Two slots per node that can have a write in flight, not one.** A CU discards
its `MEM_WR_ACK` and does not wait for it (§7), so its *next* descriptor arrives
while the previous burst is still on the AXI bus — every time, by construction,
not occasionally. One slot per CU therefore means the second descriptor finds
nothing free, is never popped, and blocks the data flits behind it that would
have freed one.

Under-sizing does not corrupt anything on its own; it deadlocks. Size it to
twice the number of clusters behind one port and leave it alone.

A data flit that arrives with no descriptor waiting for it has nowhere to go and
sits at the queue head forever. That is a protocol violation by the sender, so
there is a simulation-only `$display` for it rather than a silent hang.

### 3.1 Why `ws_iss` is load-bearing

`ws_rdy` has to clear the moment a slot is picked, or the same write is issued
twice — see §3.3. With only two bits, a slot between issue and ack then reads as
`{val, !rdy}`, which is **indistinguishable from a slot still waiting for its
data**. The next `MEM_WR_DATA` from that source matches the in-flight slot
instead of the one that wanted it, overwrites the payload already committed to
AXI, and leaves the real destination slot waiting forever. Slots leak one per
occurrence until none is free, `ws_has_free` goes low, the descriptor at the
head of the write queue is never popped, and intake wedges.

`ws_iss` names the fourth state so the match can exclude it. That is the whole
fix, and it is one bit.

**The overlap that exposes it is guaranteed, not occasional.** Two properties of
the requester combine:

* `mx_cluster_cu`'s write path is a strict `W_IDLE -> W_REQ -> W_DATA` loop over
  a double-banked burst buffer, so exactly one write is open per source at a
  time. That bounds what a source can have outstanding — it does *not* make one
  slot per source enough, because of the next point.
* It **discards** `MEM_WR_ACK` (`a_in_busy` is tied low), so it does not wait for
  the ack before starting the next burst. Write `i+1`'s descriptor therefore
  arrives while write `i` is still on the AXI bus, every time — which is why the
  table is sized at two slots per source (§3).

So every source spends part of every write in the ambiguous state. It only
becomes a *visible* failure when a **second** source writes into that window,
which is why it needed sustained multi-cluster concurrency to appear at all.

> Reproducing it needs a **delayed write response**. Against a RAM that returns
> `bvalid` in one cycle the window is barely wide enough to hit, and a bench
> passes on the buggy RTL. `tests/mas/mag_wslot_tb.v` (runner `mag_wslot`, unit
> tier in `check.py`) drives two sources with a slow write response, which is
> what makes the bug deterministic instead of a race.

### 3.2 Why the intake queues are in front of the classifier

Backpressure must not depend on what the flit *is*. Deciding `busy` from the
incoming type means a flit the port cannot classify right now blocks the port —
and because the mesh is in-order behind that port, it blocks everything else
too, including the flit that would have freed the resource. That is a deadlock,
and it presents as the whole machine going quiet.

A FIFO separates the two questions. Accepting is "is there room", which depends
only on the port's own state. Classifying happens afterwards at the head, where
taking a few cycles costs a stall and nothing else.

**Two queues, demuxed by type**, each `Q_DEPTH = 64`: reads in one, write
descriptors and write data in the other. With a single queue, a read request at
the head that cannot be taken blocks the write data behind it — and that data is
what lets a drain finish, while the drain not finishing keeps its CU in `S_DWAIT`
accepting no responses. Splitting by type does not reintroduce the hazard above,
because `busy` is still "is there room in **both**", which depends only on this
port's own state.

The port **counts its own occupancy** and raises its own busy at
`Q_DEPTH - Q_MARGIN` with `Q_MARGIN = 4`. It cannot use `sync_fifo`'s
`wr_almost` for that: `USE_ADV_FEATURES` is zero, so XPM ties `prog_full` low and
`wr_almost` reduces to `wr_busy` ([`../noc/spec.md`](../noc/spec.md) §2.1). The
margin is not what makes the link safe — retry is — but a counted margin is what
lets this port keep a few slots of headroom deliberately.

> **At MAG's boundary `mem_in_busy` is no longer that signal.** The engine's
> occupancy answer is one of two, selected by the demux of §1.1: a memory flit is
> refused when the engine has no room, and a flit of any other type is refused
> unless the agent is taking it this cycle. **The counter that samples this pin
> therefore measures demultiplexing, not congestion** —
> [`../mas/spec.md`](../mas/spec.md) §2.5 and [`../perf.md`](../perf.md) §1.1.

#### 3.2.1 The share layer qualifies that rule, and one case breaks it

**`busy` now does depend on what the flit is** — which is exactly what this
section's opening rule forbids. In the ordinary case that is affordable, because
both consumers drain: the engine's queues into AXI, and the agent's inbound into
`NODE_STATUS`, since `CU_SIGNAL` bypasses the RX FIFO entirely
([agent.md](agent.md) §5). A port offering the *other* consumer's flit delays
what is behind it on that link until the round-robin grants it — ordinary
head-of-line blocking, bounded, paid in latency.

**The case that is not bounded is a full RX FIFO.** The agent raises its inbound
busy as `rx_full && !in_is_sig` (`src/kohakunoc/noc_orchestrator.v:206`). So if a
**non-signal** control flit — a `CU_CTRL` reply, a `CU_DATA` addressed to the
agent, anything injected through the mailbox — is offered while RX is full, the
agent never takes it, the share layer holds that port busy, its sender holds, and
**the memory traffic queued behind it on that link stops and does not restart.**

That is the deadlock §3.2 describes, arriving by the route §3.2 predicted. What
changed is its blast radius: before the share layer a full RX wedged only the
agent's own link, and the orchestrator's own comment already warned that a host
which never drains RX wedges the control plane. **Now it wedges a memory port
with it.**

Nothing exercises this today — `CU_SIGNAL` is absorbed rather than queued, and
the driver does not use the mailbox ([agent.md](agent.md) §2), so RX never fills.
It is **latent, not live.** It is written down because the thing that would make
it live is someone building on the mailbox, who would otherwise have no reason to
expect a control-plane FIFO to be able to stop operand traffic.

One flit leaves **each** queue per cycle:

```
   take_wr_req    a write descriptor, when a slot is free
   take_wr_data   its data, when a slot is waiting for it
   take_rd        a read, when the read engine can take one
```

Anything else is simply not popped.

### 3.3 A picked slot stops being pickable immediately

`ws_rdy` clears the cycle a slot is selected, not when its write is
acknowledged. Releasing at ack leaves the slot ready during the cycle the FSM
spends re-entering `S_IDLE`, so the same write is issued twice — and while MAG
repeats itself, the next write's turn never comes. Every other sub-tile then
goes missing, which reads as an accumulator fault several modules away.

`ws_iss` is set at that same moment, which is what keeps the now-`{val, !rdy}`
slot from looking like one that is still waiting for data (§3.1). `ws_val`
clears only at ack, so the source cannot reuse the slot before its data is
safe.

## 4. Arbitration

**Within a port**, the single-shot raw read wins over writes: a stalled read
stalls a cluster, while a queued write has already been accepted and nothing is
waiting on it. That is the whole contest — a *streaming or quantised* read runs
in its own engine on its own state (§5.1), so it does not compete with the write
path at all, and making writes wait for it would reintroduce the starvation
splitting them fixed.

**Between ports there is no arbitration on the memory path**, because there is
nothing to arbitrate: each port has its own queues, engine, slots and AXI
channel.

**The two contests that do exist are the agent's, and neither touches an engine's
throughput.** Inbound, the ports round-robin into the agent's single input
(§1.1). Outbound, the agent wins against the engine on the port its flit's
destination row selects, and the engine holds — which it already does for
ordinary link backpressure, since it holds `valid` and `data` until a cycle with
`busy` low. The agent wins rather than the engine because its traffic is a
handful of control flits against a stream of operand words, and because engine
priority could starve dispatch exactly when the machine is busiest. See
[`../mas/spec.md`](../mas/spec.md) §2.5.

**The host window does not arbitrate either.** It is a separate FSM on the
upload's own AXI channel, and `sm_awready` is `(hst == HS_IDLE)` — it depends on
nothing but the upload's own state. An upload never waits for a fetch and a
fetch never waits for an upload. That replaced an earlier arrangement in which
the host window was served only while the NoC path was idle (§6.4).

## 5. AXI mapping

`DATA_W = 256`, so `awsize = arsize = 5` (32 bytes per beat) and both bursts are
`INCR`. `wstrb` is all ones; there is no partial write.

| flit path | AXI |
|---|---|
| `MEM_RD_REQ`, single-shot raw | `arlen = len`; one `MEM_RD_RESP` per beat, `last` from `rlast` |
| `MEM_RD_REQ`, entry, `QUANT` | `arlen = 7` per entry, stride 256 B; 8 beats in, exactly 4 `MEM_RD_RESP` out |
| `MEM_RD_REQ`, entry, pre-quantised | `arlen = 3` per entry, stride 128 B; 4 beats in, 4 `MEM_RD_RESP` out |
| write slot | `awlen = len`, `len + 1` beats, one `MEM_WR_ACK` after `bvalid` |
| host window, verbatim | `awlen` / `arlen` forwarded unchanged |
| host window, `QUANT` | 8 source beats per entry in, one `awlen = 3` burst per entry out |

Both entry burst lengths are **derived** from the entry size and `DATA_W`, not
written as 7 and 3. Hardcoding them is what would make a wider bus a silent
correctness change rather than a parameter.

`mem_rd_count` and `mem_wr_count` are 16-bit counters of transactions issued,
exposed for the bench. **They are sums over ports**: MAG adds the per-port
counters so the AXI-level totals stay one number whatever `MEM_PORTS` is.
Anything deriving a *utilisation* from them must divide by the port count as
well as by the run length — not doing so reported 101.9% and looked exactly like
a saturated bus ([`../perf.md`](../perf.md) §2.2).

> **`mem_rd`, `mem_wr`, `noc_in`, `noc_out` and `flops` are five independent
> full-duplex budgets and must never be summed.** AR/R and AW/W/B are separate
> AXI channels and the mesh's two directions are separate wires, so a sum prices
> capacity that never competes. The largest one binds. Adding them has twice
> produced a false accusation of bandwidth — see
> [`../optimization.md`](../optimization.md) §J.

### 5.1 Two read machines, on purpose

Within each port, entry reads run in their own state machine (`rs`) alongside
the write and single-shot-read machine (`st`). They have to be separate, and the
reason is not tidiness: a streaming fetch occupies the read path for the whole
run — 32 entries, hundreds of cycles. While `st` was that run, `S_IDLE` never
came round, so no write slot could be issued to AXI; the slots filled, intake
jammed on a write descriptor nothing would accept, and the data flit behind it
reported "no open write" while the mesh backpressured. Lengthening one
transaction starved the other two.

Splitting them costs one small state machine and makes reads and writes
concurrent, which they always physically were — AR/R and AW/W/B are different
channels.

That concurrency has one consequence worth stating: `m_bready` is tied high, so
a `B` response is consumed the cycle it appears whether or not anyone acts on
it. Once the read emitter could own the output register on that cycle, the
write path's ack was simply gone and `S_WR_ACK` waited forever. Every path that
waits for `B` therefore latches it (`wr_b`, `host_b`, `hq_b`).

## 6. FP16 → int7 + E5M3

**Software never sees the internal operand format.** The host uploads FP16,
which is what a driver has and what a framework produces. The dense encoding
exists between MAG and the MAC array, and it exists because it is 2.2x denser
on the NoC — a property thrown away entirely if the mesh carries FP16.

The conversion happens in one of two places, and *which* is a property of the
tensor, stated on every request that touches it:

| | where | source in memory | cost |
|---|---|---|---|
| online | on the read path, per fetch | FP16, 256 B/entry | once **per read** |
| pre-quantised | on the upload path, as it lands | int7+E5M3, 128 B/entry | once **per tensor** |

An operand is read once per output tile it participates in, so the second is
the first divided by the number of passes — and it halves the bytes the fetch
path moves for good. §6.4 has the mechanism.

One quantised read converts **one L1 entry**: 4 lanes x 32 K elements.

```
   in    8 beats x 256 bit  =  4 lanes x 32 FP16     2048 bit
   out   4 flits x 256 bit  =  4 lanes x 32 int7     1024 bit  + 4 x E5M3
```

The block scale is shared along K, so nothing can be emitted until the whole
entry has arrived. That is why `mx_quant` buffers the entry rather than
streaming it, and why the read is a fixed 8-beat burst rather than `len` beats.

### 6.1 The scale is E5M3, not a power of two

```
   scale = 2^(E - SBIAS) * (1 + M/8)     field = {E[4:0], M[2:0]}     SBIAS = 20
```

A power-of-two scale can only land the block peak somewhere in `[32, 64)` of the
int7 range, so between zero and a full bit of significand goes unused, and which
it is depends on where the peak happens to fall inside its binade. Three
mantissa bits put the peak in `[56, 63]` every time. Measured on correlated
operands, per-element relative error p50 0.54% → 0.38% and p99 48% → 23%.

E5 rather than E8: the output is FP16, whose normal range spans 30 binades, and
E5 covers 31. E4 covers 16 and does not. The three extra exponent bits an E8M0
field would spend buy range this datapath cannot express. The field is still 8
bits, so nothing about the flit format, the NoC or L1 changes — only the
interpretation.

The scale rounds **up** to the smallest representable value with `peak/scale <= 63`.
Rounding up is what keeps the peak from clipping, and the peak is the element
that matters most. A block whose peak is itself subnormal wants a scale outside
E5's range; that clamps, which degrades the block (it keeps fewer bits) where
letting the exponent wrap would corrupt it outright.

Subnormal *elements* are decoded properly rather than flushed. Flushing would
zero most of any block whose peak is below ~2e-3, which reads as the format
being poor on small tensors rather than as a dropped case.

Division by the scale mantissa is a table of eight reciprocals
(`round(4096 * 8 / m8)`), not a divider — the divisor has exactly eight possible
values.

### 6.2 Operand word layout

A quantised response word is 32 int7 elements and 4 scale fields:

```
   255                                        32 31            0
   +--------------------------------------------+---------------+
   |  32 x int7   (224 bit)                     | 4 x E5M3      |
   +--------------------------------------------+---------------+
```

Element at slot `i` occupies `[255 - i*7 -: 7]`. Scale for lane `l` occupies
`[31 - l*8 -: 8]`, replicated into **all four** words of the entry — the CU
takes them from the first and ignores the rest, which costs nothing and means
any single word is self-describing.

Slot assignment is the only difference between the two layouts:

| layout | element | slot within the word for K-slice `k/8` |
|---|---|---|
| A (`BLAYOUT = 0`) | `(lane, k)` | `lane*8 + (k % 8)` |
| B (`BLAYOUT = 1`) | `(k, lane)` | `(k % 8)*4 + lane` |

The source is always 4 lanes of 32 K. For an A operand a lane is a row of A; for
a B operand it is a column of B. Only the output packing differs, so one circuit
serves both and the driver stores both operands in the same shape.

### 6.3 Why the quantiser is here and not in the CU

Putting it in MAG's read path means every consumer gets the dense encoding for
free and there is one implementation to verify. Putting it in the CU would put a
32-element max-tree and a shift/round per element in 32 places instead of one,
and would put FP16 on the mesh — which is the 2.2x the format was chosen for.

The packing loop is **32 elements wide and runs four times, one output word per
pass**, behind a block maximum that rides on the eight fill beats. One cycle
over the whole entry was the first shape and it measured 32.5 MHz — 128 parallel
24-bit barrel shifters, nine times over a 300 MHz budget, against a fetch that
cannot deliver an entry faster than its eight AXI beats. `done` is 9 cycles
after the last beat; see [`../mas/quantiser-timing.md`](../mas/quantiser-timing.md).

### 6.4 Quantise on the way IN

The same circuit, claimed by the upload path instead of the read path.

**The marker is on the request.** AXI carries no field for "convert this" and
XDMA exposes no `AWUSER`, so it rides in the two top address bits of the memory
window — which is why the window is 32 bits of the 34-bit space rather than all
of it:

| bit | name | meaning |
|---|---|---|
| `[ADDR_W-1]` | `QUANT` | source is FP16, quantise as it lands |
| `[ADDR_W-2]` | `BLAYOUT` | pack for a B operand rather than an A operand |

The rest of the address is the **destination**, in the int7 layout. Source and
destination burst lengths are unrelated: the host may send any multiple of 8
beats in one burst, and MAG issues its own `awlen = 3` write per entry. A burst
that is not a whole number of entries leaves the last one short and the
handshake waiting for beats that will never come, so there is a
simulation-only `$display` for it.

**MAG still holds no address map, and that is the point.** It does not know
which regions are pre-quantised; it is told, per write and per read. The
driver decides — it is the only party that knows which tensors are reused —
and it never constructs int7+E5M3 itself, so the format stays entirely inside
the hardware.

**A quantiser per path, so there is no mutex.** Each `mag_mem_port` carries its
own `mx_quant` and the upload path carries one more, so there are
`MEM_PORTS + 1` of them. The upload also runs its own FSM (`hst`, with a
dedicated quantising-write state) on its own AXI master channel, index
`MEM_PORTS` — so it shares neither the converter nor the bus with any fetch.

An upload therefore never waits for a fetch and a fetch never waits for an
upload: `sm_awready` is `(hst == HS_IDLE)` and depends on nothing but the
upload's own state.

This is what replaced the earlier arrangement, where one shared instance forced
a mutex and an upload could only start while the read engine was idle. Removing
the sharing removed the only reason those two paths were coupled at all. See
[`../mas/spec.md`](../mas/spec.md) §2.4 for what a port is and why there is one
per mesh row.

**Mixed operands fall out for free.** The flag is per request, so a GEMM can
read pre-quantised weights and online activations with no extra mechanism —
which is the normal inference case, since weights are uploaded once and
activations arrive per layer.

### 6.5 The pre-quantised read path

A pre-quantised entry is four beats that **are** the four operand words. They
are captured into a staging register and moved into the emit buffer, rather
than written to the emit buffer directly: the emitter may still be handing out
the previous entry, and holding the R channel until it finishes would serialise
fetch behind emit — the exact coupling the emit buffer exists to break.

Nothing downstream changes. The response flits, their tags, and the CU's
assembly are identical to the quantised path; only the number of AXI beats and
the entry stride differ.

## 7. Acknowledgements are discarded

`mx_cluster_cu` **accepts and drops** `MEM_WR_ACK`: its single NoC local
demultiplexes inbound flits by type, and `in_ack` is answered with a
never-busy so the flit is consumed and discarded (`mx_cluster_cu.v` §115).
So the ack is still generated, routed, delivered and dropped.

> This used to be `a_in_busy` tied low on a port of its own. The behaviour is
> deliberately unchanged, but the mechanism now matters: with one shared port,
> an ack that were merely *ignored* would sit at the head of the receive FIFO,
> hold `noc_in_busy` high for good, and wedge the instructions behind it. It
> must be actively consumed, which is why the demux answers it rather than
> leaving it to the FIFO.

It is not useless: the ack is what releases the port's write slot, so it is the
backpressure that bounds how many of a cluster's writes the port is holding at
once. The CU does not need to see it because its own `W_IDLE -> W_REQ -> W_DATA`
loop already keeps one burst open at a time. What it does *not* do is wait for
the ack, which is exactly why a source needs two slots and not one (§3).

**What discarding it costs, which is not nothing.** A DRAIN retires on `w_idle`
— the last flit having *left the CU* — not on the ack, so `NODE_STATUS` reports
a round complete before its writes are committed in DRAM. That is benign only
while nothing reads what an earlier round wrote, which is true today: the
planner never re-reads C, K splits inside the cluster through the resident tile
rather than through memory, and each output tile belongs to one cluster, so no
read-after-drain exists. A K-split reduction through memory, a mover copy of a
freshly drained C, or a host readback racing the round would all break it.
Closing it means retiring DRAIN on the ack instead of on `w_idle` — a change to
the retirement condition, not to the demux.
