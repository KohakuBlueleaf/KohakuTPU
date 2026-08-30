---
title: Partitioned Xache — kx_pxache
summary: The Xache with its masters and homes spread over P partitions of one clock — dies of a part, regions of a floorplan — every boundary crossed by exactly one registered, credited hop, per-source lanes so the crossbar's bandwidth holds at every boundary, and a reorder ring per master so nothing downstream of an engine ever waits. P = 1 is kx_xache at the same LUT, latency and bandwidth.
tags:
  - axi
  - cache
  - crossbar
  - kohakuaxi
  - design
  - partition
---

# Partitioned Xache — `kx_pxache`

> **Kind: Yours throughout.** The lanes, hops and the reorder ring are this
> project's design. Where it meets a master or a DRAM controller it presents
> ordinary AXI4, and where it meets a die it presents a register on each side
> of a wire — nothing in it names a device; which partition is which die is a
> block design's business.

`src/kohakuaxi/pxache/` — the [Xache](xbar-cache.md) (`kx_xache`, the fused
crossbar-cache) is a **single-partition** fabric: every path in it is
register-to-register inside one region. `kx_pxache` is the same system with
its `M` masters and `N` homes assigned to `P` partitions, so that a master on
one die reaches a home on another through **one registered, credited hop per
boundary** and nothing else. The arrays, engines, edges and fan-in are the
Xache's, unchanged (`kx_carray`, `kx_rd_pipe`, `kx_wr_engine`, `kx_perm`,
`kx_link`); what is new is how a (master, home) pair that sits in two
partitions meets, and how a master takes its responses back.

**Provenance for every figure on this page: `xcvu13p-fhgb2104-2L-e`, Vivado
2024.2, out-of-context synthesis at a 3.333 ns (300 MHz) ask, one synthesis
per row via `scripts/tcl/ooc_mod.tcl`. Nothing is placed or routed; a
partition here is a parameter, and OOC does not know a die.**

---

## 1. What a partition costs, in one table

Every row is the whole system: caches, engines, crossbar, edges, lanes.
`M = 4`, `N = 4`, `K = 1`, `W = 512`, 64 URAM per home, the 16 KB channel
rotation (`NSWAP = 18`), `RD_OUTQ = WR_OUTQ = 4`, the ship's four DRAM-side
crossings unless the row says none.

| | P | LUT | FF | URAM | BRAM | WNS ns | Fmax MHz |
|---|---|---|---|---|---|---|---|
| `kx_xache`, ship — the baseline | 1 | **9,994** | 11,175 | 256 | 64 | +1.202 | 469 |
| `kx_pxache`, ship | 1 | **9,972** | 11,675 | 256 | 94 | +1.029 | 434 |
| `kx_pxache`, ship, masters and homes one per partition (`MP = HP = {3,2,1,0}`) | 4 | **10,960** | 26,570 | 256 | 298 | +0.569 | 362 |
| `kx_pxache`, no DRAM-side crossing, one per partition | 4 | 9,657 | 23,118 | 256 | 234 | +0.548 | 359 |
| one lane alone: `kx_lane` NT=3, W=590 (three hops, three taps) | — | 79 | 1,857 | 0 | 25.5 | +1.831 | 666 |

Read it as three numbers:

- **P = 1 is the Xache.** 9,972 against 9,994 LUT; hit latency 39 cycles
  against 39; every bandwidth scenario of the Xache's bench within 1%
  (§5). The 30 extra BRAM are the four masters' reorder rings (§3.3), and the
  500 extra FF are their bookkeeping.
- **Four partitions cost 966 LUT over the Xache** (988 over P = 1: 36 hops
  and their taps) and 14,900 FF, the hops' TX registers on 590-bit lanes
  (FF is the resource this part has to spare). BRAM 298 is the width floor:
  24 wide hops at 8.5 RAMB36 each, 12 narrow ones, 30 for the rings, 64 for
  the edges. With a register in front of every landing RAM (`HOP_RXREG = 1`,
  §2.1) the same design measured 10,528 LUT and 40,562 FF at +0.775 ns — 432
  fewer LUT, which synthesis attributes to the DRAM-side R links and the
  write engines' beat counters rather than to the hops, and one cycle more
  per hop.
- **Every boundary carries the crossbar's bandwidth.** Four masters
  streaming across four partitions read at 473 cycles per 64 KB where the
  single-partition fabric takes 467 (§5).

---

## 2. Lanes and hops

### 2.1 One hop — `kx_hop`

One valid/ready channel across one boundary, with **nothing combinational in
either direction of the crossing**:

```
   sender's partition        │ boundary │      receiver's partition
   s_valid/s_data ──► [TX reg] ─────────► landing ring (the RAM's own input register) ──► m_valid/m_data
                  credit ◄── [cr reg] ◄────────── [pp reg] ◄── pop
```

The sender's TX register has one load, the boundary wire. The wire lands in
the receiver's ring RAM — a block RAM's (and a distributed RAM's) write port
registers WE, ADDR and DIN at the clock edge, so the RAM *is* the landing
register — and each pop comes back as a registered pulse the sender counts
as **credit**: no ready ever travels back, so no skid ever sits in front of
the landing. The credit round trip is 3 cycles and the ring is 16 deep, so a
hop streams a beat per cycle. Each half takes the reset of the partition it
sits in, and a `fok` pulse tells the sender when the receiver's ring is out
of reset, so partitions may come out of reset in any order (measured: the
bench releases them 3 cycles apart, §6).

The landing ring (`BUF = "lean"`) is a `DEPTH`-entry `kohaku_sdpram` with one
read stage, kept from overflowing by the credits so it has no full flag and
no first-word-fall-through machinery. Its top bits — a lane's destination and
the flit's kind — come out of **distributed RAM**: whatever decodes the head
must not wait a block RAM's 0.83 ns clock-to-out, and moving those bits alone
took the lane from 469 to 666 MHz and the P = 4 system from −0.139 ns to
+0.775. The XPM FIFO form (`BUF = "xpm"`) is kept and measures one cycle
slower (4 accept-to-deliver against 3, `kx_hop_tb`).

**A hop is three cycles per direction**: TX register, RAM write, RAM read
(`kx_hop_tb`: 3 accept-to-deliver at W = 590 and 60, a flit per cycle). The
boundary wire's far end is then a RAM input, not a fabric flop, which an
out-of-context run cannot see; `RX_REG = 1` (`HOP_RXREG` on the system)
puts a register in front of every landing RAM for a placement that wants a
flop at both ends of a die crossing, at one cycle more per hop and 590 FF
per hop (§1 carries that row). §5 measures the three-cycle hop as +6 cycles
per boundary on a round trip.

### 2.2 One lane — `kx_lane`

A lane is one source's stream through `NT` partitions in one direction: a
chain of hops, one per boundary, with a **tap** after each. At tap `t` the
landing ring's head is examined once: a constant table `TAKE[t][dst]` says
whether this partition consumes the flit or the next hop forwards it. The
head feeds both the tap and the next hop's TX register — only the valids
differ — so **nothing is muxed in transit**; the lane is in order by
construction; and the last tap consumes any flit no tap claims, so a wrong
map cannot wedge it (the bench's `$display` names it).

### 2.3 Per-source lanes are what keep the bandwidth

Every master has an **AR lane** and an **AW/W lane** in each direction it
needs (up toward higher partitions, down toward lower); every home has an
**R/B lane** in each direction. A lane is tapped at every partition it
passes, so the tap at home `h`'s partition *is* that home's request slot for
master `m`, and the tap at master `m`'s partition is its response source from
home `h`. A pair in one partition is wires, exactly as in `kx_xache`.

Because no lane is shared between sources, every (master, home) pair keeps
its own path and a boundary carries as many streams as the crossbar would —
the max-flow of the partitioned fabric equals the crossbar's at every cut.
The price is the lane count: at P = 4 with one master and one home per
partition, 36 hops (12 AR, 12 AW/W, 12 R/B). W beats follow their AW **on the
same lane**, so a W never waits for its AW at a tap; the AW/W flit is
`{kind, W beat}` with the AW header riding in the beat's low bits, so only
the header's 63 bits are ever muxed. The R/B flit is `{kind, slot, id, resp,
last, word}` with the word on every flit and a B ignoring it, for the same
reason.

---

## 3. Ordering without waiting

### 3.1 Why the Xache's ordering cannot cross a boundary

`kx_xache` orders one master's reads with a sequence number and a *turn*: a
home's engine holds a completed burst in its drain state until it is that
master's oldest. In one partition that is safe, because every home sees the
requests in the order they were issued. Once the latency from a master to
each home differs — which is what a partition boundary is — it deadlocks:

```
   m  in P0,  m' in P3,  homes A in P3, C in P0
   m  issues  seq0 → A (far),  seq1 → C (near)
   m' issues  seq0 → C (far),  seq1 → A (near)
   A sees m'.seq1 first, completes it, HOLDS it: m' is waiting on C
   C sees m .seq1 first, completes it, HOLDS it: m  is waiting on A
   A cannot serve m.seq0 while holding; C cannot serve m'.seq0 while holding.
```

The cycle needs only two masters and two homes with unequal latencies, and
the same hold at a lane tap builds it through the tap instead. So the
partitioned fabric holds **nothing anywhere**: every response has a landing
place reserved before its request leaves.

### 3.2 A read: slot and ring reserved at the AR

Each master owns a **reorder ring**, a block RAM of `RD_OUTQ` pages of beats
(a page is 4 KB, 64 beats at 512 bits — AXI forbids a burst crossing one, so
no burst is longer). An AR takes the next free **slot**; the slot number rides
to the home in the AR flit and comes back in every R flit. A beat from any
source — a local engine or a lane tap — lands at `{slot, beat}` the cycle it
is offered, whatever order the homes answer in; the **drain** reads the
oldest slot beat by beat as its beats land and presents them in issue order,
so the master's R channel is AXI-ordered without a single wait. The pick
among sources is combinational and prefers the home of the slot being
drained: an engine's lookahead is `room = accept || !r_val`, and a ready one
cycle late ran it at a beat per three cycles (hit-32 measured 102 against
39); a plain lowest-valid pick let a nearer home land ahead of the drain,
which then idled and ran a 180-cycle tail on a single-master stream (1,224
against 1,044). With the drain's home first, both are at the Xache's figures.

A slot frees when its last beat is **issued** to the output register; the
beat carries its ID with it, because under a stall a new AR re-owned the slot
under that beat and the collector saw page 3's last beat labelled as page 7's
first.

### 3.3 A write: slots for B, one burst's beats before the next AW

A write takes a slot too, for its B: a B from home `h` completes the oldest
open slot bound for `h` (a home answers in order), and slots drain in issue
order, so `BID`s come back AXI-ordered. Its W beats go to the home latched at
the AW, and **the next AW is not taken until this burst's last beat has
gone** — an AW ahead of the beats of the one before it on the same lane would
be held at a tap by the engine's busy slot, with those beats wedged behind
it. The cost is the AW's own accept cycle per burst: four masters writing
16 KB each take 481 cycles against the Xache's 477.

### 3.4 Everything else is a wire inside its partition

The ready a master sees for a local home, a home's slot ready seen by a tap,
and a tap's accept seen by a home are gathered **at elaboration over the
homes or masters of that partition only** (`f_hp`, `f_mp` on the constant
`HP`, `MP` maps). The first build indexed every home's ready by the runtime
home field and paid a 16-level, 74%-route path from one master's table
through another partition's write engine into a third master's counter
(−0.394 ns); it was never functionally reachable, and the tool cannot know
that. Structurally there is now no unregistered path that leaves a
partition.

---

## 4. Knobs

| parameter | measured at | meaning |
|---|---|---|
| `P` | 1, 4 | partitions of one clock. P = 1 generates no lane |
| `MP[m]`, `HP[h]` | `{3,2,1,0}` | the partition of each master and home, packed `PW` bits each; the lane count and every `TAKE` table follow from them |
| `rstn_p[P]` | released together, or 3 cycles apart | one reset per partition; a hop's halves take the two they sit in |
| `RD_OUTQ`, `WR_OUTQ` | 4 | read slots (each a page of the reorder ring) and write slots per master |
| `HOP_DEPTH` | 16 | landing ring entries; ≥ 4 streams |
| `HOP_BUF` | `lean` | the ring, or `xpm` (one cycle slower) |
| `HOP_RXREG` | 0 | 1: a register in front of every landing RAM, +1 cycle per hop (§2.1) |
| the Xache's | the ship's | `M`, `N_HOME`, `K`, `W`, `SETS`, `MCDC`, `HCDC`, `NSWAP`, `SWAP_A/B`, `RAM_STYLE`, `CDC_DEPTH` — unchanged in meaning and in cost |

The engines are one per home on both sides (the Xache's SAMD); `RSAMD` and
`WSAMD` do not exist here, because a shared engine across partitions would be
the mux in transit this design has none of.

---

## 5. Performance

`tests/axi/kx_pxache_tb.v` is the Xache's bench with a partition map; the
`TB_PERF` scenarios are the Xache's (`4×4 K1`, block-RAM arrays, the 4 KB
interleave, a 24-cycle DRAM, 64-beat bursts, GB/s at 300 MHz), plus one
master streaming under a single ID. Cycles on the fabric clock.

| scenario | `kx_xache` | `kx_pxache` P = 1 | P = 4, one master and home per partition |
|---|---|---|---|
| 1 master reads 64 KB, hits, 4 outstanding | 1,044 · 18.8 GB/s | **1,038** · 18.9 | **1,036** · 19.0 |
| 4 masters read 16 KB each, hits | 465 · 42.3 | **467** · 42.1 | **473** · 41.6 |
| 4 masters read 16 KB each under **one ID** | — | 467 · 42.1 | 473 · 41.6 |
| 1 master writes 64 KB | 1,104 · 17.8 | 1,120 · 17.6 | 1,264 · 15.6 |
| 4 masters write 16 KB each | 477 · 41.2 | 481 · 40.9 | 508 · 38.7 |
| 2 KB read, hits | 37 | 39 | 39 local |
| 32-beat hit, AR accept to last beat, master 0 to home in partition 0 / 1 / 2 / 3 | 39 | 39 / 39 / 39 / 39 | **39 / 45 / 51 / 57** |

- Reads across four partitions are within 2% of the single-partition fabric:
  the lanes carry every stream at a beat per cycle and the ring hides the
  arrival order.
- **+6 cycles per boundary on a round trip** — a 3-cycle hop each way — is
  the whole latency cost, measured identical on every shape (+8 with
  `HOP_RXREG = 1`).
- The write rows at P = 4 are the bench's, not the fabric's: it waits for
  each burst's B before the next AW, and a remote B is a round trip away.
  Four masters' W beats still stream at a beat per cycle each.
- The single-ID row is the case the Xache's bench never ran (its streams use
  fresh IDs): one ID across interleaved homes streams at the same rate as
  many, because the ring orders by slot and never by ID.

---

## 6. Verification

Three benches, under xsim (the gate; Verilator is the inner loop and one of
its limits is below):

- `tests/axi/kx_hop_tb.v` — a hop at W = 590 and 60, `lean` and `xpm`:
  credits never above `DEPTH`, no ready while the receiver is in reset, the
  empty-hop latency, a streaming soak, a source reset with words in flight.
- `tests/axi/kx_lane_tb.v` — three taps and one, W = 590 and 60: staggered
  tap releases, a random soak over every destination, a head-of-line stall
  on one tap while the others drain, a source reset; per-tap scoreboards.
- `tests/axi/kx_pxache_tb.v` — the Xache's bench with `TB_P`, a partition per
  index, `TB_RSTAG` for staggered resets, a collector that matches beats by
  ID against per-(master, ID) queues, an every-master-to-every-home fork with
  both lane directions live, streaming soaks in both directions, and the
  hit-32 latency probe with the remote-above-local check.

Configurations run, every loop of the design before its synthesis: the three
lanes; the Xache's fourteen shapes — `4×4` at K 1/2/4, the 4 KB and 16 KB
interleaves, `2×4`, `8×4`, `4×8`, `8×8`, the two-clock ship, the 24-cycle
DRAM, the three `TB_PERF` scenarios — at **P = 1 and at P = 4**, and P = 4 with
resets 3 cycles apart: 32 builds, 2,877–9,192 checks each, 0 errors. P = 1
is cycle-identical to `kx_xache` on the latency probe and within 1% on
every bandwidth row (§5). `check.py full` runs the three plus the lint-only
entries (`kx_lane_lint`, `kx_pxache_lint`).

The first two designs of the loop are kept as measurements, not as code:

| design | P = 1 LUT | P = 4 LUT | P = 4 WNS | what sent it back to impl |
|---|---|---|---|---|
| per-ID ordering tables (one home in flight per ID) | 10,809 | 11,373 | −0.394 | +815 LUT of tables at P = 1; the runtime-indexed ready path of §3.4; a one-ID stream stalls at every home switch |
| reorder ring with ring-address allocation | 11,146 | 13,396 | −0.139 | 820 LUT of per-slot address and count arithmetic and a 512-beat space check; the AW/W flit built as a 590-bit mux; the tap's kind decoded off block-RAM output |
| reorder ring in pages, explicit flits, fast bits, a register before every landing RAM | 9,972 | 10,528 | +0.775 | four cycles per hop against the three asked for; the register duplicates the RAM's own input register |
| **the same with the wire landing in the RAM — ships** | **9,972** | **10,960** | **+0.569** | — |

---

## 7. What it deliberately does not do

- **No 2-cycle hop.** §2.1: the landing buffer is block RAM and is not
  bypassed; a bypass is a 590-bit 2:1 per hop, and this design spends no
  LUT on a datapath mux.
- **No fabric flop at the far end of the wire by default.** The RAM's input
  register is the landing; `HOP_RXREG = 1` adds one where a placement needs
  it, and only a placed run can say.
- **No per-destination credits.** A lane's credits are the next ring's; a
  taken flit that its consumer cannot yet accept holds the lane behind it
  for the length of that wait — bounded, because nothing downstream of an
  engine waits (§3).
- **No engine shared across partitions**, so no `RSAMD`/`WSAMD`.
- **One write burst's beats before the next AW** (§3.3); the AW's accept
  cycle per burst is the cost.
- **A burst is at most a page** — 64 beats at 512 bits — which AXI's 4 KB
  rule already says for full-width beats; a narrow burst of 256 beats would
  overflow its slot and the bench reports it, the RTL does not check it.
- **Nothing places anything.** `P` and the maps are parameters; which
  partition is which die, and the reset tree that releases each partition,
  are the block design's and are not in `src/kohakuaxi`.
- **The Verilator model of `kx_pxache_tb` does not run**: Verilator 5.020
  overflows its stack at the `fork` inside the bench's streaming task
  (`VlCoroutine`, AddressSanitizer: stack-overflow), while the same tool runs
  `kx_xache_tb` in 0.2 s; 5.020 predates the fork-in-task fixes. xsim is the
  gate of record.

---

## 8. Where to read next

- **[xbar-cache.md](xbar-cache.md)** — the Xache itself: the arrays, the
  engines, the clock model, every cost table and every bandwidth measurement
  this page compares against.
- **[README.md](README.md)** — KohakuAXI in one page.
- **[station-bus.md](station-bus.md)** — the other way across the dies, for
  host traffic.

RTL: `src/kohakuaxi/pxache/` — `kx_pxache.v` (the system), `lane/kx_hop.v`
(the hop, its TX and RX halves and the lean ring), `lane/kx_lane.v` (the
chain and its taps). Benches: `tests/axi/kx_hop_tb.v`, `kx_lane_tb.v`,
`kx_pxache_tb.v`. Measurement: `scripts/tcl/ooc_mod.tcl`, one configuration,
every report.
