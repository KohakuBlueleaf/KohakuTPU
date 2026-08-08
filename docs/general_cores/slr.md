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

| XDC | SLR | banks |
|---|---|---|
| `ddr4_c0` | **SLR3** | 72, 73, 74 |
| `ddr4_c1` | **SLR2** | 69, 70, 71 |
| `ddr4_c2` | **SLR0** | 61, 62, 63 |
| `ddr4_c3` | **SLR1** | 65, 66, 67 |
| `pcie` (XDMA) | **SLR1** | GTY 224-227 |

**Channel number is not SLR number**, and `docs/mas/spec.md` §219-247 currently
draws it as if it were. **XDMA lands in SLR1**, so host traffic enters at the
master die and the four partitions are not symmetric from the host's side.

### 2.3 What could not be confirmed

- **No published guideline** for what fraction of SLLs may be used before
  routing becomes the critical path. UG906 only says to check usage is "within
  the expectations and goals for the design". Treat 23,040 as a hard ceiling.
- **No published figure** for LUT/FF headroom to reserve for crossing routing.
  The closest AMD gives is "keep any single resource under ~85% in one SLR".

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
| `../mas/spec.md` §219-247 | maps SLR0→ch0 … SLR3→ch3. The board is c0→SLR3, c1→SLR2, c2→SLR0, c3→SLR1. |
| `../arch-design.md` §420-421 vs `../mas/spec.md` §236 | 2,176 DSP / 140k LUT per SLR against 2,048 / 111k. The measured cluster is 272 DSP, so arch-design is the right one; `../perf.md` §522 still uses a stale 256. |
| per-SLR BRAM | never stated anywhere. It is **672**. |
| `../arch-design.md` §344-357, §540-543 | one central SmartConnect fans out to four MAGs -- the one structure that unavoidably spans all four dies -- with no note on placement or pipelining. Needs per-channel `*_SLR_PIPE` (PG247) or an AXI Register Slice in Multi-SLR Crossing mode. |
| `../perf.md` §528 | mentions "comfortably one HBM stack". **VU13P has no HBM** (DS890 lists "–"). |
| nowhere | XDMA/PCIe lands in **SLR1**. The host entry point is not symmetric across the four partitions and nothing records it. |

## 7. Status

The device facts are in. **Nothing about the topology is decided**, but three
constraints are now hard rather than assumed: a cluster cannot cross a boundary
(DSP cascades), a DDR4 channel cannot cross one (pinout), and every crossing
signal is flop to flop at one cycle plus pipelining.

The single-SLR floorplan in the [README](README.md) stands. What it does not yet
say is which SLR it is, and that is not a free choice: SLR1 holds the master
config, the XDMA entry point and `ddr4_c3`.
