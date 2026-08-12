---
title: Results
summary: Every measured number for KohakuTPU on xcvu13p-fhgb2104-2L-e — Fmax by block, resources, accuracy, throughput, and what closed and what did not.
tags:
  - kohakutpu
  - results
  - measurements
---

# Results

Every measured figure for KohakuTPU lives here, so there is one place to check
whether a number is current. Design pages cite this file rather than restating
it, and other sections of the tree should link here rather than quoting.

**The device is `xcvu13p-fhgb2104-2L-e` for every figure on this page unless a
row says otherwise.** These numbers describe one accelerator on one part. They
are evidence the framework closes on real silicon; they are not specifications of
it ([projects/README.md](../README.md)).

---

## 1. How to read these numbers

**Almost everything here is out-of-context synthesis.** Nothing is placed and the
route is estimated. That has a precise consequence in each direction:

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
MHz standing alone**, so anything slower is the module's own logic. Blaming the
RAM for the 241 MHz result hid a loop-order question for two rounds.

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
floor. BRAM became a counted resource: 44 tiles per core.

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
count to `12,288 / 304 = 40`. The 272 figure gives 45 and an earlier draft using
only the cascade's 256 gave 48. **All three numbers have been quoted somewhere; 40
is the one the current cluster supports.** The LUT and BRAM headroom is unaffected
either way, and the conclusion — DSP-bound, which is the right place to be bound
on this part — moves further in the same direction with each correction, which is
exactly why it was never caught by the answer looking wrong.

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

---

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
| **FMA**, including the full alignment sweep | **0.500 ulp — correctly rounded** |
| `exp2` | 0.509 ulp |
| `inv` | 0.546 ulp |
| `rsqrt` | 0.549 ulp |
| `log2` | 0.64x its limit (0.99 ulp or 2^-18 absolute) |

`log2` needs both bounds and neither alone is meetable by any implementation: near
`x = 1` the result approaches zero while its absolute error does not, so one ulp
shrinks without bound; at large `|x|` the result spans decades and an absolute
bound falls far below one ulp.

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

Every result is identical under a behavioural DSP model and a real DSP48E2. **Two
runs of the same shape are bit-identical**, verified by hash, so a difference
between runs is a real difference and not noise.

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

Both operands pre-quantised, one memory port per mesh row:

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
| operands pre-quantised in DRAM | 217.4 → 303.9 |
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

Getting this wrong pointed falsely at bandwidth three times:

| the mistake | what it read | what was true |
|---|---|---|
| reads and writes summed, then added to both mesh directions | 91.5% "of data movement" — scheduling exhausted | the busiest path was 28.3%; the missing 30% was **latency**, and two one-line fixes took 69.6% → 80.7% |
| per-port sums divided by one port | `mem_rd 101.9%`, `noc_out 110.4%` at 8 clusters — a saturated bus | `mem_rd 25.5%`, `noc_out 27.6%` against `flops 67.9%` |
| a stall counter whose *event* changed meaning | one counter went 0.0% → 39.8% at an unchanged cycle count | the port now declines flits routinely because it demultiplexes two consumers by type; it counts demultiplexing, not congestion |

**An impossible percentage looks exactly like a saturated bus.** A derived figure
needs its **predicate** re-checked as well as its denominator whenever the thing
it counts changes shape, and a stall counter that moves while the cycle count does
not is a change in meaning until proven otherwise.

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
  standalone probe is 585 MHz against a cluster that closes at 344, and the
  pipeline argument says the seam does not move — but URAM's clock-to-out is worse
  than block RAM's and the accumulator is what the cluster closes on, so treat the
  in-context figure as unmeasured rather than unchanged.
- **`mx_cluster_core` was never synthesised standalone.** Where a figure for it
  appears it was inferred from the cluster minus its parts.
- **MW=16 has not been synthesised since MW=14 became the default.** Its last
  figure was 302.3 MHz from a 300 MHz-target run several steps earlier. "Costs
  less and carries more slack" is sound on the evidence that chose the operating
  point and is *not* a claim about what FP24 would measure on today's block.
- **The online quantisation path has not been re-run** since per-row memory ports.
  Its last measurement was 408.6 GFLOP/s at 66.5%, and the read-engine split is
  exactly what it was short of.
