---
title: XDMA channel count
summary: Four h2c and four c2h are built, one of each is used, and what narrowing to 1/1 would recover on the die that carries PCIe.
tags:
  - kohakutpu
  - host
  - measurements
---

# XDMA channel count

> **Kind: Yours throughout — a device-image parameter.** The channel count is a
> vendor IP configuration this project chose for its own image, and the recovery
> discussed is a property of that choice. The framework requires no particular
> host DMA arrangement.

**XDMA** is the vendor PCIe-to-AXI DMA block: it is how the host reaches this
accelerator's DRAM and control registers, and it is a station-bus master
([kohakuaxi/](../kohakuaxi/README.md)). Its **channel count** is how many
independent DMA engines it instantiates, `h2c` host-to-card and `c2h` card-to-
host. Channels are a synthesis-time parameter, not a runtime one.

**This build has four of each and the driver opens one of each.** The block
design sets `xdma_rnum_chnl` and `xdma_wnum_chnl` to 4; `driver/kohakuaccel/
transport/xdma.py` opens `h2c_0` and `c2h_0` and nothing else.

**Narrowing to 1/1 is a recommendation, not a description.** It has not been
made, and the measurement below is of the 4/4 configuration that is built.

## What the extra channels cost

**Measured in this project**, out-of-context post-synthesis at 4/4,
`report_utilization -hierarchical`, `xcvu13p-fhgb2104-2L-e`, Vivado 2024.2:

| | LUT | FF | RAMB36 |
|---|---|---|---|
| `xdma_0` total | 76,319 | 72,059 | 124 |
| └ `pcie4_ip_i` | 12,377 | 27,966 | 22 |
| └ `udma_wrapper` | 62,136 | 44,090 | 10 |
| └ `ram_top` | 1 | 1 | 92 |

81% of the LUTs are in `udma_wrapper`, which is the block that scales with the
channel count. The whole instance is **17.7% of one SLR**, which is the
constraint behind the floorplan in [ship.md](ship.md) §1.2.

## What 1/1 would recover

AMD's published sweep, 512-bit datapath, all else equal:

| channels | LUT | FF | RAMB36 |
|---|---|---|---|
| 1 / 1 | 41,234 | 28,909 | 52 |
| 2 / 2 | 46,328 | 34,168 | 68 |
| 4 / 4 | 58,399 | 44,223 | 100 |

4/4 → 1/1 is **−17,165 LUT, −15,314 FF, −48 RAMB36**; about 5,722 LUT per extra
h2c+c2h pair, linear across the range.

> **That delta is EXTRAPOLATION, not a measurement of this design.** AMD
> publishes no UltraScale+ rows in DMA mode, so the sweep above is for Versal
> parts, and applying its deltas to the measured 76,319 assumes the ratio
> carries across families. It is consistent across the two devices that are
> published, which is weak evidence and is all there is. **One out-of-context
> synthesis at 1/1 against the 76,319 baseline would settle it** —
> `scripts/tcl/ooc_xdma.tcl` does exactly that run and has not been used for it.

## What narrowing would cost

One h2c engine peaks near 10.8 GB/s against roughly 12.6 GB/s usable on
Gen3 x16, so 1/1 gives up about 15% of peak DMA bandwidth — **which is what the
driver already gives up**, since it opens one channel whatever the fabric
carries. Narrowing recovers the LUTs without changing what the driver can do.
2/2 is the compromise if a second engine is ever wanted, and either is a
two-token edit in the block-design script.

## Alternatives considered

**QDMA.** AMD's tables for `xcvu13p` Gen3 x16 give AXI-MM at 63,067 LUT and 106
RAMB36 against this project's measured 76,319 and 124 — about 13,000 LUT, less
than trimming channels saves, and it is a queue-based architecture requiring a
full rewrite of the transport layer. **Rejected.**

**Descriptor bypass and PCIe-to-AXI bypass** are already off in the shipped
configuration, so no saving is available there.

## Open

**Hardware enumeration finds two H2C and two C2H engines, not the four
configured.** That is unreconciled, and it matters for this page: if the built
instance is really 2/2, the 76,319 baseline is measuring something other than
what the block design asks for, and the saving from narrowing is smaller than
the table above implies. Settle it before spending the recovered LUTs.
