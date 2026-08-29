---
title: KohakuAXI
summary: A line of identical AXI stations replacing a large crossbar — what it is, where it sits, what a port costs, and what it deliberately does not do.
tags:
  - kohakuaxi
  - axi
  - interconnect
  - overview
---

# KohakuAXI — the station bus

> **Kind: Yours — this is a project, not framework machinery.** The station bus is
> an AXI fabric KohakuTPU adopts; nothing in the framework requires it, and a
> device image may use a vendor crossbar instead. What it must present where it
> meets a mesh is Fixed protocol, because `S_AXI_MEM` and `S_AXI_CTRL` are
> ([arch/axi](../../arch/axi.md)).

KohakuAXI is **two systems**, and this page is about the first:

| | what it connects | structure | page |
|---|---|---|---|
| **the station bus** | a host DMA engine, its narrow register port and a JTAG debug master to every DRAM controller, every mesh and every clock wizard across the dies | a line of identical stations, credited links between them | this page, [station-bus.md](station-bus.md) |
| **the Xache** (xbar-cache; `kx_` = Kohaku-Xache System) | M AXI masters to N DRAM channels, each channel fronted by a cache | one system with AXI only at its edges: per-home arrays, control-only engines, registered binary-index muxes, a crossing only at a port whose clock differs, channel interleaving by an address-bit swap, a streaming read engine with a per-master read queue across the channels | [xbar-cache.md](xbar-cache.md) |

They share no module and are never conflated: the station bus carries *host*
traffic to endpoints of many widths and clocks; the crossbar-cache carries
*memory* traffic at one width into DRAM, with a cache in the path. The rest of
this page is the station bus. It is **a line of identical stations**, not a
crossbar and not a tree.

It is the third project in this directory, and the only one that is not an
accelerator. Nothing in its design knows about an accelerator: it assumes an
FPGA with LUT6s and a die-to-die wire budget, and nothing else. KohakuTPU
appears in its pages as a worked deployment, never as a requirement.

**Device for every figure on this page: `xcvu13p-fhgb2104-2L-e`, Vivado 2024.2.**
Resource figures are out-of-context synthesis unless a row says *placed*.
[station-bus.md](station-bus.md) §2 carries every measurement with the sweep
that produced it; this page quotes only the ones needed to say what the thing
costs.

---

## 1. What it is

**S stations on a line. There is no root.** Every station is the same module —
`sb_stn_line` — carrying any number of local AXI **managers** (masters) and any
number of local AXI **subordinates** (slaves), including zero of either, and
having exactly **two neighbours**: a left and a right.

```
   station 0 ──────── station 1 ──────── station 2 ── … ── station S-1
   │  │  │  │         │  │  │  │         │  │  │  │        │  │  │  │
   M  M  S  S         M  S  S  S         S  S  S  S        M  M  M  S
```

The ends simply lack one neighbour. Where the managers sit is a deployment
choice, not a property of the structure — one station, several, or none.

A **flit** is what the fabric carries: a routed packet with the address, the
burst description and one flit-width slice of data. A **station** is one node of
this fabric; it is unrelated to a mesh node, and the two networks share no
vocabulary.

Two shims stand between AXI and the fabric, and they are where the whole design
lives:

| | |
|---|---|
| **NMU** (`sb_nmu`) | one external AXI manager onto the station. Packs the port's beats into whole flits; reserves response space before injecting. |
| **NSU** (`sb_nsu`) | the station onto one external AXI subordinate. Unpacks each flit into beats of that port's own width; issues its own local ID. |

**Width, clock domain and protocol are all resolved at the shim — once per port,
never pairwise between ports.** The fabric between the shims carries only whole
flits, so no endpoint ever sees a transfer shaped by some other port, and no
width appears anywhere except at the two ports it belongs to. That single
sentence is the entire cost argument, and §4 is the measurement of it.

### 1.1 The switch, and routing

```
to_right ← mux2(from_left,  inject)
to_left  ← mux2(from_right, inject)
eject    ← mux2(from_left,  from_right)
```

Three 2:1 muxes and a K:1 injection mux. That is the whole switch.

A flit carries `dst_stn` and `dst_port`, and each hop makes **one comparison**:
equal to my index, eject; less, pass left; greater, pass right. The address map
is static and known at build time, so the NMU does not route — it **labels**.
Each hop deciding for itself is what lets a flit pass *through* a station it does
not belong to, without that station knowing anything about it.

Ejection to Q subordinates is a **broadcast with a per-port valid gate**, which
is decode rather than a mux tree. That is why no term in the cost multiplies the
manager count by the subordinate count.

### 1.2 Four invariants

1. A manager may not inject until response buffer space is **reserved**.
2. Request and response never share a buffer.
3. Arbitration is **packet-atomic** — a grant is held until `last`, which gives
   AXI4's no-write-interleaving rule for free.
4. **IDs do not cross the fabric; routes do.** The flit carries its source
   station and port, and each subordinate shim issues its own local ID.

Invariants 1 and 2 together are the deadlock argument: a full response path
cannot block the request path that would drain it.

---

## 2. Where it sits

Above it: the host. PCIe arrives through an XDMA hard block, and JTAG arrives
through a debug master. Both are AXI managers on a station.

Below it: DRAM controllers, clock wizards, and **the meshes** — each mesh
presenting a wide memory port and a narrow control port.

```
   host (PCIe)          JTAG
       │                  │
     XDMA ────────────────┴──────  managers, all on one station
       │
   ┌───┴────┐  ┌────────┐  ┌────────┐  ┌────────┐
   │ stn 0  ├──┤ stn 1  ├──┤ stn 2  ├──┤ stn 3  │   one per die
   └───┬────┘  └───┬────┘  └───┬────┘  └───┬────┘
       │           │           │           │
    mesh_0      mesh_1      mesh_2      mesh_3      + DDR4 ctrl, clk wizard
```

**AXI is never wired directly to a memory agent.** A mesh's memory and control
ports are station subordinates like any other endpoint, and everything the host
reaches inside a mesh it reaches through this fabric. That is not a convention —
it is what makes the address map, the width conversion and the clock crossing
one problem solved in one place instead of four.

The **interlink** — the credit-flow link joining one mesh's memory agent to the
next one's — is a *different* connection and stays as it is. It carries
mesh-to-mesh traffic; the station bus carries host-to-mesh traffic. There is no
wire-budget argument for merging them (§4.3).

---

## 3. The interface it presents

Every port is ordinary AXI. That is the point: nothing on either side is aware
of the fabric.

| | |
|---|---|
| protocol | AXI4 at every port. A port declared Lite is emitted as a **true AXI4-Lite interface** — no ID signals, the AXI4-only fields tied to the constants Lite implies. |
| width | any port width from 8 bits up to the flit width converts fully. A manager **wider** than the flit splits, which is the supported direction. |
| clock | **a shim takes no parameter describing the clock relationship** — not the ratio, not whether it is synchronous. Each station runs its own fabric clock and the crossing lives inside the link. A port that *is* on its station's fabric clock declares `SAME_CLK = 1` and its queues become synchronous FIFOs: a crossing only where the clocks differ. |
| burst | a port that only ever issues single beats declares `SINGLE_BEAT = 1` on its subordinate shim (`NSB` on `sb_line4`): its channel queues become one-entry skids and the slice walk folds to constants. A port that bursts keeps the general walk; the bench refuses the declaration on a port that bursts. |
| ID | the port's own, whatever width. IDs terminate at the shim. |

### 3.1 Lite ports are served, not tolerated

**Every Lite subordinate port is `sb_nsu` plus a real burst-to-Lite converter,
`sb_axi2lite`.** The NSU re-expresses fabric flits as bursts; a Lite subordinate
cannot see `AWLEN`, so the converter walks the burst itself — one Lite handshake
per strobed beat, one read per slice, IDs and `last` regenerated, responses
coalesced worst-case.

That converter is load-bearing rather than tidy. Terminating a burst by
signalling instead of walking it leaves orphan write beats parked at the
endpoint: every later address then pairs with a stale beat, and reads answer only
at flit lane 0. A DDR4 controller's `S_AXI_CTRL` port and a clock wizard's
register port therefore connect **directly**, with no protocol-converter IP and
no 1×1 vendor interconnect in front of them.

### 3.2 Width conversion happens in the shim, outside the FIFOs

The conversion is native, and where it sits in each shim matters:

- the **NMU** packs beats into whole flits **after** its request FIFO, and
  serves each returned flit out beat by beat at the port's own size;
- the **NSU** unpacks flits into port-width beats **before** its request FIFO,
  and packs read words back into flits **after** its response FIFO.

So the FIFOs stay at their fabric widths and depths, and the conversion logic is
the only thing that grows. Any transfer size up to the port width, any
alignment, and bursts crossing flit boundaries are all legal.

**A subordinate wider than the flit is not supported, and fails loudly.** The
NSU has a scatter path but no gather path, so a wide subordinate would be driven
from a single narrow flit and have its read data replicated back — a
configuration that elaborates, synthesises and simulates without a warning while
corrupting every wide beat. A generate block instantiates an undefined module to
turn it into an elaboration error instead. Raise the flit width, or put a width
converter in front of that one port.

---

## 4. What it costs

### 4.1 The deployed line

Four stations, four subordinates each, three managers on station 1, flit width
256, address width 43, four outstanding transactions with store-and-forward, no
block RAM, per-station fabric clocks. **Out-of-context synthesis.**

| | |
|---|---|
| CLB LUTs | **22,106** — 14,492 as logic, 7,614 as distributed RAM |
| CLB registers | **48,167** |
| block RAM / SRL | 0 / 0 |
| control sets | 975 |

Against the SmartConnect tree it replaces, at the same endpoint set:
**81,881 LUT and 130,124 FF, so 3.70x** — and **4.77x on the one die that
carried the tree's root**. The per-die breakdown and the provenance of each
SmartConnect row are [station-bus.md](station-bus.md) §2.8; that column is not
uniformly from one build, and the page says which row came from where.

The same line at the **ship recipe** — block-RAM FIFOs, outstanding 4/8/2 on the
three managers, the control manager placing rather than packing — measures
**23,053 LUT, 42,223 FF, 90 BRAM**, of which the manager station on SLR1 is
**8,044**. Per port on that station: hub set 2,122; the 512-bit and 64-bit
managers 1,158 and 967; the 32-bit control manager 609; the 512-bit subordinate
760; each 32-bit subordinate 808–811. [station-bus.md](station-bus.md) §2.17
carries the breakdown.

### 4.2 Per port, which is the claim worth testing

Measured by sweeping one port count at a time:

| port | LUTs |
|---|---|
| 32-bit subordinate | **818** |
| 512-bit subordinate | **1,621** |
| 32-bit manager | 822–1,217 |
| 512-bit manager | **4,212** |

**Sixteen times the width for 1.98x the port cost**, arriving independently from
the manager side and the subordinate side. Cost tracks width and buffering, not
port count, and most of it is buffer.

The comparison that matters is against a crossbar **at the same port width**. At
three managers, nine subordinates, 512-bit, the marginal subordinate port costs
`axi_interconnect` 2,083 LUTs and a rebuilt SmartConnect 1,999, against the
station's 1,621. Holding the 32-bit figure of 818 beside a 512-bit crossbar port
would flatter this design by a factor of two.

**What the sweeps do not show:** each holds the other side fixed, so they
establish linearity in each count separately. Independence between them remains a
structural argument — ejection is a broadcast with a valid gate — for which the
fits are consistent evidence, not proof.

### 4.3 Clock domains, the strongest claim

Going from one clock domain to four, three managers by nine subordinates,
512-bit, same harness:

| structure | ΔLUT | ratio |
|---|---|---|
| `axi_interconnect`, max-performance | **+10,693** | 2.35x |
| `axi_interconnect`, minimum-area | +6,394 | 2.13x |
| **station bus** | **−328** | **0.99x** |

Four asynchronous domains, with the crossings inside credited links, are had for
nothing measurable. This is the design's central claim measured against vendor IP
on the axis it claims, rather than argued.

*(A rebuilt SmartConnect reports +525 for the same change. That is not a good
result — it is a failure showing up as a number: its distributed RAM is identical
to the digit in both rows, and an added asynchronous domain cannot be free in a
structure whose crossings are LUTRAM FIFOs. The reconstruction does not build the
domains however they are declared, so SmartConnect has no clock-axis baseline at
all.)*

### 4.4 Cross-SLR wires

23,040 SLLs per boundary, shared between both directions. A boundary carries
**two** streams — one request, one response — because a manager both reads and
writes across it.

| | per boundary | % of budget |
|---|---|---|
| line, FW=256 | **629** derived, **639/634/644** placed | 2.73–2.80% |
| line, FW=512 | 1,173 | 5.09% |

The derivation is checked against placement and is good to about 2%, with the
expected asymmetry — requests leave the manager station, responses return.
**SLL is not the binding constraint on this part.**

### 4.5 Placed and routed

The line in a block design with one pblock per die and the links deliberately
unpinned, driven by three JTAG masters into sixteen block-RAM endpoints:
`write_bitstream` reached, WNS **+0.018 ns**, TNS 0.000, 0 failing of 152,262
endpoints, all ten clocks constrained and met.

**The endpoints are block RAM, not the meshes**, so that run measures the fabric
and its floorplan rather than a finished system. The finished system is
KohakuTPU's `multimesh_v7` image, which carries the same wrapper with the Lite
converters of §3.1 in it.

### 4.6 Latency and bandwidth

| | |
|---|---|
| latency, 32-bit single-beat read | 21 control cycles local, 27–28 one hop, 31 two hops |
| sustained write bandwidth, one outstanding burst | 4.97 GB/s local, falling to 3.38 GB/s two hops away |

**Both are floors.** At one outstanding burst roughly half the elapsed time is
turnaround, so the bandwidth figure is latency-bound rather than width-bound.
Nothing here measures a pipelined manager, and **nothing measures the tree's
latency for comparison** — so "lower latency than a crossbar" is not a claim this
project can make in either direction.

---

## 5. What it deliberately does not do

- **No runtime observability or remapping.** The address map is static and
  decided at build time. Optional counters expose flits and waits as plain
  wires, but reading them still needs a port wired outside the fabric — a
  config port reachable only through the fabric cannot debug a hung fabric.
- **No multicast.** It would be nearly free in a broadcast-ejection station and
  the invariant break has a known fix, but nothing issues one, and adding a
  deadlock-relevant path with no test that exercises it is worse than not having
  the feature.
- **No head-to-head performance comparison against the vendor IP.** There is no
  SmartConnect or `axi_interconnect` cycle measurement anywhere, so "faster"
  stays structural rather than measured.
- **No power result.** A routed vectorless estimate exists at Medium confidence
  with no real traffic behind it, and no counterpart exists for the tree. A real
  answer needs activity-driven estimation on both.
- **It is not a win on flip-flops.** Every station output is registered by
  construction and the die-crossing pipelines are flops by design — six link
  instances carry 12,166 of the deployed line's 48,167 registers. In the
  single-die case it wins 0.72x on LUTs and **loses** on registers outright.

### When not to build this

Two or three ports on one clock at one width; or the whole `managers × subordinates × width` product is
genuinely small; or you need runtime observability and remapping.

**The case for it is heterogeneity, not size** — many clocks, many widths, many
dies. The measurements say the same thing from the other end: strip the
heterogeneity away and the margin collapses to 0.72x on a single die, while the
four-die replacement wins 3.70x overall and 5.97x on the inter-die tissue alone.
With one clock, one width and one die, the interesting number is 0.72x and it is
not worth new RTL.

---

## 6. Where to read next

- **[station-bus.md](station-bus.md)** — the design in full: the structure and
  its invariants, every measurement with the sweep that produced it, the vendor
  comparisons, the width rules, what may differ per station, the configuration
  presets, the cost model and the verification status.
- **[xbar-cache.md](xbar-cache.md)** — the second system: the fused
  crossbar-cache between AXI masters and DRAM channels, its clock model, its
  two read engines and the read queue, its whole measured tables, its per-knob
  costs, its measured bandwidth and the vendor path at the same shape.
- **[../kohakutpu/v6-plan.md](../kohakutpu/v6-plan.md)** — one deployment of it:
  what replacing KohakuTPU's SmartConnect tree changed, which die the recovered
  resource landed on, and why that die was not the one that needed it.
- **[../../arch/axi.md](../../arch/axi.md)** — the framework's own statement of
  which AXI dialects are spoken and where each appears.

RTL: `src/kohakuaccel/axi/` — `station/` for the shims and the switch, `link/`
for the credited crossing, `topo/` for the line and the sweep tops, `bd/` for the
generated block-design wrappers.
