---
title: multimesh v5 interconnect and address map, as built
summary: The SmartConnect topology, port map, clocking and address assignment of the shipping v5 design, recorded as the reference a purpose-built in-device AXI bus would have to match or beat.
tags:
  - arch
  - ship
  - axi
  - reference
---

# multimesh v5 interconnect and address map, as built

Ground truth, not a proposal. This is what `scripts/tcl/multimesh_v5_bd.tcl`
constructs today, recorded before any work starts on replacing the SmartConnect
chain with a purpose-built bus in `kohakuaxi`. Numbers marked MEASURED come from
post-synth utilization of the four-mesh design on `xcvu13p-fhgb2104-2L-e`.

Any replacement has to serve every row of §2 and §5 and fit inside §6.

## 1. Physical arrangement

The die is four SLRs in a line, `SLR0|SLR1|SLR2|SLR3`. **SLR1 is the master
SLR** on this part: the configuration bank (bank 65), the JTAG/BSCAN logic and
the tandem-capable PCIe blocks are all there, so XDMA and `jtag_axi` are
anchored to it and cannot move.

Mesh-to-SLR is deliberately NOT identity:

| SLR | mesh | shape | clusters | vector |
|---|---|---|---|---|
| SLR0 | `mesh_0` | 2x2 | 7 | 2 |
| SLR1 | `mesh_1` | 1x2 | 4 | 0 |
| SLR2 | `mesh_3` | 2x2 | 6 | 2 |
| SLR3 | `mesh_2` | 2x2 | 7 | 2 |

`leaf_smc_N` is indexed by **SLR**, not by mesh -- `leaf_smc_2` lives in SLR2 and
serves `mesh_3`. This trips everyone once.

SLR1 carries the smallest mesh because it also carries XDMA, `root_smc`,
`jtag_axi`, the control clock and one DDR4 controller. Even so it measured
99.27% CLB against 88.70% for the cleanest SLR, which is why `mesh_1` is 4
clusters and the others are 6 or 7.

## 2. Topology

```
jtag_axi  ─┐
xdma M_AXI ┼─► root_smc (SLR1) ─► M00 ─► slr_cross_0 ─► leaf_smc_0 (SLR0)
xdma LITE ─┘                    ─► M02 ─► slr_cross_2 ─► leaf_smc_2 (SLR2)
                                │                          └─ M03 ─► slr_cross_3 ─► leaf_smc_3 (SLR3)
                                └─► M01/M03/M04 ─► SLR1's own slaves (no leaf)
```

Three masters, two levels, one chained hop. SLR1 has **no leaf** -- `root_smc`
drives its slaves directly, because a leaf there would cross nothing. That merge
measured 36.0k + 11.5k = 47.5k -> 44.5k LUT.

SLR3 hangs off SLR2 rather than the root. Two reasons, both measured: it halves
SLR3's SLL crossings (`NUM_SLR_CROSSINGS` 2 -> 1), and it removes a master port
from `root_smc`, which is LUT out of the tightest SLR.

`slr_cross_N` are `axi_register_slice` with `NUM_SLR_CROSSINGS 1` and all five
channels at `REG_* 15`. They are deliberately NOT pblocked -- the parameter
exists so Vivado can distribute the pipeline stages across the boundary, and a
pblock would force them to one side.

## 3. Port map

### root_smc -- `NUM_SI 3`, `NUM_MI 5 + nclk` (= 9), `NUM_CLKS 4`

| port | destination |
|---|---|
| S00 | `jtag_axi_0/M_AXI` |
| S01 | `xdma_0/M_AXI` |
| S02 | `xdma_0/M_AXI_LITE` |
| M00 | `slr_cross_0` -> `leaf_smc_0` |
| M01 | `mesh_1/S_AXI_MEM` |
| M02 | `slr_cross_2` -> `leaf_smc_2` |
| M03 | `mesh_1/S_AXI_CTRL` |
| M04 | `ddr4_3/C0_DDR4_S_AXI_CTRL` |
| M05..M08 | `clk_wiz_{ctrl,mesh_0..3}/s_axi_lite` (one per generator) |

Clocks: `aclk` control 100 MHz, `aclk1` XDMA, `aclk2` mesh_1 fabric, `aclk3` DDR.
`aclk1` carries XDMA and NOT `aclk`: `aclk` is SmartConnect's primary and the
whole crossbar runs on it, so binding XDMA there freezes JTAG whenever PCIe is
unplugged.

### leaf_smc_N -- `NUM_SI 1`, `NUM_CLKS 3`

`NUM_MI 3`, except `leaf_smc_2` at 5.

| port | destination |
|---|---|
| M00 | `mesh_<mid>/S_AXI_MEM` |
| M01 | `mesh_<mid>/S_AXI_CTRL` |
| M02 | `ddr4_<did>/C0_DDR4_S_AXI_CTRL` |
| M03 | `slr_cross_3` -> `leaf_smc_3` (leaf_smc_2 only) |
| M04 | `axi_gpio_0/S_AXI` (leaf_smc_2 only) |

Clocks: `aclk` control, `aclk1` that SLR's mesh fabric, `aclk2` its DDR `ui_clk`.

The GPIO sits on SLR2's leaf, not the root, and has no IO pins at all -- its LED
port was removed because bank 64 is in SLR1 and the pin anchored it there.

## 4. What each mesh exposes

| interface | direction | width | domain |
|---|---|---|---|
| `S_AXI_MEM` | slave | 40-bit address, DW 256 | `axi_aclk` |
| `S_AXI_CTRL` | slave | control window | `axi_aclk` |
| `M_AXI_DRAM` | master | to its own DDR4 | `dram_aclk` |
| `M_AXIS_LINK0/1`, `S_AXIS_LINK0/1` | stream | `LKW 288` + `LKU 96` TUSER | `axi_aclk` |

`S_AXI_MEM` omits `awsize/awburst/awlock/awcache/awprot/awqos` and their AR
equivalents. `awlen` IS present, so bursts work -- the slave is full-width INCR
only, by design.

The mesh-to-mesh links do not go through the SMC at all. They are six
`mag_link_cdc` instances (`xpm_fifo_async`, independent `wr_clk`/`rd_clk`) on an
open chain `mesh_0 - mesh_1 - mesh_3 - mesh_2`; the two ends
(`mesh_0/S_AXIS_LINK0`, `mesh_2/S_AXIS_LINK1`) are tied off and report as
`BD 41-759` on every build.

## 5. Address map

Two disjoint regions. Everything a replacement bus must decode.

### Control, below 4 GiB -- reachable by all three masters

Asserted at build time: every `M_AXI_LITE` segment must end below 4 GiB, or the
script errors. XDMA's AXI-Lite is 32-bit and cannot reach higher.

| base | range | target |
|---|---|---|
| `0x000000 + id*0x100000` | 1 MiB | `ddr4_<id>/C0_DDR4_MEMORY_MAP_CTRL/C0_REG`, id 0..3 |
| `0x400000` | 64 KiB | `axi_gpio_0/S_AXI/Reg` |
| `0x800000 + id*0x10000` | 64 KiB | `mesh_<id>/S_AXI_CTRL/reg0`, id 0..3 |
| `0x900000 + n*0x10000` | 64 KiB | `clk_wiz_{ctrl,mesh_0..3}/s_axi_lite/Reg` |

The GPIO is a single 12-bit read-only channel: 6 `fault` then 6 `ready`, one per
`mag_link_cdc`. Its data register is at offset **0x00** (it was channel 2 at 0x08
while the LEDs occupied channel 1).

### Mesh memory -- `jtag_axi_0/Data` and `xdma_0/M_AXI` only

| mesh | window base | range |
|---|---|---|
| `mesh_0` | `0x100_0000_0000` | 1 TiB |
| `mesh_1` | `0x200_0000_0000` | 1 TiB |
| `mesh_2` | `0x300_0000_0000` | 1 TiB |
| `mesh_3` | `0x400_0000_0000` | 1 TiB |

Explicitly EXCLUDED from `xdma_0/M_AXI_LITE`.

**The window is a routing prefix, not address space.** `S_AXI_MEM` is a 40-bit
slave, so `reg0` is one fixed 1 TiB segment that only lands on a 1 TiB boundary
-- at 64 GiB spacing Vivado silently discards the offsets and places all four
meshes at 0. The interconnect consumes bits above 39 and passes `addr[39:0]`
through untouched, so the mesh receives the full global address with `[39]`
aperture and `[37:36]` mesh intact. The mesh id therefore appears TWICE, once in
the window and once in the address; a replacement bus may drop the prefix
entirely if it routes on `[37:36]` directly. See `docs/address-map.md`.

Each mesh's `M_AXI_DRAM` has its DDR4 at offset 0, range 4 GiB -- MAG strips the
mesh and aperture bits before driving `dram_*`, so base 0 is required.

## 6. Cost, MEASURED post-synth

| instance | n | Total LUTs | Logic LUTs | LUTRAMs | SRLs | FFs |
|---|---|---|---|---|---|---|
| `root_smc` | 1 | 47,693 | 35,503 | 11,748 | 442 | 66,276 |
| `leaf_smc` | 3 | 8,538 | 5,031 | 3,452 | 55 | 16,667 |
| `slr_cross` | 3 | 3,265 | 1,663 | 0 | 1,602 | 4,996 (8,255 at 2 crossings) |
| `mag_link_cdc` | 6 | 139 | 139 | 0 | 0 | 595 |
| `axi_gpio` | 1 | 86 | 86 | 0 | 0 | 212 |

Those figures predate the SLR rebalance -- `root_smc` was `7+nclk` MI then and
is `5+nclk` now, so it should be smaller. Retake before using as a baseline.

**Interconnect total is roughly 82k LUT**, about 7% of the design's 1.19M, and
`root_smc` alone is 4% of SLR1. That is the number a purpose-built bus is
competing against.

SLL usage, post-placement, 7,814 total:

| FROM \ TO | SLR3 | SLR2 | SLR1 | SLR0 |
|---|---|---|---|---|
| SLR3 | 0 | 935 | 56 | 1 |
| SLR2 | 1,059 | 0 | 1,523 | 0 |
| SLR1 | 102 | 1,845 | 0 | 1,133 |
| SLR0 | 0 | 0 | 1,000 | 0 |

## 7. Constraints a replacement inherits

1. **Three masters, mixed width.** `jtag_axi` and `xdma M_AXI` reach mesh memory
   at 43-bit addresses; `xdma M_AXI_LITE` is 32-bit and control-only.
2. **Control must stay below 4 GiB.** Enforced at build; XDMA AXI-Lite cannot
   reach above it.
3. **Four clock domains meet in the root** -- control 100 MHz, XDMA, mesh_1
   fabric, DDR `ui_clk` -- and three in each leaf. Any replacement crosses them.
4. **SLR crossings are the scarce resource**, not bandwidth. Reaching a
   non-adjacent SLR costs two SLL hops, which is why the chain exists.
5. **Only one IO-driven BUFG may feed the MMCMs**, and it needs
   `CLOCK_DEDICATED_ROUTE ANY_CMT_COLUMN` -- five MMCMs across four SLRs exceeds
   `rule_bufg_mmcm_3loads` otherwise. Unrelated to the bus, but it shares SLR1.
6. **`root_smc` is pblocked to SLR1** with XDMA and JTAG, which is the SLR at
   99.27% CLB. Anything that shrinks it there is worth more than the same saving
   elsewhere.

## 8. Where a purpose-built bus could win

Recorded as questions, not answers.

- **Drop the 1 TiB window prefix.** Routing on `[37:36]` directly removes 3 bits
  of address from every comparator in the fabric and the double encoding of the
  mesh id.
- **The mesh slave is already simplified** -- full-width INCR, no size/burst/
  lock/cache/prot/qos. A bus that assumes that carries far less than AXI4.
- **Only three masters, and their access patterns differ sharply**: XDMA is bulk
  streaming, JTAG is low-rate debug, AXI-Lite is control. One crossbar serving
  all three is likely over-general.
- **SLR crossing is the real cost.** A bus designed around a chain-of-adjacent-
  hops topology, rather than a crossbar that happens to be pblocked into one,
  would treat that as the primary structure.
- **The link CDCs are already a separate, simpler transport** and do not touch
  the SMC. Whether mesh memory traffic should join them rather than the AXI
  fabric is an open question.
