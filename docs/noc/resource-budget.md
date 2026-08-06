# Resource budget — what the NoC costs, and what is left for compute

All numbers measured, not estimated: `tests\run_synth_check.ps1`, out-of-context
synthesis on `xcvu13p-fhgb2104-2L-e` (1.728M LUT, 3.456M FF, 2688 BRAM, 1280 URAM,
12288 DSP). Out-of-context means utilisation is reliable and timing is optimistic.

The instrument is a **zero-compute** endpoint (`src/kohakunoc/noc_cu_null.v`) in a
complete 2×2 tile (`src/synth_top/noc_cluster_2x2.v`) — 4 routers and all 12
endpoints they can carry, no dangling ports. The CU framework is part of the NoC,
not part of the datapath, so measuring it with no arithmetic attached is what
separates "network tax" from "compute".

Two things stop synthesis from flattering the result: every bit of both flits is
folded into an output, so no part of a 288-bit path is dead; and traffic
originates from external inputs, so the mesh cannot be proven idle and
constant-folded.

---

## 1. The tax

| | LUT | FF | BRAM |
|---|---|---|---|
| Router | 4,050 | 5,960 | 0 |
| Endpoint (LUTRAM FIFOs) | 759 | 1,585 | 0 |
| Endpoint (instruction FIFO in BRAM) | 591 | 1,009 | 4 |
| **2×2 tile, 4 routers + 12 endpoints** | **20,809** | **36,143** | **0** |

The tile closes **400.8 MHz** against a 300 MHz target and uses **zero BRAM and
zero URAM**, so the entire memory budget stays available for caches.

Where an endpoint's 759 LUT goes — **96% is the three flit queues**:

| | LUT | FF |
|---|---|---|
| `u_inst` instruction FIFO (32×288) | 262 | 607 |
| `u_recv` receive FIFO (16×288) | 289 | 603 |
| `u_sig` completion FIFO (16×56) | 174 | 127 |
| framework logic | 34 | 173 |

Moving the instruction FIFO to BRAM saves 168 LUT and 576 FF per endpoint for
4 BRAM. **Depth 512 costs the same 4 BRAM as depth 32** — 288 bits × 512 fills
four RAMB36 exactly — so if the FIFO goes to BRAM there is no reason to keep it
shallow.

> `src/synth_top/noc_tile_1r.v` measures the same parts at a 1:5 ratio and reports
> a much cheaper endpoint (460 LUT). It is **not** used above: nothing in that tile
> ever emits `CU_INST`, so the instruction FIFO is partly optimised away. The 2×2
> figure is corroborated by a standalone `noc_cu_null` synthesis at 760 LUT.

---

## 2. What compute costs, for comparison

| | LUT | FF | DSP | Fmax |
|---|---|---|---|---|
| `tensorcore` | 15,649 | 5,264 | 32 | **285 MHz** |
| `FP16ALUArray` | 11,203 | 2,306 | 32 | 469 MHz |

**One tensor core is 20× one endpoint's framework cost.** The NoC framework is not
what makes a compute unit expensive.

`tensorcore` **misses 300 MHz** (WNS −0.170 ns). The network closes 400 MHz and
the arithmetic does not, so the compute sets the clock — either pipeline it or run
those units at the ÷2 rate the clocking scheme already allows (spec §10.6).

---

## 3. Feasible scale

Ratio 10 tensor : 4 vector : 1 general, plus 8 MAS and 4 control endpoints. Mesh
sized to the smallest `n` with `n² + 4n ≥ endpoints`. General-purpose unit assumed
at 5,000 LUT — no such unit exists yet, so that column is a placeholder.

| T / V / G | mesh | endpoints | LUT | of device | DSP | of device |
|---|---|---|---|---|---|---|
| 160 / 64 / 16 | 14×14 | 252 | 4,285,900 | **248%** | 7,168 | 58% |
| 100 / 40 / 10 | 11×11 | 162 | 2,676,028 | 155% | 4,480 | 36% |
| 60 / 24 / 6 | 9×9 | 102 | 1,643,280 | 95% | 2,688 | 22% |
| **40 / 16 / 4** | **7×7** | **72** | **1,078,306** | **62%** | 1,792 | 15% |
| 30 / 12 / 3 | 6×6 | 57 | 807,969 | 47% | 1,344 | 11% |

At 40/16/4 the split is: tensor cores 57% of the design, vector arrays 16%,
routers 20%, endpoint framework 4.7%.

**DSP is never the constraint** (15–58%). LUT is.

> **None of this includes L1, L2, L3 or the MAS.** Treat 40/16/4 at 62% as an
> optimistic bound, not a plan — caches have to come out of the remaining 38%.

---

## 4. Reading this for the granularity decision

Two costs scale differently, and conflating them gives the wrong answer:

- **Endpoint framework: 759 LUT.** Small — 5% of a tensor core. Attaching one more
  unit to the mesh is nearly free on its own.
- **Routers: 4,050 LUT each**, and router count follows mesh size, which follows
  endpoint count. At 14×14 that is 196 routers for 252 endpoints — **3,159 LUT of
  router per endpoint**, four times the framework cost.

So the total network cost of one more endpoint is roughly **3,900 LUT**, a quarter
of a tensor core. Clustering three units behind one framework and one L1 saves
about 7,800 LUT per cluster — half a tensor core — and shares the L1 on top.

That is the argument for coarse granularity, and note it is *not* the argument
about the framework being expensive. It is about router count.

Specialisation is a separate axis: an endpoint can be all-tensor, all-vector or
mixed at any granularity. Choosing "one unit does one thing" does not require
choosing "many small units".
