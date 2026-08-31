---
title: KTS measurements
summary: Every measured number for the Kohaku Transmit Surface on xcvu13p-fhgb2104-2L-e — each layer at four widths, the vendor register slices and converters at the same widths, and the link's throughput against its credit bound.
tags:
  - kohakutransmit
  - kts
  - results
  - measurements
---

# KTS measurements

**Device: `xcvu13p-fhgb2104-2L-e`. Tool: Vivado 2024.2. Mode: out-of-context
synthesis, one run per configuration, a 3.333 ns ask on every clock.** Fmax is
`1000 / (3.333 − WNS)`, the synthesis estimate; routing lowers it, and no
figure here is a closed-timing result. Modules were run through
`scripts/tcl/ooc_mod.tcl`, the vendor IP through `scripts/tcl/ooc_kts_ref.tcl`,
and every run's utilisation, hierarchy, 100 worst paths and timing summary are
on disk beside its result line. All dates 2026-08-31.

## 1. What to compare with what

A surface's register stage costs `W + VCW + 2` flip-flops and **no LUT**. The
thing it replaces — one stage of a valid/ready channel — is a skid buffer:
two registers of `W` and the logic that steers them. That comparison is row
one of §2.1 and is the whole cost argument; the rest of the page prices the
ends, the carriers, the switch and the bridges so a link can be budgeted.

## 2. The modules

### 2.1 Ends and stages, by width

| module | W | LUT | FF | BRAM | WNS (ns) | est. MHz |
|---|---|---|---|---|---|---|
| **`kts_pipe`, one stage** (`N` = 1) | any | **0** | `W + VCW + 2` | 0 | no register-to-register path | — |
| `kts_pipe`, two stages (`N` = 2) | 64 / 288 / 512 | 0 | 146 / 594 / 1,042 | 0 | — | — |
| `kts_ref_skid` — one valid/ready stage, the reference | 64 / 288 / 512 / 1024 | 36 / 152 / 266 / 529 | 130 / 581 / 1,031 / 2,059 | 0 | +2.64 / +2.60 / +2.58 / +2.56 | ~1,350 |
| `axis_register_slice`, fully registered (vendor) | 64 / 288 / 512 / 1024 | 76 / 300 / 524 / 1,054 | 136 / 584 / 1,032 / 2,074 | 0 | +2.6 | ~1,340 |
| `axis_register_slice`, SLR-crossing mode (vendor) | 288 | 590 | 887 | 0 | +2.59 | 1,353 |
| **`kts_tx`**, VC 2 | 64 / 288 / 512 / 1024 | 68 / 178 / 290 / 546 | 82 / 306 / 530 / 1,042 | 0 | +2.05 / +1.74 / +1.73 / +1.72 | 782 / 629 / 624 / 618 |
| **`kts_rx`**, VC 2, D 32, LUTRAM | 64 / 288 / 512 / 1024 | 190 / 446 / 702 / 1,288 | 420 / 1,540 / 2,660 / 5,220 | 0 | +1.60 | 576 |
| `kts_rx`, VC 2, D 64, LUTRAM | 288 | 792 | 1,550 | 0 | +1.69 | 607 |
| `kts_rx`, VC 2, **D 128, block RAM** | 288 | **189** | 404 | 9 | +2.11 | 820 |

A sending end at 288 bits is 178 LUT; a receiving end that can cover a
128-cycle round trip is 189 LUT and nine block RAMs. A register stage on the
wire is the row with the zero in it.

### 2.2 Carriers

| module | configuration | LUT | FF | BRAM | WNS | est. MHz |
|---|---|---|---|---|---|---|
| `kts_cdc` (both directions of one surface) | W 64 / 288 / 512 / 1024, VC 2, D 32 | 333 / 589 / 845 / 1,432 | 554 / 1,226 / 1,898 / 3,434 | 0 | +1.83 | 667 |
| `axis_clock_converter` (vendor, one stream) | 288 | 260 | 722 | 0 | +2.35 | 1,018 |
| `axi_clock_converter` (vendor, AXI4 512-bit data, 40-bit address) | 512 | 974 | 3,006 | 0 | +2.56 | 1,295 |
| `kts_wconv` 288 → 144 / 144 → 288 | VC 2, D 32 | 699 / 765 | 1,704 / 1,711 | 0 | +1.55 / +1.60 | 562 / 577 |
| `axis_dwidth_converter` 288 → 144 (vendor) | | 79 | 439 | 0 | +2.70 | 1,585 |
| `kts_over_axis` | W 288, VC 2, D 32 | 415 | 926 | 0 | +1.85 | 676 |
| `kts_over_axi4` | W 288, VC 2, D 32, 512-bit AXI data | 451 | 1,253 | 0 | +1.85 | 672 |
| `kts_over_serial`, `RELIABLE` 0 | W 288, VC 2, D 32, 64-bit words | 1,137 | 2,049 | 0 | +1.60 | 578 |
| `kts_over_serial`, `RELIABLE` 1, `WIN` 32 | same | 1,305 | 2,431 | 0 | +1.25 | 480 |

The width converter is the one place the vendor block is cheaper: KTS's
converter is a receiving end, a shift and a sending end — a repeater with its
own credits on both sides — where the vendor's is a shift between two
valid/ready ports. A `kts_cdc` prices both directions of a surface; the vendor
rows price one stream or one AXI4 port.

### 2.3 The switch

`kts_switch`, `K` = 3, VC 2, D 32 per (input, VC). Three designs were built
and measured; the third ships.

| design | W 64 | W 288 | W 512 | W 1024 |
|---|---|---|---|---|
| heads read straight off the FIFOs | 1,243 LUT / 406 MHz | 5,796 / 356 | 5,462 / 358 | 11,805 / **255** (WNS −0.596) |
| one head register per (input, VC) | 1,197 / 498 | 6,635 / 330 | 6,177 / 408 | 11,793 / 304 |
| **two-entry head queue per (input, VC)** — ships | **1,495 / 447** | **6,891 / 423** | **7,318 / 434** | **15,416 / 390** |
| `K` = 4, W 288, the shipped design | | 5,888 / 393 | | |

The first design's path ran FIFO head → destination range compare →
arbitration → credit → pop → the receiving end's owed counter, 16 levels. The
second moved the compare behind a register but left the FIFO's read enable on
the arbitration, 11 levels into the FIFO's flag logic. The third loads the
queue on a registered count, so the arbiters read registers and the FIFO
reads on a register; every width closes with margin. What remains is the
`K:1` return select of `W` bits per output per VC, which is the switch's
irreducible cost: at W 288 about 5,200 of the 6,891 LUT.

### 2.4 Bridges

| module | configuration | LUT | FF | WNS | est. MHz |
|---|---|---|---|---|---|
| `kts_axi4_in` (AXI4 slave → packets) | W 288, 256-bit data, 40-bit address, 16 slots | 627 | 1,347 | +1.69 | 610 |
| `kts_axi4_out` (packets → AXI4 master) | same | 434 | 1,295 | +1.58 | 572 |
| `kts_axi4_in` / `kts_axi4_out` | W 576, 512-bit data | 912 / 727 | 2,393 / 2,415 | +1.69 / +1.58 | 610 / 572 |
| `axi_register_slice`, AXI4 full, every channel fully registered (vendor) | 256-bit / 512-bit data, 40-bit address | 372 / 644 | 1,420 / 2,508 | +2.72 / +2.71 | ~1,600 |
| `kts_axis_in` / `kts_axis_out` | W 288, VC 2 | 37 / 606 | 306 / 1,543 | +2.25 / +1.83 | 925 / 664 |

A bridge pair at 256-bit data is 1,061 LUT; the vendor's single AXI4 register
slice at that width is 372. The pair is not a register slice — it packs an
AXI4 transaction into a packet that crosses any wire — and the comparison to
make is a pair against the slices, converters and crossings a wire of the
same length would otherwise need on five channels.

## 3. Throughput against the credit bound

`kts_link_tb`, three links on wires of 0, 4 and 32 register stages, two VCs
of 16 credits each, a 4,000-cycle window with every VC offering and popping
every cycle:

| stages | measured flits / cycle | bound `min(1, VC·D / RTT)` |
|---|---|---|
| 0 | **1.000** | 1.000 |
| 4 | **1.000** | 1.000 |
| 32 | **0.420** | 0.450 (`RTT` = 2·32 + 7 = 71) |

Every flit in order, none lost, none duplicated, on all three. The clock
crossing bench delivers **0.9 or more** flits per receiver cycle into a
receiver three times slower than its sender; the serial bench delivers every
flit through a channel dropping one word in forty in both directions.

## 4. The device facts these rest on

Only two: a register stage on this part is flip-flops in a slice, and a die
crossing must be register to register with nothing between. A surface adds
no logic to a stage and puts nothing but registers at a crossing, which is
what the zero in §2.1 says.
