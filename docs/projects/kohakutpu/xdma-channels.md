# XDMA channel count

**We BUILD 4 h2c / 4 c2h and USE one of each.** `multimesh_v5_bd.tcl:239-240`
sets `CONFIG.xdma_rnum_chnl {4}` and `CONFIG.xdma_wnum_chnl {4}`; the driver
opens `h2c_0` and `c2h_0` and nothing else
(`driver/kohakuaccel/transport/xdma.py:165`).

**So the recommendation below is a change that has not been made.** An earlier
version of this page stated 1/1 as the configuration, which it is not, and the
measurement in the next section says 4/4 in its own caption.

## Why one would be enough

## What the extra channels cost

Measured, this project, post-synthesis at 4/4
(`report_utilization -hierarchical`, xcvu13p):

| | LUT | FF | RAMB36 |
|---|---|---|---|
| `xdma_0` total | 76,319 | 72,059 | 124 |
| └ `pcie4_ip_i` | 12,377 | 27,966 | 22 |
| └ `udma_wrapper` | 62,136 | 44,090 | 10 |
| └ `ram_top` | 1 | 1 | 92 |

81% of the LUTs are in `udma_wrapper`, the block that scales with channels.

AMD's published sweep, 512-bit datapath, all else equal:

| channels | LUT | FF | RAMB36 |
|---|---|---|---|
| 1 / 1 | 41,234 | 28,909 | 52 |
| 2 / 2 | 46,328 | 34,168 | 68 |
| 4 / 4 | 58,399 | 44,223 | 100 |

4/4 → 1/1 is **−17,165 LUT, −15,314 FF, −48 RAMB36**; ~5,722 LUT per extra
h2c+c2h pair, linear.

## What narrowing would cost

One H2C engine peaks near 10.8 GB/s against ~12.6 GB/s usable on Gen3 x16, so
1/1 gives up roughly 15% of peak DMA bandwidth — which is what the driver
already gives up, since it opens one channel whatever the fabric carries.
Narrowing to 1/1 recovers the LUTs without changing what the driver can do; 2/2
is the compromise if a second engine is ever wanted, and either is a two-token
edit in the BD script.

## Alternatives considered

**QDMA**, AMD's tables for xcvu13p Gen3 x16: AXI-MM 63,067 LUT / 106 RAMB36
against our measured 76,319 / 124 — about 13k LUT, less than trimming channels
saves, and it is a queue-based architecture requiring a full rewrite of
`transport/xdma.py`. Rejected.

**Descriptor bypass and PCIe-to-AXI bypass** are already off
(`multimesh_v5_xdma_0_0.xci:97`, `:53`), so no saving is available there.

## Open

AMD publishes no UltraScale+ rows in DMA mode, so the sweep above is Versal
parts; ratios are consistent across two devices but the absolute deltas applied
to our number are extrapolation. Confirm with one OOC synth at 1/1 against the
76,319 baseline — `scripts/tcl/ooc_xdma.tcl` does exactly that.

Unreconciled: hardware enumeration found only **two** H2C and two C2H engines,
not the four configured.
