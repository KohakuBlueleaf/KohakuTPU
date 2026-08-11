# Crossing SLRs

**Everything else in this repo assumes one SLR.** The floorplan in the
[README](README.md) is a single-SLR module, and the mesh, the NoC and the MAG
ports are all drawn as if the die were flat. It is not.

This document is the honest state of that: what is decided (little), what the
constraint is (resources and routing, not throughput), and what has to be
answered before a multi-SLR design is worth drawing.

---

## 1. Why this is open rather than deferred

The vector core count is already being chosen against it. 16 is not a
throughput knee -- throughput is still near linear there and vector busy only
falls 97% to 88% between 8 and 16. **16 is where the device runs out**, once
room is left for routing and for whatever crosses SLRs. So the SLR question is
not downstream of the topology; it is one of the two things setting it.

The other reason not to defer: [`../perf.md`](../perf.md) §0.1 measured a
*within-die* layout change costing about three points of peak, and the counters
ruled out the memory system directly -- `fetch` 7.1 to 7.2 cycles per entry,
unchanged within noise, and `wslot_full` 5.5% to 2.1%, better. What was left was
routing, because a cluster's traffic began leaving by one of two rows instead of
the single row both its endpoints used to sit in. If that perturbation cost
three points, an SLR boundary in the wrong place will cost considerably more.

## 2. Device facts

Measured against the Vivado device database for `xcvu13p-fhgb2104-2L-e`, plus a
placed-and-routed Laguna crossing on that speed grade. DS890 corroborates the
totals.

| | per SLR | device |
|---|---|---|
| CLB LUT | 432,000 | 1,728,000 |
| CLB FF | 864,000 | 3,456,000 |
| BRAM36 | **672** | 2,688 |
| URAM288 | 320 | 1,280 |
| DSP48E2 | 3,072 | 12,288 |
| clock regions | 32 (8 wide x 4 tall) | 128 |
| Laguna sites | 3,840 end dies, 7,680 middle | 23,040 |

**Four SLRs, and they are identical** -- an exhaustive site census shows the same
hard IP in all four. Two asymmetries only: the end dies (SLR0, SLR3) have one
Laguna face rather than two, and **SLR1 is the master** (`IS_MASTER=1`), so
configuration, `DNA_PORT` and `EFUSE_USER` live there.

### 2.1 The crossing

| | |
|---|---|
| boundaries | 3 |
| **SLLs per boundary** | **23,040, SHARED between both directions** |
| measured crossing delay | **0.755 ns** (0.096 clk-to-Q + 0.659 SLL route), -2L |
| latency | **1 cycle**, TX_REG to RX_REG |
| at 300 MHz | the crossing alone is ~23% of the period |

The sharing was settled by tracing the routing graph rather than assumed:
`LAGUNA_X0Y120/RXD0` in SLR0 and `LAGUNA_X0Y240/RXD0` in SLR1 resolve to the
same node, `LAG_LAG_X4Y240/UBUMP0`, which Vivado reports as belonging to both.
3,840 site-pairs x 6 UBUMPs = 23,040 exactly. **It is a total, not a per-direction
budget.**

Every crossing signal must be **flop to flop**, TX_REG index matched to RX_REG
index (UG949, "Using SLR Crossing Registers"). UltraScale+ lets the router fix
hold with programmable clock delays, which is why both sides register.

### 2.2 Memory

**There is no hard DDR controller.** No `DDRMC` site exists on this part -- that
primitive is Versal-only. The XIPHY is hard; the controller is soft RTL at
roughly **11.9k LUT / 13.5k FF / 25.5 BRAM36**, about **2.8% of one SLR's LUT**
(PG150 Table 2-2).

**A DDR4 interface cannot span SLRs.** PG150 states it three times: all I/O banks
used by the interface, and the clocking pair, must be in the same SLR. The
channel is anchored to a die by its pinout.

**This board already wires one channel per SLR** -- but not in the order anyone
would guess, mapped from the XDC `PACKAGE_PIN`s:

| XDC | SLR | banks | `design_1.bd` inst | refclk pin | also in this SLR |
|---|---|---|---|---|---|
| `ddr4_c0` | **SLR3** | 72, 73, 74 | `ddr4_0` | G25 (bank 74) | -- |
| `ddr4_c1` | **SLR2** | 69, 70, 71 | `ddr4_1` | J26 (bank 71) | the current `singlemesh` |
| `ddr4_c2` | **SLR0** | 61, 62, 63 | `ddr4_2` | AE31 (bank 63) | nothing, today |
| `ddr4_c3` | **SLR1** | 65, 66, 67 | `ddr4_3` | AW14 (bank 67) | **XDMA/PCIe** GTY 224-227, `system_clk` AY23, LEDs |

**Exactly one DDR4 controller per SLR**, and that is the fact the multi-mesh
design rests on: no mesh ever needs a cross-SLR AXI path to reach its own DRAM
([`../interlink/topology.md`](../interlink/topology.md) §3.1).

**Channel number is not SLR number**, and `docs/mas/spec.md` §2.3 used to draw it
as if it were; it is corrected. **XDMA lands in SLR1**, so host traffic enters at
the master die and the four partitions are not symmetric from the host's side.

Cross-checked three ways rather than read once: bank 74 → SLR3 from
`get_iobanks`; `ddr4_0`'s placed BUFG at clock region X4Y13/X4Y14 in
`singlemesh_wrapper_clock_utilization_routed.rpt`; and the board PDF's appendix
(`DDR0_REFCLK_P` G25 … `DDR3_REFCLK_P` AW14), which matches `ddr4_c*.xdc`
exactly. **Clock regions are 4 rows per SLR** — SLR0 Y0-3, SLR1 Y4-7, SLR2 Y8-11,
SLR3 Y12-15 — which is what makes a placed BUFG's coordinate an independent
witness for which die a channel landed on.

> **`design_1.bd` declares the DDR refclks at 100 MHz and the board is 400.**
> `CONFIG.FREQ_HZ {100000000}` on `c1_sys`/`c2_sys`/`c3_sys` produces four
> `CRITICAL WARNING [ddr4:2.2-1]`; `singlemesh.bd` — the design actually on the
> card — says `400160000`. design_1 was never implemented, which is why its
> value was never caught. **Follow `singlemesh` wherever the two references
> disagree.** [`../axi/bringup.md`](../axi/bringup.md) §"Block design traps".

### 2.3 What could not be confirmed

- **No published guideline** for what fraction of SLLs may be used before
  routing becomes the critical path. UG906 only says to check usage is "within
  the expectations and goals for the design". Treat 23,040 as a hard ceiling.
- **No published figure** for LUT/FF headroom to reserve for crossing routing.
  The closest AMD gives is "keep any single resource under ~85% in one SLR".

### 2.4 What the host infrastructure costs, measured

Out-of-context per-IP synthesis from the *implemented* `singlemesh` design
(`JTAG-DMA-test.runs/*_synth_1/*_utilization_synth.rpt`), not estimates:

| IP | LUT | FF | BRAM | DSP |
|---|---|---|---|---|
| **XDMA** | **76,319** | 72,059 | 124 | 0 |
| `smartconnect_0_0` — 4S/1M, uniform 256b, 2 clk | 20,104 | 29,602 | 0 | 0 |
| `axi_smc_0` — 3S/4M, 512/256/64/32b, 3 clk | 19,709 | 30,115 | 0 | 0 |
| MIG `ddr4_0` | 19,944 | 21,263 | 25.5 | 3 |
| `jtag_axi` | 867 | 2,300 | 4 | 0 |
| `axi_gpio` | 62 | — | — | — |

**XDMA is 17.7% of an SLR on its own** — larger than the MIG and either
SmartConnect, and it was absent from every budget table until this was measured.
Whichever die hosts PCIe gives up roughly a vector core to do it.

**The two SmartConnects are not the same job.** `smartconnect_0_0` merges the
mesh's masters onto one DRAM at one width in two clock domains, and
`src/kohakuaxi/axi_n1.v` replaces it at **955 LUT** (measured, N=4, 604 MHz).
`axi_smc_0` does 512→256 width conversion, four-slave address decode and three
clock domains; nothing here replaces it and it stays.

> Swapping an IP for RTL moves the wiring from the BD's inference to your port
> list. The rule for surviving that — *an unconnected output is harmless, an
> undriven input is the fault* — and the wrapper that keeps the interfaces
> inferable are in [`../axi/bringup.md`](../axi/bringup.md).

**SLL headroom is not the constraint.** The placed design uses 2,765 of 23,040
on the SLR3↔SLR2 boundary (12.0%), 1,355 on SLR2↔SLR1 (5.9%) and none on
SLR1↔SLR0. A 64-bit AXI control path is a rounding error against that; even a
full 288-bit flit link is ~5% of one boundary. What made a cross-SLR *NoC*
unattractive was the pipeline stage per flit on a fabric whose premise is
locality — never the wire count.

> `singlemesh` places nothing at all in SLR0 (0 CLB, 0 IO, 0 GT). That is a
> property of *that* design, which instantiates one DDR4 channel and one mesh —
> **not** of the board, which wires a channel to every die per §2.2.

## 3. Where the cut should fall

The traffic classes here are very unequal, so the boundary should land on the
cheapest one. In descending order of bandwidth:

1. **`mgr` to `acu` inside one cluster.** Heaviest by far, and tightly
   coupled. A cluster must sit entirely within one SLR -- this is the one
   near-certainty on the page.
2. **cluster to MAG.** Fills and drains, continuous during a GEMM.
3. **vector core to MAG.** Heavy today, and heavier under
   [`memory-mover.md`](memory-mover.md) option (b).
4. **general core to anything.** Light. Descriptors and control.
5. **host to orchestrator.** Lightest, already latency-tolerant.

So a vertical cut between cluster columns looks cheaper than a horizontal one
through the `acu` rows, which would split every cluster. That is a hypothesis,
not a result -- it needs the SLL budget from §2 to check whether even the cheap
cut fits.

## 4. What a crossing would carry

Two candidate mechanisms, neither chosen.

**(a) Memory to memory, through the DRAM.** An SLR owns its own memory
controller and its own MAG; anything another SLR needs is written to DRAM and
read back. AXI SmartConnect across the boundary is the obvious way to wire it.
Simple, and it makes the boundary a bandwidth question rather than a timing one.
The cost is a full round trip through DRAM for every cross-SLR value, which is
exactly the cost the memory mover exists to remove within one SLR.

**(b) A NoC link across the boundary.** The mesh already routes flits; an SLR
boundary becomes a link with more pipeline stages. This preserves the
programming model -- a cluster in SLR-A can be dealt work the same way as one in
SLR-B. The cost is that SLL count and Laguna registration set a hard ceiling on
flit width, and a 288-bit flit is wide.

**The deciding number came back, and it does not kill (b).** A 288-bit flit plus
valid forward and busy reverse is about 290 wires, so **23,040 per boundary is
roughly 79 NoC links** -- far more than a mesh edge needs. For comparison a full
512-bit AXI4 in both directions is ~1,240 wires, or ~18 interfaces.

So SLL count is **not** the binding constraint. Two other things are:

- **Latency.** One cycle for the crossing itself, but UG949 asks for **at least
  three pipeline stages to cross an SLR** for wide buses above 250 MHz, and its
  own worst case at 300 MHz needed seven. A NoC absorbs that -- it is already a
  multi-hop mesh with credit-based flow control -- which is a genuine argument
  for (b) over (a), since an AXI round trip through DRAM does not hide latency
  nearly as gracefully.
- **The 0.755 ns.** At 300 MHz the crossing eats ~23% of the period before any
  fabric routing to reach the TX or leave the RX. That is what the pipeline
  stages are for.

### 4.1 What cannot cross, at all

UG949 p.30, and this is correctness rather than performance: **carry chains, DSP
cascades, and BRAM/URAM cascades do not propagate across a boundary.** SLLs are
the only data connection between SLRs.

`../arch-design.md` §463-473 already identified this for the 8-deep `PCOUT`
to `PCIN` chains, and it is confirmed. **Every cluster must be SLR-resident**,
which was the one near-certainty in §3 and is now a documented hardware rule
rather than a guess.

## 5. What multi-SLR would mean for the compiler

Worth stating early, because it is cheap now and expensive later.

`Target` already carries `clusters` and `vector_cores` as flat counts, and
codegen deals grid instances across nodes of one engine with no notion of
locality. A multi-SLR machine makes that wrong in a specific way: two clusters
are no longer interchangeable, because one of them is across a boundary from the
operand it needs.

That is a level-2 concern -- it is the same shape as the residency and packing
constraints already there -- and the honest note is that **no part of the
current stack models locality at all.** The timing model in
`interp/timing.py` explicitly does not: it charges instruction costs and takes
the busiest node, with no routing, no contention and no notion of distance. It
can rank topologies; it cannot approve one, and it will be blind to precisely
the cost this document is about.

## 6. Contradictions in the existing docs, found while filling in §2

Not fixed here -- listed so they are fixed deliberately rather than by someone
trusting the wrong page.

| where | issue |
|---|---|
| `constraints/sysytem.xdc` line 2 | part is `xcvu13p-fhgb2104-2-e`; everything else, including `tests/run_synth_check.ps1`, says `-2L-e`. The measurements in §2 used `-2L-e`. |
| ~~`../mas/spec.md` §219-247~~ | mapped SLR0→ch0 … SLR3→ch3. **FIXED** — §2.3 now names the instances and points here. |
| `../arch-design.md` §420-421 vs `../mas/spec.md` §2.3 | 2,176 DSP / 140k LUT per SLR against 2,048 / 111k. **Both are low**: the measured cluster is **304** DSP once the accumulator's block-scale and normalising-shift DSPs are counted, so 8 clusters is 2,432. `../mas/spec.md` §2.3 and `../perf.md` §4.1 are corrected; `../arch-design.md` is not. |
| ~~per-SLR BRAM~~ | never stated anywhere. It is **672** — now in §2. |
| `../arch-design.md` §344-357, §540-543 | one central SmartConnect fans out to four MAGs -- the one structure that unavoidably spans all four dies -- with no note on placement or pipelining. Needs per-channel `*_SLR_PIPE` (PG247) or an AXI Register Slice in Multi-SLR Crossing mode. |
| ~~`../perf.md` §528~~ | mentioned "comfortably one HBM stack". **VU13P has no HBM** (DS890 lists "–"). **FIXED** — §5 now says four DDR4 channels, which is what the board has. |
| ~~nowhere~~ | XDMA/PCIe lands in **SLR1**. **RECORDED**, in §2.2 and in `../mas/spec.md` §2.3. |

## 7. Status

The device facts are in, and **the topology question in §3 and §4 is now
answered**: neither cut. A mesh spanning three SLRs was implemented and its worst
path was 4.6 ns at **98.3% routing with zero logic levels**, so option (b) — a
NoC link across the boundary — was rejected on measurement rather than on the SLL
arithmetic that had looked like the constraint. What replaced it is **four
independent meshes, one per SLR, each with its own DDR4**, joined MAG to MAG by
an explicit registered link: [`../interlink/`](../interlink/README.md), with the
floorplan in [`../interlink/topology.md`](../interlink/topology.md) §3.1.

Three constraints are hard rather than assumed: a cluster cannot cross a boundary
(DSP cascades), a DDR4 channel cannot cross one (pinout), and every crossing
signal is flop to flop at one cycle plus pipelining. The third is why the
interlink is credit-based — a `TREADY` travelling back across a boundary is the
combinational crossing all of this exists to avoid.

The single-SLR floorplan in the [README](README.md) stands as the description of
**one** mesh. Which SLR each of the four occupies is no longer a free choice and
no longer an open one: one DDR4 controller per die pins each mesh to its own
memory, and SLR1 additionally holds the master config and the XDMA entry point.
