# The machine running end to end

Three benches, in order of how much machine they contain. The first two are the
NoC-level ones described below; the third,
[`tests/mas/mag_driver_tb.v`](#6-the-whole-partition--mag_driver_tb), is a real
partition driven by the real driver and is the one to read if you only read one.

This document starts at the smallest complete machine — one router, three
nodes — running an actual matmul with nothing stubbed but DRAM.

```
                        [ matmul CU (1,0) ]
                                 |  north
     west --  +----------------------------------+  -- east -- [ fake mem (2,1) ]
              |          router (1,1)            |
              +----------------------------------+
                                 |  local
                        [ orchestrator (1,1) ]
                                 |  AXI4
                              (host)
```

Two benches at this level, run by `tests/run_system_sim.ps1`, each against both
the behavioural model and the real DSP48E2:

| Bench | Shape | Covers |
|---|---|---|
| `mx_system_tb` | `C[4,4] = A[4,256]·B[256,4]` | **depth** — one sub-tile accumulated across 8 blocks of K=32 |
| `mx_system32_tb` | `C[32,32] = A[32,32]·B[32,32]` | **breadth** — 64 sub-tiles, 128 instructions, 1,024 elements |

Both drive `mx_matmul_cu`, the single-node baseline — **deliberately retained**:
it still exists, is still synthesised, and is the one-port reference the
two-port cluster is measured against. The mesh, the memory gateway, the
quantiser and the driver all arrive together in §6.

Run any of them individually with `python scripts/py/xsim.py <name>`; the tiered
`scripts/py/check.py` is what to use in the edit loop
([`simulation.md`](simulation.md) §0).

---

## 1. The path being exercised

```
   host stages a program over AXI
     -> orchestrator dispatches CU_INST across the mesh
     -> CU issues MEM_RD_REQ naming ITSELF as src
     -> memory replies MEM_RD_RESP straight to the CU
     -> cluster computes 4x32x4 per block, ACU accumulates
     -> CU issues MEM_WR_REQ with the FP16 result
     -> CU emits CU_SIGNAL, orchestrator mirrors it into NODE_STATUS
     -> host polls over AXI and reads the result back
```

The operand fetch is the part worth noticing. The CU names **itself** as `src`
in `MEM_RD_REQ`, so the response is delivered directly to it and never passes
through the orchestrator. That is what lets the orchestrator have no AXI master
at all ([`noc/spec.md`](noc/spec.md) §10.3) — it forwards instructions and never
touches operand data.

## 2. The problems

### 2.1 Depth — `mx_system_tb`

`C[4,4] = A[4,256] × B[256,4]`, as 8 blocks of K=32, with **per-block per-row
and per-column E5M3 scales**. So this exercises microscaling, not merely a
matmul: 8 different scale pairs are applied and accumulated into one resident
sub-tile.

Program: 8 `BLOCK` instructions and one `EMIT`.

```
   blocks=8  emits=1   memory reads=16  writes=1
   NODE_STATUS: code=01 (BATCH_COMPLETE)  signals=9
   35 checks, 0 errors
```

16 reads is exactly right: 8 blocks × (4 flits of A + 4 of B) delivered as two
burst requests each.

### 2.2 Breadth — `mx_system32_tb`

`C[32,32] = A[32,32] × B[32,32]`. K=32 is exactly one quantisation block, so
every one of the 64 output sub-tiles is a single `BLOCK` followed by an `EMIT` —
**128 instructions in one staged program**, producing 1,024 output elements.

This is the first bench to reuse the resident tile file: slots cycle 0..15 and
wrap four times, so every slot is loaded, emitted, and then loaded again. That
is the path where a stale accumulator bank would show up as a doubled result.

```
   blocks=64  emits=64   memory reads=128  writes=64
   NODE_STATUS: code=01 (BATCH_COMPLETE)  signals=128
   2,051 checks, 0 errors        (identical under MODEL=0 and MODEL=1)
```

The top-left corner of the result, hardware against FP64:

```
   hardware (FP16 read back out of the mesh)
        12.40    -73.38    174.50    -62.22    -28.02     28.09
       219.75   -200.25   -492.00   -159.25    226.50   -197.00
       104.38   -526.00  -1338.00    103.00     94.00   -106.69
      -110.00    246.00   -151.00   -180.88     67.19   -446.75

   FP64 ground truth
        12.40    -73.34    174.53    -62.23    -28.02     28.09
       219.78   -200.25   -491.88   -159.25    226.44   -197.03
       104.38   -525.75  -1338.00    103.00     94.00   -106.69
      -109.97    246.00   -151.00   -180.88     67.19   -446.84
```

### 2.3 The mesh and the two-port cluster — now covered in §6

There is no NoC-level mesh bench any more. `mx_mesh2x2_tb` drove
`mx_cluster_cu` against the `noc_fake_mem` stub and was **deleted**, not
disabled — see the note at the end of this section. Everything it covered is
covered against the *real* MAG by `tests/mas/mag_system_tb.v` and
`tests/mas/mag_driver_tb.v`. The diagram below is **`mag_driver_tb` at
`NCL=2`** — a 2×2 grid with MAG on the west edge:

```
          x=0     x=1    x=2    x=3
   y=0      .      .      .      .
   y=1   MAG p0 -- [R] -- [R] --  .      [R] = router, grid 1..2
                    |      |
                   mgr0   mgr1
   y=2   MAG p1 -- [R] -- [R] --  .
                    |      |
                   acu0   acu1
   y=3      .      .      .      .
```

**A cluster is one COLUMN of a band, so its manager and its accumulator sit on
ADJACENT routers.** A band is two rows; managers take the band's outer row and
accumulators the inner one. At `NCL = 8` there are two bands, and the second is
**mirrored** against the bottom, so the accumulators meet in the middle and every
manager is on an outer row — two dataflow rings back to back:

```
   NCL=8   4 cols x 4 rows        NCL=4   4 cols x 2 rows
   MAG(0,1) | mgr0 mgr1 mgr2 mgr3      MAG(0,1) | mgr0 mgr1 mgr2 mgr3
   MAG(0,2) | acu0 acu1 acu2 acu3      MAG(0,2) | acu0 acu1 acu2 acu3
   MAG(0,3) | acu4 acu5 acu6 acu7
   MAG(0,4) | mgr4 mgr5 mgr6 mgr7

   NCL=2   2 cols x 2 rows
   MAG(0,1) | mgr0 mgr1
   MAG(0,2) | acu0 acu1
```

**A band need not be full.** 3, 5, 6 and 7 clusters simply leave columns empty.
Those counts are **supported**, merely not optimal — whether a shape uses the
machine well is the driver's business to report, not the mesh generator's to
forbid.

> **Measured, and it is not free.** 2 CU and 8 CU both pass end to end on this
> layout, with 8 CU's error profile unchanged (p50 1.71e-04, worst 2.43e+00, 49
> of 524,288 over 10%). It costs about **three points of peak at 8 CU** —
> `flops` 72.7% against 75.7% on the old geometry. It is not the memory system:
> `fetch` moved 7.1 → 7.2 cycles/entry and `wslot_full` *fell* 5.5% → 2.1%. The
> cost is routing, which is what you would expect once a cluster's memory
> traffic leaves by one of two rows rather than the one row both its endpoints
> shared. The layout buys physical locality and simpler program planning and
> pays a few percent for them; tuning it is **deferred until the vector and
> general cores exist**, since they attach to the edges this frees up.
> [`perf.md`](perf.md) §0.1.

**Both west attachments are MAG ports, and there is no third node.** They are
*MAG* ports rather than *memory* ports: each carries operand traffic **and** the
agent's control traffic on the same wires. The agent used to sit on a west or
east edge as a node of its own; it now has none, and **answers at port 0's
coordinate, `(0,1)`** — a `CU_SIGNAL` sent there reaches the agent and a
`MEM_RD_REQ` sent there reaches the memory engine, because the port demuxes on
flit type ([`mas/spec.md`](mas/spec.md) §2.5). That is why the diagram has no
`ORC`.

`mag_driver_tb` is drawn above; it gives itself one MAG port per mesh row, so
`NCL = 2` is the two ports shown and `NCL = 8` is four. `mag_system_tb` runs a
single port at `(0,1)` and ties the row-2 west edge off entirely — both clusters
reach that one port, which is what makes it the sharper test of the demux.

> **`mag_system_tb` has not been moved to this layout.** It is a hand-built,
> fixed-shape regression bench and it still places each cluster's manager and
> accumulator side by side in one row — `MGR(1,1)/ACU(2,1)` and
> `MGR(1,2)/ACU(2,2)`. Nothing it tests depends on the placement (it is a demux
> and correctness test, not a topology one), but **the diagram above is
> `mag_driver_tb`'s mesh, not its.** `mag_driver_tb` is the bench that builds
> what the design specifies.

**A port attaches to the NoC, not to a cluster.** Every cluster can reach every
port through the mesh, so which one its memory traffic leaves by is a routing
choice: columns nearer MAG exit by their manager's row, the farther half by
their accumulator's. That keeps both of a band's ports carrying, and it suits
the directions — the links are full duplex, and a manager mostly *receives* fill
responses while an accumulator mostly *sends* results, so the two rows load
opposite directions rather than competing for one.

The structural facts that arrangement exists to exercise are unchanged:

**Each cluster spans two routers** — manager on one local port, accumulator on
the local port of the router directly beneath it. Sharing one router would put
operand fetch and result write-back through the same buffers and arbiter, which
is exactly the contention the two-port split exists to remove. Adjacency is the
other half: a cluster's two endpoints never straddle another cluster's router,
which is what the old left-half/right-half arrangement did.

**MAG's ports are *off-grid* coordinates on west edge ports.** That works because
the router routes to the **clamped** destination first and only then uses the
unclamped coordinate to pick an edge port, so a packet for (0,2) travels to
router (1,2) and exits west. It is also what lets MAG put one MAG port per
mesh row ([`mas/spec.md`](mas/spec.md) §2.4), and what lets a cluster on any row
reach any of them.

**The north, south and east edges are unattached**, and deliberately so. Now that
MAG is on the west edge only, they are what a vector unit or a general core would
attach to.

**Clusters are split by output column**, and each sweeps the whole of K itself.
No peer reduction is needed — K inside a cluster is free, whereas splitting K
across clusters costs a reduction that dominates the compute
([`compute/tensor-isa.md`](compute/tensor-isa.md)).

**K spans more than one quantisation block**, which is the point: with a single
block every accumulator command is a `LOAD` and the cross-block accumulate path
is never exercised. `mag_system_tb` uses `C[16,16] = A[16,64] × B[64,16]` — two
blocks, the minimum that makes a *fused* drain meaningful, since a fused sweep's
last K block must not also be its first.

> **Why `mx_mesh2x2_tb` was deleted rather than repaired.** It had fallen a full
> generation behind on two interfaces at once. It packed the pre-widening
> instruction layout with `n` as 8 bits, so under the current decode every
> `GEMM` field landed one byte off — `gm` got `gn`, `gn` got `nk`, `nk` got the
> anchor. And it drove `noc_fake_mem`, which wrote `3'b000` into flit bits
> `[258:256]` where the response **word index** belongs, so `mx_cluster_cu`'s
> `rword` was always 0, the `rword == 2'd3` commit never fired, and no L1 entry
> was ever written — the CU sat in `S_FILL` forever. Repairing it meant
> rewriting it into the two MAG benches, which already existed and already
> passed. **A bench nobody maintains that grades memory it never waited for is
> worse than no bench:** this one reported wrong *answers* for what was actually
> a hang.

## 3. Precision

Three quantities, and the distinction between them is the point:

| | what it is |
|---|---|
| **EXACT MXFP7** | the matmul as a CPU would compute it, integer arithmetic, block scales applied by shifting |
| **FP64** | the same sum in `real` |
| **HARDWARE** | the FP16 written back to memory |

```
   4x256x4           worst  3.97e-4                0.41 FP16 ULP

   32x32x32          worst  4.86e-4 at C[0][18]    0.50 FP16 ULP
                     mean   1.41e-4 over 1,024     0.14 FP16 ULP

   32x128x64, 2 CU   worst  2.29e-3                2.3  FP16 ULP     <- see below
                     mean   1.71e-4 over 2,048     0.18 FP16 ULP
```

**Half an FP16 ULP at worst for a single K block**, across 1,024 elements and
the whole machine. One FP16 ULP is 9.77e-4, so the accumulator is not the
limiting factor there — the output format is.

> **The `32x128x64, 2 CU` row came from `mx_mesh2x2_tb` and cannot be
> re-measured**: that bench has been deleted (§2.3). It is kept because the
> argument below is about the arithmetic rather than about the bench, and
> `mag_driver_tb` exercises multi-chunk K on the same datapath. Treat the two
> figures as historical, not as something to reproduce.

The four-block case is worth reading carefully: the **mean is unchanged** at
0.18 ULP, but the worst case rises to 3 ULP. That is not accumulator drift —
it is **cancellation**. Four blocks with independent scale pairs are summed, and
where the final value is small relative to the intermediate terms, the relative
error of the result is amplified by the ratio between them. It is a property of
the problem, not of the hardware, and it is why the mean matters more than the
maximum for judging the accumulator.

Both benches assert that EXACT and FP64 agree with each other *before* comparing
either against hardware. Without that, a drifting model would be
indistinguishable from a hardware error.

What this error does **not** include: quantisation. Both benches preload
operands into the fake memory already in int7 + E5M3 form, so the numbers above
are purely what the FP22 accumulator and the FP16 emission cost. `mag_driver_tb`
(§6) does include it — it uploads FP16 and lets `mx_quant.v` convert — and there
the quantisation error dominates everything measured here.

## 4. What these two benches do not cover

```
   quantiser            operands are preloaded already-quantised.  §6 covers it
   peer accumulation    one cluster, so ACC_SEND / ACC_ADD_PEER are unused
   multiple CUs         one compute node on the router.  §6 covers 2 to 8
   the two-port cluster these drive mx_matmul_cu, the 1-port baseline.  §6
   backpressure         no congestion; the mesh is never contended
   MEM_WR_ACK           the CU retires on send rather than waiting for the ack
```

The fake memory (`tests/noc/noc_fake_mem.v`) implements the wire protocol of
[`noc/spec.md`](noc/spec.md) §5.1/§5.2 and nothing else — no cache, no reordering, no quantiser. That is
deliberate: a system test should fail because the system is wrong, not because
the stub grew its own bugs.

It does honour the **link contract in full**: it holds `valid` and `data` until a
cycle in which `noc_out_busy` is low, and accepts only on `valid && !busy`. That
had to be fixed — it used to drop `noc_out_valid` every cycle and re-assert it
from the *previous* cycle's `busy`, which destroys any flit that meets a
receiver raising `busy` in between. A stub that gets this wrong loses flits the
design did not lose, which is the one failure a stub must not invent. See
[`noc/spec.md`](noc/spec.md) §2.1.

## 5. Bugs these tests found

**Two continuous drivers on a mesh link.** The since-deleted `mx_mesh2x2_tb`
(§2.3) tied off "all unused north ports" in a generate loop, which also drove
`n_in[0][1]` — the (0,0)→(0,1) vertical link — alongside a cluster's accumulator
port. The link resolved to X.
Cluster 1 never noticed, because its route to memory is a single westward hop;
cluster 0 routes **west then south straight through it**, so its memory requests
vanished and it hung with no error anywhere. Edge ports are now tied off one by
one. **No single-cluster test could have found this** — it needs two nodes whose
routes differ.

**`gemm_busy` reported done when the last tile was *issued*.** The cascade is
~19 cycles deep, so results were still in flight; the CU signalled completion,
`DRAIN` seized the accumulator control mux, and the tail sub-tiles came back as
zeros — 11 of 64 per cluster. Now gated on the ACU command FIFO being empty,
which is exact and needs no latency constant.

**A synchronous L1 read needs two cycles of control delay, not one.** Counters
assign the address at T, the RAM sees it at T+1, data is valid at T+2. Consuming
it at T+1 shifted every result by one sub-tile — structured and silent, and it
looked like an addressing bug rather than a timing one. This is exactly the
latency that memory *inference* hides.

**The accumulator's reuse contract caught a real violation the moment it
existed.** Making the tile single-bank turned "commands to the same address must
be ≥5 cycles apart" from a structural guarantee into a requirement on the
caller, so it is checked in simulation. It immediately flagged `mx_matmul_cu`,
which emits a tile 2–3 cycles after the last accumulate into it. That CU now
waits out the distance; the two-port cluster never violates it, because sweeping
K outermost puts `Gm·Gn` cycles between reuses by construction.

> Worth noting what *kind* of bug that is. Nothing had failed — the banked
> accumulator served the pattern correctly. The check exists so that removing
> the banks could not silently break a caller, and it earned its keep on the
> first run.

**The staging window was one 4 KB page, not `STAGE_FLITS`.** The orchestrator
decoded a stage write as `waddr[15:12] == 4'h2`, i.e. 0x2000–0x2FFF = 512
words = **102.4 flits**, while `STAGE_FLITS` defaults to 128 and the RAM is
sized for it. Everything past flit 102 was decoded as a register write instead
and silently discarded.

The failure looked nothing like an address bug: the program simply stopped, at
exactly 51 of 64 sub-tiles, with `run=1 left=10 credit=0` — which reads like a
credit deadlock. Re-running with 4 credits instead of 16 stopped at 51 again,
which is what ruled flow control out: a rate problem moves, a decode boundary
does not. 51 pairs is 102 instructions, and instruction 102 is the first whose
five staged words straddle 0x3000.

The window is now derived from `STAGE_WORDS`. **Nothing shorter than 103 flits
could have found this**, and the previous longest bench was 9.

**Unsized literals in the instruction encoding.** `(WA_BASE + b*4) * 32` written
straight into a concatenation contributes 32 bits, not the 34 the address field
is, so the payload came out 4 bits short and every field below it shifted. The
CU then read nonsense and executed nothing. Symptom: `blocks=0`, no memory
traffic, no error anywhere. Same trap as [`simulation.md`](simulation.md) §3. The 32×32×32
bench hits it again with the 4-bit tile field, which is why `tile4` is an
explicitly sized reg.

**A fixed-cycle wait for `emit_valid`.** The CU counted 10 cycles after issuing
`EMIT` rather than waiting on the flag. Deepening the accumulator pipeline made
10 too few, and the CU wrote a zero result while every unit test still passed.
See [`compute/accumulator.md`](compute/accumulator.md) §5.

**AXI write handshake.** Waiting for `awready && wready` together deadlocks —
the orchestrator's write FSM takes AW first, then W, so they are never both
high.

---

## 6. The whole partition — `mag_driver_tb`

Everything above stubs memory. This one does not:

```
   fake AXI master (the real driver's transport)
        -> main_orch, running a real control program
        -> axi_xbar2 -> MAG  (quantising upload path, and MEM_PORTS MAG ports
                              -- one per mesh row on the west edge, each with
                              its own queues, read engine, mx_quant and write
                              slots. The dispatch agent SHARES those ports and
                              has no mesh attachment of its own, so each port
                              carries operand AND control traffic, demuxed by
                              flit type -- mas/spec.md s2.5)
        -> generated mesh, up to eight 2-port clusters (`-d NCL=`), one per
           COLUMN of a band: managers on the outer rows, accumulators inside
        MAG's AXI masters -> axi_ram, 262,144 words (8 MB), one channel per port
```

Three things are different in kind, not just in scale.

**Operands are FP16 in memory.** The driver uploads what software actually has;
`mx_quant.v` converts to int7 + E5M3 on the way out of MAG, per 32-element block,
choosing each block's scale. Nothing in the driver produces a quantised value —
`driver/src/kohakutpu/mxfp7.py` exists only to *predict* what the hardware will
do, so a mismatch is a real disagreement and not two copies of one bug.

**The shape is arbitrary.** `driver/src/kohakutpu/kernel.py` tiles any
`(M, N, K)` onto the one tile the hardware holds and streams the passes as
rounds. `TILES = 512`, `GA = 128` and `GB = 256` here, so `choose_tile` returns
`gm=16, gn=32, nk=4` — a **64×128×128** pass — and a 256×256×256 problem is 8
passes, one per output tile, each sweeping the whole of K. Rounds are cut
against the staging window, the command RAM and the dispatch credit, whichever
binds first. See [`isa/kernel.md`](isa/kernel.md).

**Cycles are attributed, not just counted.** The bench buckets every cycle per CU
into fill / gemm / drain / idle, because a `GEMM` counter says the work happened
and not whether the machine was computing or waiting.

| shape | run cycles | fill | gemm | drain | idle | MAC/cyc | GFLOP/s |
|---|---|---|---|---|---|---|---|
| 64×64×128 | 8,213 | 56.7% | 17.3% | 12.1% | 13.8% | 63.8 | 38.3 |
| 128×128×128 | 32,361 | 61.3% | 17.1% | — | — | 64.8 | 38.9 |
| 256×256×256 | 239,786 | 71.9% | 13.4% | 6.7% | 7.9% | 70.0 | 42.0 |

Against a two-cluster peak of 1,024 MAC/cycle (614 GFLOP/s at 300 MHz) that is
6–7%, and the fill column says why. It is structural — the tile is small enough
that a pass does four tile-ops per L1 entry it loads — and
[`arch-design.md`](arch-design.md) §4.2 works the arithmetic through.

> **Baseline, superseded.** That diagnosis was acted on: the 256-cube ran
> in **18,701 cycles at 538.3 GFLOP/s (87.6% of peak)** and the bench itself has
> grown — the mesh is generated rather than written out, sized by `-d NCL=` up
> to eight clusters, with **one MAG port per mesh row**
> ([`mas/spec.md`](mas/spec.md) §2.4), and operands arrive over AXI through
> MAG's memory window rather than being loaded into the RAM behind its back.
> Current figures: [`perf.md`](perf.md) §0.
>
> **Every cycle count in this section predates the mesh layout change** (§2.3):
> they were measured when a cluster was a (row, left-column) pair rather than a
> column of a band. Both 2 CU and 8 CU pass end to end on the current layout, at
> about three points less peak at 8 CU ([`perf.md`](perf.md) §0.1). Nothing here
> should be quoted as a figure for the layout the RTL builds now.

**C write amplification is exactly 1.00x**, which is the whole reason K is the
innermost loop: the output tile stays resident across the entire K sweep and is
written once. That number is worth checking rather than assuming, because the
failure mode of getting the loop order wrong is not a wrong answer — it is a
correct answer that costs `K/Kc` times the write traffic, and nothing else in
the machine would report it.

> **The read bandwidth is demand, not achieved.** `axi_ram` answers immediately.
> Real DRAM latency does not reduce operand traffic, it lengthens the wait, so
> the fill share on hardware can only be worse than this table.
