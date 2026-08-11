# Every optimization path, surveyed

The full option space, with a verdict on each. `docs/dataflow.md` shows the
signal-level before/after for the ones that survive; this document is the
selection argument, including the paths that were rejected and why.

Baseline: 512-cube, 2 clusters, `TILES=64`, **40.6 GFLOP/s of a 614 peak**.
Measured split per cluster: fill 48.2 cycles/entry (floor 8), gemm 1.69
cycles/op (floor 1), drain 9.05 cycles/sub-tile (floor 2).

Verdicts: **TAKE** · **LATER** (real, not now) · **REJECT** (with reason).

---

## A. The fetch path

### A1. Tagged responses — **TAKE, first**
Responses carry `{entry, word}`; MAG stops holding `rq_x/rq_y/rq_txn` and
`take_rd` drops `st == S_IDLE`. Its own gain is ~0 — it is the enabling change
that makes A2, A4 and A10 representable at all. Without it the protocol matches
responses by arrival order and cannot name a second outstanding request.

### A2. Streaming descriptors — **TAKE**
`FILL sel, bank, base, count, stride` in one flit; MAG generates the address
sequence. Round trip paid once per run instead of per entry. Also removes 31 of
32 intake-queue slots and gives DRAM one sequential run.

### A3. MAG fetch/emit overlap — **TAKE**
Double-buffer the quantiser output so the AXI read of *n+1* runs under the NoC
emit of *n*. Two physically independent interfaces, currently serialised by one
buffer.

### A4. Multicast — **TAKE, highest value at scale**
Destination mask on the descriptor. All clusters sweep the same rows of A, so
MAG currently runs its quantiser — 70% of its busy time — once per *consumer*
instead of once per *byte*. The only option whose benefit grows with cluster
count; every other divides a constant.

### A5. Wider AXI, `DATA_W` 256 → 512 — **LATER**
Halves the 8-cycle floor to 4. Real and simple, and a DDR4 MIG interface is
512-bit anyway. Deferred because at 48.2 cycles/entry the floor is not what
binds; revisit once A1–A4 land and the floor is actually reached.

### A6. Multiple MAG instances — **LATER**
One MAG per group of clusters. Straightforward capacity, but A4 divides the
same pressure without replicating the quantiser, so do A4 first and re-measure.

> **Taken, in a different shape and for a different reason.** Not several MAGs
> and not for capacity: one MAG with `MEM_PORTS` independent memory ports, one
> per mesh row. `fetch` cycles per entry tracked the cluster count exactly while
> every bus sat under 60%, so what ran out was the single read engine — a
> server, not a wire. `docs/mas/spec.md` §2.4.

### A7. Pre-quantised operands in DRAM — **LATER, strong**
Store int7+E5M3 instead of FP16: halves DRAM bytes *and* deletes
`qfill`/`qwait`/`qemit` from the read path entirely. Rejected for now only
because it changes the host-side data contract and the driver's memory image,
which is a wide blast radius while the fetch mechanism is still moving.

### A8. Cluster-local URAM scratchpad — **REJECT for now**
Would cut per-entry cost by serving L1 from local URAM. But it addresses the
*cost of an entry* by adding a memory, when A1–A3 address it by removing
waiting, at no storage cost. Costs ~7 URAM/cluster and a new address space the
kernel must manage. Reconsider only if A1–A4 land and fetch is still the
critical path.

### A9. L2 cache in MAG — **DEFERRED, and it is coming**
Not rejected. The original verdict said "strictly dominated" on a **bandwidth**
argument — an L2 divides DRAM traffic but not the quantiser, and a GEMM has no
data-dependent addressing, so a cache rediscovers at run time what the kernel
already wrote down. All of that is still true and all of it misses the point.

**The reason for an L2 is LATENCY, not bandwidth.** DRAM latency is hundreds of
cycles, and every argument above is about how many bytes move rather than how
long the first one takes. Nothing in this document measured that, because the
bench RAM answers in a handful of cycles — `rfill_dry` sits at 0.1–0.3% at every
cluster count, which says the read engine is never waiting on memory *in
simulation* and says nothing whatever about DDR4.

So: architecturally required, not currently the binding constraint, and it does
not affect functionality — the machine computes the same answer with or without
it. The same reasoning applies to an L2 in the CU, which is worth exploring for
the same reason and is equally not urgent.

What this **does** invalidate is using A9 as an argument against caching in
general. When it is built, size it against the DRAM round trip the bench does
not model, not against the byte counts here.

### A10. Banked L1 + async FILL — **TAKE, after A1–A4**
`FILL` retires on issue, `GEMM` waits on a per-bank outstanding count, so the
fetch of chunk *k+1* overlaps the compute of chunk *k*. Needs L1 doubled
(BRAM). Worth ~10% once fetch is no longer dominant — do it last, not first.

---

## B. The compute path

### B1. Non-draining `acc` chain — **TAKE**
1.69 cycles/op against a floor of 1. The 17-cycle cascade drains at every
K-chunk boundary (4 × 128 × 17 ≈ 8.7k cycles, ~10% of the excess); the rest is
the sweep not issuing every cycle. Cheap and local.

### B2. Wider TCU array — **REJECT**
Raises peak, which is not the problem: the machine achieves 6.6% of the peak it
already has. Widening the array widens the gap.

### B3. More TCUs per cluster — **REJECT**, same reason as B2.

---

## C. The drain path

### C1. Streaming DRAIN — **TAKE**
9.05 cycles/sub-tile against a floor of 2, and the same defect as FILL: an
instruction naming a bulk operation executed as individually-acknowledged
singles. Shares the A1/A2 mechanism, so it is nearly free once those exist.

### C2. Overlap drain with the next fill — **LATER**
Requires the three-unit split (access / execute / writeback). Real, but the
gain is small until C1 lands.

### C3. Wider drain port — **REJECT**
Drain is 3.4% of runtime at 512-cube. Not where the time is.

---

## D. Tile and dataflow shape

### D1. Larger tile (`TILES` 64 → 256/1024/4096) — **REJECT**
It works, and it is still wrong. Operand traffic per unit compute is
`8(Gm+Gn)/(Gm·Gn)`, so growing the tile does buy bandwidth — by **spending
output granularity**. A `Gm×Gn` tile forces a `4Gm × 4Gn` minimum output block,
and compute-bound at 32 clusters needs Gm=Gn=32, making the smallest problem
that fills the machine 4096×128, one step from 8192-wide. Padding then bills
27% on a 3000-wide GEMM before any efficiency is counted.

This is the trap: it *looks* like an optimization on a 2-cluster square
benchmark and is a regression on real shapes at real cluster counts.

### D2. Deeper accumulator (URAM) — ~~REJECT~~ **TAKEN**, `TILES = 4096`
Only exists to enable D1. Same objection, plus it spends URAM on granularity
nobody wants. (This was my recommendation in an earlier draft; it was wrong.)

> **Reversed, on a measurement neither draft had.** `mx_cluster_node` never
> passed `TILE_PRIM` down, so the trade was unreachable from a generated top and
> the rejection was argued rather than measured. Threaded through, the 6+0 mesh
> reads **30 URAM for 6 clusters and BRAM 254 → 224** — a 1:1 exchange, because
> 352 bits is 5 primitives either way and only the depth behind them changes.
> `TILES` 512 → 4096 for no net memory, intensity 21.3 → 64.0.
>
> The granularity objection survives as a *caution*, not a cost: `TILES` is a
> ceiling and `choose_tile` discounts by padding, so a small problem still gets
> a small tile. "It spends URAM on granularity nobody wants" was measuring the
> wrong thing — the meshes were using 0 of 320 URAM per SLR.
> [`compute/accumulator.md`](compute/accumulator.md) §3.1,
> [`perf.md`](perf.md) §4.

### D3. Larger L1 — **TAKE, but only as part of A10**
Needed for banking. Not an optimization on its own.

### D4. Different loop order — **REJECT**
K is already innermost and C write amplification measures exactly 1.00× at
every size. Any other order spills partial tiles. Nothing to gain.

### D5. K-split across clusters (task #12) — **LATER**
Needed for tall-skinny shapes where M·N cannot fill the clusters. Requires the
ACU peer path, currently tied off. Orthogonal to throughput on square shapes.

---

## E. Control and instruction issue

### E1. Loop opcodes `REPEAT`/`ENDR` (task #19) — **REJECT for now**
Would make the program O(1) in tile count. But streaming rounds already solve
program-RAM flooding, and at 512-cube the binding constraint is the staging
window (29 rounds) with only 41 of 128 command slots used. It compresses the
part that is not full.

### E2. Round double-buffering — **TAKE, cheap**
Host uploads round *n+1*'s flits into the other half of the staging window
while *n* runs. Idle is 13.3% at 512-cube and it is almost entirely this. Pure
driver + staging-window change, no new hardware.

### E3. Fewer commands per pass — **DONE**
`wr_setup` shadowing brought a kick from 6 commands to ~4.3. Command RAM is no
longer near binding.

---

## F. Driver and kernel

### F1. Pass ordering for operand sharing — **TAKE, required by A4**
Multicast only pays if the clusters that share operands are issued together. A
mask nothing sets does nothing. This is the driver half of A4 and must land
with it.

### F2. Padding-aware tile choice — **TAKE**
`choose_tile` currently maximises against hardware limits and ignores the
actual M, N. It should weigh padding waste — the guard that keeps D1's failure
mode from reappearing by accident.

### F3. Ragged edge tiles — **LATER**
`Gm`/`Gn` are already per-instruction, so the last tile in each dimension can be
smaller with no hardware change. Driver-only; do it when F2 shows it matters.

---

## G. Ordering

| # | change | why here |
|---|---|---|
| 1 | A1 tagged responses | enables everything; gain ~0 by itself |
| 2 | A2 streaming FILL | biggest single fetch win |
| 3 | A3 MAG overlap | local to one module |
| 4 | A4 + F1 multicast | must land together; the only one that scales |
| 5 | C1 streaming DRAIN | reuses A1/A2 |
| 6 | B1 acc chain | local, independent |
| 7 | E2 round double-buffer | driver-only, recovers idle |
| 8 | A10 + D3 banked L1 | last; smallest gain, largest RTL change |

Then re-measure and reconsider A5, A6, A7 against whatever is then binding.

---

## H. What this deliberately does not do

No larger tile, no deeper accumulator, no scratchpad, no cache. Output
granularity stays 32×32 per cluster, so padding on ragged shapes stays cheap,
and no URAM is consumed — leaving it for the FP16 vector unit and general core.

Every **TAKE** either removes waiting or removes duplicated work. None of them
buys throughput by making the machine's minimum useful problem bigger, which is
the property that makes an optimization look good on a benchmark and bad in
use.

---

## I. Status — the verdicts above against what was measured

The predictions below were made before any of it was built. Recording where
they held and where they did not is the point of keeping them.

**Shapes in this document are `M x K x N`**, as in the driver and in
[`perf.md`](perf.md). Strings recorded before that convention have been
converted; the numbers are the ones measured and only the labels moved.

**256³, 2 clusters, 300 MHz: 42.0 → 538.3 GFLOP/s pre-quantised, 87.6% of the
1024 MAC/cycle peak.** 12.8x, and eight clusters reach 1,856 GFLOP/s at 75.5%
on `512x256x1024`. The rates assume 300 MHz, and the modules that gate it now
measure above it out of context: `mx_cluster_cu` **325.6 MHz** (WNS +0.155 ns at
a 310 MHz target), `mx_quant` 400.6, `mag_mem_port` 330.0. Those are unplaced,
so they are upper bounds rather than sign-off. **Both GFLOP/s figures predate
the mesh layout change** ([`system.md`](system.md) §2.3) and are quoted here as
the outcome of the levers below, not as figures for the layout the RTL builds
now. Full table in [`perf.md`](perf.md) §0. Running record:
`.plans/optimization-log.md`; current profile and what is left:
`.plans/CONTEXT.md`.

| | predicted | outcome |
|---|---|---|
| A1 tagged responses | TAKE, first | **done** — ~0 alone, as predicted, and nothing else was expressible without it |
| A2 streaming descriptors | TAKE | **done** — but it *requires* A3, it does not merely benefit; see below |
| A3 MAG fetch/emit overlap | TAKE | **done**, with A2 |
| A4 multicast | TAKE, highest at scale | **built, NOT armed** — needs a rendezvous; measured faster **and wrong** |
| A5 `DATA_W` 512 | LATER | **withdrawn** — it was "the top lever" only under a metric that summed full-duplex channels; see §J1 |
| A6 multiple MAG instances | LATER, after A4 | **done, and not for the predicted reason** — several memory *ports* inside one MAG, one per mesh row, while A4 stayed withdrawn. It was never capacity: nothing was saturated. It was one server |
| A7 pre-quantised operands | LATER, strong | **done, and it was strong** — 217.4 → 303.9 |
| A8 cluster-local URAM | REJECT for now | **still reject** — the residency that mattered fit in LUTRAM |
| A9 L2 in MAG | REJECT | **still reject** — MAG is idle 88% at 2 clusters |
| A10 banked L1 + async FILL | TAKE, after A1–A4 | **done without A4** — 303.9 → 362.0 |
| B1 non-draining `acc` chain | TAKE | **superseded** — the tail was a tile-size artefact, and the drain was fixed by fusing it instead |
| C1 streaming DRAIN | TAKE | **done** — needed burst writes first, then fusing |
| C2 overlap drain with fill | LATER | **done** — as `OP_ADD_EMIT`, 362.0 → 391.1 |
| D1 larger tile | REJECT | **wrong: TAKEN, at 512 then 4096.** The rejection was right about depth already paid for and wrong about the ceiling being the shape |
| D2 deeper accumulator (URAM) | REJECT | **wrong: TAKEN.** `TILE_PRIM` was unreachable from a generated top, so this was argued and never measured; it is 5 URAM in for 5 BRAM out |
| D3 larger L1 | TAKE with A10 | **done** |
| E2 round double-buffering | TAKE, cheap | **not needed yet** — idle is 10.8%, one round at this shape |
| F1 pass ordering | TAKE, required by A4 | **already true**, discovered rather than built |
| F2 padding-aware tile | TAKE | **done** — and it is what keeps 22×23 from beating 16×32 on paper |

### Where the predictions were wrong, and why it matters

**D1 was rejected for the right reason and the wrong quantity.** "A bigger tile
hides per-access overhead rather than removing it, and pays in output
granularity" is correct — and it does not apply to depth the design is *already
buying*. The resident tile is 5 BRAM36 at any depth up to 512, so `TILES = 64`
was leaving 448 sub-tiles unused. That single change was 85.1 → 173.4. The same
argument then ran a second time one primitive up (D2): 5 URAM288 buys 4096 for
the 5 BRAM36 that bought 512, so the depth was free twice.

**"Intensity × bytes/cycle" is the right shape of argument and the wrong place
to stop.** It assumes each operand byte is fetched once, and the *schedule*
decides that. B was being re-read once per m-tile — a quarter of all traffic —
and no intensity figure shows it. **Count beats.**

**A2 requires A3; it does not merely benefit.** A streaming fetch holds the
read path for hundreds of cycles, and while one FSM served reads, writes and
the host window, that starved the other two into a deadlock. Lengthening one
transaction is a structural change to everything sharing the state.

**Two changes measured faster and were wrong** (A4 unarmed, an unbounded
pipelined drain). A third measured 499.6 GFLOP/s while computing nothing at
all. Bound the worst element against the MXFP7 model, not just p50 — and treat
a rate improvement with no matching component counter as unexplained.

---

## J. Shelved: considered, and withdrawn by measurement

Each of these was planned work with a real case behind it. The case is gone,
not the idea — they are recorded here rather than left on the task list so that
nobody picks one up expecting the win it used to promise, and so the condition
that would revive it is written down.

**Current numbers these verdicts rest on** (one MAG port per mesh row, corrected
resource metric — read and write are separate AXI channels, and the NoC's two
directions are separate wires, so these are independent budgets and the largest
one binds):

```
                    shapes M x K x N
2 CU 256³           flops 87.6%  mem_rd 30.3%  mem_wr 20.2%  noc_in 22.8%  noc_out 32.8%
8 CU 256x256x1024   flops 67.9%  mem_rd 25.5%  mem_wr 17.0%  noc_in 19.2%  noc_out 27.6%
8 CU 512x256x1024   flops 75.5%  mem_rd 23.6%  mem_wr 18.9%  noc_in 21.3%  noc_out 26.0%
```

> **These were measured before the mesh layout change** — clusters were still a
> (row, left-column) pair rather than a column of a band
> ([`system.md`](system.md) §2.3). The verdicts rest on the *ratios* between
> independent budgets, and nothing in the layout change touches how many beats or
> flits a pass moves, so the arguments below stand: on the new layout 8 CU
> measures `flops` 72.7% against 75.7% here, with `fetch` unmoved (7.1 → 7.2
> cycles/entry) and `wslot_full` *lower* (5.5% → 2.1%). The memory budgets did
> not become the binding ones, which is what these verdicts turn on. The
> absolute percentages remain figures for the previous topology —
> [`perf.md`](perf.md) §0.1.

### J1. `DATA_W` 256 → 512 (was A5, "the top lever")

**Withdrawn.** It was the top lever under a metric that reported `mem` as
`(rd + wr)/run` on one port and invited the reader to add it to `noc_in` and
`noc_out`. That read 91.5% "of data movement" and made a wider bus look like
the only way forward. Every one of those links is full duplex; the sum priced
capacity that never competes. Split apart and divided by the port count,
**no memory budget exceeds a third**. Doubling the beat width halves a number
that is already 25%.

*Revive when* `mem_rd` or `mem_wr` becomes the largest bar — realistically many
more clusters per port, or a change to the traffic shape such as an L2.
*Cost if attempted:* `mx_quant`'s beat port, `mag_mem_port`'s read and write
paths, the upload emit mux, `axi_ram`, the response decode. `Q_ARLEN`/`P_ARLEN`
already derive from `DATA_W`, so entry sizes follow correctly.

### J2. Multicast A to clusters sharing a fetch (was A4, "highest at scale")

**Withdrawn, mechanism retained.** `peers`/`npeer` in the FILL instruction, the
`e_dst` re-send loop in `mag_mem_port`'s emitter and the `lead` election in
`mx_cluster_cu` all exist and are deliberately disarmed: armed without a
rendezvous it measured 85.1 → 105.1 GFLOP/s **and** took the worst element from
1.0 to 2.2e+02, because a shared fetch delivered A entries to a cluster
executing its FILL B and `mx_cluster_cu` has one assembly register. There is an
assertion for that now.

It saves DRAM reads and quantiser passes, and neither is scarce. It also does
**not** relieve `noc_out`: the emitter re-sends the same latched words once per
destination, so the flit count is unchanged.

*Revive when* `mem_rd` is the largest bar. *Note* that per-row ports made the
rendezvous much easier than the original design assumed — the natural sharing
set is now the clusters whose memory traffic exits by **one port** (2 today),
which already share a server and a queue, rather than all eight across the mesh.
Under the current layout that set is **two adjacent columns of one band**: a
port attaches to the NoC rather than to a cluster, and columns nearer MAG exit
by their manager's row while the farther half exit by their accumulator's
([`mas/spec.md`](mas/spec.md) §2.4). Adjacent columns are still a set of two and
still share a server and a queue, so the rendezvous argument is unchanged; do
not read the new grouping as an improvement to it, since the layout as a whole
measures three points *worse* at 8 CU ([`perf.md`](perf.md) §0.1).

### J3. K-split reduction across clusters

**Not needed by any shape this machine targets.** Clusters are parallelised by
**column band** — each owns a disjoint slice of N — which needs no inter-cluster
communication at all, and every measured configuration has enough N to fill
them. K-split would instead give each cluster a slice of K and reduce partial
sums between them, using the accumulator's `peer_in`/`peer_out` ports (tied off
today).

*Revive when* a real workload has N too small to divide across the cluster
count — a tall-skinny GEMM, N < clusters × 128. Then column-band splitting
starves clusters and K-split is the only way to use them. It costs a reduction
tree across the mesh and an extra pass over C, so it is strictly worse whenever
N is adequate.

**REVIVED, for a reason that is not occupancy.** The shelving argument above is
about *filling clusters*, and it survives — but only just, and the correction is
worth making because an earlier draft of this paragraph overstated it. At
`Gm=16, Gn=32` the output block is `64 × 128`, so a `256 × 1024 × 256` GEMM
(M × K × N) has `4 × 2 = **8** output tiles`, not the 128 first claimed —
`Gm`/`Gn` count 4-element sub-tiles, not elements.

Eight tiles across eight clusters is *exactly* saturated: enough to fill them,
with no slack for imbalance and nothing left over. So the N-only split was still
the proximate defect, and a 2D grid still fixes it — but this shape was never
going to be comfortable, and a third grid dimension has more value here than
"purely a dispatch bug" suggested. The new argument is **range**.

A dot product over biased operands — every post-ReLU activation — grows
**linearly in K**, and FP16 saturates at 65,504, so `K = 2048` overflows once
`mu_a * mu_b > 32`. The accumulator is not the problem: `S1E7M16` reaches ~2^64
and holds the value intact. `mx_fpacc_to_fp16` destroys it on `EMIT`.

Splitting K does not change the final value, so it does not fix the range by
itself. What it changes is **where the last additions happen**: each cluster
emits its partial in the 24-bit accumulator float, and the vector core sums the
partials in E8M15, whose range is FP32's. One conversion, at the end, instead of
one per cluster.

So J3's revival condition now reads: *revive when the output grid is too small
to divide, **or** when K is deep enough that the result does not fit the output
format* — and the second half arrives much sooner than the first. The blocker
was never the split; it was not having anywhere to do the final reduction.

> **The first half of that condition has moved, and it is now much rarer.**
> Dispatch is on the 2D grid `m_tiles x n_tiles`, not on N alone, so what has to
> be too small is the whole output grid rather than one dimension of it: a
> tall-skinny GEMM at `Gm=16, Gn=32` supplies `M/64` tiles per column band
> before N is consulted at all. Only a shape that is small in **both** M and N
> and deep in K starves clusters now, and that is precisely the shape §11's
> range argument is about — which is why range, not occupancy, is the live
> reason.
[`compute/vector-core.md`](compute/vector-core.md) §11 has the arithmetic and
the cost (under 5% at S=4).

### J4. Loop opcodes and address offsets in the orchestrator ISA

**Folded into the dispatch work rather than pursued separately.** The driver
expands loops host-side into explicit passes, so the program grows with the
problem. Loop opcodes would shrink the dispatched program, which is attractive
because dispatch *is* now the multi-cluster limiter — but the measurement says
the cost is in **delivery order**, not program size:

```
8 CU, excess over the compute floor          shapes M x K x N
  256x256x1024    4 passes/cluster    7,731 cycles
  512x256x1024    8 passes/cluster   10,614 cycles     ~4,850 fixed + ~720/pass
  2 CU, 16 passes/cluster              7,148 cycles     ~140/pass
```

The per-pass cost is ~5× larger at eight clusters than at two, i.e. roughly
linear in cluster count. That is the dispatcher draining `prog_len` flits for
one destination before starting the next, not the number of instructions.
**Interleaving delivery is the fix; fewer instructions is a smaller, later
win.** See the dispatch task.

> **One of the two serialisations here is now gone, and it is the smaller one.**
> Dispatch used to leave the machine through a single east-edge link, because the
> agent had a NoC node of its own. It now shares MAG's memory ports and a flit
> leaves from the port on its destination's row, so *delivery* is spread across
> every attachment ([`mas/spec.md`](mas/spec.md) §2.5). What that does **not**
> touch is the cause measured above: one staging cursor drained per destination,
> which is upstream of the link and unaffected by widening it.
>
> **Measured, and it confirms that reading.** 2 CU is identical at 18,701 cycles;
> 8 CU `512x256x1024` went 43,382 → **43,315**, i.e. 67 cycles of a 10,614-cycle
> excess. Dispatch paths got shorter — an agent flit no longer crosses the mesh
> from the east edge — but **0.6% of the excess is not the excess**, and the
> per-pass cost above is still what it was. The cursor is the work.

---

### Not done, and required by the goal

- **Dispatch: the multi-cluster limiter.** ~4,850 cycles fixed plus ~720 per
  pass at eight clusters against ~140 at two. Serial delivery from one staging
  cursor. Parallel or round-robin dispatch is the change. The single *link* it
  used to leave through is no longer one — the agent shares MAG's memory ports
  and steers outbound by destination row (§J4) — but the cursor is untouched.
- **The head and tail bubbles**, worth ~10% even at two clusters: the first
  pass fills L1 with the array idle, and the last drain has no sweep left to
  hide it.
- **FP16 vector unit**, **general core**. The mesh now has room for them without
  rearranging anything: MAG attaches on the **west edge only**, so north, south
  and east are free.

### Deferred by decision, not by measurement

- **Mesh layout tuning.** The current arrangement — one cluster per column of a
  band, managers outside and accumulators inside — costs about three points of
  peak at 8 CU against the old geometry (72.7% vs 75.7%), paid in routing rather
  than memory service ([`perf.md`](perf.md) §0.1). It is **not on the list**:
  the vector unit and the general core attach to the north, south and east edges
  this layout frees, and they will change what an optimal placement is. Tuning
  before they exist would be tuning against a floorplan that is about to change.
- **Placed timing.** Everything above is cycles, and every Fmax quoted here is
  out-of-context and unplaced: `mx_cluster_cu` 325.6 MHz, `mag_mem_port` 330.0,
  `mx_quant` 400.6, routers 406–452. The multi-port `mag` wrapper and the
  changed NoC output port have still had no synthesis run of their own. What is
  not yet known is whether any of it survives placement in a full partition.
