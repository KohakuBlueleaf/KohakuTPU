# Where the cycles go, and what it would take to reach peak

Measured, then derived. Every number in the first half came out of a real run
(`ktpu.hw.sim` reports it per run); everything in the second half follows
from those numbers plus the RTL's own constants.

Peak is **512 MAC/cycle per cluster** (4 TCUs, each 4x8x4 = 128 MACs), so the
2-cluster machine peaks at 1024 MAC/cycle — **614 GFLOP/s at 300 MHz**.

---

## 0. Where it stands now

§1 onward is the **baseline this document was written to diagnose**, and the
diagnosis was acted on. `tests/mas/mag_driver_tb.v`, both operands
pre-quantised, one MAG port per mesh row:

**Shapes are `M x K x N`** — every table in this document, and every shape string
in the driver. They were written `M x N x K` when these runs were taken, which
made one string name two different GEMMs: `256x1024x256` read as `M x N x K` is
the healthy wide shape below, and read as `M x K x N` it is the K-heavy one that
used to run on a single cluster. Every row here has been **converted**, so the
numbers are the ones that were measured and only the labels moved.

| clusters | shape M x K x N | run cycles | GFLOP/s | % peak |
|---|---|---|---|---|
| 2 | 256x256x256 | 18,701 | 538.3 | 87.6% |
| 2 | 256x256x1024 | 72,684 | 554.0 | 90.2% |
| 4 | 256x256x512 | 20,647 | 975.1 | 79.4% |
| 4 | 256x256x1024 | 41,638 | 967.0 | 78.7% |
| 8 | 256x256x1024 | 24,115 | 1669.7 | 67.9% |
| 8 | 512x256x1024 | 43,382 | 1856.3 | 75.5% |

> **EVERY ROW OF THAT TABLE PREDATES THE MESH LAYOUT CHANGE.** It was measured
> when a cluster was a (row, left-column) pair — manager in the left half of the
> columns, accumulator directly across in the right half, with another cluster's
> manager physically between a cluster's own two endpoints. The RTL now places a
> cluster as one **column of a band**, managers on the outer rows and
> accumulators inside ([`system.md`](system.md) §2.3), which changes both hop
> counts and which port a cluster's traffic leaves by. Treat every figure above
> as a figure for the *previous* topology, and do not carry one forward as if it
> had been measured on this one.

### 0.1 The new layout, measured — and it is not free

**Both cluster counts pass end to end.** 2 CU runs `128x128x128`; 8 CU runs and
its error profile is unchanged from the old geometry:

```
   8 CU   p50 1.71e-04   worst 2.43e+00   49 of 524,288 over 10%
```

**It costs about three points of peak at 8 CU**: `flops` **72.7%** against
**75.7%** on the old geometry, same work, same arithmetic.

That cost is **not** the memory system. Two counters say so directly:

```
   fetch        7.1  ->  7.2  cycles/entry     unchanged within noise
   wslot_full   5.5% ->  2.1%                  BETTER
```

A memory-service regression would show up as `fetch` rising and write slots
filling; `fetch` did not move and slot pressure more than halved. What is left
is **routing**, which is what the change predicts: a cluster's memory traffic
now leaves by one of *two* different rows depending on its column
([`mas/spec.md`](mas/spec.md) §2.4), where both of its endpoints used to share
the single row they sat in.

**What the layout buys is physical locality and simpler program planning** — a
cluster's two endpoints are adjacent instead of straddling another cluster's
router, and a partial band is buildable — and it pays a few percent of peak for
them. That is the trade as measured, stated so nobody reads the layout change
as a free win.

> **This is not an open optimisation item.** Layout tuning is **deferred until
> the vector core and the general core exist**, because those units attach to
> the north, south and east edges and will change what an optimal placement even
> is. Do not treat the three points as a regression to chase.

> **These are cycles, converted.** GFLOP/s is `2 · MACs / cycles · 300 MHz` — the
> cycle counts are measured, the rates are that arithmetic on top of them.
>
> **300 MHz now closes, with margin.** The modules this table depends on have
> since been synthesised out-of-context for `xcvu13p-fhgb2104-2L-e`:
> `mx_cluster_cu` **325.6 MHz**, `mx_acu_fp` 327.7, `mag_mem_port` **330.0**,
> `mx_quant` 400.6, and on the NoC side a router at 406 (452 for two routers
> linked), an output port switch at 644 and an input port switch at 732. `mag`
> was not measured as a whole top level, only its memory port.
>
> Out-of-context means nothing is placed and the route is estimated, so those
> frequencies are **upper bounds**. The rates above no longer rest on an
> unmeasured assumption that the logic is shallow enough for 300 MHz; they do
> still rest on a floorplan that has not been built.

The eight-cluster rows are the ones with headroom left, and what they are short
of is not memory: it is instruction dispatch serialising across clusters. The
running record is `.plans/optimization-log.md`; the surviving and withdrawn
levers are [`optimization.md`](optimization.md) §I–§J.

> **Dispatch has since stopped funnelling through one link, and it was free.**
> The agent gave up its own NoC node and now shares MAG's memory ports, so a
> dispatched flit leaves from the port on its destination's row instead of from a
> single east-edge attachment ([`mas/spec.md`](mas/spec.md) §2.5). Measured
> against the rows above, which are the figures from **before** that change:
>
> | shape M x K x N | before | after |
> |---|---|---|
> | 2 CU 256x256x256 | 18,701 cyc · 538.3 · 87.6% | **identical** |
> | 4 CU 256x256x512 | `flops` 79.4% | **79.6%** |
> | 8 CU 512x256x1024 | 43,382 cyc · 1856.3 · 75.5% | **43,315 · 1859.2 · 75.7%** |
>
> Free, and marginally better. The reason is worth naming: an agent flit no
> longer crosses the mesh from the east edge to reach a cluster, so dispatch
> paths are physically shorter. **Do not read more into it than that** — 67
> cycles is 0.15% at eight clusters, well inside what a topology change can move
> either way, and the cause of the eight-cluster shortfall is still the serial
> staging cursor ([`optimization.md`](optimization.md) §J4).
>
> Error profiles are unchanged at every cluster count, and `mag_system` (257
> checks) and `mag_wslot` pass.
>
> One counter changed meaning with it: `in_bp` went 0.0% → **39.8%** at 2 CU and
> **22.4%** at 8 CU while the cycle counts did not move. It now counts routine
> demultiplexing rather than congestion — §1.1.

---

## 1. Measured — the baseline

| shape M x K x N | run cycles | fill | gemm | drain | idle | MAC/cyc | GFLOP/s | % peak |
|---|---|---|---|---|---|---|---|---|
| 64x128x64 | 8,213 | 56.7% | 17.3% | 12.1% | 13.8% | 63.8 | 38.3 | 6.2% |
| 128x128x128 | 32,361 | 61.3% | 17.1% | 11.6% | 9.8% | 64.8 | 38.9 | 6.3% |
| 256x256x256 | 239,786 | 71.9% | 13.4% | 6.7% | 7.9% | 70.0 | 42.0 | 6.8% |
| 512x512x512 | 1,983,413 | 73.0% | 10.3% | 3.4% | 13.3% | 67.7 | 40.6 | 6.6% |

The rate is flat across a 512x range in problem size. That is the signature of
a **structural** limit rather than a small-problem artifact: the machine has
one operating point and tiling does not move it.

`C` write amplification is **exactly 1.00x** at every size, including 512-cube
where each output tile is accumulated over 4 K-chunks. The output-stationary
dataflow works as designed; none of the loss below is redundant C traffic.

### 1.1 Per component, 128x128x128

```
MAG service FSM               stalls (cycles LOST, not spent)
  idle    31.3%                 in_bp     0.0%   fabric never backpressures
  qfill   35.0%                 out_bp    0.0%   MAG always hands over
  qwait    7.8%                 cu_send   0.1%   CU never blocked sending
  qemit   27.2%                 cu_dry   64.7%   CU STARVED in FILL
  wr      11.7%
```

> **`in_bp` no longer means what that line says, and nothing got slower.** The
> counter samples `mem_in_busy` — cycles in which MAG refuses NoC traffic — and
> MAG now refuses **routinely**. These are **MAG ports, not memory ports**: one
> serves two consumers, its own engine and the dispatch agent, told apart by
> flit type, and it holds busy on any cycle whose offered flit belongs to the
> *other* one
> ([`mas/spec.md`](mas/spec.md) §2.5). It is now read as **"port declined a flit
> (incl. not-mine)"**, which is what it counts.
>
> ```
>    2 CU 256x256x256    in_bp 0.0% -> 39.8%   at an unchanged 18,701 cycles
>    8 CU 512x256x1024   in_bp 0.0% -> 22.4%   at 43,382 -> 43,315 cycles
> ```
>
> **No time transferred anywhere.** The rates, the cycle counts and the error
> profiles are the same or fractionally better (§0); a counter moved by tens of
> percent while the thing it was supposed to explain did not move at all, which
> is the signature of a renamed event rather than a new cost.
>
> So `in_bp` measures **demultiplexing, not congestion**, and it is no longer
> evidence of pressure on the fabric. The 0.0% above remains the right reading of
> the *baseline* machine, where the only reason to refuse was a full queue — but
> the two numbers count different events under one name and must not be compared.
> **A derived figure needs its predicate re-checked, not just its denominator,
> whenever the thing it counts changes shape**; §2.2 is the same lesson from the
> two earlier occasions.

`cu_dry` at 65% with every backpressure counter at zero is the whole diagnosis:
the CU is **starved, not blocked**. Nothing is pushing back on it; there simply
is no data to take. So the fault is upstream of the fabric, in how operands are
requested and served.

### 1.2 The fetch path, per L1 entry

An L1 entry is 4 lanes x 32 FP16 = **256 B in memory**, fetched as 8 AXI beats
of 32 B, returned as 4 operand flits after quantisation.

| | cycles/entry |
|---|---|
| AXI beats alone (the floor) | 8 |
| MAG actual: qfill 9 + qwait 2 + qemit 7 | **18** |
| CU actual, serial requests | **~37** |
| CU actual, 512-cube (2 clusters sharing MAG) | **~48** |

Two separate losses, and they compound:

* **MAG is 2.2x off its own floor.** `S_Q_FILL` collects a whole entry, *then*
  `S_Q_EMIT` hands out four flits. The two never overlap, and `take_rd` only
  fires in `S_IDLE`, so the next request cannot even start until the last flit
  of the previous one has left.
* **The CU exposes the full round trip per entry.** `S_FILL` issues one request
  and waits for the whole burst before issuing the next, so ~37 cycles of
  latency is paid 32,768 times per cluster at 512-cube instead of once.

---

## 1.3 The configuration is not the one the design specifies

Before any of the analysis below: `mx_cluster_cu.v`'s own header states the
sizing rule the cluster was designed against.

> The chain eats eight 256-bit operand words per cycle and a port delivers one,
> so feeding the TCUs directly from the NoC is an 8x deficit no matter how many
> ports are spent on it. Reuse closes the gap instead: a Gm x Gn sub-tile block
> needs `4(Gm+Gn)/(Gm*Gn)` words per cycle, which is **0.375 at 16x32**.

The bench instantiates `TILES = 64`, i.e. Gm = Gn = 8:

| Gm x Gn | TILES | words/cycle needed | port supplies | margin |
|---|---|---|---|---|
| 8 x 8 | 64 | **1.000** | 1.0 | **none** |
| 16 x 16 | 256 | 0.500 | 1.0 | 2.0x |
| **16 x 32** (designed) | **512** | **0.375** | 1.0 | **2.7x** |
| 32 x 32 | 1024 | 0.250 | 1.0 | 4.0x |

At 8x8 the operand demand is **exactly** the port's capacity. Every cycle lost
to request latency, arbitration or FSM overhead comes straight off the result,
because there is no headroom to absorb it. The measured 6.6% is that: a
break-even configuration plus overhead.

So the first correction to everything below: this is **not** a machine at a
fundamental roofline. It is a machine running at the one point its own sizing
rule says has zero margin. The designed point (16x32) has 2.7x of headroom,
which is what makes overhead survivable rather than fatal.

**But capacity alone is not the fix either.** Going to 16x32 without touching
the fetch mechanism converts a 100%-utilised port into a 37%-utilised one and
leaves the *per-access* overhead untouched -- a bigger tile issues fewer, larger
fetches, so it hides overhead rather than removing it. And a large tile has its
own cost, paid in padding (s5.1). Both halves are required:

1. **fewer, larger accesses** -- so each fetch amortises its own overhead, and
2. **less overhead per access** -- so the amortisation is not doing all the work.

Doing only (1) is what "make each CU 128x128x128 instead of 4x32x4" would be:
the same bad mechanism, invoked less often, with a padding bill attached.

---

## 2. The roofline, and why tuning cannot reach peak

Arithmetic intensity, per pass over a `gm x gn` output tile across the whole K:

```
MACs      = 16 * gm * gn * K
bytes     = (gm + gn) * (K/32) * 256          FP16 operands, 256 B per entry
MACs/byte = 2*gm*gn / (gm+gn)   =   g          for gm = gn = g
```

**Intensity equals the tile edge in sub-tiles.** It does not depend on M, N or
K -- only on the tile. That single fact drives everything below.

`TILES = 64` caps `gm*gn`, so g = 8, so the machine gets 8 MACs per byte. To
run 1024 MAC/cycle it needs **128 B/cycle**. The AXI port (`DATA_W = 256`)
supplies **32 B/cycle**.

```
   GFLOP/s
    614 |························································· peak
        |                                                  ,-'
        |                                             ,-'
    500 |----------------------------------------,-'------------- target
        |                                   ,-'
        |                              ,-'
    300 |                         ,-'
        |                    ,-'
    154 |···············,-'   <- ceiling at g=8, 32 B/cycle
        |          ,-'
        |     ,-'
     41 |x,-'   <- measured (bubbles on top of the ceiling)
        +----+----+----+----+----+----+----+----+----+----+
          4    8   16   24   32   40   48   56   64
                  arithmetic intensity g (MACs/byte)

   the machine is on the SLOPE, not under the roof: it is bandwidth
   starved, and the slope is set by the tile size, not by scheduling
```

**Ceiling at the baseline: 154 GFLOP/s (25% of peak).** The measured 41 is
bubbles *on top of* that ceiling. Removing every bubble would reach 154 and stop, because
at g=8 the memory port physically cannot feed the array faster.

This is the important correction to make about all the fill/prefetch work: the
bubbles are worth ~3.7x, and then it is over. Reaching 500 GFLOP/s is a
memory-hierarchy decision, not a scheduling one.

> **The last sentence is wrong, and it is worth knowing how.** 538.3 GFLOP/s was
> reached without widening a single bus. The roofline above is sound arithmetic
> on an assumption the schedule controls — that each operand byte is fetched
> once — and the schedule was re-reading B once per m-tile, a quarter of all
> traffic that no intensity figure shows. **The ceiling is a function of the
> schedule, so a schedule change moves it.** Every subsequent "it must be
> bandwidth" in this project has also been wrong; §2.2 is the check.

### 2.1 Three levers, each independent

| lever | mechanism | intensity | BW needed | cost |
|---|---|---|---|---|
| **int7 in DRAM** | store pre-quantised, not FP16 | 8 -> 16 | 64 B/cyc | driver packs once; deletes MAG's whole quantise pipeline |
| **`DATA_W` 256 -> 512** | wider AXI | 8 | 128 B/cyc, supplied 64 | a DDR4 MIG interface is 512-bit anyway |
| **`TILES` 64 -> 512** | Gm 8 -> 16, Gn 8 -> 32 | 8 -> 21.3 | 32 B/cyc | none — 5 BRAM36 whatever the depth, see s4 |

> **Measured outcome, 256-cube on 2 clusters.** All three levers were taken
> except the wider bus, and the tile went to 512 rather than 1024 for the
> reason in §4. **42.0 → 538.3 GFLOP/s pre-quantised**, 12.8x, at **87.6%** of
> the 1024 MAC/cycle peak (§0). The last measurement of the online path — both
> operands quantised on every read — was **408.6 GFLOP/s, 66.5%**, taken
> *before* per-row memory ports; it has not been re-run since, and the
> read-engine split is exactly what the online case was short of.
>
> The table's "intensity" reading is the right *shape* of argument and the
> wrong *quantity* to stop at: it assumes each operand byte is fetched once,
> and the schedule decides whether that is true. Count beats — but count them
> **per channel**. At the 256-cube the machine moves 6,144 read beats and
> 4,096 write beats against a 16,384-cycle compute floor, and those are
> different AXI channels: 37.5% and 25% of two independent budgets, not 62.5%
> of one.

Storing int7 in DRAM is worth taking anyway: it halves traffic *and* removes
`qfill`+`qwait`+`qemit` (70% of MAG's budget) because MAG stops converting on
every fetch. Today the same element is quantised g times -- once per reuse.

### 2.2 The resource line: five budgets, and the sum of them means nothing

`run_matmul.py` prints one line per run, and it is the only place a rate is
decomposed into what the machine was actually doing:

```
flops   MAC/cycle against 512 per cluster
mem_rd  AXI read beats,   against one per cycle per MAG port
mem_wr  AXI write beats,  against one per cycle per MAG port
noc_in  flits into MAG's ports,  against one per cycle per port
noc_out flits out of them,       against one per cycle per port
```

> `noc_in` and `noc_out` count **everything crossing a MAG port**, operand and
> control alike, because one port carries both — see §1.1.

**They are independent, full-duplex budgets and must never be added.** AR/R and
AW/W are different AXI channels — `axi_ram` drives reads and writes from two
always blocks and serves both in the same cycle — and the NoC's two directions
are different wires. Adding them prices capacity that never competes. **What
binds is the largest, never the total.**

**And each memory figure is divided by the port count**, because the counters
are sums over MAG's memory ports (`mem_rd_count` and `mem_wr_count` are summed
inside `mag.v`) and each port has its own channel — [`mas/spec.md`](mas/spec.md)
§2.4.

Both halves of that were got wrong once, and **both times the error pointed
falsely at bandwidth**:

| the mistake | what it read | what was true |
|---|---|---|
| `mem` reported as `(rd + wr)/run`, then added to `noc_in` and `noc_out` | 91.5% "of data movement" — scheduling exhausted, only a wider bus left | busiest path `noc_out` 28.3%, busiest resource the array itself; the missing 30% was **latency**, and two one-line fixes to elastic registers took 69.6% → 80.7% |
| per-port sums still divided by one port per cycle | `mem_rd 101.9%`, `noc_out 110.4%` at 8 clusters — a saturated bus | `mem_rd 25.5%`, `noc_out 27.6%` against `flops 67.9%`; nothing close to saturated |

An impossible percentage looks exactly like a saturated bus, which is the very
conclusion splitting the metric was meant to stop guessing at. **A derived
percentage needs its denominator re-checked whenever the thing it counts changes
shape.**

**There is now a third case, recorded before anyone reads it as a regression.**
`in_bp` jumped 0.0% → 39.8% on a run whose cycle count did not move, because the
*event* it counts changed rather than the denominator: a memory port refuses NoC
traffic routinely now that it demuxes between two consumers by flit type (§1.1,
[`mas/spec.md`](mas/spec.md) §2.5). The rule generalises accordingly — **check
the predicate as well as the denominator**, and treat a stall counter that moves
while the cycle count does not as a change in meaning until proven otherwise.

Read correctly, on the current machine (shapes `M x K x N`):

```
2 CU 256x256x256    flops 87.6%  mem_rd 30.3%  mem_wr 20.2%  noc_in 22.8%  noc_out 32.8%
8 CU 256x256x1024   flops 67.9%  mem_rd 25.5%  mem_wr 17.0%  noc_in 19.2%  noc_out 27.6%
8 CU 512x256x1024   flops 75.5%  mem_rd 23.6%  mem_wr 18.9%  noc_in 21.3%  noc_out 26.0%
```

No memory budget exceeds a third at any cluster count, and the busiest thing in
the machine is the array. That is what withdrew the wider bus as a lever
([`optimization.md`](optimization.md) §J1) and it is the standing answer to
"surely we are bandwidth-bound by now".

---

## 3. Instruction and fetch redesign

The current ISA makes the losses in s1.2 inexpressible-away: they are
properties of what the instructions *mean*, not of how they are implemented.

```
FILL  sel, addr, n            synchronous: retires when the last entry lands
GEMM  gm, gn, nk, anchor, acc
DRAIN addr, n, anchor, last
```

`FILL` blocks, so fill and gemm can never overlap. And it says nothing about
what comes next, so the CU cannot start fetching ahead.

### 3.1 Proposed

```
FILL  sel, bank, addr, n, stride    non-blocking; retires on ISSUE
GEMM  bank, gm, gn, nk, anchor, acc stalls until `bank` has landed
DRAIN addr, n, anchor, last
```

Three changes, in order of value:

**(a) `FILL` becomes a DESCRIPTOR, not a loop.** Today the CU sends `n`
separate read requests and pays a round trip on each. One descriptor -- base,
count, stride -- lets MAG stream the whole run as long AXI bursts. This removes
the per-entry round trip entirely rather than hiding it, cuts NoC request
traffic by `n`x, and lets the DRAM controller see sequential bursts instead of
32 scattered reads. **This is the fix; prefetch depth is a workaround for not
having it.**

**(b) `FILL` retires on issue, not on completion.** The CU signals
`INST_COMPLETE` once the descriptor is accepted, then a per-bank counter tracks
outstanding entries. `GEMM` waits on that counter. Completion becomes a
dependency the hardware evaluates, not a guess the manager has to make about
latency.

**(c) `bank` double-buffers L1**, so the driver emits software-pipelined code:

```
FILL A->0 ; FILL B->0        prologue
FILL A->1 ; FILL B->1        chunk 1 streams while chunk 0 computes
GEMM 0                       waits on bank 0 only
FILL A->0 ; FILL B->0        refill bank 0 for chunk 2
GEMM 1
...
DRAIN
```

L1 must double: `GA`/`GB` 32 -> 64 entries each. At 928 bits per entry that is
59 Kb per operand per cluster -- BRAM, not URAM, and small.

### 3.2 MAG, correspondingly

* Accept a descriptor and stream it, rather than one request per `S_IDLE` visit.
* Overlap fetch and emit: `S_Q_FILL` of entry *n+1* concurrent with `S_Q_EMIT`
  of entry *n*. Double-buffer `q_w0..3`. Takes 18 cycles/entry to ~9.
* With int7 in DRAM the quantiser leaves the read path altogether and MAG
  becomes a burst engine: ~4 cycles/entry, the AXI floor.

### 3.3 What this is worth

| | cycles/entry | ceiling | with TILES=1024 |
|---|---|---|---|
| today | 48 | 154 GF/s | -- |
| + descriptor fetch | ~18 | 154 GF/s | ~400 GF/s |
| + MAG overlap | ~9 | 154 GF/s | ~550 GF/s |
| + int7 in DRAM | ~4 | 308 GF/s | **~610 GF/s** |

The ISA work alone never beats the 154 ceiling -- it just reaches it. Combined
with `TILES`, it clears the 500 target.

---

## 4. URAM

The accumulator is `TILES` sub-tiles of 4x4 values. `ACC_MW = 14` mantissa plus
exponent and sign is ~24 bits, so a sub-tile is 16 x 24 = **384 bits**.

A URAM288 is **4096 deep x 72 bits**. A whole sub-tile must be read or written
per access, so the array is *width*-limited: `ceil(384/72)` = **6 URAMs** wide,
each 4096 deep.

That is the crux: **6 URAMs give 4096 sub-tiles, and 6 URAMs is also the
minimum for any TILES at all.** Depth is free until 4096.

| TILES | g | URAM/cluster | depth used |
|---|---|---|---|
| 256 | 16 | 6 | 6% |
| 1024 | 32 | 6 | 25% |
| **4096** | **64** | **6** | **100%** |

So `TILES = 4096` would cost exactly what `TILES = 256` costs, in URAM.

> **This was NOT taken, and the reasoning above is what to be careful of.** It
> is right about the primitive count and wrong about what a deep accumulator
> costs. `TILES = 4096` at `Gm = Gn = 64` makes the output block 256x256, so
> every dimension of every problem pads up to a multiple of 256 and 32 clusters
> need an 8192-wide problem to fill. Bandwidth bought by spending output
> granularity is bandwidth charged back on every shape that is not enormous.
>
> **What was built instead: `TILES = 512` on BRAM, `Gm = 16`, `Gn = 32`.** The
> resident tile is 352 bits wide against a 72-bit BRAM36 port, so it is
> `ceil(352/72) = 5` primitives **at any depth up to 512** — the same "depth is
> free" argument, applied to the primitive that was already being paid for, and
> spending **no URAM at all**. Intensity 8.0 → 21.3, output block 64x128, so 32
> clusters fill at a 724-wide problem.
>
> The URAM stays available for what the architecture still owes: an L2 in MAG,
> an FP16 vector unit, a general core. Spending all of it on matmul residency
> was never the plan.

### 4.1 Cost at 32 clusters

```
6 URAM/cluster x 32 clusters = 192 URAM
VU13P has 1280                = 15% of the device
```

**192 URAMs, 15%.** For comparison the same 32 clusters need 32 x 256 = 8192
DSPs of 12288 (67%), so URAM is not close to being the binding resource --
DSPs are.

At `TILES = 4096`, g = 64, so 32 clusters (16384 MAC/cycle) need 16384/64 =
**256 B/cycle** of memory bandwidth = 76.8 GB/s at 300 MHz. That is roughly
four DDR4 channels, or comfortably one HBM stack. With int7 in DRAM it halves
to 128 B/cycle.

---

## 5. When the problem is larger than the accumulator

**There is no cliff, and the reason is worth being precise about.**

The accumulator never holds the *problem*; it holds **one output tile**. At
`TILES = 4096`, g = 64, a tile is 256x256 outputs. A larger GEMM is more
passes, and every pass has identical shape, cost and intensity.

Intensity is `2*gm*gn/(gm+gn)` -- **M, N and K do not appear**. So a 256x256
GEMM and an 8192x8192 GEMM run at the same MAC/cycle. This is already visible
in s1: 64-cube through 512-cube vary by less than 10% despite 512x the work.

What does grow with problem size is total operand traffic, `M*N*K*2/g` bytes --
but it grows *proportionally to the MACs*, which is what keeping intensity
constant means.

Two second-order effects, both shrinking as tiles grow:

* **Per-pass overhead** -- the first entry's fetch latency and the DRAIN tail
  are fixed per pass, amortised over `gm*gn*nk` work. At g=8 that is 64
  sub-tiles; at g=64 it is 4096, so overhead per unit work falls 64x.
* **Edge tiles** -- a dimension not a multiple of `4g` is padded with zeros. At
  g=64 the tile is 256 wide, so a 300x300 GEMM pads to 512x512 and wastes 65%.
  **This is the real cost of a large tile**, and it is a small-and-awkward-shape
  problem, not a large-shape problem.

So the honest summary of your intuition: **yes, exceeding "what URAM can hold"
costs nothing**, because the accumulator was never sized to hold the problem.
The thing to watch at large `TILES` is not big shapes but *ragged* ones.

---

## 6. Order of work

1. **`TILES` 64 -> 4096** (6 URAM/cluster). Moves the ceiling 154 -> 614.
   Requires `L1_ENTRIES >= gm*nk`; at g=64, nk=1 needs 64 entries.
2. **Descriptor `FILL`** (s3.1a). Removes the per-entry round trip. Biggest
   single bubble.
3. **MAG fetch/emit overlap** (s3.2). 18 -> 9 cycles/entry.
4. **int7 in DRAM** (s2.1). Halves traffic and deletes the quantise pipeline.
5. **Banked async `FILL`** (s3.1b, s3.1c). Overlaps fill with gemm; worth ~10%
   once the above are done, and it is the largest RTL change of the five.

1 and 2 together are most of the distance.
