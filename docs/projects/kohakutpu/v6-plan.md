---
title: v6 — the station-bus deployment
summary: KohakuTPU's next device image replaces the SmartConnect tree with a four-station line and narrows XDMA to one channel per direction. What changes, what it recovers, and which die the recovery lands on.
tags:
  - kohakutpu
  - device
  - roadmap
---

# v6 — the station-bus deployment

> **Kind: Yours throughout — a device-image decision.** Which fabric carries host
> traffic, and how many DMA channels to build, are choices this project makes for
> its own image. The station bus it adopts is a separate project with its own
> pages ([projects/kohakuaxi](../kohakuaxi/README.md)); the framework requires
> neither.

**Status: the fabric change shipped.** The station line was validated standalone
against block-RAM endpoints (§4), and the same wrapper — `sb_bd_line4`, with the
`sb_axi2lite` converters §4.1 calls for — is the bus in the `multimesh_v7` image
that runs on the card. §4.1 records what the standalone build did *not*
establish, because that gap is what the converters were added to close.

**Status: the asymmetric mesh growth in §6 has not been built.** Sizing it needs
per-core figures against CLB sites, which §6.1 explains and this page does not
have.

This page is a **roadmap for one device image**, not a specification. What the
station bus *is* — the structure, the invariants, the per-port cost argument and
the full measurement set — is [kohakuaxi/](../kohakuaxi/README.md). This page is
only what deploying it into KohakuTPU's ship changes, and what the change buys.

A **station** is one node of the AXI fabric outside the meshes: it carries local
AXI masters and local subordinates, and has a left and a right neighbour. A
**link** joins two adjacent stations across an SLR boundary under credit flow
control. Neither is a mesh node; the two networks share no vocabulary.

---

## 1. What changes against v5

v5's interconnect is a **tree**: a root SmartConnect (3 slave interfaces,
9 master interfaces) in SLR1, a leaf SmartConnect per other SLR, and register
slices between them. v6 replaces the whole tree with one **line** of four
stations, one per SLR.

| | v5 | v6 |
|---|---|---|
| interconnect | `root_smc` + 3x `leaf_smc` + 3x `slr_cross` | `sb_line4` — 4 stations on a line |
| fabric clock | one SmartConnect on the control clock | one fabric clock **per station** |
| cross-SLR | register slices | credited links, clock crossing inside `sb_link_cdc` |
| status port | AXI GPIO reading interlink `ready`/`fault` | **removed** — §1.1 |
| XDMA | 4 h2c / 4 c2h | **1 / 1** |
| mesh shape | unchanged initially | unchanged initially |

### 1.1 The GPIO endpoint goes away

v5 spends an AXI endpoint on a GPIO so software can read the interlink's
`ready` and `fault` bits. Neither has a station-bus equivalent, because neither
condition can arise:

- **`fault`** exists because the v5 interlink CDC never backpressures — its
  `tready` is tied high — so its FIFO genuinely can overflow. A station-bus flit
  departs only against a credit, so overflow is **structurally impossible**
  rather than merely unlikely.
- **`ready`** exists because the vendor FIFO primitive drops writes while its
  reset is busy. The station bus's `async_fifo` folds that into `wr_full`, so a
  writer that respects the flag cannot lose a beat.

Endpoints per SLR therefore drop from five to four, and nothing is gathered
across SLRs to read status.

---

## 2. Topology, and why a line rather than a star

```
   SLR0 ──── SLR1 ──── SLR2 ──── SLR3
    │       ╱  │  ╲      │         │
   subs  mgrs  subs     subs      subs
```

No root. Every station is the same module with local managers, local
subordinates and two neighbours. The managers sit on station 1 because that is
where XDMA's hard block is, not because station 1 is privileged.

| | |
|---|---|
| masters | JTAG 64-bit @100 MHz, XDMA 512-bit @250 MHz, XDMA-Lite 32-bit @250 MHz |
| slaves | 4 per station: mesh `S_AXI_MEM` **256-bit**, mesh CTRL, DDR controller, clock wizard |
| protocol | AXI4 throughout; the 32-bit ports are AXI4-Lite, served through `sb_axi2lite` |
| address width | **43** |

**A star from SLR1 puts two links across the SLR1↔SLR2 boundary**, because
SLR1↔SLR3 must traverse it. Derived cross-SLR wire counts at the deployed
configuration:

| boundary | line | star |
|---|---|---|
| SLR0↔SLR1 | 1,171 | 1,172 |
| SLR1↔SLR2 | 1,171 | **2,344** |
| SLR2↔SLR3 | 1,171 | 1,172 |

The line is also what the RTL expresses: a station has a left neighbour, a right
neighbour and three 2:1 muxes. A third link port does not exist.

### 2.1 The address map is v5's, not a generated one

The station bus imposes no map — the segment base, mask, destination and port
are parameters — so v6 is **given** v5's map rather than choosing one.

| window | offset | range | station | reachable by |
|---|---|---|---|---|
| `ddr4_{id}` control | `id * 0x100000` | 1 MB | id | all masters |
| `mesh_{id}` CTRL | `0x800000 + id*0x10000` | 64 KB | id | all masters |
| clock wizard, 64 KB each | `0x900000 +` | 64 KB | its own SLR | all masters |
| `mesh_{id}` MEM | `(id+1) << 40` | 1 TB | id | XDMA `M_AXI` only |

Two properties of that map must survive into v6:

- **Every control register sits below 4 GB**, because XDMA's `M_AXI_LITE` is
  32-bit. The 1 TB mesh windows are explicitly excluded from that address space.
- **Mesh MEM is selected by address bits `[42:40]`**, value `id+1`. That is how
  XDMA reaches a specific memory agent, and it is why the master side needs 43
  address bits. The low 40 bits pass through to the mesh untouched.

The uniform "top bits select the station" map inside `sb_line4.v` is a **test
fixture** for simulation and the standalone block design. It is not v6's map.

---

## 3. Cross-SLR wire budget

23,040 SLLs per boundary, shared between both directions ([ship.md](ship.md)
§1.1). Wire counts are **derived** from the flit widths at
`STNW=2 PORTW=2 SRCW=2 TAGW=4 AW=43`; each stream costs its payload width plus
two.

| | per boundary | % of budget |
|---|---|---|
| v5 interlink CDC (386/direction) | 772 | 3.35% |
| v6 line, FW=512, `LINK_FULL=0` | 1,173 | 5.09% |
| **v6 line, FW=256, `LINK_FULL=0`** | **629** | **2.73%** |
| v6 line, FW=512, `LINK_FULL=1` | 2,346 | 10.18% |

**The derivation is checked against placement, not trusted.** The standalone
validation build measures **639, 634 and 644** SLLs on the three boundaries
against 629 derived — 2.75–2.80% of budget, so the model is good to about 2%.

**Set `LINK_FULL=0`.** With every manager on station 1, requests only flow
outward and responses only inward, so half the link streams would be dead — but
instantiating them still builds their pipeline registers and still spends the
wires. `LINK_FULL=1` is a *topology declaration* for a line with masters on both
sides of a boundary, not an option to weigh on area.

**SLL is not the binding constraint, so keep the mesh-to-mesh interlink as it
is.** There is no wire argument for replacing it: at FW=256 the station link is
merely comparable, not decisively better.

---

## 4. Floorplan, and what the validation build establishes

`sb_line4` goes in as **one RTL block** containing all four stations and the
links. Pblocks target cell paths, so this places identically to four separate
blocks while avoiding four hand-maintained block-design wrappers.

| pblock | contents |
|---|---|
| `pb_slr0` | station 0, mesh_0, its DDR4, its clock wizards |
| `pb_slr1` | station 1, mesh_1, DDR4, clock wizard, **XDMA**, JTAG-AXI, control clocking |
| `pb_slr2` | station 2 and that SLR's mesh/DDR4/clock wizard |
| `pb_slr3` | station 3 and that SLR's mesh/DDR4/clock wizard |
| **unpinned** | the link pipeline registers — they **are** the die crossing and must be free to place |

`CONTAIN_ROUTING` is false on each, as in v5.

**The validation build closes and writes a bitstream.** Three JTAG masters into
sixteen block-RAM endpoints, the real station line between them,
`xcvu13p-fhgb2104-2L-e`, Vivado 2024.2, **placed and routed**:

| | |
|---|---|
| WNS | **+0.018 ns**, 0 failing of 152,262 endpoints |
| WHS | +0.010 ns, 0 failing |
| CLB LUTs / registers | 24,554 / 47,795 |
| SLLs per boundary | 639 / 634 / 644 — all 2.8% of budget |
| `write_bitstream` | completed |

Bus 200 MHz, control 100, XDMA 250, DDR 300, mesh clocks 180–300. Ten clocks,
all constrained, all met.

### 4.1 What the block-RAM endpoints do not establish

That build proves the **fabric and the floorplan**. It does not prove the
station bus can attach to the real endpoints, because block-RAM controllers are
AXI4 at whatever width they are asked for. Connecting a mesh and a DDR4
controller instead surfaces two conversions the SmartConnect tree was performing
silently:

| endpoint | station port | needs |
|---|---|---|
| DDR4 controller `S_AXI_CTRL` | AXI4 with a 4-bit ID | the port is AXI4-Lite with *no* ID; a plain protocol converter keeps the ID and is rejected |
| mesh `S_AXI_CTRL` | 32-bit | the mesh control port is **64-bit** |

The second is a **generator limit, not a fabric one**: the subordinate shim
accepts any port width up to the flit width, but the wrapper generator rejects a
width that is neither 32 nor the flit width, so a 64-bit port cannot be emitted
today. Widening that check is the cheaper fix and removes the converter
entirely.

Any claim that the v6 block design is ready means **ready for those stand-ins**.
The real mesh needs the two conversions above, or a wrapper that can emit their
widths.

### 4.2 Two constraints this floorplan needs, which are easy to omit

Both are tool behaviour rather than design content, and both fail quietly:

- **The reference clock needs an explicit `create_clock`.** With no board part,
  nothing else defines it, no MMCM output clock derives from it, every
  `get_clocks` matches nothing, and place-and-route reports enormous slack on a
  design it was never asked to time.
- **One reference feeding several MMCMs across SLRs needs
  `CLOCK_DEDICATED_ROUTE ANY_CMT_COLUMN`**, named by a **load** pin — the
  auto-inserted global buffer has no name until `opt_design` runs.

A third belongs with them: `set_clock_groups` built from
`get_pins -hier -filter NAME` resolves to nothing, so the constraint becomes a
silent no-op. Use literal paths in braces, and error if a group resolves empty.

---

## 5. XDMA at one channel per direction

**Built at 4 h2c / 4 c2h; the driver opens one of each.** Measured in this
project, out-of-context synthesis at 4/4 on `xcvu13p-fhgb2104-2L-e`,
`report_utilization -hierarchical`:

| | LUT | FF | RAMB36 |
|---|---|---|---|
| `xdma_0` total | **76,319** | 72,059 | 124 |
| └ `udma_wrapper` — the block that scales with channels | 62,136 | 44,090 | 10 |

AMD's published 512-bit sweep gives 4/4 → 1/1 as **−17,165 LUT, −15,314 FF,
−48 RAMB36**, about 5,722 LUT per h2c+c2h pair, linear. **That delta is
extrapolation** — the published rows are Versal parts, applied to a measured
UltraScale+ baseline; [xdma-channels.md](xdma-channels.md) states what would
settle it.

1/1 costs nothing functionally, since the driver already opens one channel
whatever the fabric carries. It gives up roughly 15% of peak DMA bandwidth — one
engine near 10.8 GB/s against ~12.6 GB/s usable on Gen3 x16 — which nothing
currently uses.

---

## 6. What the freed resource buys

Two inputs, and the second is extrapolated rather than measured.

**Interconnect.** At the deployed configuration — `FW=256`, `AW=43`,
`BALANCED`, no block RAM, `LINK_FULL=0`, `LINK_CDC=1` — the whole four-station
line measures **22,106 LUT / 48,167 FF**, against v5's tree total of 81,881 LUT
/ 130,124 FF: **3.70x**. Per die the saving concentrates where the tree's root
sat — SLR1 goes from 41,788 to 8,756, a factor of **4.77**. Both columns and
their per-row provenance are in
[kohakuaxi/station-bus.md](../kohakuaxi/station-bus.md) §2.8.

**XDMA**, above: about 17,000 LUT and 48 RAMB36, all on SLR1.

Together, roughly **50,000 LUTs on SLR1** before any mesh change.

### 6.1 SLR1 is the emptiest die, not the fullest

From v5's own post-placement report — **placed and routed**, the whole four-mesh
design:

| | SLR0 | SLR1 | SLR2 | SLR3 |
|---|---|---|---|---|
| **CLB occupancy** | **95.49%** | **88.61%** | 91.14% | 90.79% |
| CLB LUTs | 296,764 | 263,137 | 280,927 | 299,757 |
| LUT occupancy | 68.70% | 60.91% | 65.03% | 69.39% |
| CLB registers | 292,934 | 289,289 | 281,248 | 295,743 |
| BRAM | 439 (65.3%) | 342 (50.9%) | 410 (61.0%) | 439 (65.3%) |
| URAM | 171 (53.4%) | 116 (36.3%) | 158 (49.4%) | 171 (53.4%) |
| DSP | 2,444 (**79.6%**) | 1,382 (45.0%) | 2,124 (69.1%) | 2,444 (79.6%) |

Whole device: 1,140,585 LUTs (66.0%), 1,159,214 FF (33.5%), 1,630 BRAM (60.6%),
616 URAM (48.1%), 8,394 DSP (68.3%).

**SLR1 is the least utilised die on every axis**, because it carries the
interconnect and the DMA but the smallest share of mesh. So the interconnect
saving lands where there was already the most room, and it does **not** relieve
the binding constraint — which is SLR0, at 95.49% CLB and 79.6% DSP.

**The design is CLB-packing-bound, not LUT-bound.** Every die sits at 88–95% CLB
occupancy while using only 61–69% of its LUTs. Adding logic needs CLB *sites*,
and a saving expressed in LUTs converts into sites only as well as the placer
packs what remains. A LUT count is therefore the wrong unit for sizing a mesh
increment, and this is the one place on this page where the obvious arithmetic
misleads.

### 6.2 The consequence: grow mesh_1 only

Cores can only go where there is room, and after the saving that is SLR1:

- **SLR1 DSP is at 45% against 79.6% on SLR0 and SLR3.** On DSP alone it has
  room for roughly three quarters again as much compute as it now holds.
- Its URAM (36.3%) and BRAM (50.9%) have comparable headroom.
- Its CLB occupancy, 88.61%, is the lowest of the four, and the ~50,000 LUTs
  recovered from interconnect and XDMA sit inside that die.

So v6 is **asymmetric: grow mesh_1 and nothing else.** SLR0 and SLR3 are at 95%
CLB and 80% DSP and cannot take more whatever the interconnect costs. **Any plan
that adds cores uniformly across the four dies is bounded by SLR0 and will fail
placement long before SLR1 fills.**

**Sizing the increment is not settled here.** It needs per-core resource figures
against CLB sites rather than LUTs, which is a mesh question rather than an
interconnect one and is not measured on this page.

---

## 7. Settled parameter choices

| knob | value | why |
|---|---|---|
| `FW` | **256** | The widest slave, mesh `S_AXI_MEM`, is 256 bits, so the fabric meets it exactly and the 512-bit XDMA master splits 2:1. 22,106 LUT against 30,785 at 512, and 629 cross-SLR wires against 1,173. At 200 MHz it carries 6.4 GB/s, which is what the 512-bit-at-100 MHz SmartConnect it replaces provided. |
| `AW` | **43** | Forced: mesh MEM sits at `(id+1) << 40`. Costs 3.6% over 32 bits. |
| bus clock | **200 MHz** | Meets the bandwidth above. Sweeping the constraint from 150 to 500 MHz moves area by 0.3% up to 300 MHz, then 6.3% at 350 and 11% beyond, and the structure saturates near 390 MHz — so 200 is free, and so would 300 be if the bandwidth were ever wanted. |
| `LINK_FULL` | **0** | A declaration, not a saving. Every master sits on station 1. |
| mesh-to-mesh interlink | **keep** | No SLL argument for replacing it (§3). |

The bus-clock row is **out-of-context synthesis on a synthesis-only netlist**,
and an out-of-context Fmax bounds what the RTL can do rather than what a placed
design will do. §4's routed result is the one to act on.
