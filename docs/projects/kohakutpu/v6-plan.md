---
title: v6 — station-bus line, and what the freed resource buys
summary: Replace v5's SmartConnect tree with a four-station line, drop XDMA to one channel per direction, and spend the recovered CLB on cores.
tags:
  - multimesh
  - plan
---

# v6 — station-bus line

v5's interconnect is a tree: `root_smc` (3 SI, 9 MI) in SLR1, a `leaf_smc` per
other SLR, and `slr_cross` register slices between them. v6 replaces the whole
tree with one line of four stations, one per SLR.

## 1. What changes

| | v5 | v6 |
|---|---|---|
| interconnect | `root_smc` + 3× `leaf_smc` + 3× `slr_cross` | `sb_line4` — 4 stations on a line |
| fabric clock | SMC on the ctrl clock | one fabric clock **per station** |
| cross-SLR | `slr_cross` register slices | credited links, CDC inside `sb_link_cdc` |
| status port | AXI GPIO reading interlink `ready`/`fault` | **removed** — see below |
| XDMA | 4 h2c / 4 c2h | **1 / 1** |
| mesh shape | unchanged initially | unchanged initially |

### The GPIO goes away

v5 spends an AXI endpoint on a GPIO so software can read `mag_link_cdc`'s
`ready` and `fault`. Neither has a station-bus equivalent:

- `fault` exists because `mag_link_cdc` never backpressures — `tready` is tied
  high — so its FIFO genuinely can overflow. A station-bus flit only departs
  against a credit, so overflow is **structurally impossible**.
- `ready` exists because XPM drops writes during reset-busy. `async_fifo` folds
  that into `wr_full`, so a writer that respects the flag cannot lose a beat.

So endpoints per SLR drop from five to four, and nothing is gathered across
SLRs to read status.

## 2. Topology

```
   SLR0 ──── SLR1 ──── SLR2 ──── SLR3
    │       ╱  │  ╲      │         │
   subs  mgrs  subs     subs      subs
```

No root. Every station is `sb_stn_line` with local managers, local
subordinates, and two neighbours. Managers happen to sit on station 1 because
that is where XDMA's hard block is.

| | |
|---|---|
| masters | jtag 32-bit @100 MHz, XDMA 512-bit @250 MHz, XDMA-Lite 32-bit @250 MHz |
| slaves | 4 per station: mesh `S_AXI_MEM` **256-bit**, mesh CTRL, DDR ctrl, clk_wiz |
| protocol | AXI4 full; 32-bit ports are single-beat AXI4-Lite-compatible |
| **address width** | **43** |

### The address map is v5's, not a generated one

From `multimesh_v5_bd.tcl:598-631`. The station bus imposes no map —
`SEG_BASE`/`SEG_MASK`/`SEG_DST`/`SEG_DPORT` are parameters — so v6 is given
this one.

| window | offset | range | station | reachable by |
|---|---|---|---|---|
| `ddr4_{id}` ctrl | `id * 0x100000` | 1 MB | id | all masters |
| `mesh_{id}` CTRL | `0x800000 + id*0x10000` | 64 KB | id | all masters |
| clk_wiz, 64 KB each | `0x900000 +` | 64 KB | its own SLR | all masters |
| `mesh_{id}` MEM | `(id+1) << 40` | 1 TB | id | XDMA `M_AXI` only |

Two properties of this map must survive into v6:

- **Every control register sits below 4 GB**, because XDMA's `M_AXI_LITE` is
  32-bit. The 1 TB mesh windows are explicitly excluded from that address space
  (`multimesh_v5_bd.tcl:632-635`).
- **Mesh MEM is selected by address bits [42:40]**, value `id+1` — this is how
  XDMA reaches a specific MAG, and why the master side needs 43 bits. The low
  40 bits pass through to the mesh untouched.

`axi_gpio_0` at `0x400000` disappears in v6 with the interlink status port it
served.

The uniform "top bits select the station" map in `sb_line4.v` is a **test
fixture** for the simulation and the standalone BD. It is not v6's map.

### Why a line and not a star

A star from SLR1 puts two links across the SLR1↔SLR2 boundary, because
SLR1↔SLR3 must traverse it:

| boundary | line | star |
|---|---|---|
| SLR0↔SLR1 | 1,171 | 1,172 |
| SLR1↔SLR2 | 1,171 | **2,344** |
| SLR2↔SLR3 | 1,171 | 1,172 |

The line is also what the architecture actually expresses: a station has a left
and a right neighbour and three 2:1 muxes. A third link port does not exist.

## 3. Cross-SLR budget

23,040 SLLs per boundary, shared both directions
(`docs/projects/kohakutpu/ship.md:59`).

Derived from `sb_line4.v:121` at `STNW=2 PORTW=2 SRCW=2 TAGW=4 AW=43`; each
stream costs `W+2`. Earlier revisions of this table used `AW=40`.

The validation build's placement measures the deployed row directly: **639, 634
and 646** SLLs on the three boundaries against 629 derived — 2.75–2.80% of
budget, and the derivation is good to about 2%.

| | per boundary | % |
|---|---|---|
| v5 interlink (`mag_link_cdc`, 386/direction) | 772 | 3.35% |
| v6 line, FW=512, `LINK_FULL=0` | 1,173 | 5.09% |
| v6 line, FW=256, `LINK_FULL=0` | 629 | 2.73% |
| v6 line, FW=512, `LINK_FULL=1` | 2,346 | 10.18% |

**Set `LINK_FULL=0`.** With every manager on station 1, REQ only flows outward
and RSP only inward, so half the link streams are dead — but instantiating them
still builds their pipeline registers and still spends the wires.

SLL is not the binding constraint. **Keep `mag_link` for mesh-to-mesh** — there
is no SLL argument for replacing it, and at FW=256 the station link is merely
comparable, not decisively better.

One free win independent of all this: `mag_ilink`'s `TUSER_W` is 96 but only 73
bits are defined (`mag_ilink.v:151-178`), so 23 bits per direction cross the die
carrying zeros — **138 nets across v5**, recoverable by narrowing the parameter.

## 4. XDMA at 1/1

Measured today at 4/4: **76,319 LUT, 72,059 FF, 124 RAMB36**, of which
`udma_wrapper` — the block that scales with channels — is 62,136 LUT (81%).

AMD's published 512-bit sweep gives 4/4 → 1/1 as **−17,165 LUT, −15,314 FF,
−48 RAMB36**, about 5,722 LUT per h2c+c2h pair, linear.

The driver opens only `h2c_0`/`c2h_0`, so 1/1 costs nothing functionally. It
gives up roughly 15% of peak DMA bandwidth (one engine ≈10.8 GB/s against
~12.6 GB/s usable on Gen3 x16), which nothing currently uses.

Details and the QDMA comparison: `docs/projects/kohakutpu/xdma-channels.md`.

## 5. Block design and floorplan

`sb_line4` goes in as **one RTL block** containing all four stations and the
links. Pblocks target cell paths, so this places identically to four separate
blocks while avoiding four hand-maintained BD wrappers and their AXIS plumbing.

| pblock | contents |
|---|---|
| `pb_slr0` | `u_line/g_stn[0].*`, mesh_0, its ddr4, its clk_wiz, its bus clk_wiz |
| `pb_slr1` | `u_line/g_stn[1].*`, mesh_1, ddr4, clk_wiz, **xdma_0**, jtag_axi, clk_wiz_ctrl |
| `pb_slr2` | `u_line/g_stn[2].*` and that SLR's mesh/ddr4/clk_wiz |
| `pb_slr3` | `u_line/g_stn[3].*` and that SLR's mesh/ddr4/clk_wiz |
| **unpinned** | `u_line/g_link[*]` — the pipeline registers ARE the die crossing and must be free to place |

`set_property CONTAIN_ROUTING false` on each, as v5 does.

### The validation build closes

`scripts/tcl/v6_bd.tcl -tclargs impl` builds this floorplan in a throwaway
project — three `jtag_axi` masters into sixteen block-RAM endpoints, the real
station line between them — and runs it to a bitstream:

| | |
|---|---|
| WNS | **+0.018 ns**, 0 failing of 152,262 endpoints |
| WHS | +0.010 ns, 0 failing |
| CLB LUTs / Registers | 24,554 / 47,795 |
| SLLs per boundary | 639 / 634 / 644, all 2.8% of budget |
| `write_bitstream` | completed |

Bus 200 MHz, ctrl 100, XDMA 250, DDR 300, mesh clocks 180–300; ten clocks, all
constrained, all met. The endpoints are block RAM rather than the mesh, so this
validates the fabric and the floorplan, not the finished system.

### What the block-RAM endpoints hid

That build proves the fabric and the floorplan, **not** that the station bus can
attach to the real endpoints. Connecting a mesh and a MIG instead of block RAM
surfaces two conversions SmartConnect was doing silently:

| endpoint | station port | needs |
|---|---|---|
| `ddr4_*/C0_DDR4_S_AXI_CTRL` | AXI4, 4-bit ID | a 1×1 **SmartConnect** — the port is AXI4-Lite with *no* ID, and `axi_protocol_converter` keeps the ID and fails BD 41-237 |
| `mesh_*/S_AXI_CTRL` | 32-bit | `axi_dwidth_converter` — the mesh control port is **64-bit** |

The second is a generator limit, not a fabric one: `sb_nsu` accepts any
`SDW <= FW`, but `gen_station_wrap` rejects a width that is neither 32 nor `FW`,
so a 64-bit port cannot be emitted today. Widening that check is the cheaper fix
and removes the converter.

Neither appears in the validation build because block RAM controllers are AXI4
at whatever width you ask for. Any claim that the v6 BD is "ready" means ready
for those stand-ins; the real mesh needs the two converters above or a wrapper
that can emit their widths.

Three things had to be fixed before the validation build ran, each of which
would have shipped silently:

- The `jtag_axi` masters were 32-bit. The station field sits at bit `AW-4` = 39,
  so three of the four stations were unreachable and would have gone untested.
- `set_clock_groups` was built from `get_pins -hier -filter NAME`, which
  resolves empty — the constraint was a no-op, which is the same failure that
  produced the retracted "+14.241 ns" result. It now uses literal paths and
  errors if a group resolves to nothing.
- Every endpoint segment was unassigned, because the exclusion loop ran before
  the address spaces existed. They are now assigned at the map the RTL decodes,
  so the address editor and the hardware agree.

Two constraints carried over from the validation build, both of which cost real
time when missed:

- The reference clock needs an explicit `create_clock`. With no board part,
  nothing else defines it, no MMCM output clock derives, every `get_clocks`
  matches nothing, and place-and-route reports enormous slack on a design it
  was never asked to time.
- One reference feeding several MMCMs across SLRs needs
  `CLOCK_DEDICATED_ROUTE ANY_CMT_COLUMN`, named by a **load** pin — the
  auto-inserted BUFG has no name until `opt_design`.

## 6. Resource budget — **pending**

Two inputs, one of which does not exist yet:

1. **Interconnect saving.** At matched port count (3 SI, 9 MI, 512-bit, four
   clocks): `root_smc` 41,788 LUT from the project's own OOC report, against a
   station at 15,780 (`BALANCED`, no block RAM) — 2.65×. A SmartConnect rebuilt
   at that shape reads 21,885, so it is 1.39× against the rebuild and 2.65×
   against the shipped instance; the shipped figure is the one that matters
   here, and the rebuild is only good for comparing shapes.

   The whole-line figure at the deployed configuration (`FW=256`, `AW=43`, no
   block RAM, `LINK_FULL=0`, `LINK_CDC=1`) is **22,106 LUT / 48,167 FF**,
   against v5's tree total of 81,881 LUT / 130,124 FF — **3.70×**. Per die the
   saving concentrates where the device is full: SLR1 is 8,756 against
   `root_smc`'s 41,788, a factor of 4.77.
2. **XDMA saving**, above: ~17k LUT and 48 RAMB36 on SLR1.

**SLR1 is where the saving lands** — that is where `root_smc` (41,788 LUT) and
XDMA (76,319 LUT) both sit. The interconnect frees 33,032 LUTs there; XDMA at
1/1 frees about 17,165 more. Roughly **50k LUTs on SLR1**, before any mesh
change.

### SLR1 is the emptiest die, not the fullest

v5's own post-placement report (`JTAG-DMA-test.runs/impl_1/`) contradicts the
assumption this section used to carry:

| | SLR0 | SLR1 | SLR2 | SLR3 |
|---|---|---|---|---|
| **CLB occupancy** | **95.49%** | **88.61%** | 91.14% | 90.79% |
| CLB LUTs | 296,764 | 263,137 | 280,927 | 299,757 |
| LUT occupancy | 68.70% | 60.91% | 65.03% | 69.39% |
| CLB Registers | 292,934 | 289,289 | 281,248 | 295,743 |
| BRAM | 439 (65.3%) | 342 (50.9%) | 410 (61.0%) | 439 (65.3%) |
| URAM | 171 (53.4%) | 116 (36.3%) | 158 (49.4%) | 171 (53.4%) |
| DSP | 2,444 (**79.6%**) | 1,382 (45.0%) | 2,124 (69.1%) | 2,444 (79.6%) |

Whole device: 1,140,585 LUTs (66.0%), 1,159,214 FF (33.5%), 1,630 BRAM (60.6%),
616 URAM (48.1%), 8,394 DSP (68.3%).

SLR1 is the least utilised die on **every** axis — it carries the interconnect
and the DMA but the smallest share of mesh. So the interconnect saving lands
where there is already the most room, and it does **not** relieve the binding
constraint. That is SLR0, at 95.49% CLB and 79.6% DSP.

**The design is CLB-packing-bound, not LUT-bound.** Every die sits at 88–95%
CLB occupancy while using only 61–69% of its LUTs. Adding logic needs CLB
*sites*, and a saving expressed in LUTs converts into sites only as well as the
placer can pack what remains.

### What the freed resource actually buys

Cores can only go where there is room, and after the saving that is SLR1:

- **SLR1 DSP is at 45% against 79.6% on SLR0/SLR3.** On DSP alone it has room
  for roughly three quarters again as much compute as it now holds.
- Its URAM (36.3%) and BRAM (50.9%) have comparable headroom.
- Its CLB occupancy, 88.61%, is the lowest of the four, and the ~50k LUTs
  recovered from interconnect and XDMA sit inside that die.

So the v6 proposal is **asymmetric: grow mesh_1 only.** SLR0 and SLR3 are at
95% CLB and 80% DSP and cannot take more whatever the interconnect costs. Any
plan that adds cores uniformly across the four dies is bounded by SLR0 and will
fail placement long before SLR1 fills.

Sizing the increment needs the per-core resource figures, which are a mesh
question rather than an interconnect one, and are not measured here.

The core-count recommendation depends on v5's post-placement per-SLR
utilisation, which is still running.

## 7. Open decisions

- ~~**FW=512 or FW=256.**~~ **Decided: 256.** The mesh's `S_AXI_MEM` is 256 bits,
  so the fabric meets its widest slave exactly, and the only 512-bit master
  (XDMA) splits 2:1 through `sb_nmu` — a case now simulated, at 606 checks, and
  one that required clamping `RSP_DEPTH` to `AXI_MAXB * SUB` because it
  otherwise hangs silently. Measured cost on the four-station line at `AW=43`,
  no block RAM: **22,106 LUT against 30,785** at 512, and 629 cross-SLR wires
  per boundary against 1,173. At 200 MHz it carries 6.4 GB/s, matching the
  512-bit-at-100 MHz SmartConnect it replaces.
- **Bus clock.** jtag stays 100 MHz; the fabric is independent and per-station.
  The star figure this bullet used to quote came from a topology that was
  abandoned, and OOC Fmax on a synthesis-only netlist is not a placement
  result either way. At the deployed configuration the binding clock is
  `bus_clk1` — the station carrying the three masters — at 357.9 MHz OOC
  against a 200 MHz request, with all eleven clocks constrained and met.
  **Sweeping the constraint from 150 to 500 MHz moves area by 0.3% up to
  300 MHz**, then 6.3% at 350 and 11% beyond, and the structure saturates near
  390 MHz. So 200 MHz is free, and so would be 300 if the bandwidth were ever
  wanted. 200 is the working target because it matches what the SmartConnect
  tree provided.
- **Whether to keep `mag_link`.** Yes, on this evidence.
