---
title: Results
summary: Every measured number for KohakuTPU on xcvu13p-fhgb2104-2L-e — Fmax by block, resources, accuracy, throughput, and what closed and what did not.
tags:
  - kohakutpu
  - results
  - measurements
---

# Results

> **Kind: none — this page reports measurements of parts labelled elsewhere.** No
> row here is a design surface, so none is Fixed, addon, convention or yours in
> its own right. What governs the page instead is this directory's provenance
> rule: every figure names its part, tool, mode and producing script, and no
> frequency here is a closed-timing figure.

Every measured figure for KohakuTPU lives here, so there is one place to check
whether a number is current. Design pages cite this file rather than restating
it, and other sections of the tree should link here rather than quoting.

**The device is `xcvu13p-fhgb2104-2L-e` for every figure on this page unless a
row says otherwise.** These numbers describe one accelerator on one part. They
are evidence the framework closes on real silicon; they are not specifications of
it ([projects/README.md](../README.md)).

---

## 1. How to read these numbers

**Every figure's provenance, unless a row states otherwise:**

| | |
|---|---|
| part | `xcvu13p-fhgb2104-2L-e` |
| tool | Vivado 2024.2 |
| mode | **out-of-context**, **synthesis only** — no opt, no place, no route |
| produced by | `scripts/tcl/ooc_*.tcl`, one script per top module; each script's header states what its run measures |

Three kinds of figure appear on this page and they are not interchangeable.
**MEASURED** is read off a netlist, a report or a bench. **ARITHMETIC** is
derived from measured figures, with the derivation shown. **PROJECTED** is
neither, and is labelled at the point of use. Anything whose conditions cannot
be stated is marked `[unverified]` rather than printed as fact.

**No frequency on this page is a closed-timing result.** They are
out-of-context synthesis slack, and synthesis slack is optimistic: `m62_c1` lost
0.740 ns going from synthesis to routing, so a figure here can be a full speed
grade above what the same logic does once placed. The framework's statement of
what an out-of-context number means is
[workflow/measure.md](../../workflow/measure.md); this page obeys it.

That has a precise consequence in each direction:

| | |
|---|---|
| **utilisation** | reliable — LUT, FF, BRAM, URAM and DSP counts are what the netlist contains |
| **frequency** | an **upper bound**. It answers "is the logic deep enough to fail?", never "will it place" |

Within that, a figure bounds in one of two directions and every row below says
which:

- **A run that met its target is a lower bound** on that block's frequency: it
  cleared the constraint with the reported slack and the tool stopped trying.
- **A run that missed is a ceiling**: that is what the logic would do and it was
  not enough.

A device past roughly 70% full erodes out-of-context slack, and **none of the
cluster-count figures in §5 have been through place-and-route on a populated
die**. Where a page multiplies one cluster by 32 or 45, that is arithmetic.

Three conventions:

- **Shapes are `M x K x N`.** Some figures were recorded when they were written
  `M x N x K`; those have been converted, so the numbers are the ones measured and
  only the labels moved.
- **GFLOP/s is `2 · MACs / cycles · 300 MHz`.** The cycle counts are measured; the
  rates are that arithmetic on top of them.
- **One MAC is 2 FLOP**, and the unit is FLOPS rather than IOPS because MXFP7 is a
  floating-point format ([number-format.md](number-format.md) §6).

---

## 2. The matmul path

Out-of-context. The cluster and accumulator rows come from a **310 MHz target
(3.2258 ns)** re-measurement; rows marked ‡ are older **300 MHz target
(3.3333 ns)** runs, not re-measured since.

| block | LUT | FF | BRAM36 | DSP | Fmax | bound |
|---|---|---|---|---|---|---|
| `mx_mac` (one DSP48E2) | **0** | **0** | 0 | 1 | — | — |
| `mx_tcu` (4x8x4) ‡ | 336 | 790 | 0 | 64 | 1072.6 | lower |
| `mx_cluster` (core + exact accumulator) ‡ | 4,751 | 4,789 | 0 | 256 | 353.6 | lower |
| `mx_acu_fp` (FP22, MW=14, DEPTH=16, block RAM) | 9,901 | 5,585 | 5 | 48 | **343.4** | lower |
| `mx_cluster_cu` (one-port cluster, current) | 15,306 | 17,754 | 5 | **304** | **346.6** | lower |
| `mx_matmul_cu` (single-port baseline) ‡ | 12,973 | 11,486 | 5 | 256 | 306.4 | lower |

`mx_matmul_cu` is the superseded single-port design, kept as a measured baseline.
It cannot be fed at rate and it is not on the current path, but it is what two of
the older system benches drive.

> **Two cluster figures are in circulation and both are real.** Before the
> normalising shift moved into DSPs (§2.2) the cluster measured **17,629 LUT /
> 17,782 FF / 272 DSP at 325.6 MHz**; after, **15,306 / 17,754 / 304 at 346.6**.
> A separate standalone run of the earlier configuration reported 17,521 LUT and
> 17,612 FF at 325.6 MHz with WNS +0.155 ns. The 272-DSP rows are the older
> configuration and the 304-DSP rows are current; §5.1 is why the difference
> matters more than it looks.

**In the shape that ships** — a 512-deep resident tile, 512 L1 entries per side in
block RAM — the same cluster measures **16,390 LUT, 18,404 FF, 35 BRAM36, 304 DSP
at 344.3 MHz**. Fewer LUTs than the bench default despite four times the L1,
because block RAM is where 928 bits by 512 belongs: 13 RAMB36 per port, 26 for the
two, 5 for the resident tile and 4 for the receive queue.

Three things worth reading off this table:

**Every `mx_mac` is 0 LUT, 0 FF, 1 DSP.** The multiply *and* the entire K=32
reduction happen inside the DSPs — the cascade for K=8, the `C` port across
compute units. That was the design's central claim and it holds exactly.

**The accumulator is the block the whole cluster closes on.** After all the timing
work the cluster closes within a few MHz of what the accumulator measures standing
alone, so everything else in the cluster is effectively free of the frequency
question.

**The resident tile is 5 BRAM36 at any depth up to 512**, because a 352-bit port
needs `ceil(352/72) = 5` primitives and depth is then free.

### 2.1 Where a cluster's LUTs are

| component | LUT | of which SRL | FF | DSP |
|---|---|---|---|---|
| `mx_mac` x256 | **0** | 0 | **0** | **256** |
| TCU 0 | 336 | 224 | 784 | 64 |
| TCU 1 | 448 | 280 | 728 | 64 |
| TCU 2 / TCU 3 | 476 each | 280 | 728 | 64 |
| operand delay (top) | 450 | 394 | 581 | 0 |
| accumulator (exact variant) | 2,565 | 0 | 1,240 | 0 |

**A tensor CU's LUTs are almost all operand skew** — 224 of 336 in TCU 0 are
SRLs, and the routing logic is the rest. **The accumulator is 54% of that
cluster** and holds the critical path; it is the only part that is real fabric
arithmetic.

### 2.2 The normalising shift as a DSP multiply

Measured as a matched before/after pair on the same tree.

| | Fmax | LUT | FF | DSP |
|---|---|---|---|---|
| `mx_acu_fp`, barrel shifter in fabric | 327.7 | 10,616 | 5,928 | 16 |
| `mx_acu_fp`, shift as a DSP multiply | **343.4** | **9,901** | **5,585** | 48 |
| `mx_cluster_cu`, in fabric | 325.6 | 17,629 | 17,782 | 272 |
| `mx_cluster_cu`, as a DSP multiply | **346.6** | **15,306** | **17,754** | 304 |

−6.7% LUT and +15.7 MHz standing alone; **−13% and +21.0 MHz inside the cluster**
— a larger win in context than alone, because the cluster was tight enough that
the tool had been replicating logic to hold the frequency.

The isolated primitive comparison that justified the trade, sixteen copies of one
variable shift:

| 16 copies of one variable shift | LUT | FF | DSP |
|---|---|---|---|
| fabric barrel shifter | 1,200 | 704 | 0 |
| multiply by a one-hot | **288** | 496 | 16 |

> **Measure LUTs unflattened when the block is timing-critical.** Three
> output-identical simplifications taken alongside this were −458 LUT of 9,060
> with no clock constraint and **+307** with one: at WNS +0.06 ns the tool spends
> LUTs replicating logic, and the replication moves more than the logic does. The
> flattened, constrained number is the one that ships; the unflattened one is the
> one that says whether the *logic* shrank.

### 2.3 The per-tile output scale: built, measured, cancelled

| | Fmax | LUT | DSP |
|---|---|---|---|
| feature off | 343.4 | 9,821 | 48 |
| feature on | 330.7 | 10,673 | 48 |

**+852 LUT, −12.7 MHz, no extra DSP.** Bit-identity was verified two ways.
Cancelled anyway ([accumulator.md](accumulator.md) §8). A variant that made the
scale always present with 1.0 as its neutral value measured **10,297 LUT at
330.7 MHz** — bit-identical and not cost-identical, because widening the
block-scale product widens the normaliser datapath from 30 to 39 bits whatever
value is in it.

### 2.4 The accumulator's timing history

**84.7 MHz to 349.4 MHz in fourteen measured steps**, out-of-context against a
300 MHz target, with the full 384-check suite re-run after each. The worst
relative error stayed at 3.339790e-04 throughout — bit-identical, step for
step — so **none of it was bought with precision**.

| step | Fmax | LUT | FF |
|---|---|---|---|
| unpipelined: normalise + add in one cycle | 84.7 | 13,037 | — |
| split `normalise \| add` | 129.7 | 11,787 | — |
| narrow the normaliser input 30 → 22 bits | 136.3 | 11,185 | — |
| split the adder `align \| round`, 2 banks | 217.5 | 13,912 | — |
| move the add across the align/round seam | 208.6 | 14,344 | — |
| split the normaliser `leading-one \| assemble` | 233.7 | 13,654 | 17,497 |
| resident tile as LUTRAM, load mux off the tail | 219.7 | 11,086 | 5,210 |
| split the round stage, 3 banks | 242.4 | 11,091 | 6,724 |
| register the align-stage selects | 238.9 | 11,263 | 6,744 |
| parallel leading-one and sticky | 234.3 | 11,708 | 6,744 |
| one-level operand mux, zero-ness as control | 293.2 | 11,310 | 6,764 |
| concatenated `{exp,mant}` compare | 302.3 | 11,369 | 6,758 |
| explicit BRAM tile, 4 banks, `READ_LAT=1` | 241.2 | 10,469 | 6,310 |
| **explicit BRAM tile, 1 bank, `READ_LAT=2`** | **349.4** | **9,945** | **6,232** |

**Six of the fourteen steps moved Fmax by less than 10%, and three moved it
backwards.** The table is kept in full because the dead ends are the informative
part: the six pipeline splits were worth +150 MHz between them, and then fixing
three combinational loops that carried a value between iterations — which
synthesise as ~25-level LUT chains inside a single pipeline stage, where no seam
elsewhere can reach them — was worth +68 MHz on its own.

Three later steps took the whole cluster over the line:

| step | cluster Fmax |
|---|---|
| starting point | 294.9 |
| magnitude taken **before** the multiply rather than after | 296.4 |
| a fabric multiply replaced by the predicate its consumer wanted | 299.9 |
| a per-instruction boolean decoded once instead of per cycle | **325.6** |

The tile memory comparison behind the last row of the fourteen:

```
   inferred LUTRAM, 3 banks, async read     11,049 LUT   0 BRAM   312.3 MHz
   explicit BRAM,   4 banks, READ_LAT=1     10,469 LUT  20 BRAM   241.2 MHz
   explicit BRAM,   1 bank,  READ_LAT=2      9,945 LUT   5 BRAM   349.4 MHz
```

**The primitive was never the problem.** The same 352-bit memory measures **837
MHz standing alone**, so anything slower is the module's own logic. A memory
that is an order of magnitude faster than the block around it cannot be what
bounds the block, and reaching for the primitive is the wrong first move.

The `READ_LAT=1` row is worth its own line: without the block RAM's output
register the path begins at the RAM's clock-to-out — about 1.2 ns — rather than at
a flip-flop, and that alone cost about 70 MHz.

### 2.5 Two configuration figures

| | LUT | Fmax |
|---|---|---|
| shift amount declared 8 bits | 6,109 | 396.1 |
| shift amount clamped to 5 bits | **4,751** | 353.6 |

**1,358 LUT — 22% of that cluster — for range that cannot be used**, since a shift
past the accumulator width pushes the value out regardless. Correctness unchanged;
both benches still passed.

| | LUT | Fmax |
|---|---|---|
| operand buffer written with a variable part-select | 45,956 | 273.7 |
| the same loop unrolled so each index is constant | **13,664** | **292.9** |

**−70%, 32,292 LUTs for one loop rewrite.** A variable part-select tells synthesis
that any of 896 bits might come from any position, so it builds a barrel mux
across the entire buffer, twice.

---

## 3. The vector path

One lane, out-of-context:

| | measured | estimated beforehand |
|---|---|---|
| **Fmax** | **324.8 MHz** (WNS +0.147 ns at a 310 MHz target) — lower bound | — |
| LUT | **1,249** | ~750 |
| FF | **705** | — |
| DSP | **3** | 3 |
| BRAM / URAM | **0** | 0 |
| latency | **14 cycles, II = 1** | — |

**The LUT estimate was 40% low and the reason is worth recording**: a 14-stage
pipeline at II=1 has to carry about twenty control signals from where they are
produced to where they are consumed. The datapath is roughly what was predicted;
the delay lines are what was not.

### 3.1 The assembled core, and the shrink

**One lane at 324.8 MHz says nothing about the assembled core.** `vec_lanes` and
`vec_cu` started at 305.1 and **229.3 MHz**, and the paths that bound them were
all control reaching a datapath, never the arithmetic.

| | before | after |
|---|---|---|
| `vec_lanes` | 37,916 LUT / 16,746 FF / 0 BRAM / 48 DSP / 358.4 MHz | **24,683 / 15,032 / 40 tiles / 48 / 358.4** |
| `vec_cu` | 48,415 LUT / 23,439 FF / 4 BRAM / 51 DSP / 336.8 MHz | **35,629 / 22,145 / 44 tiles / 51 / 358.4** |

**`vec_lanes` −34.9%, `vec_cu` −26.4%**, with Fmax *up* and the worst path no
longer in the core at all — it is inside the ALU, so the core now sits at the ALU
floor. BRAM became a counted resource: 44 tiles per core then, **36.5 now**.

Where those tiles are, `vec_core` out of context (`scripts/tcl/ooc_mod.tcl`,
3.333 ns ask, `MODEL=0`, L1 512 block, register file block; `build/ooc/vec_*`):

| `vec_core` | LUT | FF | RAMB36 / RAMB18 | tiles | WNS |
|---|---|---|---|---|---|
| coefficient ROM as three 22-bit ROMs, register file 24-bit copies | 29,140 | 24,696 | 4 / 81 | 44.5 | +0.797 |
| **coefficient ROM as one 66-bit word — what `vec_tables.py` emits** | 29,140 | 25,320 | 20 / 33 | **36.5** | +0.797 |
| + register-file copies padded to 36 (`RF_PAD 36`) | 29,140 | 25,320 | 20 / 33 | 36.5 | +0.797 |
| + two lanes per register-file word (`RF_PACK 2`) | 29,200 | 25,318 | 36 / 1 | 36.5 | +0.797 |

The 8 tiles are the sixteen ROMs going from 3 RAMB18 each to 1 RAMB36 each;
the register file is one RAMB18 per lane per block copy in every row, its floor
([vector-core.md](vector-core.md) §6).

| change | LUT |
|---|---|
| operand network (phase window + constant indices) | **−3,404** |
| coefficient ROMs to block RAM | **−3,575** |
| register file to block RAM | −3,352, of which **+1,129 came back** |
| predicate write-back | −1,987 |
| stage-0 narrowing | −1,256 |
| write crossbar | −1,089 |
| lane rotate | −565 |
| fused exp-and-sum leaf write-back | +249, but **+42 in `vec_cu`** |

The +1,129 that came back is the load-bearing one. **Moving storage to block RAM
moves its clock-to-out onto every consumer's path, and port granularity is the
unit that matters, not the module.** A RAMB18's clock-to-out is about 1.5 ns on
this speed grade. Of the register file's three read ports, two feed ALU operands
and have a whole cycle; the third feeds the store converters and did not —
`vec_cu` fell to **286.0 MHz**. The load side had already hit this and left
another block at **286.9 MHz**, the same number one direction earlier. A
whole-module primitive parameter hid the fact that only one of three consumers
could not afford it.

Extrapolated to 128 lanes: **~160k LUT and 384 DSP** — about **37% of an SLR's
LUTs against 12.5% of its DSPs**, so the vector core is **fabric-bound, not
DSP-bound**. One assembled core measures roughly **33,000 LUT**, which is the
number to use when costing a new instruction: something costing ~3,000 LUT lands
in every core, so at six cores it is ~18,000 — **half a core's worth of area for a
capability every core gains.**

`mm_mesh` — the memory agent with the mover, one matmul cluster, one vector core
and two routers — measures **328.8 MHz** after the shrink. Earlier points on the
same top are 325.6 and 324.6 MHz against a 320 MHz target, on accumulator paths.

---

## 4. Blocks measured in this ship

Measured here because KohakuTPU is what was built; the blocks themselves belong to
the framework, and framework pages should link to this row rather than quoting it.

| block | LUT | FF | BRAM | DSP | Fmax | note |
|---|---|---|---|---|---|---|
| `mx_quant` (the MXFP7 quantiser) | 4,267 | — | 0 | 32 | **400.6** | 310 MHz-target run; it is KohakuTPU's, on the memory-agent side |
| `mag_mem_port` | — | — | — | — | 330.0 | |
| `NoCRouter` | 3,281 | — | — | — | **≥450** | 2.5 ns with +0.278 ns slack, 7 logic levels |
| `NoCRouter`, 2×2 grid, in-port FIFOs 512 / `"block"` — what the ship emits | 3,052 | 3,165 | **20** | 0 | 386 | 3.333 ns ask, `build/ooc/router_block512` |
| `NoCRouter`, same, FIFOs 32 / `"distributed"` (`ROUTER_DEPTH` / `ROUTER_MEM`) | 3,762 | 5,965 | **0** | 0 | 426 | +710 LUT for 20 tiles; `build/ooc/router_dist32` |
| router (earlier run) | — | — | — | — | 406 | 452 for two routers linked |
| output port switch | — | — | — | — | 644 | |
| input port switch | — | — | — | — | 732 | |
| `noc_orchestrator` | 2,563 | 2,465 | 0 | 0 | 570.0 | 300 MHz-target run |
| `axi_n1` (N=4) | 955 | — | — | — | 604 | replaces a vendor interconnect measured at 43,714 LUT at the root |

Memory-primitive probes, standing alone: a 352-bit block memory at **837 MHz**;
352 x 4096 in URAM at **585 MHz**. Both are far above anything they sit inside,
which is what makes "blame the module, not the primitive" a checkable claim rather
than a slogan.

> The quantiser is **not** in any cluster figure in §2. It is built and it lives
> on the memory-agent side of the mesh by design
> ([number-format.md](number-format.md) §5), so none of the cluster rows include
> it.

### 4.1 Memory shapes

The measurements behind the framework's depth rule — one block deep needs no
proof, a deeper chain ships on a routed one
([arch/physical/device-facts](../../arch/physical/device-facts.md#memory-blocks-the-geometry-that-is-a-rule)).
Out-of-context synthesis, `scripts/tcl/ooc_mod.tcl`, 3.333 ns ask, Vivado
2024.2; Fmax is `1000 / (period − WNS)`, an estimate that routing lowers. The
staging store is `src/kohakuaccel/sysnode/core/mag_stage.v` with both ports
live (in the node port A is tied off, which prunes the 1,024-bit entry mux);
the primitive is `src/kohakuaccel/common/kohaku_sdpram_be.v` at 256 bits wide.

| shape | LUT | FF | URAM / BRAM | WNS | est. MHz |
|---|---|---|---|---|---|
| staging, 4 banks × 16 blocks, read latency 2, registered dispatch and outputs | 3,504 | 8,289 | 64 | +1.897 | 696 |
| staging, 4 banks, read latency 3 | 3,504 | 12,386 | 64 | +1.897 | 696 |
| staging, 2 banks × 2-deep chain, read latency 3 | 2,137 | 6,230 | 64 | +1.682 | 606 |
| staging, one array, 4-deep chain, read latency 2 | 815 | 3,146 | 64 | +0.909 | 412 |
| staging, **one array, 4-deep chain, read latency 5** (rows + columns) — **ships** | 819 | 3,149 | 64 | +1.828 | 664 |
| staging, one array, `CASCADE_HEIGHT` 1 (the tool's mux), read latency 5 | 2,915 | 7,357 | 64 | +1.499 | 545 |
| 256 × 16,384 URAM, chain, read latency 2 / 5 | 0 / 0 | 256 / 256 | 16 | +0.909 / +2.159 | 412 / 852 |
| 256 × 16,384 URAM, `CASCADE_HEIGHT` 4, read latency 3 | 0 | 256 | 16 | +1.475 | 538 |
| 256 × 16,384 URAM, `CASCADE_HEIGHT` 1, read latency 3 / 5 | 520 / 524 | 268 / 1,311 | 16 | +1.807 / +2.030 | 655 / 767 |
| 256 × 4,096 URAM, one block, read latency 3 | 0 | 256 | 4 | +2.159 | 852 |
| 64 × 8,192 block RAM, tool chain / `CASCADE_HEIGHT` 1 / 4, read latency 2 | 11 / 43 / 22 | 0 / 65 / 0 | 16 BRAM | +2.021 / +1.949 / +1.553 | 762 / 722 / 562 |
| 64-wide LUTRAM, 32 / 64 / 128 / 256 deep | 184 / 184 / 312 / 544 | 64 | 0 | +2.7 | — |
| crossbar-cache array `kx_carray`, 8 banks × 8 blocks (K=1) | 1,658 | 5,914 | 64 | +1.515 | 550 |
| crossbar-cache array, one 8-deep chain (K=1) | 584 | 1,649 | 64 | +1.404 | 518 |
| crossbar-cache array, **one bank of 4-deep chains at K=2, write lanes** — **ships**; whole-Xache numbers in [kohakuaxi/pxache §1](../kohakuaxi/pxache.md#1-what-a-partition-costs-in-one-table) | — | — | 60 | — | — |

At the node (`scripts/tcl/ooc_sysnode.tcl`, PORTS 2, ILINK 1, DRAM CDC 1):
**30,879 LUT / 44,710 FF** with the 4-bank staging store, 29,940 / 42,672 at
2 banks (2-deep chains, read latency 3) and **29,311 / 39,589 at one bank**
(4-deep chains, read latency 5) — the 1,568 LUT are the banks' return select
(a 4:1 of 1,024 bits) and their control. Routed inside one SLR
(`scripts/tcl/impl_sysnode.tcl`), the one-bank node and the four-bank node
both place and route with no congestion window above level 5, and the slack
of both is the RV64 core's redirect path (−0.354 and −0.290 on the same
path); that routed pair, not the synthesis number, is why the chain ships.
The register in front of the DRAM port's return bus (`R_REG`) is a further
−100. `WRITE_MODE_B = no_change` on an UltraRAM fails XPM elaboration in
Vivado 2024.2; the wrapper's `WR_MODE` knob therefore ships as `read_first`.

---

## 5. Device level

### 5.1 Cluster-count arithmetic — and it is arithmetic

**One cluster is what was synthesised. Nothing at 32 or 45 clusters has been
built**, so every column but the first is multiplication.

| | per cluster | x32 | x45 | of device (x45) |
|---|---|---|---|---|
| LUT | 17,521 | 560,672 | 788,445 | **45.6%** |
| FF | 17,612 | 563,584 | 792,540 | 22.9% |
| BRAM36 | 5 | 160 | 225 | 8.4% |
| DSP | 272 | 8,704 | 12,240 | **99.6%** |
| URAM | 0 | 0 | 0 | 0% |
| mesh ports | 2 | 64 | 90 | — |
| MACs/cycle | 512 | 16,384 | 23,040 | — |

At 300 MHz, 45 clusters is **~13.8 TFLOPS of AMP FP16-MXFP7**, DSP-bound with
LUTs at 46% and BRAM at 8%.

**That table uses the 272-DSP cluster and is therefore not current.** The measured
cluster is **304 DSP** once both the block-scale multiply (16, one per lane) and
the normalising shift (32, two per lane) are counted, which takes the DSP-bound
count to `12,288 / 304 = 40`.

**Three cluster counts are derivable from this project's own figures, and only
one is current:** 48 from the cascade's 256 DSPs alone, 45 from the 272-DSP
cluster, **40 from the 304-DSP cluster that is built.** Use 40. The arithmetic
behind each is the same division and the difference is entirely in what the
cluster's DSP count is taken to include — so a figure quoted without naming its
per-cluster DSP count cannot be checked, and every one of the three has appeared
somewhere.

The LUT and BRAM headroom is unaffected either way, and the conclusion —
DSP-bound, which is the right place to be bound on this part — moves further in
the same direction with each correction. **That is why an error of this kind is
not caught by the answer looking wrong**: it strengthens the claim it is
supporting.

The 32-cluster configuration is what a four-partition floorplan would build: about
9.8 TFLOPS on roughly a third of the LUTs.

### 5.2 What the host IP costs

Out-of-context per-IP synthesis from the *implemented* single-mesh design:

| IP | LUT | FF | BRAM | DSP |
|---|---|---|---|---|
| XDMA | **76,319** | 72,059 | 124 | 0 |
| `smartconnect_0_0` | 20,104 | 29,602 | — | — |
| `axi_smc_0` | 19,709 | 30,115 | — | — |
| DDR4 MIG | 19,944 | 21,263 | 25.5 | 3 |
| JTAG-AXI | 867 | 2,300 | 4 | — |
| AXI GPIO | 62 | — | — | — |

**XDMA is 17.7% of an SLR on its own.** Whichever die hosts PCIe gives up roughly
a vector core's worth of fabric to do it, which is the constraint behind the
floorplan in [ship.md](ship.md) §2.

### 5.3 Placed occupancy

From a placed multi-mesh run:

| | |
|---|---|
| URAM | **120 of 1,280 — 9.38%** |
| the most crowded die | **95.80% CLB** |
| another die | 93.6% CLB |
| SLL use, one boundary | 2,765 of 23,040 (12.0%) |
| SLL use, another | 1,355 (5.9%) |
| SLL use, the third | none |

A full 288-bit flit link is about 5% of one boundary, so **the interlink is not
what constrains the crossing** — fabric occupancy is. The single-mesh design on
the card places nothing at all in one SLR.

### 5.4 v8t2 — the first fully routed device image

`multimesh_v8t2` (four 2×2 2+2 meshes, four system nodes with interlink, the
partitioned Xache, one-clock station bus; [v8-plan.md](v8-plan.md) §7) through
`write_bitstream`, 2026-09-01. Zero routing errors, hold met (WHS 0.000).

| per SLR | 0 | 1 | 2 | 3 |
|---|---|---|---|---|
| CLB | 70.61% | **87.96%** | 71.68% | 65.93% |
| LUT | 158,480 | 218,755 | 159,827 | 158,411 |
| FF | 208,409 | 275,521 | 217,218 | 207,647 |
| BRAM tile | 405 | 512.5 | 408 | 405 |
| URAM / DSP | 135 / 792 | 135 / 792 | 135 / 792 | 135 / 792 |

Die crossings: 17,931 nets — Xache hop + readback chains 13,955, station
links 1,881, interlink pipes 297 × 6 (their registers in 3,574 Laguna
sites), everything else < 50 each. Congestion concentrates in level-5–7
windows on SLR1-north and SLR2-south long wires, owned by `u_mag`,
`u_quant` and the Xache's URAM columns (100% within every hot window).

Routed worst path per clock (ask in ns / WNS / levels): sysnode 3.333 /
**−1.896** / 13; noc 3.333 / −0.457…−1.176 / 7; vec 3.333 / −0.544…−1.439 /
6–8; mat2x 1.667 and div2 3.333, skew-dominated −0.974…−1.486; bus 5.000 /
−0.527 / 10; MIG ui 3.332 / −0.313…−0.748; XDMA 4.000 / −1.003; ctrl
10.000 / +2.517. Of the 200 worst setup paths, 195 start in `u_mag` on the
sysnode clock (127 mesh 1, 66 mesh 2, 2 mesh 3 — the RV64 redirect/PC
cluster and MAG engine CE fanout, 24 reset-class), 5 in the Xache hops, and
**none crosses an SLR** — nor does any of the 400 worst. The sysnode clock is a DRP knob: as built the image is met at any
sysnode rate ≤ 191 MHz, and the binding paths are module-scale (RV64 core,
MAG fanout), not floorplan-scale.

### 5.5 v8t3 — the same machine with no mesh, and a die that is 22% full and congested

`multimesh_v8t3` is v8t2 with every fix of the round (flat Xache rows, the
LUT6 slot mux, one shared boundary trunk per direction, 4-bank MAG staging,
the imem cascade) and **all four meshes removed**: each die is one system node
with its interlink, its station, its Xache partition and its MIG. Routed
2026-09-02, `write_bitstream` clean.

| per SLR | 0 | 1 | 2 | 3 |
|---|---|---|---|---|
| CLB | 22.01% | **44.36%** | 21.99% | 20.47% |
| LUT | 52,704 | 113,930 | 55,257 | 52,609 |
| FF | 73,828 | 138,729 | 78,692 | 73,774 |
| BRAM tile | 143.5 | 266 | 161.5 | 143.5 |
| URAM / DSP | 129 / 50 | 129 / 50 | 129 / 50 | 129 / 50 |

Timing improved against v8t2 — **WNS −1.621, TNS −38,163** against v8t2's
−1.896 / −99,608 — and the SLR boundaries are nowhere near full: **2,421 /
2,368 / 2,344 SLLs of 23,040** per boundary, 10.2–10.5%.

The result that matters is the one those two facts frame. Four meshes came out,
CLB occupancy fell from 71–88% to 20–44%, and congestion dropped only one
level: still **5 on the placer's final windows and 6 on the router's initial
ones**, and every window is in SLR2, exactly where v8t2's were. A die at 22%
occupancy with 10% of its SLLs used is not short of anything. Congestion that
survives emptying the die by three quarters is not a capacity problem.

**Nothing is stretched.** `scripts/tcl/v8t3/86_congestion.tcl` bins all 736,738
placed primitives by owner and clock region. Every `mesh_i` and every `ddr4_i`
is wholly inside its own SLR; the Xache's four partitions split 10,262 / 12,907
/ 12,826 / 10,322 across the four dies as designed, and each interlink pipe has
exactly 297 cells on each side of its boundary. The floorplan did what it was
told.

What differs is the arrangement *within* each die. Writing each module's centre
of mass as its clock-region column (a die is columns X0–X7; every MIG lands in
X4):

| die | node | Xache partition | MIG | also |
|---|---|---|---|---|
| 0 | **X2** (45,602 of 63,508) | **X3** (6,678) | X4 | — |
| 1 | **X2** (33,715) | **X3** (7,628) | X4 | XDMA X5–X7, 121,987 |
| 2 | **X5** (28,980), X4 (20,637), X3 (14,640) | **X3** (9,353) | X4 | — |
| 3 | **X5** (40,185), X6 (16,103) | X3–X5 (3,301 / 3,248 / 2,859) | X4 | — |

In dies 0 and 1 the node and its Xache partition are adjacent columns with the
MIG outboard of both. In die 3 they overlap at X5. **Die 2 is the only one
where they sit on opposite sides of the MIG**: the node's mass at X5, its
partition at X3, and `ddr4_2`'s 53,597 cells filling X4 between them — with a
third of the node (20,637 cells) inside that same column.

Every congestion window in the design is in that gap, and its occupants are
exactly those four. In the worst placer window: `ddr4_2` 42.3% of the cells,
`mesh_2` 31.0%, station 2 20.5%, the Xache 6.2%.

**40% of the nets routed through that window have neither driver nor load
inside it** — 9,794 of 24,772; the second window 9,489 of 22,552. Attributed
by the scope that declares them, the pass-through is:

| owner | pass-through nets | share |
|---|---|---|
| **Kohaku Xache** | **4,321** | **44.1%** |
| `ddr4_2` | 2,249 | 23.0% |
| `mesh_2` | 1,838 | 18.8% |
| `station_bus` | 1,361 | 13.9% |

and three levels down, the Xache's share is its two boundary trunks and its
home: `g_chain.g_b[2].g_d[0].u_tk` 1,043, `g_b[1].g_d[1].u_tk` 729,
`g_b[1].g_d[0].u_tk` 444, `g_home[2].u_c` 766, `g_home[2].u_we` 428. The
sampled driver→load pairs name the traffic outright — 120 nets run
`g_b[2].g_d[0].u_tk → g_b[1].g_d[0].u_tk`, one boundary trunk straight into
the other.

That is the Xache chain **passing through die 2**. Die 2 is the middle of the
chain and the middle of the interlink line, so everything between die 3 and
dies 1–0 traverses it — and because its node and its partition sit on opposite
sides of the MIG, that traffic crosses the MIG's column to do so.

Why the placer chose it is legible from the same table: partition 2's boundary
trunk pulls it toward partition 1, which is firmly at X3, while node 2's
interlink pulls it toward node 1 at X2 on one side and node 3 at X5 on the
other — and node 1 cannot move right because XDMA owns X5–X7 of its die. The
node follows the interlink, the partition follows the trunk, and in die 2 alone
the two pulls point opposite ways across the MIG.

A second mechanism points at the same place. Every die-spanning clock is rooted
in **column X4** at the SLR1↔SLR2 boundary: the sysnode clock
(`clk_wiz_mesh1/…/clkout4_buf/O`) at **X4Y7 with 182,409 leaf loads**, the bus
clock at X4Y7 with 30,868, the control clock at X4Y8 with 9,197. So the column
that already holds each MIG also carries the root and the first hop of
distribution for a clock feeding all four dies, immediately below the rows the
congestion windows occupy (clock rows Y8–Y10). UG949 warns exactly this: clock
loads near an SLL crossing compete with SLL routing.

The two mechanisms take two tests, deliberately not mixed. **v8t4 tests the
clock one**: four sysnode clocks of ~45k loads each, rooted in their own dies,
and no clock root at a die boundary carrying four dies of distribution. It does
NOT address the first — the Xache chain still passes through die 2 and still
crosses the MIG's column — so a v8t4 that improves slack without moving the
congestion level would be the expected result, not a surprise. **The floorplan
test is `CMP_COLS`** in `scripts/tcl/v8t3/00_config.tcl`, which gives each
die's node, Xache partition and station a pblock on one side of the MIG's
column; it defaults to empty, so neither v8t3 nor v8t4 carries it.

### 5.6 v8t4 — a clock per die

`multimesh_v8t4` is `multimesh_v8t3` **with one knob**: `PER_DIE_CLK 1` in
`scripts/tcl/v8t3/00_config.tcl`. The two builds source the same stage scripts,
so nothing else can differ between them.

| | v8t3 | v8t4 |
|---|---|---|
| sysnode clock | one, `clk_wiz_mesh1/clk_out4`, four dies | one per die, off that die's own wizard |
| bus clock | one, `clk_wiz_ctrl/clk_out2`, four stations | one per station, `clk_out2..5`, its own BUFG |
| sysnode reset | one `proc_sys_reset` + `xcvu13p_rst_tree` | one per die, no tree |
| bus reset | one + tree | one per die, no tree |
| Xache boundary trunk | register pipe, one clock | clock crossing (`KX_PCLK 1`) |
| interlink hop | register pipe, one clock | `kts_cdc` crossing (`IL_ASYNC 1`) |
| station link | `sb_link` | `sb_link_cdc` (`LINK_CDC 1`) |

No clock net and no reset net spans four dies. A node still meets its Xache
partition, its station's port 0 and its own DRAM master with **no crossing at
all** — they share die *i*'s clock — so the crossings appear only where a die
boundary already was.

Independent clocks release independently, and the interlink surface has no
`ready`: a die that starts sending into a die still in reset loses those flits.
So all nine resets are held until **every** wizard has locked (`lock_cat` +
`lock_all` into each `proc_sys_reset`'s `ext_reset_in`).

Three crossings, three existing verified structures. `kts_pipe_bd` at
`ASYNC 1` puts a `kts_cdc` between its two pipe halves;
`tests/transmit/kts_pipe_bd_tb.v` runs three links at once — 3:1, 1:3 and
synchronous — with
the landing die released 2,000 ns after the sending die, and passes 26,058
checks with the slow receiver still taking 1.000 flit per receiver cycle. The
Xache at `PCLK 1` passes its P=4 and P=2 matrices (4,013–4,015 checks each,
staggered per-partition resets). The station link at `LINK_CDC 1` is the v6/v7
shape.

**Routed 2026-09-02, `write_bitstream` clean, hold met (WHS 0.000).**

| | v8t2 | v8t3 | **v8t4** |
|---|---|---|---|
| WNS | −1.896 | −1.621 | **−0.699** |
| TNS | −99,608 | −38,163 | **−7,965** |
| failing endpoints | — | — | 34,001 of 1,027,751 |
| congestion, placer | L6/L7 | L5 ×2 | **L5 ×2** |
| congestion, router | — | L5 ×1, L6 ×3 | **L5 ×2, L6 ×2** |

| per SLR | 0 | 1 | 2 | 3 |
|---|---|---|---|---|
| CLB | 22.52% | 46.97% | 24.90% | 20.91% |
| LUT | 53,002 | 115,304 | 56,014 | 52,924 |
| FF | 74,773 | 142,918 | 81,910 | 75,070 |
| BRAM tile | 148 | 275 | 170.5 | 148 |
| URAM / DSP | 129 / 50 | 129 / 50 | 129 / 50 | 129 / 50 |

SLL per boundary 2,499 / 2,497 / 2,461 of 23,040 (10.7–10.9%). The three
crossings cost **+2,744 LUT, +9,648 FF, +27 BRAM tiles** against v8t3, with
URAM and DSP unchanged — 0.25% of the device's LUT for **+0.922 ns of WNS and
4.8× the TNS**.

The congestion behaved as §5.5 predicted, which is the useful part. The level
did not fall — placer congestion stays at L5 — but the **hot region moved**:
v8t3's six windows were all in SLR2, v8t4's four router windows are three in
SLR1 and one in SLR2, and `ddr4_2` fell out of the top occupants entirely
(42% of the worst window in v8t3, absent or 15% now). What is left in every
window is `xache/u_kx` (20–39%), a node's `u_mag` (26–34%) and
`station_bus/u_line` (12–17%) — the through-traffic, untouched, because
nothing in v8t4 addresses where the chain runs. That is what `CMP_COLS` is
for.

### 5.7 Which primitive holds a FIFO — the cost law, measured

A block-RAM tile is 72 × 512. Every FIFO in the Xache and the station bus is
515–640 bits wide and **16 deep** except one, so each spends ⌈w/72⌉ tiles to
hold 16 of 512 rows: 97% of every tile is empty. LUTRAM wastes nothing, so the
LUT it costs to free one tile is

`LUT per tile ≈ 1.125 × depth`

— from the two geometries alone, and it predicts what was measured out of
context on `kx_pxache` at v8t4's shape (P 4, K 2, 16384 sets, 4 banks, one
trunk a boundary, a clock a partition, `HCDC` all ones), 3.333 ns:

| class | width × depth | tiles | LUTRAM on xpm, LUT per tile (alone) |
|---|---|---|---|
| boundary trunk rings | ~523 × 16 | 90 | **39** |
| DRAM-edge read CDC | 521 × 16 | 30 | 40 |
| DRAM-edge write CDC | 577 × 16 | 34 | 38 |
| **master reorder buffer** | 515 × **256** | 30 | **420** |
| station bus, every FIFO | 530–640 × 16 | 90.5 | 39 |

**Where the LUT and FF go.** One converted 523 × 16 ring, depth-6 hierarchy,
xpm `distributed`: **446 LUT / 1,284 FF, of which the LUTRAM is 300 LUT / 12
FF.** The memory carries **1,034 FF for a 517-bit word** — two output
registers, where block RAM's own output flop had been one of them for free —
and the ring's FASTW split builds **two** complete `xpm_fifo_async`, each with
its own gray pointer CDC, FWFT counter and reset synchroniser (146 LUT / 238
FF), for halves that are written and read in lockstep. The LUTRAM itself is
already at the primitive's floor: 28 bits a LUT because a 16-deep queue fills
half of every RAM32.

So the LUTRAM side of the trade is rebuilt in the design's own idiom, the
shape of the synchronous `kx_hop_ring` on two clocks:

- **`kohaku_aring`** (`src/kohakuaccel/common/`): one LUTRAM array, one
  gray-coded pointer crossing, one output register. `FULL 0` where the sender's
  credits bound occupancy (the trunk rings — no full flag, no read-pointer
  crossing at all); `FULL 1` for a valid/ready edge (`kx_scdc`, and
  `async_fifo`'s new `"lean"` type, which the station bus selects through
  `LUT_PER_BRAM`). `sync_fifo` gained the same `"lean"` inferred ring on one
  clock.
- **`kx_lram`**: the reorder ring's LUTRAM inferred directly, one registered
  read, no wrapper: 2,887 LUT at 515 × 256, 1,444 at × 128, **592 at × 64**.

Nothing about a queue's width, depth or interface changes at any tier, and
every tier passes the Xache bench on the lean structures (4,013–4,015 checks in
the per-partition-clock and two-clock-DRAM configurations), `kohaku_aring` its
own (17,887 checks, 3:1 and 1:3, both flow-control shapes, reader released
late), and the station bus at `LUT_PER_BRAM` 0 and 120 (7 checks each).

**What SDP LUTRAM costs.** A RAM32M16 or RAM64M8 gives seven usable read
ports of its eight, so simple-dual-port LUTRAM packs **56 bits a LUT at depth
32 or 64 and 28 at depth 16**, and past 64 rows adds a LUT read mux per bit
per 64 rows. A 16-deep class therefore costs about width/2 LUT whatever its
depth up to 32 — depth is no lever there — while a deep buffer costs
width × depth / 56 plus the mux, where depth is the whole lever. Each
component alone, block against lean (`build/ooc/{trunk,link577,rb,nsu}_*`):

| component | block LUT / FF / tiles | lean LUT / FF | LUT a tile freed |
|---|---|---|---|
| `kx_trunk`, two rings 523 + 526 × 16, ASYNC | 906 / 1,641 / 15 | 1,228 / 2,233 | 21.5 |
| `kx_link` 577 × 16 (DRAM write CDC) | 75 / 121 / 8.5 | 358 / 621 | 33 |
| `kx_link` 521 × 16 (DRAM read CDC) | 75 / 121 / 7.5 | 327 / 563 | 34 |
| `kx_lram` 515 × 256 (reorder ring, a page a slot) | 12 / — / 7.5 | 2,887 / 515 | 383 |
| `kx_lram` 515 × 64 (reorder ring, 16 beats a slot) | 12 / — / 7.5 | 592 / 515 | 77 |
| `sb_nsu`, five depth-16 queues | 1,042 / 1,814 / 16.5 | 1,580 / 2,109 | 33 |

Against xpm's `distributed` structures a lean trunk is −232 LUT / −1,427 FF,
a lean link −50 / −650, and the station line at `LUT_PER_BRAM 120` −2,769 /
−11,583 (26,487 / 41,484 against 29,256 / 53,067; 26,473 / 43,797 at 0).

**The read slot.** The reorder ring was `RD_OUTQ × 4096/STRB` = 256 deep
because a slot held a whole 4 KB page and, in block RAM, the rows were free.
`kx_pxache` gained `RB_BEATS`: the beats a read slot holds (0 = a page). At 16
the ring is 64 deep and the LUTRAM 592 a master instead of 2,887. A read burst
longer than a slot is a protocol error (the bench reports it), so the same
value bounds every master's bursts: `mag_dram_port` gained `AR_MAX`
(`DRAM_AR_MAX` on `mag`, `sysnode`, `ktpu_node_v8t`), which issues a longer
request as back-to-back ARs on its id and tells the return side once, and the
build config sets both from `KX_RB_BEATS`. The Xache bench passes every tier
at slot 0 and 16 (4,013 / 3,837 checks, four partitions on trunks and
per-partition clocks), the DRAM port at `AR_MAX` 0 and 4 (64 checks each),
the mover chain through the node at 16 (597 checks).

`kx_pxache`'s `MEM_TRUNK` / `MEM_RB` / `MEM_HRD` / `MEM_HWR` are each
`"block"` | `"distributed"`, threaded through `kx_pbd_4x4` and the build
config (`KX_MEM_*`). The station bus has the same dial as a threshold,
`LUT_PER_BRAM`, evaluated per FIFO in `sb_nsu.v` — the law above in
LUTs-per-tile form.

**The whole Xache, chain live.** `kx_pxache`'s `MP` / `HP` default to every
master and home on partition 0, so a whole-Xache run must set them: with the
defaults no request crosses a boundary and the trunks are dead logic, kept only
as xpm's clock-crossing cells and deleted outright on the lean ring. With
`MP = HP = {3,2,1,0}` (`build/ooc/kxlive_*`, one run a tier, `clk_p[0..3]`,
`m_clk[0..3]` and `h_clk[0..3]` each an asynchronous clock at 3.333 ns):

| Xache tier | LUT | FF | BRAM | Fmax MHz | per die LUT (0 / 1 / 2 / 3) | per die tiles |
|---|---|---|---|---|---|---|
| T0, v8t4 as shipped | 17,659 | 29,099 | 184 | 260.7 | 3,800 / 5,219 / 5,128 / 3,513 | 38.5 / 53.5 / 53.5 / 38.5 |
| T0 with the 16-beat read slot | 17,887 | 28,980 | 184 | 279.6 | 3,862 / 5,115 / 5,100 / 3,811 | same |
| T1 trunk rings | 19,948 | 32,718 | 94 | 278.9 | 4,140 / 5,814 / 5,789 / 4,204 | 23.5 each |
| T2 + reorder ring, 16-beat slot | 22,073 | 34,699 | 64 | 280.4 | 4,789 / 6,439 / 6,414 / 4,432 | 16 each |
| T3 + DRAM read CDC | 22,946 | 36,436 | 34 | 287.7 | 4,940 / 6,694 / 6,642 / 4,672 | 8.5 each |
| T4 + DRAM write CDC | 24,089 | 38,462 | 0 | 271.2 | 5,249 / 6,959 / 6,901 / 4,982 | 0 |

A die is its partition; a trunk's landing rings count on the landing die and
its mux and credits on the sending die (`scripts/py/kx_slr.py`). The worst
path at every tier is a same-die master write-queue pointer into the home
engine's lock enable on die 2, 13 to 15 logic levels, 79% route; die 0 and 3
close at 300 MHz, die 1 misses by 0.060 ns.

**Station tiers** (`sb_line4`, v8t4's knobs, `build/ooc/sbtier_lpb0`,
`sblean_*`). S1 leaves 19 tiles, all on station 1: the xdma manager's request
queue (647 × 256, 9 tiles) and response queue (264 × 256, 4), and the jtag
manager's (143 × 256, 2, and 264 × 256, 4). Their depth is now a knob
(`sb_line4` `MREQ0..2` / `MRSP0..2` / `MMAXB0..2`, the build's `SB_MREQ*`,
`SB_MRSP*`, `SB_MMAXB*`, `SB_LPB1`): `sb_nmu` raises a depth to its floor,
one `MAX_BURST` packet, times two flits for the 512-bit xdma response, so 64 /
128 there is behaviour-identical to 256. The jtag driver splits at 256 KB, so
its bursts reach 256 beats and that manager keeps its 256 floor.

| station tier | LUT | FF | BRAM | Fmax | station 1 LUT / FF / tiles |
|---|---|---|---|---|---|
| S0, block everywhere | 26,473 | 43,797 | 90.5 | 299.8 | 11,289 / 15,982 / 41 |
| S1, threshold 120, lean rings | 26,487 | 41,484 | 19 | 299.8 | 11,387 / 15,553 / 19 |
| S2, xdma 64 / 128, station 1 at 270 | 27,850 | 42,063 | 6 | 299.8 | 12,752 / 16,134 / 6 |
| S2, xdma 128 / 128, station 1 at 290 | 28,925 | 42,120 | 6 | 299.8 | 13,827 / 16,191 / 6 |
| S3, xdma 64 / 128, station 1 at 580 | 30,087 | 42,433 | 0 | 323.3 | 14,989 / 16,504 / 0 |
| S3, xdma 128 / 256, station 1 at 580 | 31,904 | 42,497 | 0 | 323.3 | 16,806 / 16,568 / 0 |

Stations 0, 2 and 3 are the same in every tier (4,736 / 5,622 / 4,741 LUT,
0 tiles from S1 on). The line bench passes at every setting (671 checks, jtag
on the control clock and on the bus clock), the mesh end-to-end bench at 7.

**The logic pass on T4 and S2** (`build/ooc/kxlive_t4z`, `sblean_s2x_64_128`;
same shape, same generics). Xache: the master's slot-free flags and the
trunk's credit-not-zero flag are registers from their next-state counts
instead of a subtract-and-compare in front of the arbitration, which is where
the 13-level path from a master's write-queue pointer to the home engine's
take began; and the request inject flit lets the W data ride every flit type
instead of zeroing it on AR, AW and WX, an AND a bit that the census had
folded into the trunk's slot mux (the response side already rode its word).
Station: a hub with four or more sources keeps its payload select
as one LUT6 a bit shared by the skid's hold and output registers (`sb_hub`
`PAYMUX`; the 4-source PW-270 hub alone is 466 LUT against 844), and the
manager's credit reclaim, which read the response FIFO's block RAM through a
16-entry tag table into two adders, is registered and lands one cycle late.

| | LUT | FF | BRAM | Fmax MHz | worst path |
|---|---|---|---|---|---|
| Xache T4 before | 24,089 | 38,462 | 0 | 271.2 | master wq pointer → home take, 13 levels, die 1 −0.354 |
| Xache T4 after the LUT pass | 23,019 | 38,481 | 0 | 317.9 | master write-home latch → home take, 11 levels; partition clocks +0.187 / +0.359 / +0.526 / +0.768 |
| Xache T4 after the Fmax pass (below) | **23,055** | 47,917 | 0 | **358.0** | every path 9 levels; +0.540 at 3.333, +0.064 at 2.857 |
| station S2 before | 27,850 | 42,063 | 6 | 299.8 | jtag RSP block RAM → tag table → credit, 14 levels |
| station S2 after | **26,730** | 42,092 | 6 | **330.5** | — |

Per die the Xache is 5,088 / 6,513 / 6,417 / 5,000 LUT. What remains is at
its floors: 8,808 LUTRAM (width-bound), the trunk slot muxes 3,325 and the
reorder-ring landing muxes 2,094 at one LUT6 a bit, the array bank select
2,128 (one a bit, the `BANKS 4` choice), the write engines' W select ~2,800,
and ~4,000 of control. Keeping the trunk mux's LUT6s with `DONT_TOUCH`
changed nothing. Below this is a tier that keeps a class on block RAM (T3
saves 1,328 LUTRAM for 34 tiles) or a shape change. The station's 1,120 LUT
is −275 a station from the hub select.

**Xache Fmax to 358 MHz** (`scripts/tcl/ooc_paths.tcl`: the same synthesis
at a 2.857 ns ask with every path under 0.25 ns of slack bucketed by its
start and end register groups, `build/ooc/kxlive_t4*_350/paths.txt`). At
317.9 MHz every failing class started at a master's write-home latch and ran
through the trunk arbitration into the far home's read take, 11 levels. Three
registers, each one entry at full rate with no hold mux, each costing one
cycle of latency and no LUT:

| register | what it cuts | Fmax after |
|---|---|---|
| the master's inject offer (`qo_vq` / `qo_dq`): the AXI master holds its beat, so accept = empty or popping | master valid → trunk `sel` → response ready → home take | 327.2 |
| a head after every landing ring (`hv_q` / `hd_q`, 12 of them): the ring refills it the cycle it leaves | ring output → dst decode → consumer ready or next trunk → the ring's own pop | 321.4 (the classes moved) |
| the write engine's grant (`pick`), re-qualified against the live valids as `kx_rd_pipe` already did | one head's decode → the isolate-lowest over every source → the other head's ready | **358.0**, +0.064 ns, every path 9 levels |

23,055 LUT, 47,917 FF (the twenty 526-bit registers), per die 5,085 / 6,432
/ 6,437 / 5,100 LUT and 10,861 / 13,100 / 13,099 / 10,858 FF. Hit-32
latency from master 0 to home 0 / 1 / 2 / 3 at T4 is
36 / 53 / 65 / 81 cycles against 36 / 50 / 60 / 75 before: the remote
requests pay one cycle at the inject and two a hop. Toward 400 MHz the next
classes are the response landing (home read pipe `rid` → the master's slot
counters through the landing pick, 2.68 ns), the head's destination decode
into the next ring (2.60) and a head's valid into the home's take (2.55);
each wants one more register: the master's landing pick and the home's take.

---

### 5.8 v8t5 — every path under a nanosecond at synthesis, by class

`scripts/tcl/v8t5_paths.tcl` (`v8t3/87_synth_paths.tcl`) opens the
synthesised project read-only beside the implementation and collects every
setup endpoint with less than `V8_PATH_MARGIN` (1.0 ns) of slack, one path per
endpoint: a row each in `build/multimesh_v8t5_synth_paths.tsv`, then the same
paths by clock (`_perclock.txt`), by start/end register class with bit
indices stripped (`_classes.txt`), the classes merged across the four dies
(`_classes_merged.txt`), by owning module (`_owners.txt`), by logic level
(`_levels.txt`), and a node-by-node `report_timing` of the worst path of every
failing class (`_classes_full.rpt`, 400 of 577, indexed). Synthesis slack is
optimistic against route (m62_c1 lost 0.740 ns), which is what the margin is
for. Hold at synthesis is not collected.

| clock | ask | WNS | TNS | failing | < 0.25 | < 0.50 | < 0.75 | < 1.00 |
|---|---|---|---|---|---|---|---|---|
| sysnode, die 0 | 3.333 | −0.791 | −157.4 | 523 | 1,079 | 3,928 | 6,549 | 11,121 |
| sysnode, die 1 | 3.333 | −0.791 | −178.3 | 582 | 2,193 | 4,933 | 7,028 | 13,095 |
| sysnode, die 2 | 3.333 | −0.791 | −173.2 | 556 | 2,856 | 5,743 | 8,858 | 14,450 |
| sysnode, die 3 | 3.333 | −0.791 | −157.3 | 523 | 1,087 | 3,910 | 6,939 | 10,859 |
| XDMA | 4.000 | −0.161 | −0.7 | 6 | 14 | 634 | 821 | 1,668 |
| bus, station 1 | 5.000 | −0.020 | −0.1 | 8 | 19 | 143 | 730 | 862 |
| MIG ui × 4 | 3.332 | +0.566 … +0.836 | 0 | 0 | 0 | 0 | 32 | 284 |

2,198 failing endpoints in 577 classes, 164 once the die index is folded.
Every one belongs to one of eight families; the worst path of each, node by
node, is in the report.

| family | endpoints | worst | levels | data path | where the time goes |
|---|---|---|---|---|---|
| **DRAM port AR split**: `s1_rln` → every ready in the node | 1,110 | −0.791 | 13–16 | 3.84 = 1.26 logic + 2.57 route | the captured length runs through four carry chains in one cycle — `rd_span` add, `rd_mb` round, `ck_left` subtract, the `≤ AMB` compare — into `rd_push`, `rd_take`, the grant, and out to the mover's AR skid enable (48 CE pins), every engine's `arvalid`/`awvalid`, the interlink and the node port, the capture registers, `ck_done`, `rr_rd`, `g_req[].sel_*` |
| **RV64 instruction memory → decode** | 856 | −0.185 | 7–8 | 3.23 = 1.39 + 1.84 | the cascaded RAMB36 read (0.947 clock-to-out) → i-cache hold → decode of call/return and `rs1` → RAS push/pop → `ras_tos` (512), `redir_pend_pc` (256), `ras_sp`, `redir_pend`, `d_hold_v`, the RAS LUTRAM write pins |
| RV64 `wb_val` → `pc`, `d_valid` | 260 | −0.052 | 12 | 3.18 = 0.85 + 2.34 | writeback value through forwarding into the next PC |
| **Xache trunk return counters** `pb` → `pg`, `pb`, `pg` | 132 | −0.711 | 1 | 3.84 = 0.24 + 3.60 | `pb` on the receiving die, its increment/gray LUT (`g_pc[k].pg[j]_i_1`) pinned with the SENDING die by the constraint's `*_reg*` patterns, `pg` back on the receiving die: two die crossings on a 6-bit counter |
| **Reset into the interlink pipes** | 18 | −0.759 | 1 | 3.79 = 0.12 + 3.68 | the die's `proc_sys_reset` output (fan-out 131) → the one LUT1 inverter synthesis shares between `kts_pipe_bd`'s halves, pinned with the landing die inside `kts_cdc` → the sending half's `f_q`/`b_q` reset pins on the sending die: a reset that crosses the boundary twice |
| Xache write engine → the node's W queue | 18 | −0.208 | 11 | 2.97 = 0.58 + 2.39 | a chain head's valid through the home write engine's beat into the master's `wready`, the node's `wready`, the DRAM port's W-queue write |
| station 1 → node 1, and JTAG → station 1 | 24 | −0.158 / −0.020 | 10 / 17 | 2.92 / 4.40 | the NSU response ring's full flag into the node's DRAM read queue; the JTAG bridge's burst length through the NMU's credit compare into its request-queue write enable (bus clock) |
| XDMA (Xilinx IP) | 6 | −0.161 | 14–15 | 4.00 = 1.13 + 2.87 | the lite master's read FSM |

Failing endpoints by logic level: 150 at 1 level (the two crossing families),
856 at 7–8 (the RV64 decode), 1,110 at 12–16 (the AR split), 82 at 10–11 and
17. Under the margin but not failing, 51,296 endpoints sit at 6–10 levels;
the largest classes there are the Xache home take (`hv_q` → `word_q`, 512
endpoints at +0.028, 11 levels), the home write into the array (`b_val` →
`word_q`, 512 at +0.059) and the Xache response ids into the node's DRAM AW/W
queues (+0.000 … +0.076), the instruction memory into `e_imm` (+0.039), the
mover's descriptor kind (+0.027) and the JTAG bridge into the NMU's write
counters (+0.103, 19 levels).

The same families as RTL statements, in path order, from the node-by-node
report. Each line is one stage the path runs through in a single cycle. The
four sysnode families are as the v8t5 image built them; the tree now carries
the shape §5.9 measures, and the citations below are to that tree.

**AR split**: `s1_rln` captured at the take → the span add, the memory-beat
round, the beats-left subtract and the `≤ AMB` compare, all combinational on
the captured length → `src/kohakuaccel/sysnode/core/mag_dram_port.v:285`
`rd_push` → `:287` `rd_take` → `:819` `q_ready` →
`src/kohakuaccel/sysnode/core/mag.v:931` `m_arready = q_ready && ar_r` (and
`:930` for AW) → the endpoints: `mag.v:556` `mv_arready` →
`src/kohakuaccel/sysnode/mover/mm_mover.v:460` the AR skid →
`src/kohakuaccel/common/sb_skid.v:34` `out_en`, `:35` `hold_en`, `:64`
`hold_data` (48 CE pins), `:61` `out_data`; `mag.v:624` `h_arvalid` cleared
on `m_arready[UP]`; `mag.v:880` `cp_arready` into the node port;
`src/kohakuaccel/sysnode/interlink/mag_ilink.v:286` `loc_aw`; `mag.v:911`
`sel_h`/`sel_w`; and back inside the port, `mag_dram_port.v:305` `rd_gnt`,
`:315` the capture registers, the split's own counter, `rr_rd`, and the
arbiter's request vector. In the tree the split's state is four registers
(`:281`) loaded at the take from a per-requester beat count that is itself a
register beside the request vector (`:248`, `:296`) and stepped at each AR
(`:372`); `rd_push` reads one flag.

**RV64 instruction memory → decode**: the cascaded instruction RAM
(`src/kohakuaccel/pe/rv64-sys/rv64_sys_pe.v:229`, 0.947 ns to data) → the
D-word hold select → `src/kohakuaccel/pe/rv64-sys/core/rv64_core.v:252`
`d_word` → `:254` `d_call` / `:256` `d_ret` (decode of `rd`/`rs1` fields) →
`p_call`/`p_ret` into the predictor →
`src/kohakuaccel/pe/rv64-sys/core/rv64_bpred.v:189` the RAS LUTRAM write
enable and `:202`–`:203` `ras_tos`, `ras_sp`. The second branch of the same
start: `rv64_core.v:268` `d_predict` → `:629` `d_redir` → `:675` `pc_next`
→ `pc` and the pending-redirect target (256 endpoints); and `:333` `e_btgt
<= d_pc + imm_d`, the 64-bit add on the decoded immediate. In the tree the
RAM runs at read latency 2 with its output register enabled by the core's
fetch advance (`:123`, `rv64_sys_pe.v:232`), so decode starts from a
register (`FETCH_LAT` in [spec/parameters](../../spec/parameters.md)).

**RV64 writeback → next PC**: `rv64_core.v:981` `wb_val` → `:414`
`op_rs1_raw` (the forwarding select) → `:438` `op_rs1` → the 64-bit equality
and signed/unsigned compares → `:603` `br_take` → `:612` `taken` → `:625`
`e_redir` → the redirect priority mux → `pc_next` → `pc`, `d_valid`. The
64-bit compare sat between the forwarding mux and the redirect in one cycle.
In the tree the compare is four 16-bit chunks combined in two levels
(`:593`–`:598`), every E-level redirect is registered (`:177`, `:666`–`:675`)
and `pc_next` selects between registers.

**Xache trunk return counters**, `src/kohakuaxi/pxache/lane/kx_trunk.v:245`
`pb`/`pg`, `:246` `pnext`, `:247`–`:253` the counter on `m_clk`; the
increment/gray LUT that synthesis names `g_pc[k].pg[j]_i_1` is claimed by the
sending die because `scripts/tcl/v8t3/60_constraints.tcl:116` lists
`pb_reg*`/`pg_reg*`/`fok_m_reg*` only and `:127` gives the sender everything
else. The three patterns at `:116` drop `_reg`.

**Reset into the interlink pipes**: the crossing's `a_rst` and the sending
pipe half's `rst` in `xilinx-fpga/xcvu13p/bd/kts_pipe_bd.v` were one
`!rstn_tx` expression, so synthesis built one inverter and the crossing's
pblock took it to the landing die; the endpoints are
`src/kohakutransmit/carrier/kts_pipe.v:52` (the `f_q`/`b_q` reset branch)
and `src/kohakutransmit/carrier/kts_cdc.v:124` `o_crd_valid`. In the tree
each half inverts its own reset in a register of its own (`kts_pipe_bd.v:58`,
`:81`, `:98`, `:106`).

**Xache write engine → node W queue**, start
`src/kohakuaxi/pxache/kx_pxache.v:574` `hv_q` (a chain head) → `:1537`
`hq_w_v` → `src/kohakuaxi/xache/engine/kx_wr_engine.v:112` `src_wval` →
`:127` `wr_beat` → `:130` `mw_wrdy` → `kx_pxache.v:1251` `lw_h` → `:1258`
`x_wready` → `src/kohakuaxi/xache/edge/kx_link.v:29` (`SAME`: `s_ready =
m_ready`, a wire) → the node's `M_AXI_DRAM` wready →
`mag_dram_port.v:475` the W queue's `rd_en`. The register belongs at
`kx_pxache.v:1258`.

**Station 1 → node 1**, start `src/kohakuaccel/common/kohaku_aring.v:96`
`wr_busy` (the full flag from the crossed read pointer) →
`src/kohakuaccel/axi/station/sb_nsu.v:591` `m_rready` → the mesh's
`S_AXI_MEM` rready → `mag.v:608` `m_rready[UP]` → `mag_dram_port.v:665`
`r_take` → `:630` the R queue's head load. **JTAG → station 1**: the bridge's
burst length → `src/kohakuaccel/axi/station/sb_nmu.v:335`–`:339` the credit
and tag compares → `:343` `ar_go` → `:355` `req_push` → `:557` the request
queue's write enable, 17 levels on the 5 ns bus clock.

### 5.9 The sysnode alone at the shipped shape — three synthesis loops

`scripts/tcl/ooc_paths.tcl` on `ktpu_node_v8t` at the v8t5 generics
(`MESH_ID` 0, `ILINK` 1, `PORTS` 2, `L2_MAG_BANKS` 4, `L2_MAG_ENTRIES` 16384,
`DRAM_CDC` 0, `DRAM_AR_MAX` 16), out of context, a 2.857 ns clock on every
clock port with each port its own asynchronous group; the census is every
path under 0.25 ns of slack at that period, `levels.txt` the same paths by
logic level, `hier.rpt` the utilisation to depth 4. One run per loop, each
after the module benches of §10 pass on the loop's RTL. The LUT baseline is
the node's row in the v8t5 block-design synthesis
(`build/multimesh_v8t5_synth_util_hier.rpt`). A block-design synthesis sees
about 0.5 ns less slack than an out-of-context run of the same netlist
(§5.8), and route about 0.74 ns less than synthesis (§5.6).

| loop | RTL | LUT | FF | WNS at 2.857 | worst class | levels | data path | paths < 0.25 | classes | below zero at 3.333 |
|---|---|---|---|---|---|---|---|---|---|---|
| v8t5 node, BD synthesis row | as shipped | 23,580 | 34,289 | | | | | | | 523–582 endpoints a die (§5.8) |
| 1 | the AR split's state as registers stepped at each AR; the RV64 fetch word in the instruction RAM's output register (`FETCH_LAT` 2, both RAM enables from the core's advance, the predictor's tables and the I-cache at the same latency); `rleft_one` beside `rleft_z`; the mover's alignment test off the walker's register; one registered reset per pipe half | 23,792 | 34,397 | −0.993 | `rr_rd` → `ck_left` | 14 | 3.832 | 3,449 | 253 | 4 classes: the split's load from the arbiter's pick (3.686–3.832) and `wb_val` → `pc` (3.356) |
| 2 | the memory-beat count per requester off its own presentation, registered beside the request vector, so the pick only muxes; every E-level redirect (mispredict, trap, JALR check) registered and applied at the next fetch advance, its target in two registers picked by the registered compare | 23,623 | 34,510 | −0.693 | `wb_val` → the pend registers | 12 | 3.446 | 3,497 | 276 | 3 classes, one family: the pend registers' enable was the kill itself |
| 3 | the branch compare in four 16-bit chunks with a two-level combine; the pend enable from registers only; the misalign test per operand source on the low three bits, then one select | 23,620 | 34,512 | −0.301 | link VC FIFO → `b_owed` | 10 | 3.093 | 3,629 | 268 | none; the worst class has +0.175 |

Block RAM, URAM and DSP are the v8t5 row's in every loop: 62 RAMB36 and 8
RAMB18, 65 URAM, 47 DSP.

Selected instance rows from `hier.rpt` (the block-design report stops at the
node, so the sub-module rows have no shipped baseline):

| instance | module | loop 1 LUT | loop 2 LUT | loop 3 LUT | loop 3 FF |
|---|---|---|---|---|---|
| (top) | ktpu_node_v8t | 23,792 | 23,623 | 23,620 | 34,512 |
| u_mag/u_mag | mag | 7,496 | 7,511 | 7,511 | 16,489 |
| u_mag/u_mag/u_dram | mag_dram_port | 2,365 | 2,360 | 2,360 | 2,185 |
| u_mag/u_pe | rv64_mag_pe | 15,844 | 15,660 | 15,657 | 17,871 |
| u_mag/u_pe/u_cpu | rv64_syscore | 7,189 | 6,979 | 6,993 | 7,168 |
| u_mag/u_pe/u_cpu/u_core | rv64_core | 6,024 | 5,810 | 5,822 | 3,666 |
| u_mag/u_pe/u_cpu/u_ic | rv64_icache | 271 | 272 | 274 | 687 |
| u_mag/u_pe/u_mover | mm_mover | 4,168 | 4,177 | 4,177 | 5,791 |

Loop 3 by logic level (paths under 0.25 ns at 2.857): 24 at 5, 32 at 6, 525
at 8, 486 at 9, 2,403 at 10, 100 at 11, 23 at 12, 30 at 13, 4 at 14, one each
at 16 and 18.

Loop 3's classes below zero at 2.857 ns, the set a 350 MHz synthesis would
have to clear:

| family | worst | levels | data path | classes | paths |
|---|---|---|---|---|---|
| interlink: a link's VC FIFO (`kts_rx`, LUTRAM) read → `mag_ilink` `b_owed`, `ob_left`, `door_sent`, `a_r`; `mag_switch` `n_lblk` | −0.301 | 9–10 | 3.093 | 5 | 126 |
| interlink: the same FIFO's read → its own LUTRAM write pins | −0.207 | 8 | 2.650 | 10 | 20 |
| DRAM port: the R queue's FIFO read → `g_rdq[].rleft`, `rph`, `rleft_one`, `rleft_z` | −0.201 | 9–10 | 2.933 | 10 | 165 |
| mover: `mx_tdesc` `d_axis` / `psum` → `e_kind`, `e_flt`, `e_xp`, `e_rd`, `e_wr` | −0.195 | 12–13 | 3.034 | 6 | 61 |
| RV64: the scratchpad URAM → `wb_val` | −0.183 | 6 | 2.967 | 1 | 56 |
| RV64: `wb_val` → `m_val` (the ALU behind the forward) | −0.170 | 18 | 3.009 | 1 | 64 |
| RV64: `wb_val` → `u_mmu/resolved_q` | −0.136 | 13 | 2.975 | 1 | 1 |
| mover: `e_wr` → the completion FIFO's LUTRAM write pins | −0.097 | 10 | 2.752 | 32 | 32 |

The FETCH_LAT 2 fetch stage and the registered E-level redirect each add one
cycle to a redirect: a mispredicted branch, a trap or a failed JALR check now
loses four fetch slots.

### 5.10 v8t5 placed and routed

The implementation run's own reports (`impl_1/*_routed.rpt`,
`*_utilization_placed.rpt`, `runme.log`), the ship flow's routed reports in
`build/multimesh_v8t5_impl_*.rpt`, and two read-only opens of the routed
checkpoint: `scripts/tcl/v8t5_routed_paths.tcl` (the §5.8 census on the
routed design, margin 0.5 ns: 172,417 endpoints in 22,878 classes, 8,183 of
them failing, in `build/multimesh_v8t5_routed_*`) and
`scripts/tcl/v8t5_routed_nets.tcl` (`report_cdc`). Fully routed, 0 nets with
routing errors; hold met at +0.002 ns; pulse width met; DRC with no critical
warning.

| clock | period | WNS | TNS | failing | < 0.25 | < 0.50 |
|---|---|---|---|---|---|---|
| sysnode, die 0 | 3.333 | −1.025 | −7,129 | 20,642 | 30,977 | 41,911 |
| sysnode, die 1 | 3.333 | −1.023 | −5,210 | 15,448 | 26,232 | 39,109 |
| sysnode, die 2 | 3.333 | −0.790 | −5,505 | 18,191 | 29,083 | 40,001 |
| sysnode, die 3 | 3.333 | −0.850 | −3,654 | 13,392 | 22,716 | 32,718 |
| PCIe GT `TXOUTCLK[3]_3` (Xilinx IP) | 2.000 | −0.023 | −0.2 | 18 | 1,604 | 4,645 |
| MIG ui × 4 | 3.332 | +0.019 … +0.059 | 0 | 0 | 133 … 837 | 1,352 … 4,919 |
| XDMA | 4.000 | +0.016 | 0 | 0 | 507 | 2,226 |
| ctrl 2, ctrl 3 (bus) | 5.000 | +0.072, +0.120 | 0 | 0 | 197, 123 | 523, 548 |
| every other clock and every inter-clock pair | | positive | | | | |

WNS through the flow: −1.387 estimated after placement, −0.903 after
physical optimisation, −0.786 at the router's start, −1.647 after its first
global iteration, −1.080 after the fifth, −1.058 after hold fixing, −1.025
routed.

| die | LUT | % | FF | BRAM tiles | URAM | DSP | contents |
|---|---|---|---|---|---|---|---|
| SLR0 | 54,318 | 12.6 | 78,737 | 109.5 | 129 | 50 | mesh 0 (23,233 LUT), station 0 (3,591), ddr4_0 (20,076) |
| SLR1 | 117,693 | 27.2 | 148,062 | 186.5 | 129 | 50 | mesh 1 (23,665), station 1 (10,181), ddr4_1 (20,083), XDMA, debug hub |
| SLR2 | 57,005 | 13.2 | 87,681 | 117 | 129 | 50 | mesh 2 (23,532), station 2 (4,061), ddr4_2 (20,072) |
| SLR3 | 53,900 | 12.5 | 78,083 | 109.5 | 129 | 50 | mesh 3 (23,112), station 3 (3,567), ddr4_3 (20,075) |

Die crossings: 2,401 / 2,457 / 2,514 SLLs on the 0–1 / 1–2 / 2–3 boundaries
(10.4 … 10.9 % of 23,040 each), 7,372 in all, none through a Laguna TX or RX
register (at `IL_ASYNC` 1 the crossing is inside `kts_cdc`, at `PER_DIE_CLK`
1 no reset spans a die, so `60_constraints.tcl` emits no `USER_SLL_REG`). By
owner: the station bus 1,900 nets, the Xache 3,331, the block-design level
1,999 (of which 2 span dies 0–2, 36 span 0–3 and 5 span 1–3), the debug hub
51 (17 of them 1–3). The connectivity matrix counts 39 nets from die 1 to die
3 and 18 from 3 to 1.

Congestion. The placer's final report has three level-5 (32×32) windows: a
long-south one at CLEL_R_X68Y588–DSP_X82Y615 and short east/west ones at
CLEM_X72Y714–CLEL_R_X86Y760, each with the Xache (`u_kx`, 30–57 % of the
window's cells) and a DDR4 controller's calibration logic
(`u_ddr_cal_addr_decode`, `u_ddr_ui`) as the occupants. The router's initial
estimate: global/short level 5, timing level 5, eight level-5 windows, all
around `u_kx` with `ddr4_1`/`ddr4_2`/`ddr4_3` calibration, at X65–96 Y336–647
(dies 1 and 2). After rip-up the effective levels are north 1, south 4, east
2, west 2; the two CLB-congestion dumps hold four and two CLB tiles and no
net.

Routed classes merged across the four dies, the worst first
(`build/multimesh_v8t5_routed_classes_merged.txt`, 13,196 classes):

| slack | endpoints | levels | data path | logic | net | start → end |
|---|---|---|---|---|---|---|
| −1.025 | 4 | 7 | 3.646 | 0.782 | 2.864 | DRAM port R-queue FIFO (RAMB36) read → the same FIFO's `ENARDEN` |
| −1.023 | 704 + 672 + 352 + 352 + 320 + 336 | 10 | 4.16 | 0.87 | 3.29 | mover `wa_nxt` → `u_dst`/`u_src` `psum`, `apsum`, `idx` (CE and reset pins) |
| −1.006 | 62 + 82 | 14–15 | 4.11 | 1.97 | 2.14 | instruction RAM → `e_btgt` |
| −1.002 | 33 + 38 | 11 | 4.03 | 1.21 | 2.82 | mover `stat_fault` → CPU `pc`, `redir_pend_pc` |
| −0.985 | 54 + 49 | 10–11 | 4.30 | 0.98 | 3.33 | `e_s2_q` → `pc`, `redir_pend_pc` |
| −0.979 | 151 | 13 | 4.30 | 1.62 | 2.68 | mover `u_src/psum` → `e_rd` |
| −0.969 | 256 + 256 + 14 | 8 | 3.9–4.0 | 1.8–2.0 | 1.9–2.3 | instruction RAM → `ras_tos`, `ras_sp` |
| −0.953 | 2 + 2 + 288 + 6 | 10–11 | 4.0–4.2 | 1.1–1.3 | 2.9 | station response ring full flag → DRAM port R queue, `rleft`, `rleft_z` |
| −0.950 | 148 + 16 | 6–7 | 3.5–3.9 | 0.5–0.7 | 2.9–3.1 | Xache write engine, mover `w_kind` → DRAM port W queue |
| −0.920 | 256 | 6 | 3.92 | 1.62 | 2.31 | scratchpad URAM → `wb_val` |
| −0.908 | 352 + 176 + 176 | 7 | 3.68 | 0.72 | 2.96 | mover `occ` → `u_dst` `psum`, `apsum`, `idx` |
| −0.904 | 1,429 + 3,768 + 1,485 + 865 | 3–4 | 3.75–4.00 | 0.25–0.59 | 3.2–3.6 | mover `w_kind`, the mover's data FIFO → staging `wide_q` |
| −0.876 | 544 (die 0), 550, 433 | 6–9 | 3.8–4.0 | 0.5–0.9 | 3.0–3.4 | Xache chain head `hd_q[525]` → boundary trunk `u_tk` |
| −0.773 | 5,817 | 6 | 3.87 | 0.53 | 3.34 | Xache chain head `hd_q[525]` → the same `hd_q` register |
| −0.762 | 12 + 96 + 104 + 192 | 0 | 3.57–3.61 | 0.08 | 3.5 | node reset synchroniser `u_rs_mag/q` → mover `d_axis`, `d_count`, `d_astep`, `d_stride` |
| −0.712 | 5,885 | 0 | 3.83 | 0.08 | 3.75 | node reset synchroniser `u_rs_mag/q` → staging `wide_q` |
| −0.750 | 139 + 167 + 192 + 123 + 199 + 160 + 194 + 174 | 0 | 3.3–3.7 | 0.08 | 3.2–3.6 | staging `brow_q` → the banks' URAM `ADDR_A`/`ADDR_B` |
| −0.686 | 4,338 | 1 | 3.65 | 0.13 | 3.52 | Xache master `rq_dp` → reorder buffer LUTRAM read |
| −0.681 | 57 | 0 | 3.85 | 0.08 | 3.77 | DRAM port return register `rq_bus` → mover `ix_data` |
| −0.663 | 103 + 5 + 27 + 38 | 1 | 3.7–3.9 | 0.15–0.22 | 3.5–3.7 | CPU `mv_cfg_en` → mover `seed`, `xf_id`, `idx_count`, `d_base` |
| −0.644 | 75 + 86 + 72 + 84 | 0–1 | 3.2–3.5 | 0.08–0.29 | 3.1–3.2 | Xache home read engine `ra` → the home's URAM address pins |

By end owner merged across dies (endpoints under 0.5 ns / failing):
`mesh_N/u_mag/u_pe` 57,519 / 25,268; `mesh_N/u_mag/u_mag` 46,145 / 20,487;
Xache boundary trunks `u_tk` 13,009 / 5,670; Xache homes `u_c` 8,583 /
4,741; Xache chain heads `hd_q` 5,817 / 3,904; the station's NSU 4,918 /
1,400; Xache reorder buffers `u_rb` 4,338 / 1,515; Xache hedges 6,486 /
2,610. Failing endpoints by logic level: 6,477 at 0, 3,665 at 1, 1,656 at 2,
6,878 at 3, 5,461 at 4, 4,258 at 5, 10,088 at 6, 8,558 at 7, 5,917 at 8,
4,413 at 9, 7,263 at 10, 2,057 at 11, 1,001 at 12 and above.

The families above as RTL: what the v8t5 image built, and what the tree
carries (§5.11 measures it):

- **Staging dispatch register**. v8t5 loaded `arow_q`, `brow_q`, `bword_q`,
  `bstrb_q` and the `BANKS × WORDS × DATA_W`-bit `wide_q` in the `else`
  branch of the reset `if`, so the synchroniser's reset was every one of
  those bits' clock enable (the `u_rs_mag/q` → `wide_q` class, 5,885
  endpoints at 0 levels), and `brow_q`/`arow_q` were one register each on
  every bank's URAM address pins. In the tree
  `src/kohakuaccel/sysnode/core/mag_stage.v:151`–`:166` load them
  unconditionally and every one of them is a copy per bank (`:141`, `:162`),
  as `wide_q` was (`:135`).
- **Mover walkers' enables**. v8t5: `wa_ext`
  (`src/kohakuaccel/sysnode/mover/mm_mover.v:445`–`:450`, from `wa_nxt`,
  `wa_room` and a 40-bit compare) through the command FIFO's push into the
  walkers' `next`, ending on the CE and reset pins of `mx_tdesc`'s `psum`,
  `apsum`, `idx` (2,700 endpoints, 79 % route). In the tree each walker
  presents its element from a two-entry queue (`mx_tdesc` `OREG` 1,
  `src/kohakuaccel/sysnode/mover/mx_tdesc.v`), so `next` pops `AW + 3` flops
  and the walker steps on its own count.
- **Mover fault → PC**. `mm_mover` `stat_fault` →
  `src/kohakuaccel/sysnode/sysnode.v:543` (`irq_summary`, an OR) →
  `src/kohakuaccel/pe/rv64-sys/rv64_syscore.v:779` (`irq_ext`) → the core;
  v8t5 took it straight into `src/kohakuaccel/pe/rv64-sys/core/rv64_csr.v:149`,
  `:160`, `:165` (`mip`, `pend_ext`, `irq_pending`) →
  `src/kohakuaccel/pe/rv64-sys/core/rv64_core.v:799` (`irq_raw`) →
  `trap_take` → the redirect. In the tree both interrupt lines land in a
  register at the core's edge first (`rv64_core.v:125`–`:129`).
- **CPU → mover configuration**. `rv64_syscore.v:88` (`mv_cfg_en`, a
  register) → `mm_mover.v:681` (`cfg_en`): one level and 3.5 ns of net in
  v8t5; in the tree the write lands in `cfg_*_q` (`mm_mover.v:186`–`:194`)
  first, and `stat_busy` covers the cycle through `go`.
- **DRAM port R queue**. v8t5 read the block-RAM `sync_fifo`
  (`src/kohakuaccel/sysnode/core/mag_dram_port.v:626`, first-word
  fall-through) into the return-side compares and back into its own enable:
  1.075 ns on the enable net to eight RAMB36 alone. In the tree the queue's
  head is a register (`hd_*`, `:638`–`:674`) refilled as it leaves.
- **Xache**. `src/kohakuaxi/pxache/kx_pxache.v:575` (`hd_q`, the chain
  head): the destination decoded from its own top bits is its own enable,
  into the boundary trunk and back into the head's 526 bits; `rq_dp` (the
  drain pointer) addresses the reorder ring's LUTRAM as one copy, the home
  read engine's `ra` (`src/kohakuaxi/xache/engine/kx_rd_pipe.v`) the home's
  URAM banks, and the array's `flushing`
  (`src/kohakuaxi/xache/array/kx_carray.v`) the data-in muxes. All four stay
  as in v8t5: §5.11 measures the register and the replications out of
  context and every variant puts a level into the home's response-ready
  cone, so these are a placement-level loop, not an RTL one.

Methodology: TIMING-54 (a scoped clock group inside the PCIe GT wizard,
Xilinx's), TIMING-47 and XDCB-3 (`build/multimesh_v8t5_clocks.xdc:23`–`:24`
put `xdma_0_axi_aclk` in its own group and, through
`-include_generated_clocks pcie_refclk`, in the reference clock's group),
TIMING-24 (the same command's group between ctrl `clk_out1` and `clk_out2`
overrides an XPM `set_max_delay -datapath_only` at constraint position 501),
TIMING-16 117 (all in the sysnode families above), LUTAR-1 16 (the debug
hub's FIFO resets and the DDR4 PHY PLL resets), TIMING-9 and TIMING-10 one
each. `report_cdc`: every Critical row (1,006 CDC-1, 8 CDC-4, 6 CDC-7, 3
CDC-10, 2 CDC-11, 4 CDC-13, 2 CDC-14) is inside the PCIe/XDMA IP under its
own clock groups; the design's own rows are 88 CDC-2 and 88 CDC-5, all the
station bus's `kohaku_aring` lean-ring gray-pointer synchronisers
(`wg_s1`, `rg_s1`, `rd_r1` in `src/kohakuaccel/common/kohaku_aring.v`)
without `ASYNC_REG`, and 16,772 CDC-15 clock-enable-controlled structures.

### 5.11 v8t6 — the routed-v8t5 fixes, out of context

The RTL of §5.10's list as the v8t6 build carries it, measured the same way
as §5.9 (`ooc_paths.tcl`, 2.857 ns, every clock its own group) against the
loop-3 node and the v8t5 Xache tier (`build/ooc/kxlive_t4h_350`). Benches:
33 runs, all PASS — the DRAM port at `AR_MAX` 0 and 16 and the read-only
and bandwidth variants, the staging store, the mover's eleven (chains of
1, 2 and 4, config, L2, transform), the system set (mag_system,
mag_1m_upload, mag_wslot, mm_mesh_1m, interlink 2-mesh, stage and 4-mesh
with the pipe carrier), the Xache four, the station line and the kts_cdc.

| block | run | LUT | FF | WNS at 2.857 | worst class | paths < 0.25 | classes |
|---|---|---|---|---|---|---|---|
| node, loop 3 (§5.9) | `node_v8t5_fix3` | 23,620 | 34,512 | −0.301 | link VC FIFO → `b_owed`, 10 levels, 3.093 | 3,629 | 268 |
| node, walkers with a muxed start state | `node_v8t6` | 24,020 | 35,462 | −0.301 | the same | 1,985 | 230 |
| node, as v8t6 builds it | `node_v8t6b` | 23,837 | 35,470 | −0.301 | the same | 2,014 | 230 |
| Xache, v8t5 tier | `kxlive_t4h_350` | 23,055 | | +0.064 | `rid` → the master's slot counters, 9 levels, 2.676 | 1,627 | 23 |
| Xache, head decode registered and replicated, `rq_dp` and `flushing` replicated | `kxlive_v8t6` | 23,143 | 48,072 | −0.075 | `rid` → the home's `word_q` enable, 10 levels, 2.828 | 1,612 | 13 |
| Xache, head decode registered, `rq_dp` and `ra` replicated | `kxlive_v8t6b` | 22,905 | | −0.258 | the same, 11 levels, 3.011 | 4,000 | 54 |
| Xache, head decode registered and replicated only | `kxlive_v8t6c` | 22,755 | 47,935 | −0.143 | the same, 10 levels, 2.896 | 3,388 | 39 |

The v8t6 node against loop 3: the two walker queues +196 LUT (1,014 and
1,020 against 909 and 929), the mover's configuration register and start
state +33, the staging +186 FF (the per-bank copies), the DRAM port +516 FF
(the head register at the 512-bit memory beat), the walkers +172 FF, the
configuration register +73 FF; block RAM, URAM and DSP unchanged; the worst
class and slack unchanged. The Xache in v8t6 is the v8t5 netlist with the
`ASYNC_REG` marks only.

### 5.12 v8t6 placed and routed

The same sources as §5.10 for the v8t6 project (`impl_1/*_routed.rpt`,
`runme.log`, `build/multimesh_v8t6_impl_*.rpt`) and the routed census
`scripts/tcl/v8t6_routed_paths.tcl` (margin 0.5 ns: 149,500 endpoints in
20,236 classes, 6,749 of them failing, `build/multimesh_v8t6_routed_*`),
which now sets every merged class against the v8t5 census as well
(`scripts/tcl/v8t3/87_synth_paths.tcl` `V8_BASE`, the result in
`build/multimesh_v8t6_routed_classes_diff.txt`). Fully routed, 0 nets with
routing errors; hold met at +0.002 ns; pulse width met; DRC with no critical
warning; the bitstream 113,318,504 bytes.

| clock | period | WNS | TNS | failing | < 0.25 | < 0.50 | v8t5 WNS / TNS / failing |
|---|---|---|---|---|---|---|---|
| sysnode, die 0 | 3.333 | −0.453 | −686 | 4,925 | 13,981 | 25,951 | −1.025 / −7,129 / 20,642 |
| sysnode, die 1 | 3.333 | −1.080 | −6,917 | 21,993 | 33,011 | 43,913 | −1.023 / −5,210 / 15,448 |
| sysnode, die 2 | 3.333 | −0.702 | −3,471 | 14,450 | 24,629 | 36,421 | −0.790 / −5,505 / 18,191 |
| sysnode, die 3 | 3.333 | −0.406 | −550 | 4,070 | 12,316 | 21,904 | −0.850 / −3,654 / 13,392 |
| XDMA | 4.000 | −0.069 | −13 | 375 | 1,232 | 3,174 | +0.016 / 0 / 0 |
| MIG ui × 4 | 3.332 | −0.017 … +0.058 | −0.02 | 1 + 1 | 146 … 759 | 1,551 … 3,707 | +0.019 … +0.059 / 0 / 0 |
| PCIe GT `TXOUTCLK[3]_3` (Xilinx IP) | 2.000 | +0.004 | 0 | 0 | 1,289 | 4,890 | −0.023 / −0.2 / 18 |
| ctrl 2, ctrl 3 (bus) | 5.000 | +0.457, +0.029 | 0 | 0 | 0, 193 | 3, 1,295 | +0.072, +0.120 |
| every other clock and every inter-clock pair | | positive | | | | | positive |
| the design | | −1.080 | −11,637 | 45,815 | | | −1.025 / −21,499 / 67,691 |

WNS / TNS through the flow (v8t5 in brackets): −1.498 / −3,842 estimated
after placement (−1.387 / −16,831), −0.879 / −3,449 after physical
optimisation (−0.903 / −13,553), −0.809 / −953 at the router's start
(−0.786 / −5,445), −1.748 / −18,285 after its first global iteration
(−1.647 / −26,484), −1.119 / −12,888 after its eighth and last (v8t5:
sixth, −1.080 / −22,809), −1.105 / −12,561 after hold fixing (−1.058 /
−22,509), −1.080 / −11,637 routed.

| die | LUT | % | FF | BRAM tiles | URAM | DSP | contents |
|---|---|---|---|---|---|---|---|
| SLR0 | 54,354 | 12.6 | 79,067 | 109.5 | 129 | 50 | mesh 0 (23,496 LUT), station 0 (3,588), ddr4_0 (20,104) |
| SLR1 | 118,503 | 27.4 | 150,055 | 186.5 | 129 | 50 | mesh 1 (24,217), station 1 (10,202), ddr4_1 (20,076), XDMA (53,377), debug hub |
| SLR2 | 57,646 | 13.3 | 89,200 | 117 | 129 | 50 | mesh 2 (24,149), station 2 (4,063), ddr4_2 (20,085) |
| SLR3 | 54,056 | 12.5 | 79,281 | 109.5 | 129 | 50 | mesh 3 (23,279), station 3 (3,568), ddr4_3 (20,071) |

Placed in all: 284,539 LUT (+1,626 on v8t5), 397,521 FF (+4,974), 522.5
block RAM tiles, 516 URAM, 3,565 CARRY8, the same memories and DSPs. The
meshes are +263 / +552 / +617 / +167 LUT on their v8t5 selves (the walkers'
queues and the configuration register in every one, then the placer's
physical synthesis pushing block-RAM output registers into the fabric on
different dies: 28 nets and 550 cells this time, 3 and 209 in v8t5); the
Xache 22,585 LUT / 47,917 FF / 256 URAM.

Die crossings: 7,358 nets (v8t5 7,355): 2,462 touch die 0, 4,891 die 1,
4,919 die 2, 2,533 die 3. By owner the six Xache trunks 573–575 each, the
station links 132–373, the six interlink pipes 298 each, the block-design
level 1,996 (36 span dies 0–3, 6 span 1–3, 1 spans 0–2), the debug hub 51.
No Laguna register, as in v8t5, and no die-crossing path among the 400
worst.

Congestion. The placer's final report has eight level-5 windows in one
place, the top of die 2 at X73–93 Y560–600, occupied by ddr4_2's
calibration (`u_ddr_cal_addr_decode` 18–23 %, `u_ddr_mc_pi` 12–17 %) and
`u_kx` (13–27 %); v8t5 had three, on dies 1 and 2. The router's initial
estimate: global/short level 5 (32×32) as in v8t5, timing level 6 (64×64;
v8t5 level 5), eighteen windows: die 2's top around `u_kx` and ddr4_2
(X80–103 Y546–602), die 1 around `u_kx` and ddr4_1 (X83–106 Y346–433), a
level-6 long-north window on die 0 (X68–99 Y75–138: mesh 0's `u_mag` 29 %,
ddr4_0's calibration 25 %), die 0's mover and DRAM port (X66–87 Y90–153),
die 1's mover, L2 and DRAM port (X72–103 Y444–475), die 2's CPU core with
`u_mag` and `u_kx` (X91–106 Y610–665). Per die, the router's initial
global / long / short estimate (north; south): SLR0 32×32 2.11 % / 32×32
3.98 % / 16×16 0.99 %; 8×8 / 16×16 / 8×8 (v8t5 8×8 / 4×4 / 8×8; 4×4 / 8×8 /
8×8); SLR1 16×16 2.04 % / 32×32 4.20 % / 8×8 1.52 %; 16×16 2.12 % / 32×32
5.18 % / 16×16 1.44 % (v8t5 8×8 / 16×16 / 16×16; 8×8 / 32×32 / 4×4); SLR2
32×32 2.98 % / 64×64 4.62 % / 32×32 1.87 %; 32×32 2.64 % / 32×32 4.28 % /
16×16 1.64 % (v8t5 16×16 / 32×32 / 16×16; 32×32 / 32×32 / 16×16); SLR3 4×4
/ 8×8 / 4×4; 8×8 / 8×8 / 4×4 (v8t5 8×8 / 16×16 / 4×4; 16×16 / 16×16 /
16×16). After rip-up the effective levels are north 3, south 4, east 2,
west 3 (v8t5 1 / 4 / 2 / 2) at 87.6 / 87.0 / 88.5 / 86.8 % maximum
utilisation (91.1 / 86.0 / 91.3 / 90.1), the hotspots at X82–101 Y462–587;
global routing 10.7 % vertical, 9.9 % horizontal; 42 pins with tight setup
and hold (v8t5 9), all in the PCIe IP's asynchronous FIFO.

Failing endpoints by logic level (v8t5 in brackets): 1,516 at 0 (6,477),
2,208 at 1 (3,665), 1,215 at 2 (1,656), 2,426 at 3 (6,878), 5,351 at 4
(5,461), 4,415 at 5 (4,258), 4,000 at 6 (10,088), 6,623 at 7 (8,558), 9,404
at 8 (5,917), 4,632 at 9 (4,413), 3,121 at 10 (7,263), 545 at 11 (2,057),
359 at 12 and above (1,001). By end owner merged across dies (endpoints
under 0.5 ns / failing): `mesh_N/u_mag/u_pe` 52,025 / 18,394 (v8t5 57,519 /
25,268); `mesh_N/u_mag/u_mag` 34,637 / 9,827 (46,145 / 20,487); Xache
trunks `u_tk` 12,078 / 5,100 (13,009 / 5,670); Xache homes `u_c` 7,192 /
3,583 (8,583 / 4,741); Xache chain heads `hd_q` 5,722 / 3,487 (5,817 /
3,904); the station's NSU 4,747 / 2,188 (4,918 / 1,400); Xache hedges 4,326
/ 1,264 (6,486 / 2,610); Xache reorder buffers `u_rb` 2,013 / 162 (4,338 /
1,515).

Routed classes merged across the four dies, the worst first
(`build/multimesh_v8t6_routed_classes_merged.txt`; the die-1 copy of a class
is named where it alone carries the slack):

| slack | endpoints | levels | data path | logic | net | start → end |
|---|---|---|---|---|---|---|
| −1.080 | 35 | 10 | 4.09 | 1.07 | 3.02 | die 1: register-file bank read register → forward select → `op1_h` → `m_val` (v8t5 −0.72 / −0.56; the other dies under −0.72) |
| −1.077 … −0.995 | 7 + 13 + 2 + 12 + 5 + 3 + 1 + 12 | 7–12 | 4.0–4.4 | 0.9–1.3 | 2.8–3.5 | die 1: `e_csr_addr`, `m_val`, `w_val_q`, `op_held`, `wb_val`, `z2` → `m_val`, `mtvec_nz`, `r_taken` |
| −1.020 | 704 (300 failing) | 8 | 3.86 | | | walker `u_src` `d_count` → `apsum` (die 1; die 3 at −0.4) |
| −1.008, −0.974 | 148 + 150 | 12–13 | 4.02 | | | walker `psum` → the element queue's `q_e0` (the address adder) |
| −0.998 … −0.55 | 493 + 509 + 499 + 492 + 308 + 319 + 325 + 436 + 250 + 311 + 313 | 9–10 | 4.0 | | | `e_amo_op` → the CSR file (`mepc`, `minstret`, `stval`, `mtval`, `mtvec`, `stvec`, `mscratch`, `sepc`, `mcycle`, `mtimecmp`, `sscratch`); v8t5 −0.44 … −0.63 with 2–33 endpoints each |
| −0.988 … −0.80 | 24 + 80 + 80 + 120 + 116 + 103 | 8–9 | 3.8–3.9 | | | mover `xb_cnt` → the command FIFO's LUTRAM, `ar_addr_i`, `e_rd`, `e_wr`, `ra_nxt`, `ra_base` |
| −0.977 | 255 | 6 | 3.96 | | | scratchpad URAM → `wb_val` (v8t5 −0.920) |
| −0.955, −0.713 | 47 + 512 | 11 | 3.98 | | | Xache head destination decode → DRAM port AR-queue read register, → home `word_q` |
| −0.929, −0.907 | 1,408 (824) + 704 (468) | 8 | 3.85 | | | walker `u_dst` `d_count` → `psum`, `apsum` |
| −0.928, −0.708 | 144 + 144 | 10 | 3.90 | | | DRAM port `rd_busy` → the mover's AR skid |
| −0.916, −0.833 | 523 + 1,044 | 8 | 4.01 | | | DRAM port AR-queue empty flag → Xache trunk ring, chain head (v8t5 −0.724 / 628, −0.613 / 854) |
| −0.868, −0.891 | 80 + 52 | 8 | 3.83 | | | DRAM port `wr_req_r` → interlink AW skid |
| −0.857 | 312 (310) | 4 | 4.00 | 0.50 | 3.50 | station NSU request ring `o_v` → `rd_data` (v8t5 −0.556, 83) |
| −0.851, −0.825 | 2,101 (1,614) + 2,099 (1,418) | 6 | 3.87 | 0.53 | 3.34 | Xache chain head `hd_q[525]` → trunk ring, → its own `hd_q` (v8t5 −0.876 / 3,068, −0.773 / 2,788) |
| −0.844 … −0.294 | 80 + 248 + 256 + 224 + 70 + 50 + 256 | | | | | DRAM port `sr_v` → mover `m_awaddr`, interlink `d_r`, W skid, `pr_ctr`, command FIFO, NSU write queue |
| −0.823 | 2,345 (606) | | | | | transform `s_id` → quantiser `src` (v8t5 −0.589) |
| −0.673 | 255 | | | | | node reset synchroniser `u_rs_mag/q` → network port `cp_wdata` |
| −0.649, −0.602, −0.460 | 1,046 + 1,044 + 502 | | | | | DRAM port AR-queue output register → Xache trunk ring, chain head, master queue |
| −0.643 | 252 | | | | | system reset → DRAM port `rleft` |
| −0.643 | 765 (297) | | | | | agent `noc_out_data` → interlink `dbell_n` (v8t5 −0.250) |
| −0.581 … −0.250 | 2,509 (646) + 1,058 (130) + 407 (40) + 753 (108) + 113 (12) | 3–4 | | | | mover `w_kind`, the data FIFO → staging `wide_q` (v8t5 −0.90, 7,547 endpoints, 4,000 failing) |
| −0.511 | 512 (508) | | | | | Xache chain head → home `word_q` (v8t5 −0.264, 448) |
| −0.453 | | 10 | 3.61 | 0.79 | 2.82 | die 0's worst: `host_b_reg` replica → DRAM port R-queue `REGCE` (the head register's load) |
| −0.406 | 1,408 (870) | 8 | | | | walker `u_src` `d_count` → `psum` (v8t5 −0.759, 32) |

Against v8t5 (`*_classes_diff.txt`): gone are the R-queue's own enable
(−1.025), `wa_nxt` → the walkers (−1.02, 2,736 endpoints), `stat_fault` →
`pc` (−1.002, 71), `e_s2_q` → `pc` (−0.985, 103), `occ` → the walkers
(−0.908, 528), the response ring's full flag → `rleft` (−0.940, 288),
`u_wsel` → the W queue's write enables (−0.948), `wb_val` → `resolved_q`
and `pc` (−0.947), `u_rs_mag/q` → `wide_q` (−0.712, 5,885) and `brow_q` →
the URAM address pins. The reset synchroniser's other enables moved to
+0.494 (`d_count`, v8t5 −0.721), +0.280 (`if_resp_data`, −0.436), −0.046
(`dbell_n`, −0.555), −0.043 (quantiser `src`, −0.390) and −0.493
(`ix_data`, −0.133). The walker's enable moved from `wa_nxt` to its own
`d_count` compare (−1.02 on die 1, −0.41 on die 3) and its address adder
from `psum` → `e_rd` (−0.979, 151) to `psum` → `q_e0` (−1.008, 148).
Worse: the CSR-write cone (−0.44 → −1.00), die 1's register-file forward
(−0.72 → −1.08), head → `word_q` (−0.264 → −0.511), the NSU request ring
(−0.556 → −0.857), `s_id` → `src` (−0.589 → −0.823), `noc_out_data` →
`dbell_n` (−0.250 → −0.643). The by-class picture is what the per-die table
says: dies 0, 2 and 3 lost 90 %, 37 % and 85 % of their TNS; die 1, the
die with the XDMA, the debug hub and the 10,202-LUT station, lost 33 % more
and holds the WNS in its CPU.

The families as RTL:

- **Register-file forward into the M register.**
  `src/kohakuaccel/pe/rv64-sys/core/rv64_core.v:425`–`:438` (`op1_h`,
  `op_rs1`), `:705` (`lo_h`), `:949` (`m_val <= e_result`): the bank's read
  register, the forward select, the 110-fanout operand, the ALU and the
  result select, 10 levels at 1.07 ns of logic. On die 1 it routes with
  3.02 ns of net (v8t5 2.6–2.75, the other dies inside 3.333), the CPU
  spread over X111–129 Y339–356. A placement item first (the die's mesh
  beside the XDMA and station 1), a level out of the forward path second.
- **CSR writes.** `rv64_core.v:315`–`:352` (`e_amo_op`), `:503`–`:518`, and
  `src/kohakuaccel/pe/rv64-sys/core/rv64_csr.v:30` (`wr_en`), `:359`,
  `:405`: the write data and enable of every CSR from the E-stage decode,
  9–10 levels into 64-bit registers on every die.
- **Walkers.** `src/kohakuaccel/sysnode/mover/mx_tdesc.v:142`–`:156`
  (`at_max`, the innermost-first carry), `:184`–`:199` (`psum`, `apsum` on
  the wrap), `:225`–`:234` (`w_addr = d_base + off_sum`, the sum of every
  dimension's partial sum), `:341`–`:342` (`q_e0v`, `step`): the count
  compare chain into the partial sums' enables (8 levels) and the address
  adder into the queue entry (12–13 levels). The queue took the mover's
  compare out of the enable; the walker's own chain and adder are the
  depth that is left.
- **Mover burst counter.** `src/kohakuaccel/sysnode/mover/mm_mover.v:216`–`:224`
  (`xb_cnt`, `ent_first`, `ent_last`: 16-bit compares), `:887`: the
  compares feed the address and enable registers and the command FIFO's
  write, 8–9 levels.
- **DRAM port controls.** `src/kohakuaccel/sysnode/core/mag_dram_port.v:166`
  (`rd_busy`), `:252` (`rd_req`), `:759`; `:194` (`sr_v`), `:570`
  (`sr_go`), `:583`–`:600`; `:747` (`rleft` under reset): `rd_busy` gates
  the mover's AR skid 10 levels away, `sr_v` fans out to the mover, the
  interlink's skids and the station's write queue, and the system reset
  reaches 252 `rleft` bits as their enable.
- **Network port.** `src/kohakuaccel/pe/rv64-sys/core/rv64_nport.v:160`–`:171`
  (`cp_wdata`): the node reset synchroniser is the enable of 255 bits.
- **Interlink.** `src/kohakuaccel/sysnode/interlink/mag_ilink.v:316` (`d_r`),
  `:543`–`:563` (`ltx_dat`), `src/kohakuaccel/sysnode/interlink/il_pkt_arb.v:146`
  (`sel_r`): the switch's select and the port's data register feed the
  link's transmit register.
- **Xache.** `src/kohakuaxi/xache/array/kx_carray.v:198`–`:201` (`word_q`):
  the chain head's destination decode into the home's word register, and
  the head into the trunk and itself, as in §5.10 with the same netlist.
- **XDMA.** The XDMA's output register (hidden inside the IP) → station 1's
  manager NMU request ring, the LUTRAM write enable, 7 levels at 4.000 ns
  (0.850 logic, 2.927 net). The NMU's accept path has no register between
  the XDMA and the ring's write: a skid at the NMU or an AXI register slice
  on the XDMA master in the block design.

Where the blocks landed, per die (`scripts/tcl/routed_bbox.tcl` on both
routed checkpoints, `build/multimesh_v8t6_routed_bbox.txt` and the v8t5
file: every block's primitive cells, slice bounding box, 10–90 % slice
band and clock regions; `*_routed_clock_roots.rpt` the roots). The four
DDR4 controllers sit in slice columns X117–145, clock-region column X4, on
every die, 54,000 cells each, fixed by their pins.

| die | CPU core, v8t6 (10–90 % band; regions) | CPU core, v8t5 | mesh median, v8t6 / v8t5 | clock root, v8t6 / v8t5 |
|---|---|---|---|---|
| 0 | X71–89 Y123–162; one region, X2Y2 97 % | X96–115 Y106–137; X3Y1 46 %, X3Y2 44 % | X94 Y121 / X130 Y133 | X3Y3 / X3Y3 |
| 1 | X101–141 Y320–373; four regions across the DDR column, X4Y5 43 %, X3Y5 27 %, X3Y6 23 % | X102–137 Y265–283; X4Y4 53 %, X3Y4 45 % | X149 Y429 / X107 Y285 | X3Y5 / X4Y6 |
| 2 | X131–156 Y646–675; four regions across the DDR column, X4Y11 33 %, X4Y10 32 %, X5Y10 23 %, X5Y11 11 % | X151–173 Y592–626; X5Y10 71 %, X5Y9 25 % | X165 Y640 / X155 Y560 | X4Y9 / X4Y9 |
| 3 | X176–206 Y796–817; X6Y13 80 % | X150–198 Y782–810; X5Y13 67 %, X6Y13 28 % | X170 Y813 / X167 Y792 | X3Y12 / X5Y12 |

The failing endpoints per die, mean clock skew and mean net delay (v8t6 /
v8t5): die 0 −0.118 / −0.236 ns and 2.51 / 2.71 ns; die 1 −0.131 / −0.137
and 2.66 / 2.60, on its worst thousand −0.228 / −0.048; die 2 −0.138 /
−0.130 and 2.55 / 2.69; die 3 −0.176 / −0.102 and 2.27 / 2.62. Die 1's
failing endpoints by owner (v8t5): the CPU and its mover 9,565 (6,819), the
MAG 4,166 (4,865), the Xache 6,856 (3,405), the station 1,370 (277); die 2:
4,564 (5,834), 2,916 (4,819), 6,434 (7,229), 449 (307).

Clocks: every die's sysnode clock is its own MMCM and BUFG inside the die
(MMCM_X0Y1, X0Y5, X0Y9, X0Y15), all four driven by one reference BUFG on
die 1 (BUFGCE_X0Y119, pin AY23) over 3.03–3.54 ns of uncompensated route.
That route sets each die's phase, which the dies' mutual asynchrony absorbs
(one clock group per die, `IL_ASYNC` 1); the skew inside a die comes from
its root and the regions a path spans. Station 1 is 25,874 cells against
10,900 on the other dies and shares regions X4Y5 and X5Y5 with die 1's
CPU; the compute-half pblock knob (`CMP_COLS` in
`scripts/tcl/v8t3/60_constraints.tcl:56`–`:68`, a narrower pblock per die
holding the node, its station and its Xache partition) is unset in this
build.

The die crossings and the interlink's two ports, v8t6 (10–90 % slice
bands; the pipe halves sit in the clock-region rows the pblocks give them,
the bottom and top rows of a die): a middle die's interlink sits at the die's
centre with one port reaching each edge, an end die's has one port to reach.

| die | interlink switch | port 0 | port 1 | own pipe halves |
|---|---|---|---|---|
| 0 | X89–111 Y144–188, region X3Y2 / X2Y2 | Y146–175 | Y157–201 (77 rows in all) | top row X2Y3 / X3Y3 |
| 1 | X93–120 Y414–449, region X3Y7 | Y349–436 (127 rows in all, down to the die-0 side) | Y412–452 | bottom row X2Y5 / X3Y5, top row X3Y7 |
| 2 | X145–170 Y605–634, region X5Y10 | Y533–632 (123 rows, 228 cells in the bottom row X4Y8) | Y610–679 (222 cells in the top row X5Y11) | bottom row X4Y8, top row X5Y11 / X4Y11 |
| 3 | X146–170 Y778–801, region X5Y13 | Y777–803 | Y780–802 | bottom row X4Y11 / X5Y11 (in die 2) and X5Y12 |

What that costs is in the interlink classes: the switch select and the
DRAM port's `sr_v` into the ports' transmit registers, −0.447 (257
endpoints) and −0.666 (248), 10 % of a middle die's failing set at most;
no pipe or link class is worse than −0.189. The mesh's 10–90 % row span is
84 / 133 / 93 / 75 rows on dies 0 to 3.

Methodology: TIMING-54 1, TIMING-47 28 (v8t5 32; XDCB-3 is gone, the XDMA
clock in one group), TIMING-24 100, TIMING-16 57 (117, every one of them
on die 1), LUTAR-1 16, TIMING-9 and TIMING-10 one each.

### 5.13 v8t7 — the placement loop: three pipe stages and a compute box per die

`multimesh_v8t7` is v8t6's RTL and tiers with two placement knobs
(`scripts/tcl/v8t7/00_config.tcl`): `IL_STAGES` 3, three register stages in
each half of every interlink pipe carrier (`xilinx-fpga/xcvu13p/bd/kts_pipe_bd.v`
`STAGES`, one in every earlier image), and `CMP_COLS`, a compute-half pblock
per die holding the node, its station and its Xache master: die 0 in
clock-region columns X0–X3, dies 1–3 in X5–X7, on the side of the DDR4
column (X4) where each node already sat. The Xache homes and hedges stay
die-wide (`CMP_HOME` empty): a die half holds 128 URAM
(`scripts/tcl/part_regions.tcl`, `build/xcvu13p_regions.txt`) and a node's 65
plus a home's 64 are 129. Benches: the carrier at 1 and 3 stages (25,997 and
25,752 checks), the 4-mesh interlink with the 3-stage carrier and with wires
(16 checks each). Analysis gate as v8t5: one hoisted Xache leaf with no
pinned neighbour, and 24 ring synchronisers reset from the writing side
(v8t5 11); the analyser now pins a leaf whose neighbours split between a
die's box and its die pblock to the die.

| clock | period | WNS | TNS | failing | v8t6 WNS / TNS / failing |
|---|---|---|---|---|---|
| sysnode, die 0 | 3.333 | −0.623 | −1,055 | 5,967 | −0.453 / −686 / 4,925 |
| sysnode, die 1 | 3.333 | −0.888 | −2,853 | 11,129 | −1.080 / −6,917 / 21,993 |
| sysnode, die 2 | 3.333 | −0.634 | −954 | 5,637 | −0.702 / −3,471 / 14,450 |
| sysnode, die 3 | 3.333 | −0.691 | −1,775 | 7,751 | −0.406 / −550 / 4,070 |
| XDMA | 4.000 | −0.227 | −121 | 947 | −0.069 / −13 / 375 |
| MIG ui 1, 3 (Xilinx IP) | 3.332 | −0.226, −0.226 | −34, −27 | 406, 400 | −0.017, +0.058 |
| MIG ui 0, 2 | 3.332 | −0.016, −0.081 | −0.04, −1.0 | 4, 17 | −0.001, +0.014 |
| PCIe GT, ctrl clocks, every inter-clock pair | | positive | | | positive |
| the design | | −0.888 | −6,819 | 32,258 | −1.080 / −11,637 / 45,815 |

Hold met at 0.000 ns, 0 nets with routing errors, no die-crossing path
among the 400 worst, no TIMING-16 row left (57 in v8t6). WNS / TNS through
the flow: −0.924 / −1,934 estimated after placement (v8t6 −1.498 /
−3,842), −0.359 / −920 after physical optimisation (−0.879 / −3,449),
−0.317 / −48 at the router's start (−0.809 / −953), −1.140 / −8,437 after
its first global iteration, −0.943 after its third and last (v8t6 eight),
−0.934 / −7,425 after hold fixing, −0.888 / −6,819 routed. Effective
congestion after rip-up north 2, south 2, east 1, west 1 (v8t6 3 / 4 / 2 /
3); the router's initial long-route pressure moved into the boxes: die 1
south 8.7 % of tiles (5.2), die 3 south 5.5 (1.4), die 0 north 3.3 (4.0).
Placed: 285,658 LUT (+1,119), 404,291 FF (+6,770; the pipes 14,064 FF
against 6,936), memories unchanged, 7,355 die crossings.

The slack distribution of the sysnode clocks' failing endpoints, by 0.1 ns
bin from the worst: 61 below −0.8 (all on die 1), 283 in −0.8 … −0.7, 676,
1,515, 2,542, 3,403, 5,227, 7,324 and 9,453 in the last bin above −0.1;
22,004 of the 32,258 are above −0.3. Within 0.1 ns of each die's own WNS:
78 / 65 / 93 / 223 endpoints; within 0.2 ns: 395 / 405 / 281 / 729. v8t6
had 57 endpoints below −1.0 and 807 below −0.8. Failing endpoints by logic
level: 474 at 0 (v8t6 1,516), 137 at 1 (2,208), 289 at 2, 1,334 at 3, 3,738
at 4, 3,008 at 5, 2,908 at 6, 4,854 at 7, 6,717 at 8, 3,609 at 9, 4,168 at
10, 1,022 at 11 and above. Mean clock skew of the failing endpoints per die
−0.078 / −0.165 / −0.178 / −0.134 (v8t6 −0.118 / −0.131 / −0.138 / −0.176);
die 1's worst thousand −0.242, now on its Xache paths (−0.194) rather than
its CPU (−0.130).

Where the blocks landed (`build/multimesh_v8t7_routed_bbox.txt`; 10–90 %
slice bands): the CPU cores in X73–94 Y204–234 (one region, X2Y3 90 %),
X168–187 Y393–431 (X6Y6 43 %, X5Y6 25 %, X6Y7 16 %, X5Y7 15 %), X171–189
Y563–597 (X6Y9 71 %) and X171–187 Y785–822 (X6Y13 68 %, X5Y13 30 %); the
interlink ports of die 2 in Y538–591 and Y573–618 (v8t6 Y533–632 and
Y610–679), the 3-stage pipe chains spread over the rows between; the meshes
now 118 / 131 / 119 / 104 rows tall (v8t6 84 / 133 / 93 / 75), the movers
of dies 1 and 3 over three region rows (Y335–454, Y769–859). The Xache
partitions sit on the DDR4 column: chain, home and hedge in X4, the master
at the box's inner edge (X141–156 on die 1). Clock roots X3Y3 / X4Y6 / X5Y9
/ X5Y12.

Against v8t6 (`build/multimesh_v8t7_routed_classes_diff.txt`, 10,484
classes here, 7,479 only there). Better: the Xache chain head into the
trunk ring +546 ns of TNS (−0.851 → −0.534) and into itself +332; transform
`s_id` → quantiser `src` +154 (now +0.000); walker `u_src` `d_count` →
`apsum` +113 (−1.020 → −0.528) and `u_dst` +89; the reset synchroniser →
`cp_wdata` +93 (now +0.370); the CSR writes +81 … +37 each (`mepc` −0.998 →
−0.630); `w_kind` → `wide_q` +76 and +67; `sr_v` → interlink `d_r` +65
(−0.666 → −0.136) and → `m_awaddr` +41; the interlink rx FIFO → `ltx_dat`
+53 (now +0.005); scratchpad → `wb_val` +35. Gone: the die-1 register-file
forward classes at −1.04 … −0.97, `xb_cnt` → the command FIFO and
`ar_addr_i` (−0.99, 80), the head decode → DRAM port AR queue (−0.955).
Worse, and new under the margin: the hedge `u_w` CDC full flag `rg_s2` →
the trunk rings' `rd_data` (1,573 endpoints, 1,461 failing, −0.631) and →
the chain heads (2,088, 1,529, −0.690); the head decode → home `word_q`
(1,018, −0.844); `hv_q` → trunk ring (519, −0.837); master `home` → home
`word_q` (634, −0.565) and → head (711, −0.435); home `rid` → `word_q`
(390, −0.600); `u_src` `d_count` → `psum` −0.406 → −0.615 (748 failing,
die 3); `w_kind_reg` → `wide_q` −0.342 → −0.722; `host_b` → `rleft` (284,
−0.679); instruction RAM → `e_btgt` (254, −0.474); `sr_v` → the W skid
−0.425 → −0.633.

By end owner merged (failing): `mesh_N/u_mag/u_pe` 10,851 (v8t6 18,394),
`mesh_N/u_mag/u_mag` 7,842 (9,827), Xache trunks 3,436 (5,100), homes
2,482 (3,583), chain heads 2,698 (3,487), hedges `u_w` 1,383, the NSU 1,039
(2,188). Per die the Xache is now the largest failing owner on die 1
(4,400 of 11,129) and die 2 (2,973 of 5,637); the CPU and mover on dies 0
and 3.

The remaining families as RTL and place:

- **Xache partition split by the box.** The hedges and homes on the DDR4
  column (`CMP_HOME` empty), the masters inside the box, the chain heads
  and trunk rings between: the hedge's full-flag synchroniser
  (`src/kohakuaccel/common/kohaku_aring.v`, the `rg_s2` register) is one
  register on 3,661 endpoints across that gap, and the head decode
  (`src/kohakuaxi/pxache/kx_pxache.v:575`) into the home's `word_q`
  (`src/kohakuaxi/xache/array/kx_carray.v:198`–`:201`) is 10 levels with
  2.7 ns of net and −0.27 ns of skew on die 1. Fan-out first (copies of the
  full flag per consumer group), then a box of the partition's own around
  the column (X3–X5 holds 192 URAM).
- **CPU forward into the M register**, `e_s1_q`, `wb_val`, `z1` → `m_val`
  (`src/kohakuaccel/pe/rv64-sys/core/rv64_core.v:438`, `:949`): 10 levels
  at 1.0–1.2 ns of logic with 2.8–3.1 ns of net on every die, the worst
  −0.885 (die 1), −0.634 (die 2), −0.623 (die 0). The cores are compact
  now; what is left is the cone.
- **Walkers**, `d_count` → `psum` and `psum` → `q_e0`
  (`src/kohakuaccel/sysnode/mover/mx_tdesc.v:142`–`:156`, `:225`–`:234`):
  −0.615 and −0.856, the movers of dies 1 and 3 three region rows tall
  inside the boxes.
- **Mover → DRAM port W queue**, `ewidth`, `w_kind` → the W-queue block RAM
  data pins (`src/kohakuaccel/sysnode/core/mag_dram_port.v:476`): die 1's
  WNS, 5 levels, 3.2 ns of net.
- **XDMA → station 1's NMU request ring**, 9 levels at 4.000 ns (−0.227,
  947): the NMU's accept logic before the ring write, a skid at the NMU.
- **MIG ui clocks 1 and 3** (−0.226): the memory controllers' own AXI
  upsizer and calibration paths, 7 and 4 levels with 2.6–2.9 ns of net,
  beside the Xache partitions now sitting on their column.

## 6. Accuracy

### 6.1 The block scale: E5M3 against E8M0

Measured per element on correlated operands:

| | p50 relative error | p99 |
|---|---|---|
| power-of-two scale (E8M0) | 0.54% | 48% |
| **E5M3** | **0.38%** | **23%** |

Same 8-bit field, so nothing about the flit format, the mesh or L1 changed — only
the interpretation ([number-format.md](number-format.md) §2).

### 6.2 Accumulator mantissa width

384 checks per width. The demanding case is a 32-block K sweep accumulated into
one resident sub-tile — 32 roundings deep, which is what a real K=1024 matmul
does. **One FP16 ULP is 9.77e-4.**

| MW | width | worst rel. error | in FP16 ULP | verdict |
|---|---|---|---|---|
| **16** | FP24 | 3.34e-4 | **0.34** | pass |
| **14** | FP22 | 3.37e-4 | **0.35** | pass |
| 12 | FP20 | 4.27e-3 | 4.4 | marginal |
| 11 | FP19 | 2.04e-3 | 2.1 | marginal |
| 10 | FP18 | 5.40e-3 | 5.5 | fails |

**There is a cliff between 22 and 20 bits, not between 24 and 20.** The ordering
among MW=10/11/12 is not meaningful — they sit near the check's tolerance and the
differences are data-dependent. The signal is the step between 14 and 12, which is
about 13x.

Every figure comes from the same bench at different widths, checked against **two**
independent ground truths — an exact integer model and an FP64 model — with the
bench asserting that those two agree before either is trusted. That is what makes
the reported error attributable to the accumulator rather than to quantisation or
to a drifting model.

> Narrowing exposed a real bug the wide case hides: in the rounding-carry path the
> fraction was taken one bit too wide, which overflows the output concatenation
> and pushes the sign bit out. At MW=16 nothing in the suite rounds up far enough
> to reach that path. **Sweeping a parameter is a test in its own right.**

### 6.3 The vector ALU

26,897 checks, streamed at one instruction per cycle, against both a behavioural
DSP and a real DSP48E2:

| | result |
|---|---|
| `mov neg abs max min select cmp` | **bit exact** |
| products and sums of powers of two | **bit exact** |
| `a*b - a*b`, `x - x` (and `x - x` is **+0**) | **bit exact** |
| `exp2(k)`, `log2(2^k)`, `inv(2^k)`, `rsqrt(2^even)` | **bit exact** |
| **FMA**, including the full alignment sweep | **0.500 ulp on this suite** — one subtractive corner later moved the known bound to 1 ulp, see below |
| `exp2` | 0.509 ulp |
| `inv` | 0.546 ulp |
| `rsqrt` | 0.549 ulp |
| `log2` | 0.64x its limit (0.99 ulp or 2^-18 absolute) |

`log2` needs both bounds and neither alone is meetable by any implementation: near
`x = 1` the result approaches zero while its absolute error does not, so one ulp
shrinks without bound; at large `|x|` the result spans decades and an absolute
bound falls far below one ulp.

**A later bench moved the FMA's known bound to one ulp.** The SIMD-PE float-lane
bench, built to oversample exponent-distant addends, reached an
effective-subtraction corner this suite's four mantissa patterns per shifter
position never land on: discarded alignment residue carried as a plain sticky
reads exactly one ulp high — 19 of 4,000 on that stream, 0 of 6,000 in this
suite's random band. The exact statement of the property lives in
[vector-core.md](vector-core.md) ("The rounding property, stated exactly");
the same construction is in `mx_fpacc`'s split path.

**The alignment sweep is the load-bearing test.** It walks the exponent difference
across every barrel-shifter position, which is the only way to reach the case
where the product's top bit is a value bit rather than a sign bit, and the bypass
below it. Random operands never land on either.

Transcendental table quality, predicted against measured:

| function | domain | predicted | measured | margin over 2^-16 |
|---|---|---|---|---|
| `exp2(f)` | [0,1) | 2^-23.2 | **2^-19.9** | 3.9 bits |
| `log2(m)` | [1,2) | 2^-21.1 | **2^-19.5** | 3.5 bits |
| `inv(m)` | [1,2) | 2^-20.0 | **2^-19.4** | 3.4 bits |
| `rsqrt(m)` | [1,2)+[2,4) | 2^-21.7 | **2^-19.8** | 3.8 bits |

Measured is consistently ~1.5 bits worse than the minimax prediction, and **that
gap is the point of measuring**: the approximation is no longer what limits these
functions — the coefficient and Horner quantisation is. Adding segments would buy
almost nothing.

### 6.4 End to end

| bench | shape | result |
|---|---|---|
| `mx_system_tb` | 4x256x4 on a 1x5 mesh | worst 3.97e-4 → **0.41 FP16 ULP**, 35 checks |
| `mx_system32_tb` | 32x32x32 on a 1x5 mesh | worst 4.86e-4 → **0.50 ULP**, mean 1.41e-4 → 0.14 ULP over 1,024 |
| `mx_cluster_node_tb` | 32x32x32, one GEMM | 2,112 checks, 0.50 ULP worst |
| `mag_system_tb` | 16x32x16, agent + 2 clusters | 257 checks, 0.49 ULP |

The 4-block worst case is **cancellation** — a property of the problem, not of the
hardware — which is why the mean matters more than the maximum for judging the
accumulator. At these sizes the accumulator is not the limiting factor; the output
format is.

Error profiles from full driver runs:

| run | against the MXFP7 model | against FP64 |
|---|---|---|
| 2 clusters, 256x256x256 | p50 1.70e-04, max 1.00e+00, 20 of 65,536 over 1%, 4 over 10% | p50 3.88e-03, p99 2.48e-01 |
| 4 clusters, 256x512x256 | max 2.43e+00, 7 of 131,072 over 10% | — |
| 8 clusters | p50 1.71e-04, worst 2.43e+00, 49 of 524,288 over 10% | — |

Every result is identical under a behavioural DSP model and a real DSP48E2. **In
SIMULATION two runs of the same shape are bit-identical**, verified by hash.

> **On the card they are not.** Measured 2026-08-13 on `multimesh_v3`: repeating
> one matmul with fixed operands leaves ~0.6% of elements differing between runs
> by up to 40% of peak, and a further ~0.3% reproducibly wrong by up to 11,000x.
> The two sets never overlap. So a difference between two HARDWARE runs is not
> automatically a real difference — expect ~0.5% disagreement before concluding
> anything. See §6.5.

### 6.5 The card on realistic operands

Measured against FP64 on a real ViT-B/16 projection with normalised activations,
`128x256x256`. **Quote `p50` / `p90` / `>10%`; the max is a defect (§6.6), not a
property of the format.**

| | rel p50 | rel p90 | >10% |
|---|---|---|---|
| software `int7 + E8M0` | 2.26% | 14.16% | 14.0% |
| software `int7 + E4M3` | 4.06% | 26.84% | 25.3% |
| **the card**, blown elements excluded | **1.64%** | **10.82%** | **10.8%** |

**The `int7 + E8M0` software model is PESSIMISTIC about this silicon by ~1.4x.**
Anyone using it to argue a number format should say so; the hardware quantiser is
better than the model of it, consistently across four operand distributions.

The operand distribution decides the answer, so a figure without one is
meaningless: the same card measures 0.39% p50 on `lowrank` operands and 1.64% on
real weights. **`lowrank` is optimistic** — A and B share a basis there, which
real weights and activations do not — so iid `normal` is the better proxy for a
linear layer. This corrects earlier guidance that pointed the other way.

### 6.6 Two unexplained observations, open

~0.3% of output elements are **reproducibly** wrong by up to 11,000x (often
pinned at the FP16 maximum), and a disjoint ~0.6% **flicker** between runs. The
read path, the write path, K-chunking and multi-mesh are all eliminated, as is
free accumulation order — reordering moves every element slightly, and would move
one by ~6e-4 rather than the observed 2.139.

**The blown elements are OPERAND RANGE, and that one is closed.** Their count
follows operand magnitude and nothing else — scaling an operand by an exact power
of two (which changes no mantissa) takes 75 blown at a true peak of 5.79 to
**195** at 11.57 and to **zero** at 1.45. It is a contraction driven past the
drain and saturating at the FP16 maximum ([accumulator.md](accumulator.md) §7).
Keep the contraction in range, and **do not read a saturated element as evidence
about the machine** — it is the most common way a synthetic benchmark
manufactures a hardware defect that is not there.

**The flickering is separate and still open**, and it does not follow magnitude.
Non-deterministic unit assignment is eliminated (the idle set was identical every
run, and pinning placement changed nothing), as is stale tile state and
transport — the bad elements sit **one per 4x4 sub-tile**, and sixteen of those
share one 32-byte word, so no skew or dropped beat can produce it. Between runs
on byte-identical operands `run1/run0` is an **exact power of two in 75 of 85**
cases with neither run correct, which points at the **exponent of the per-block
scale** rather than at the multiply-accumulate. (The hardware's block scale is
E5M3; a run-to-run ratio that is an exact power of two moves its E5 field and
leaves its M3 field alone, which is what makes the scale rather than the
significand the suspect.)

The obvious next experiment — feed the cluster host-packed MXFP7 so the
quantiser is bypassed — **cannot be run as a comparison**, because there is no
on-the-fly quantiser left to bypass: a fetch is never transformed, so the
"bypassed" path is now the only path there is
([number-format.md](number-format.md) §5.1). If the scale is still suspect, the
place to look is the converting move's output rather than the fetch's.

**No reproducer is currently in the tree**, so the finding above is a claim about
a measurement nobody can presently repeat. One is cheap to write: run the same
kernel twice on byte-identical operands and diff the results, which takes about
thirty seconds on the card.

The rates recorded in §6.4 above — "4 of 65,536 over 10%", maxima of 1.00 and
2.43 — are very likely the same phenomenon seen earlier and recorded as a rate
rather than recognised as a defect.

---

## 7. The superseded FP8 baseline

Kept because it is the only measured baseline for the FP16 ALU path that exists,
and because it is what the MXFP7 design is measured against. **These describe the
previous FP8 → FP12 → FP16 design and say nothing about the current element
format.**

| unit | LUT | FF | DSP | latency |
|---|---|---|---|---|
| FP8 vector mul, design 1 | 123 | 118 | 1 | 3 cycles, II=1 |
| FP8 vector mul, design 2 | 163 | 108 | 2 | 3 cycles, II=1 |
| FP vector add, E5M6 (implementation run) | 333 | 119 | 1 | 2 cycles, II=1 |
| FP vector add, E5M10 (implementation run) | 546 | 148 | 1 | 2 cycles, II=1 |
| FP12 inversion | 38 | — | 0 | combinational |
| **FP8-FP12 4x8x4 tensor core** | **12,731** | 7,549 | 64 | 16 cycles, II=1 |
| FP16 ALU array (16 lanes) | 6,643 | 2,090 | 32 | 4 cycles, II=1 |

**10,656 of the tensor core's 12,731 LUTs were its 32 floating-point adder
units** — 84% of the core was its adder tree. That is the number the whole MXFP7
design exists to remove:

```
   per 128 MACs      old   12,731 LUT + 64 DSP
                     new    1,188 LUT + 64 DSP      ~10.7x fewer LUTs
```

Same DSP count, same MAC count, different numerics. Essentially all of the
difference is accumulation leaving the fabric.

---

## 8. Throughput

Peak is **512 MAC/cycle per cluster**, so a two-cluster machine peaks at 1,024
MAC/cycle — **614 GFLOP/s at 300 MHz**.

### 8.1 The baseline, and why it was flat

Two clusters, a small resident tile, both operands quantised online:

| shape M x K x N | run cycles | fill | gemm | drain | idle | MAC/cyc | GFLOP/s | % peak |
|---|---|---|---|---|---|---|---|---|
| 64x128x64 | 8,213 | 56.7% | 17.3% | 12.1% | 13.8% | 63.8 | 38.3 | 6.2% |
| 128x128x128 | 32,361 | 61.3% | 17.1% | 11.6% | 9.8% | 64.8 | 38.9 | 6.3% |
| 256x256x256 | 239,786 | 71.9% | 13.4% | 6.7% | 7.9% | 70.0 | 42.0 | 6.8% |
| 512x512x512 | 1,983,413 | 73.0% | 10.3% | 3.4% | 13.3% | 67.7 | 40.6 | 6.6% |

**The rate is flat across a 512x range in problem size.** That is the signature of
a structural limit rather than a small-problem artifact: the machine had one
operating point and tiling did not move it. Result write amplification was
**exactly 1.00x** at every size, so none of the loss was redundant output traffic —
the output-stationary dataflow worked as designed.

Per component, at the 128-cube:

```
   memory agent service FSM        stalls (cycles LOST, not spent)
     idle    31.3%                   in_bp     0.0%
     qfill   35.0%                   out_bp    0.0%
     qwait    7.8%                   cu_send   0.1%
     qemit   27.2%                   cu_dry   64.7%   <- the CU STARVED
     wr      11.7%
```

**Starvation with every backpressure counter at zero is the whole diagnosis.**
Nothing was pushing back; there simply was no data to take, so the fault was
upstream of the fabric in how operands were requested and served.

Per L1 entry — 256 B in memory, 8 AXI beats, 4 response flits:

| | cycles/entry |
|---|---|
| AXI beats alone (the floor) | 8 |
| memory agent actual | **18** |
| CU actual, serial requests | **~37** |
| CU actual, 512-cube, two clusters sharing | **~48** |

Two separate losses that compounded: the agent collected a whole entry and *then*
handed out four flits with no overlap, and the CU exposed the full round trip per
entry rather than paying it once.

**And the configuration was not the one the design specifies.** The bench ran the
tile at 8x8, where operand demand is exactly the port's capacity:

| tile | words/cycle needed | port supplies | margin |
|---|---|---|---|
| 8 x 8 | **1.000** | 1.0 | **none** |
| 16 x 16 | 0.500 | 1.0 | 2.0x |
| **16 x 32** (designed) | **0.375** | 1.0 | **2.7x** |
| 32 x 32 | 0.250 | 1.0 | 4.0x |

So the 6.6% was a break-even configuration plus overhead, not a machine at a
fundamental roofline.

### 8.2 Current

Both operands in MXFP7 in DRAM, one memory port per mesh row. *At the time these
were taken that was a per-request choice against converting on the fetch; a
fetch is never transformed now, so it is simply the format the operands are in.*

| clusters | shape M x K x N | run cycles | GFLOP/s | % peak |
|---|---|---|---|---|
| 2 | 256x256x256 | 18,701 | 538.3 | **87.6%** |
| 2 | 256x256x1024 | 72,684 | 554.0 | **90.2%** |
| 4 | 256x256x512 | 20,647 | 975.1 | 79.4% |
| 4 | 256x256x1024 | 41,638 | 967.0 | 78.7% |
| 8 | 256x256x1024 | 24,115 | 1669.7 | 67.9% |
| 8 | 512x256x1024 | 43,382 | 1856.3 | 75.5% |

**42.0 → 538.3 GFLOP/s on the 256-cube: 12.8x.** The individual steps, each
measured on its own:

| change | GFLOP/s |
|---|---|
| operands already MXFP7 in DRAM, against converting on the fetch | 217.4 → 303.9 |
| banked L1 with a non-blocking fill | 303.9 → 362.0 |
| the drain fused into the sweep's last K block | 362.0 → 391.1 |
| resident tile 64 → 512 sub-tiles | 85.1 → 173.4 |

> **Every row of the current table predates a mesh layout change**, measured when
> a cluster's two endpoints straddled another cluster's router. The layout since
> places a cluster as one column of a band. Measured, the new layout **costs about
> three points of peak at 8 clusters** — 72.7% against 75.7% on the same work with
> the same arithmetic — and that cost is **not** the memory system: fetch was
> unchanged within noise at 7.1 → 7.2 cycles per entry, and write-slot pressure
> more than halved, 5.5% → 2.1%. What is left is routing, which is what the change
> predicts. What it buys is physical locality and simpler program planning.
>
> Treat every figure above as a figure for the *previous* topology.

A later change gave the dispatch agent its own path through the memory ports
rather than a single mesh attachment. Measured against the same runs: two clusters
**identical** at 18,701 cycles; four clusters 79.4% → 79.6%; eight clusters
43,382 → **43,315** cycles and 1856.3 → **1859.2** GFLOP/s. **Free, and marginally
better** — and 67 cycles is 0.15% at eight clusters, well inside what a topology
change can move either way, so nothing more should be read into it.

The roofline that said this was impossible is worth keeping, because it was wrong
in an instructive way. At the baseline tile the arithmetic gave a hard ceiling of
**154 GFLOP/s, 25% of peak**, and concluded that reaching 500 was a
memory-hierarchy decision rather than a scheduling one. **538.3 was reached
without widening a single bus.** The arithmetic was sound on an assumption the
*schedule* controls — that each operand byte is fetched once — and the schedule
was re-reading B once per m-tile, a quarter of all traffic that no intensity
figure shows. **The ceiling is a function of the schedule, so a schedule change
moves it.**

### 8.3 Rate improvements that were not

Recorded because each one looked like a win.

- **Shared fetch, armed without a rendezvous:** 85.1 → 105.1 GFLOP/s **and the
  worst element went from 1.0 to 2.2e+02** against the MXFP7 model. A follower
  cannot tell which fill an arriving entry belongs to ([isa.md](isa.md) §3).
- **A third change measured 499.6 GFLOP/s while computing nothing at all.**

The rule both produced: **bound the worst element against the software model, not
just the median — and treat a rate improvement with no matching component counter
as unexplained.**

### 8.4 How to read the resource line

Five budgets are reported per run: the array's MAC rate, AXI read beats, AXI write
beats, and mesh flits in and out of the memory agent's ports.

**They are independent, full-duplex budgets and must never be added.** Read and
write are different AXI channels served in the same cycle, and the mesh's two
directions are different wires. Adding them prices capacity that never competes.
**What binds is the largest, never the total.** Each memory figure is also divided
by the port count, because the counters are sums over ports and each port has its
own channel.

Read correctly, on the current machine:

```
2 CU 256x256x256    flops 87.6%  mem_rd 30.3%  mem_wr 20.2%  noc_in 22.8%  noc_out 32.8%
8 CU 256x256x1024   flops 67.9%  mem_rd 25.5%  mem_wr 17.0%  noc_in 19.2%  noc_out 27.6%
8 CU 512x256x1024   flops 75.5%  mem_rd 23.6%  mem_wr 18.9%  noc_in 21.3%  noc_out 26.0%
```

**No memory budget exceeds a third at any cluster count, and the busiest thing in
the machine is the array.** That is what withdrew a wider bus as a lever and it is
the standing answer to "surely we are bandwidth-bound by now".

Three ways of reading it wrong, each of which produces a figure that looks
exactly like a saturated bus:

| the error | what it reads | what is true |
|---|---|---|
| summing reads and writes, then adding both mesh directions | 91.5% "of data movement" — scheduling exhausted | the busiest single path is 28.3%; the missing 30% is **latency**, and two one-line fixes took 69.6% → 80.7% |
| dividing per-port sums by one port | `mem_rd 101.9%`, `noc_out 110.4%` at 8 clusters | `mem_rd 25.5%`, `noc_out 27.6%` against `flops 67.9%` |
| a stall counter whose *event* has changed meaning | one counter goes 0.0% → 39.8% at an unchanged cycle count | the port declines flits routinely because it demultiplexes two consumers by type; it counts demultiplexing, not congestion |

Two rules follow, and both are cheap to apply. **A derived figure needs its
predicate re-checked as well as its denominator** whenever the thing it counts
changes shape. And **a stall counter that moves while the cycle count does not is
a change in meaning until proven otherwise** — a real congestion change costs
cycles.

The 2.62 GB/s read figure that accompanies the baseline runs is **demand**,
measured against a bench RAM with no latency. Real memory would not reduce the
traffic, only lengthen the wait for it, so the fill share on hardware is a floor.

---

## 9. Defects found by measurement

### 9.1 An L1 footprint band that returns wrong data

**Measured, unexplained, guarded.** A vector kernel whose buffers occupy **352 to
480 of the core's 512 L1 words returns wrong data**. 320 words and below is clean,
and so is **exactly 512**.

| L1 words used | 256 | 288 | 320 | 352 | 384 | 416 | 448 | 480 | 512 |
|---|---|---|---|---|---|---|---|---|---|
| two independent kernels | clean | clean | clean | **wrong** | **wrong** | **wrong** | **wrong** | **wrong** | clean |

The corruption is 16 L1 words wide. The cost is a capability limit rather than a
wrong answer, because the driver caps the footprint below the band: at a channel
count of 320 with 32 groups, a normalisation group is `10·hw` elements, so **the
spatial extent is capped at `hw <= 128`** — an 8x16 tile works and a 12x16 does
not.

### 9.2 The L1 offset wrap

The 8-bit L1 offset field ([isa.md](isa.md) §4.6). Measured on the card against
the machine's own software MXFP7 model, before and after wiring the bank bits
through:

| shape M x K x N | p50 before | over 10% before | p50 after | over 10% after |
|---|---|---|---|---|
| 64x576x64 | 1.652e-01 | 2,778 of 4,096 | **2.182e-05** | **0** |
| 64x640x64 | 1.699e-01 | 2,847 | **2.531e-05** | **0** |
| 64x1024x64 | 1.650e-01 | 2,814 | **2.489e-05** | **0** |
| 64x1280x64 | 3.016e-05 | 443, max 0.73 | **2.483e-05** | **0** |
| 77x2048x64 | 6.36e-05 | 1,197 of 4,928, max 1.08 | **2.433e-05** | **0** |
| 128x640x64 | 1.43e-01 | 5,191 of 8,192 | **2.361e-05** | **0** |

An earlier campaign on the planner side of the same defect:

| shape | last B offset | worst element before | after |
|---|---|---|---|
| 64x256x256 | 255 — exactly fits | 4.15e-02 | 4.15e-02 |
| 64x288x256 | **287** | **8.23e+02** | 2.62e-02 |
| 64x320x320 | **319** | **3.49e+03** | 1.71e-01 |

**Eleven of eleven measured shapes follow the rule exactly, and it is not a
capacity threshold**: 576 B entries passes at one shape and fails at another with
the same entry count and the dimensions swapped. Neither chunk count nor capacity
alone explains any of it; only the *product* against 256 predicts every case.

End to end on a transformer block, the same fix: per-head worst element
**1.21e-02 → 1.07e-03**, and the full-width path unchanged at 1.07e-03 — **which
is the format's own cost of 1.06e-03**, so the error that remains is MXFP7 and
nothing else. Traffic fell from 122 matmuls to 68.

On the compiler path the same defect was worse and completely silent: the bank
fields were absent entirely and the offset addressed past two banks. Adding the
fields with a range check **immediately failed 25 tests** on an entry the path had
been reaching only by relying on 8-bit wrap.

### 9.3 Bugs the two-model discipline caught

Every matmul bench runs against both a behavioural model and a real DSP48E2, which
makes a failure attributable. It paid for itself immediately.

**A DSP input register had to be 2, not 1.** With the pre-adder path selected, the
A/D operands reach the multiplier through two register stages while B was given
one, so B arrives a cycle early and multiplies against the wrong operand. **This
is invisible with stable operands** — every stage happens to be looking at the
same tile, so the misalignment cancels — and only appears when a new tile enters
every cycle. The behavioural model passed and the real DSP failed **only** in the
streaming section, which pointed straight at the DSP configuration rather than at
the arithmetic.

The simulation library also holds global set/reset asserted for the first 100 ns,
so unisim registers ignore everything before that regardless of the design's own
reset. Without waiting past it, the first tile silently produces nothing.

### 9.4 Three driver defects, found bringing up `multimesh_v7`

None of these is an RTL fault and none raises an error on the card. All three
present as a unit that never signals, with the bus healthy throughout.

| defect | symptom | fix |
|---|---|---|
| `Program.kick` wrote `DST, BASE, LEN` and seeded credit **before** the kick | `PROG_STAT` shows `run=1`, `flits_left>0`, `credit=0` | the order the spec already specified — [control-registers.md](../../spec/control-registers.md) §2.7 |
| the write-shadow elided `PROG_LEN` on kick 2, which shares kick 1's length | node 1 takes **every** completion, node 2 signals nothing | drop the `LEN`/`DST` shadow whenever `PROG_BASE` is written |
| DRAM writes shorter than 32 bytes zero the rest of the line | a paint loop of consecutive `write64` leaves only its **last** word | `Window` enforces `host_write_granule` |

The third is the sharpest, because the preflight hides it: `verify_write_path`
uses `write_block` and is byte-exact, so the write path certifies clean while
every hand-written `write64` silently destroys its neighbours. Writing a full
32-byte line then poking one word into it gives
`['0x0', '0xdeadbeefdeadbeef', '0x0', '0x0']`. Deterministic, and it supersedes
the earlier account that single JTAG writes "vanish about half the time".

**The bring-up ladder that found them**, `multimesh_v7`, 2026-08-23, at
100/200/100/100:

| rung | result |
|---|---|
| master width | 64-bit |
| write path, byte-exact | clean on all 4 meshes |
| `A_CAPS` | `0x01040120` on all 4 |
| enumeration | 38 units — 8+2 / 6+2 / 8+2 / 8+2, `CU_VERSION 4` |
| one flit to one unit | **10 of 10** on mesh 0 |
| `32x64x64` against fp32 | p50 **1.72e-03**, p99 **6.60e-03**, peak 0.59 |
| two runs, same operands | **identical, 2,048 of 2,048** |

The one-flit rung is the load-bearing one and is now
`Mesh.probe_dispatch()`. A matmul engages MAG, DRAM, L2 and every cluster at
once and cannot say which is broken; each of the three defects above was found by
a rung that added exactly one thing.

### 9.5 `multimesh_v7` clocks: three of four ship rates are unreachable

Measured on mesh 0, 2026-08-23, `scripts/py/fmax_ladder.py`. **Each domain is
laddered while the other three are held at low**, and each is driven by a
workload that actually reaches the unit it clocks — a matmul for `mat2x` (MG), an
`rmsnorm` for `vec` (VC). Scored as relative error against fp32, never as
pass/fail.

| domain | clean to | first degradation | dies | `ship` asks |
|---|---|---|---|---|
| `mat2x` | **400** | 450 | 700 | **600** |
| `vec` | **350** | 400 | 450 | 300 |
| `noc` | **300** | — | 350 | 300 |
| `mag` | **250** | 300 | 350 | **300** |

`mat2x` is bit-identical from 200 to 400 — p50 `1.561e-02`, p99 `1.064`, max
`9.425` at every step — so that floor is the number format, not the clock. It
degrades to p50 `1.574` (157 %) at the 600 the profile asks for.

**`noc` has no graceful degradation**: clean at 300, hung at 350, no intermediate
error. A routing fabric loses a packet rather than corrupting a number, so a
timing failure there is a timeout, not a wrong answer.

Two traps this ladder walked into, both worth keeping:

- **The workload must drive the unit the domain clocks.** Laddering `vec`
  against a matmul returns bit-identical error at all eleven steps, because MG
  runs the matmul and VC sits idle. That reads as "clean to 600" and is *no
  measurement at all*.
- **A pass/fail verdict hides the answer.** The first version of this ladder
  reported `ok` per rung, which meant only "the unit signalled" — a FILL has no
  numeric output to be wrong. Every rung passed at every frequency and the table
  said nothing.

`boards/multimesh_v7.json` now carries `fmax_measured` and a `safe` profile
(200/300/300/200) that sits inside all four. `ship` (300/600/300/300) is what
v7.1 exists to earn.

---

## 10. Verification

| bench | what it covers | checks |
|---|---|---|
| `mx_tcu_tb` | one tensor CU, raw packed partials | 1,520 |
| `mx_cluster_tb` | full cluster, extracted and scaled | 4,176 |
| `mx_fp24_tb` | accumulator float primitives | 13,208 |
| `mx_acu_fp_tb` | accumulator ops, resident tile, peer | 384 |
| `mx_cluster_node_tb` | 32x32x32, one GEMM | 2,112 |
| `mx_system_tb` | 4x256x4 through a 1x5 mesh | 35 |
| `mx_system32_tb` | 32x32x32 through a 1x5 mesh | 2,051 |
| `mag_system_tb` | 16x32x16, agent + 2 clusters | 257 |
| `mag_driver_tb` | up to 256x256x256, tiled by the driver | §8 |
| `vec_alu_tb` | one vector lane, streamed | 26,897 |
| `mx_cluster_data_tb` | unit-to-unit bulk transfer, both directions | — |

Everything in the matmul datapath is **exact integer arithmetic checked
bit-for-bit against a model computed in the bench. No tolerances.** The coverage
that matters is the cases random operands never reach:

- **the packing worst case**, all three operands at `-64`, which is what rules out
  a packing offset of 20;
- **full-scale sums**, so a K=32 sum reaches ±131,072 and uses all five guard bits;
- **the borrow correction**, with the lower field forced negative on all eight
  chains — the only thing that correction fixes;
- **streaming**, a new tile every cycle, which is the only way the per-stage skew
  and the cross-CU path are exercised at all;
- **non-uniform scales** per row and column, accumulated across blocks;
- **the alignment sweep** in the vector ALU (§6.3);
- **a peer round trip** that adds a tile to itself, so the answer must be exactly
  2T and no float model of the accumulator is needed.

> **A test never seen to fail is not a test.** Two reduction kinds had no coverage
> anywhere, and the vector-length mask they reduce under is the kind of thing a
> *passing* test can miss entirely: a uniform predicate gives the same answer
> whether the mask is right or stuck at all-ones. The test that closed it splits
> the predicate mid-vector so a stale mask is wrong in both directions — **and it
> was then verified by forcing the mask to all-ones and watching it fail.** Do
> that for anything whose failure mode is "quietly returns a plausible value".

One bench was **deleted rather than fixed**: it had fallen a generation behind on
two interfaces at once, packing a superseded instruction layout and driving a
memory stub that wrote a constant where a response index belongs, so no L1 entry
was ever committed. The multi-cluster coverage it existed for is now against the
real memory agent rather than a stub, which is the stronger test.

---

## 11. What closed, and what did not

**Closed, out-of-context, against a 300 or 310 MHz target:**

| | |
|---|---|
| `mx_cluster_cu` | **346.6 MHz** current, 304 DSP — lower bound |
| `mx_acu_fp` | **343.4 MHz** at MW=14 — lower bound |
| `vec_alu` (one lane) | **324.8 MHz**, WNS +0.147 ns at 310 — lower bound |
| `vec_cu` (assembled core) | **358.4 MHz** after the shrink — lower bound |
| `mx_quant` | **400.6 MHz** — lower bound |
| `mm_mesh` (agent + cluster + vector core + 2 routers) | **328.8 MHz** — lower bound |

**Did not close, and is recorded as a ceiling:**

| | |
|---|---|
| a mesh spanning three SLRs | **4.6 ns worst path at 98.3% routing with zero logic levels** — rejected on measurement, and the reason four independent meshes exist ([ship.md](ship.md) §2) |
| `vec_cu` with all three register-file ports in block RAM | **286.0 MHz** against a 300 floor |
| the accumulator's tile as inferred LUTRAM | **287.3 MHz**, at 22,845 LUT |
| the quantiser packing a whole entry in one cycle | **32.5 MHz** — 128 parallel barrel shifters, nine times over budget |
| `mx_acu_fp` unpipelined | **84.7 MHz** — the starting point of §2.4 |

**Not measured, and should not be assumed:**

- **No place-and-route on a populated die** for any cluster-count configuration.
  Every scaling figure in §5.1 is arithmetic on one synthesised cluster.
- **The resident tile in URAM has not been re-measured in context.** The
  standalone probe is 585 MHz against a cluster measuring 344 in the same mode, and the
  pipeline argument says the seam does not move — but URAM's clock-to-out is worse
  than block RAM's and the accumulator is what the cluster closes on, so treat the
  in-context figure as unmeasured rather than unchanged.
- **`mx_cluster_core` was never synthesised standalone.** Where a figure for it
  appears it was inferred from the cluster minus its parts.
- **MW=16 has not been synthesised since MW=14 became the default.** Its last
  figure was 302.3 MHz from a 300 MHz-target run several steps earlier. "Costs
  less and carries more slack" is sound on the evidence that chose the operating
  point and is *not* a claim about what FP24 would measure on today's block.
- **The online quantisation path no longer exists** and will not be re-run. Its
  last measurement was 408.6 GFLOP/s at 66.5% on per-row memory ports. The
  transform slot moved off the fetch path entirely, so a fetch is never
  transformed; the figure stands as history and is not a number this machine can
  produce again.
