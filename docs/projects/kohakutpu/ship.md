---
title: The ship and the device
summary: What xcvu13p-fhgb2104-2L-e actually provides, why KohakuTPU is four independent meshes rather than one, and what each die ended up holding.
tags:
  - kohakutpu
  - device
  - floorplan
---

# The ship and the device

A **ship** is one complete assembly floorplanned for a specific device. This page
is KohakuTPU's: which part, why the machine is shaped the way the silicon forced
it to be, and what each die holds.

The framework's side of assembly — how a ship is generated, what a mesh map
contains, how the interlink works — is [arch/ship/](../../arch/ship/README.md)
and [arch/physical/](../../arch/physical/README.md). This page is the choices, not
the mechanism.

---

## 1. The device

`xcvu13p-fhgb2104-2L-e`. Everything downstream of the format hangs off two facts
about it: **DSP48E2 rather than DSP58**, so there is no native INT8 SIMD and the
packing in [matmul.md](matmul.md) exists to build one; and **four SLRs**, so the
machine is four machines.

| | per SLR | device |
|---|---|---|
| CLB LUT | 432,000 | 1,728,000 |
| CLB FF | 864,000 | 3,456,000 |
| BRAM36 | 672 | 2,688 |
| URAM288 | 320 | 1,280 |
| DSP48E2 | 3,072 | 12,288 |
| clock regions | 32 (8 wide x 4 tall) | 128 |
| Laguna sites | 3,840 end dies, 7,680 middle | 23,040 |

**The four SLRs are identical.** An exhaustive site census shows the same hard IP
in all four, with two asymmetries only: the end dies have one Laguna face rather
than two, and SLR1 is the master, so configuration and the device-DNA and
user-eFUSE primitives live there.

**There is no hard DDR controller** — that primitive is Versal-only on this
family. The XIPHY is hard and the controller is soft RTL, about 11.9k LUT /
13.5k FF / 25.5 BRAM36, roughly 2.8% of one SLR's LUTs. **A DDR4 interface
cannot span SLRs**, which is what makes the memory map below a constraint rather
than a preference.

**This part has no HBM.** There is no fallback if the DDR4 channels are not
enough.

### 1.1 Crossing an SLR

| | |
|---|---|
| boundaries | 3 |
| SLLs per boundary | 23,040, **shared between both directions** |
| measured crossing delay | 0.755 ns (0.096 clock-to-Q + 0.659 SLL route), -2L |
| latency | 1 cycle, transmit register to receive register |

At 300 MHz the crossing alone is about 23% of the period. One hard rule follows
and it is the reason a cluster is what it is: **carry chains, DSP cascades and
BRAM/URAM cascades do not propagate across a boundary.** SLLs are the only data
connection between dies, so **every cluster must be SLR-resident** — the DSP
cascade in [matmul.md](matmul.md) §3 is a physical object that cannot be cut.

A crossing also has to be `flop -> SLL -> flop` with nothing in between, because a
Laguna site *is* a flip-flop and a single combinational gate on the path — an AND
with a valid, a mux on a ready — forfeits it and turns the crossing into ordinary
interconnect.

### 1.2 The memory map is not the obvious one

**Exactly one DDR4 controller per SLR**, and the channel numbering does not match
the die numbering:

| channel | SLR | notes |
|---|---|---|
| `ddr4_c0` | SLR3 | |
| `ddr4_c1` | SLR2 | the single-mesh design on the card today |
| `ddr4_c2` | SLR0 | |
| `ddr4_c3` | SLR1 | **XDMA/PCIe is also here** |

So **XDMA lands in SLR1**, and it is expensive: measured at 76,319 LUT and 72,059
FF, **17.7% of an SLR on its own** ([results.md](results.md) §5.2). Whichever die
hosts PCIe gives up roughly a vector core's worth of fabric to do it, which is
why the smallest mesh goes there.

---

## 2. Four meshes, not one — decided by measurement

The obvious arrangement is one large mesh spanning the die. **It was implemented,
and rejected on measurement**: its worst path was 4.6 ns at 98.3% routing with
zero logic levels. A path that is almost entirely route and has no logic in it
cannot be fixed by pipelining the logic, because there is none.

What replaced it is **four independent meshes, one per SLR, each with its own
DDR4**, joined memory-agent to memory-agent by an explicit registered link. The
fact the whole arrangement rests on is the one-controller-per-SLR line above: **no
mesh ever needs a cross-SLR path to its own DRAM**, so the only nets that cross
are the four links.

| mesh | SLR | size | DRAM | why this die |
|---|---|---|---|---|
| 0 | SLR0 | 6+4 | `ddr4_2` | empty otherwise; only `ddr4_2` is on it |
| 1 | SLR1 | 4+4 | `ddr4_3` | XDMA is here, so it is the most crowded die |
| 2 | SLR3 | 4+4 | `ddr4_0` | the grid diagonal of SLR1 |
| 3 | SLR2 | 6+4 | `ddr4_1` | already held a 6+4 |

`6+4` is six matmul clusters and four vector cores. The small meshes go on SLR1
and its **grid diagonal**, which is where the crowded die's neighbours are
cheapest to reach.

**Three of the four links are SLR-adjacent and one is not, and that is forced** —
a 4-cycle cannot embed in a 4-node path without one edge spanning. The long one is
what a plain shift-register pipe exists for, legal precisely because the link
protocol is credit-based and has no handshake to preserve. **Add stages there and
nowhere else**: a pipeline stage anywhere with a real ready signal reintroduces
the combinational crossing the link asserts against.

Every mesh master sees only its own DRAM's 4 GB at offset 0. The mesh id rides the
interlink header rather than the local address, which is why a mesh's masters need
no address-decode change to become one of four.

> **Populations move between generations, and the pages here name different
> ones.** The four-mesh plan above is 6+4 / 4+4 / 4+4 / 6+4. A placed run recorded
> in [notes/cache/](../../notes/cache/README.md) measured meshes of 6+2 and 6+0
> from the placed hierarchy. Both are true of their own build. Treat a population
> as a property of a named build, never as a property of "the ship", and check
> which build a figure came from before carrying it.

---

## 3. Mesh shapes, and what a router costs

The generated mesh maps that exist are named by router grid and population:

| map | population | notes |
|---|---|---|
| 2x1 | 6+0 | both routers fully packed — local, north, south and one of west/east are all endpoints. Six clusters on two routers instead of four |
| 2x2 | 6+0, 6+2, 6+4, 4+4 | the 6+0 variant is 6+2 with the vector cores replaced by nulls, so router shape and memory-agent placement are identical and only the endpoints move |
| 3x2 | 6+3, 6+4 | the 6+3 map is row-local — every row is agent, matmul, matmul, vector, so nothing crosses a column |

Two things about that table are the actual design content.

**A cluster may sit on a router edge port, not only on a local.** That is what
lets a 2x2 grid carry six clusters and four vector cores: the east column's
clusters hang off the routers' *east* ports rather than requiring another router
row.

**Router count is the thing being economised.** This is also why a cluster has one
mesh port rather than two ([isa.md](isa.md) §2.1): eight clusters at two locals
each force a 4x4 grid where one local each fits 2x4, and a router is thousands of
LUTs apiece. The second endpoint bought no bandwidth, because the link is full
duplex and the two ends loaded opposite directions of it.

The row-local 3x2 6+3 map exists for the same reason in a different currency:
keeping every cluster's traffic inside its own row means nothing crosses a column,
which is a routing property rather than a bandwidth one.

---

## 4. What the machine is bound by

At the cluster level, the machine is **DSP-bound**, which is the correct place to
be bound on this part — a cluster is essentially all DSP and its fabric cost is
the manager, the sequencer and the mesh attachment rather than the arithmetic
([matmul.md](matmul.md) §6). The exact cluster count the DSPs admit depends on
which cluster measurement is used and both are in [results.md](results.md) §5.1.

At the vector level it is the opposite: the vector core is **fabric-bound**, at
roughly 37% of an SLR's LUTs for 128 lanes against 12.5% of its DSPs
([vector-core.md](vector-core.md) §2). So the two units bind on different
resources, and a mesh's population is a trade between them rather than a single
scaling knob.

At the *device* level neither is what ran out first. The placed multi-mesh design
measured **URAM at 120 of 1,280 — 9.38%** and one die at **95.80% CLB**, so the
binding resource on a populated die is fabric and placement rather than any hard
block. That is what makes the accumulator's move to URAM free
([accumulator.md](accumulator.md) §1.1) and what motivates the staging discussion
in [notes/cache/](../../notes/cache/README.md).

**The vector core count stops at 16 for a reason worth naming precisely.**
Throughput is still near linear there and vector occupancy only falls from 97% to
88% between 8 and 16 cores. **16 is where the device runs out**, not where the
architecture stops paying.

---

## 5. What has and has not been through place-and-route

This is the caveat that governs everything in [results.md](results.md).

Almost every frequency and utilisation figure this project quotes is
**out-of-context synthesis**: nothing is placed and the route is estimated. That
makes utilisation reliable and every Fmax an **upper bound** — it answers "is the
logic deep enough to fail?", not "will it place".

Placed data exists and it is thinner: a multi-mesh design has been placed and
gives the URAM, CLB and SLL occupancy figures above; a single-mesh design is the
one on the card and is where the host-IP costs were measured. **No cluster-count
scaling figure in this project is a placed result.** Where a page multiplies one
cluster by 32 or 45, that is arithmetic and is labelled as such.

One inconsistency is recorded rather than resolved: the constraints file names the
part without the `L` suffix while everything else says `-2L-e`, and all the
measurements were taken on `-2L-e`. Speed grade changes timing, so a figure taken
against the wrong part number would be wrong in a way nothing else would catch.
