---
title: Station bus — a line of AXI stations
summary: Replaces a large AXI crossbar with a line of identical stations joined by credit-flow links. Per-port cost is O(1) because every width, clock and protocol difference is resolved once at the port instead of pairwise between ports.
tags:
  - axi
  - interconnect
  - design
  - general
---

# Station bus — a line of AXI stations

Many AXI masters to many slaves without a crossbar, when the endpoints
span multiple clock domains, data widths and dies. Nothing here assumes an
accelerator or a vendor part beyond "FPGA with LUT6 and a die-to-die budget".

This is a general AXI fabric. It is used by KohakuTPU, but nothing in the
design knows that: KohakuTPU appears here only as a worked example in §2, and
"one station per SLR on a 4-SLR part" is *that example's* shape, not a rule.

## 1. What it is

**S stations on a line. There is no root.** Every station is the same module,
carrying any number of local AXI masters and any number of local AXI slaves,
including zero of either, and having exactly two neighbours.

```
   station 0 ──────── station 1 ──────── station 2 ── … ── station S-1
   │  │  │  │         │  │  │  │         │  │  │  │        │  │  │  │
   M  M  S  S         M  S  S  S         S  S  S  S        M  M  M  S
```

S is arbitrary and the ends simply lack one neighbour. Where the masters sit is
a deployment choice: one station, several, or none on a given station.

A master plugs in through an **NMU** shim, a slave is driven by an **NSU**
shim, and those two shims are where width, clock domain and protocol are
resolved — once per port, never pairwise between ports.

### The switch

```
to_right ← mux2(from_left,  inject)
to_left  ← mux2(from_right, inject)
eject    ← mux2(from_left,  from_right)
```

Three 2:1 muxes and a K:1 injection mux. That is the entire switch.

- **Ejection to Q slaves is free** — broadcast the payload, gate each
  `valid` with `dst_port == my_index`. A demux is decode, not a mux tree. This
  is why the `K×Q` term never appears; preserve it above all else.
- **Injection from K masters** is a K:1 mux plus round-robin, `F·ceil(K/4)`.
  The only place port count multiplies width, and it is O(K).
- **Every station output is registered.**

### Routing is one comparison per hop

A flit carries `dst_stn` and `dst_port`. At each station:

| `dst_stn` vs mine | action |
|---|---|
| equal | eject to `dst_port` |
| less | pass left |
| greater | pass right |

The address map is static and known at build time, so the NMU does not route —
it **labels**. Each hop decides for itself, which is what lets a flit pass
*through* a station it does not belong to.

### Four invariants

1. A master may not inject until response buffer space is reserved.
2. REQ and RSP never share a buffer.
3. Arbitration is packet-atomic — a grant is held until `last`, which gives
   AXI4's no-write-interleaving rule for free.
4. IDs do not cross the fabric; routes do. The flit carries `{src_stn,
   src_port}` and each slave shim issues its own local id.

## 2. Results

§2.1–2.6 measure the fabric on its own, against no particular deployment:
one station swept over presets, flit width and address width, then a
four-station line swept over port count, width, options and clock period, then
the vendor interconnects at the same port counts. §2.7–2.14 apply those numbers to a real
system.

`xcvu13p`, out-of-context synthesis, Vivado 2024.2. All resource figures are
**CLB LUT sites** from the utilization report, the unit vendor IP is also
reported in; see §2.14 before comparing any of them against a primitive count.
Every resource figure comes from `scripts/py/ooc_sweep.py`, which records
utilization, per-clock Fmax and the per-instance breakdown from a single
synthesis run, so no two rows disagree about which netlist they describe.

**Measured or derived.** Resource counts and cycle counts are measured — from
the netlist and from `sb_line4_tb` respectively. Two things are derived and
labelled where they appear: peak bandwidth columns (`FW/8` bytes per fabric
cycle at a stated clock) and LUT-per-GB/s. §2.13's wire counts are derived and
then checked against the placed design.

**Every figure here comes from RTL that can complete a maximum-length read.**
`RSP_DEPTH` is clamped inside `sb_nmu` to `MAX_BURST * SUB` (see *Width rules*),
because a port whose response FIFO is shallower than its longest permitted burst
does not overflow — it hangs, silently and forever. `sb_root9` was passing 4 and
16 on ports that had no declared bound, so it could not have completed a
maximum-length read.

What the bound is measured against matters more than the clamp itself. The 4 KB
rule permits 256 beats on a 32-bit port, so clamping to the *theoretical*
maximum gives every control shim a 256-deep response FIFO: +5,013 LUTs on the
3×9 station and +2,788 on the four-station line, for bursts those ports never
issue. Clamping to a *declared* `MAX_BURST` instead returns both to where they
were. The control ports declare 1 — single-beat is a protocol guarantee on
`M_AXI_LITE`, and the deployment specifies its other 32-bit ports the same way.
A 32-bit master that did burst would stall against its declared bound rather
than corrupt.

### 2.1 One station, every preset

3 masters, 9 slaves, four clock domains, 32- and 512-bit ports — `root_smc`'s
port count. `FW=512`, `AW=43`. `MIN_AREA` is one outstanding transaction and no
store-and-forward; `BALANCED` is four and store-and-forward; `PERF` is eight;
`SAFE` adds the 4000-cycle timeout. The BRAM rows set `LUT_PER_BRAM=0`, which
lets every FIFO deep enough to pay for itself move into block RAM; the no-BRAM
rows set 820 and keep all of them in distributed RAM.

| preset | CLB LUTs | FF | BRAM |
|---|---|---|---|
| `MIN_AREA`, BRAM | 12,814 | 21,964 | 24 |
| `MIN_AREA`, no BRAM | 14,731 | 25,318 | 0 |
| `BALANCED`, BRAM | 13,848 | 22,708 | 24 |
| `BALANCED`, no BRAM | 15,780 | 26,080 | 0 |
| `PERF`, BRAM | 14,049 | 22,816 | 24 |
| `PERF`, no BRAM | 15,981 | 26,188 | 0 |
| `SAFE`, no BRAM | 16,705 | 27,592 | 0 |

### 2.2 One station, flit width

The same 3-master 9-slave station at `BALANCED`, no BRAM, `AW=43`; only `FW`
moves. Peak fabric bandwidth is `FW/8` bytes per fabric cycle, quoted at
200 MHz. The `FW=256` row is the `NARROW` preset of §4.

| `FW` | CLB LUTs | FF | GB/s @200 MHz | LUT per GB/s |
|---|---|---|---|---|
| 128 | 10,076 | 15,940 | 3.2 | 3,149 |
| 256 | 12,398 | 19,501 | 6.4 | 1,937 |
| 512 | 15,780 | 26,080 | 12.8 | 1,233 |
| 1024 | 22,641 | 38,755 | 25.6 | 884 |

Wider is cheaper per unit bandwidth, monotonically, over the whole range. The
`FW=128` row is the only one the response-depth clamp moved (9,675 → 10,076):
at 128 the 512-bit master splits four ways, so its reservation is four flits per
beat and the FIFO has to be four times deeper. The narrow end of this table pays
for the wide master.

### 2.3 One station, address width

`FW=512`, `BALANCED`, no BRAM; only `AW` moves. 43 bits is KohakuAccel's map
(mesh MEM at `(id+1)<<40`), 32 and 64 bracket the useful range.

| `AW` | CLB LUTs | FF |
|---|---|---|
| 32 | 15,416 | 25,269 |
| 40 | 15,733 | 25,884 |
| 43 | 15,780 | 26,080 |
| 48 | 15,904 | 26,405 |
| 64 | 16,440 | 27,461 |

### 2.4 Port count

The four-station line at `FW=512`, `AW=43`, `BALANCED`, no block RAM, varying
one port count at a time. `NQ` is slaves *per station*, so the line carries
`4·NQ`; port 0 of each station is `FW`-wide and the rest are 32-bit. Masters all
sit on station 1; master 1 is 512-bit and the others are 32-bit.

| `NQ` | slaves | CLB LUTs | FF | control sets |
|---|---|---|---|---|
| 1 | 4 | 21,030 | 62,940 | 440 |
| 2 | 8 | 25,085 | 66,718 | 618 |
| 4 | 16 | 30,785 | 74,115 | 975 |
| 6 | 24 | 37,885 | 81,599 | 1,330 |
| 8 | 32 | 43,948 | 89,053 | 1,679 |

| `NM` | added master | CLB LUTs | FF | ΔLUT |
|---|---|---|---|---|
| 1 | — (one 32-bit) | 25,751 | 61,630 | — |
| 2 | the 512-bit one | 29,963 | 72,317 | **+4,212** |
| 3 | 32-bit | 30,785 | 74,115 | **+822** |
| 6 | 3 × 32-bit | 34,437 | 79,556 | +3,652 (**1,217** each) |

The slave sweep gives **818 LUTs per added 32-bit slave port** (endpoints
`NQ=1` and `NQ=8`, 28 ports apart). Predicting the three interior points from
that single slope deviates by 3.1%, 0.2% and 1.3%, the worst being `NQ=2`.

The master steps are measured one at a time, so width separates cleanly:
**4,212 LUTs for the 512-bit master** against **822 and 1,217** for the 32-bit
ones. Port cost tracks width and buffering, not port count — a 32-bit master
costs about what a 32-bit slave does, and the one wide master costs four times
either.

That separation depends on `MAX_BURST`. Before the control ports declared
themselves single-beat, each carried a 256-deep response FIFO and measured
~3,880 — four times its real cost, and indistinguishable from the 512-bit port.
An over-conservative buffer floor does not just cost area, it hides where the
area goes.

**What these two sweeps do and do not show.** Each holds the other side fixed,
so they establish that cost is linear in `NQ` at `NM=3` and linear in `NM` at
`NQ=4`. They do not by themselves establish independence — that would need a
two-dimensional sweep, which nothing here runs. Independence is a structural
claim (§1: ejection is a broadcast with a valid gate, so no per-port term
multiplies by the other side's count) for which these fits are consistent
evidence, not proof. A crossbar has no such structure — adding a slave widens
every master's response path — and §2.6 measures its slope for comparison.

`NQ=4` here is a second synthesis of §2.5's `FW=512` configuration, run
separately: 30,785 LUT and 74,115 FF in both. Repeat runs of one configuration
are bit-identical, so the single-run figures throughout §2 carry no run-to-run
spread.

### 2.5 A four-station line: width, address width, options, clock

The whole line rather than one station: 4 stations, 4 slaves each, 3 masters on
station 1, `AW=43`, `BALANCED`, no block RAM, `LINK_CDC=1` (a separate fabric
clock per station, crossing inside the link), `LINK_FULL=0`. Three links.

| `FW` | CLB LUTs | logic | LUTRAM | FF | control sets | GB/s @200 MHz |
|---|---|---|---|---|---|---|
| 128 | 17,120 | 11,090 | 6,030 | 35,113 | 981 | 3.2 |
| 256 | 22,106 | 14,492 | 7,614 | 48,167 | 975 | 6.4 |
| 512 | 30,785 | 20,073 | 10,712 | 74,115 | 975 | 12.8 |
| 1024 | 49,008 | 32,670 | 16,338 | 114,637 | 979 | 25.6 |

Control-set count is flat across an eightfold change in width; the growth is
entirely datapath. As at the single station, cost per unit bandwidth falls
monotonically with width — 5,350 LUTs per GB/s at 128 down to 1,914 at 1024.

Address width at line level, same configuration:

| `FW` | `AW`=32 | `AW`=43 | `AW`=64 |
|---|---|---|---|
| 256 | 21,345 | 22,106 | 24,255 |
| 512 | 30,339 | 30,785 | 33,251 |

Going from 32 to 64 address bits costs 14% at `FW=256` and 10% at `FW=512`; the
43 bits KohakuAccel's map forces cost 3.6% and 1.5% over 32.

Options, at `FW=512` and `AW=43` unless the row says otherwise:

| option | CLB LUTs | FF | BRAM | ΔLUT vs `BALANCED` |
|---|---|---|---|---|
| `MIN_AREA` — 1 outstanding, no store-and-forward | 30,512 | 73,844 | 0 | −273 |
| `BALANCED` — 4 outstanding, store-and-forward | 30,785 | 74,115 | 0 | — |
| `PERF` — 8 outstanding | 31,838 | 74,315 | 0 | +1,053 |
| `LINK_CDC=0` — one clock for the whole line | 31,113 | 73,445 | 0 | **+328** |
| `LUT_PER_BRAM=0` — FIFOs into block RAM | 24,981 | 56,491 | **130.5** | −5,804 |
| `FW=256` | 22,106 | 48,167 | 0 | −8,679 |
| `FW=256` + block RAM | 17,685 | 36,166 | **90** | −13,100 |

**Per-station clock domains are free.** Collapsing the line onto one clock does
not save anything — it *costs* 328 LUTs and saves 670 flip-flops, a wash either
way at 1% of the design. Four asynchronous domains with the crossings inside six
credited links are had for nothing measurable, which is the whole structural
claim of §3 and the one place a crossbar cannot follow: SmartConnect's
equivalent is per-port clock converters, and §2.6 could not even get a
reconstruction to build them.

**`LINK_FULL` is not in that table, because it is not a knob.** A boundary
always carries exactly two streams — REQ one way, RSP the other — since a master
reads *and* writes across it. `LINK_FULL=0` builds that pair, choosing its
direction from which side of the boundary `MGR_STN` sits on:

```verilog
localparam integer NEED_R = LINK_FULL || (s >= MGR_STN);
localparam integer NEED_L = LINK_FULL || (s <  MGR_STN);
```

`LINK_FULL=1` builds **four** streams — REQ and RSP in both directions — which is
required only when masters sit on *both* sides of a boundary, as they would with
a DMA engine per die. It removes nothing from other stations; NMUs are generated
only on `MGR_STN` regardless.

So the 37,312 LUTs a `LINK_FULL=1` line measures is the price of admitting
masters anywhere on the line, not an option to weigh against `BALANCED`. At
`MGR_STN=1` it is the wrong setting rather than an expensive one, and the
measured 639/634/644 SLLs per boundary confirm the deployed build carries one
REQ and one RSP stream, matching 629 derived.

Block RAM saves 5,804 LUTs for 130.5 tiles — **44 LUTs per tile**, far below the
~820 at which KohakuAccel values one, which is why `LUT_PER_BRAM=820` keeps
everything in distributed RAM there. On a device with spare block RAM the knob is
worth taking. It is per station and per FIFO, so a die whose block RAM is
contested and one with spare can sit on the same line.

The outstanding-transaction setting spans 4.3% from `MIN_AREA` to `PERF` and is
not a lever worth optimising; flit width, at 28% for one halving, is.

Area against the constraint, same configuration, sweeping only the bus period.
`bus_clk1` is the binding clock throughout — the station carrying the masters.

| target | CLB LUTs | `bus_clk1` Fmax | slack | closes |
|---|---|---|---|---|
| 150 MHz | 30,778 | 353.4 | +3.837 | yes |
| 200 MHz | 30,785 | 371.9 | +2.311 | yes |
| 250 MHz | 30,818 | 371.9 | +1.311 | yes |
| 300 MHz | 30,883 | 385.7 | +0.740 | yes |
| 350 MHz | 32,833 | 385.7 | +0.264 | yes |
| 400 MHz | 34,217 | 390.5 | −0.061 | **no** |
| 450 MHz | 34,269 | 390.5 | −0.339 | no |
| 500 MHz | 34,396 | 390.5 | −0.561 | no |

**Area is flat to 300 MHz** — 0.3% across that whole range — then rises 6.3% at
350 and 11% beyond, where synthesis is spending LUTs to chase timing it does not
reach. The structure saturates at about 390 MHz however hard it is pushed, and
350 MHz is the highest target that closes. At the deployed 200 MHz the
constraint costs nothing at all.

Out-of-context synthesis again: this bounds what the RTL can do, not what a
placed design will do. §2.9's routed result is the one to act on.

### 2.6 Vendor interconnect at the same port count

A SmartConnect built to the same shape as §2.1 — 3 slave interfaces, 9 master
interfaces, 512-bit, four clock domains — from real endpoint IP so widths and
frequencies propagate: `axi_vip` masters, `axi_bram_ctrl` + `blk_mem_gen`
slaves, one `clk_wiz` per domain. Figures are the `smc` instance alone, taken
from the hierarchical report, not the whole block design.

| shape | clocks | CLB LUTs | logic | LUTRAM | SRL | FF |
|---|---|---|---|---|---|---|
| 3×16, 512-bit | 4 | 36,229 | 26,353 | 7,600 | 2,276 | 36,850 |
| 6×9, 512-bit | 4 | 35,712 | 25,739 | 5,900 | 4,073 | 30,691 |
| 3×9, 512-bit | 4 | 21,885 | 15,290 | 4,522 | 2,073 | 22,650 |
| 3×9, 512-bit | 1 | 21,402 | 14,811 | 4,522 | 2,069 | 19,990 |
| 3×9, 256-bit | 4 | 19,703 | 15,375 | 2,998 | 1,330 | 21,323 |
| 3×5, 512-bit | 4 | 14,437 | 9,460 | 2,938 | 2,039 | 15,032 |
| 2×6, 512-bit | 3 | 12,265 | 8,095 | 2,788 | 1,382 | 14,343 |
| 1×5, 512-bit | 2 | 5,986 | 4,016 | 1,860 | 110 | 6,771 |

Holding masters at 3 and clocks at 4, going from 5 slave ports to 9 costs 7,448
LUTs — **1,862 per slave port**, against the line's 818 (§2.4). Adding three
masters at 3×9 → 6×9 costs 13,827, **4,609 each**.

**Does the marginal slave cost rise with the master count?** That is the `K×Q`
question, and the honest answer is *suggestive, not settled*:

| interval | LUTs per added slave port |
|---|---|
| SmartConnect, 5 → 9 slaves (3 masters) | 1,862 |
| SmartConnect, 9 → 16 slaves (3 masters) | 2,049 |
| station, 4 → 8 slaves (3 masters) | 1,014 |
| station, 8 → 16 slaves | 713 |
| station, 16 → 24 slaves | 888 |
| station, 24 → 32 slaves | 758 |

SmartConnect's marginal cost rises about 10% between its two intervals; the
station's shows no trend across four, scattering ±20% around 818. A purely
additive fit underpredicts SmartConnect at 3×16 by 3.8% and the station at its
worst interior point by 3.1%, so **the totals alone do not separate the two
structures** — only the direction does, and two SmartConnect intervals is thin
evidence for a direction. Settling it properly needs a slave sweep at a second
master count, which nothing here runs.

Halving the flit width takes the line from 30,785 to 22,106 LUTs, −28%. Halving
SmartConnect's data width at the same 3×9 shape takes it from 21,885 to 19,703,
**−10%**. The station bus is the more width-sensitive structure, because almost
all of it is datapath; a crossbar carries more control that does not shrink.

**The four-clock rows do not contain four clock domains.** `3×9` at one clock
and at four report the *same* 4,522 LUTRAM, and an added asynchronous domain
cannot be free in a structure whose crossings are LUTRAM FIFOs. Compare the
shipped instance, which reports 31,280 logic + **10,508** memory: the rebuild is
2.05× low on logic and 2.32× low on memory, and the memory gap is where the
missing clock converters would sit. Driving each port from its own `clk_wiz`
with a declared `FREQ_HZ` fixed width propagation — the crossbar really is
512-bit here, not the 32-bit one earlier attempts produced — but did not fix
domain propagation.

So this section is usable for comparing *shapes against each other*, and the
per-port slope above is sound because both rows miss the same thing. It is not
usable as a stand-in for the instance being replaced. §2.8 uses the shipped
figure.

#### `axi_interconnect`, where the clock domains do propagate

The same harness with `axi_interconnect` instead, which takes an explicit
`ACLK` per slave and per master port rather than a `NUM_CLKS` count. Instance
figures, 3×9:

| strategy | width | clocks | CLB LUTs | FF | RAMB36 |
|---|---|---|---|---|---|
| max-performance | 512 | 4 | 16,532 | 19,957 | 24 |
| max-performance | 512 | 1 | 6,699 | 5,554 | 0 |
| minimum-area (SASD) | 512 | 4 | 10,047 | 12,523 | 0 |
| minimum-area (SASD) | 512 | 1 | 4,450 | 3,805 | 0 |

Max-performance now costs more than minimum-area, which is the check that the
crossbar is really 512-bit — `CONFIG.XBAR_DATA_WIDTH` is not inferred from the
connected ports and silently defaults to 32, and the tell of that failure is the
two strategies coming out the wrong way round.

**This is the clock-domain measurement SmartConnect would not give.** Going from
one clock domain to four costs `axi_interconnect` **9,833 LUTs at
max-performance and 5,597 at minimum-area** — 2.47× and 2.26×. The same change
on the station bus costs **−328 LUTs** (§2.5). That is the design's central
claim, measured against vendor IP on the axis it claims, rather than argued.

It also places the station bus honestly among the alternatives at 3×9, four
clocks, 512-bit: SASD `axi_interconnect` 10,047, **station 15,780**,
max-performance `axi_interconnect` 16,532, rebuilt SmartConnect 21,885, shipped
`root_smc` 41,788. SASD is smaller and is not a substitute — one outstanding
transaction, a single serialised path, AXI3 internally. The station bus sits
between the two `axi_interconnect` strategies while carrying four outstanding
transactions and full AXI4.

### 2.7 Tree against line, at equal endpoints

A tree reaches another die through an AXI master port on the parent, an AXI
slave port on the child, and a register slice between them. A line reaches it
through a link, which is not an AXI port and appears in neither station's port
count. For S dies that is 2(S−1) AXI ports the line does not instantiate, and
those ports are full-width shims, not wires.

The same asymmetry sets the port counts below: `root_smc` needs 9 master ports
to serve 5 local endpoints, because the rest feed leaves and reach clock
wizards sitting in other dies.

What that costs, from §2.8's rows. The connective tissue between dies — three
`slr_cross` register slices against three credited links — is **9,795 LUTs
against 1,641**, a factor of 6.0. Per-die, where both sides are also serving
local endpoints, the ratio is smaller: 8,516 / 13,266 / 8,516 against 3,718 /
4,239 / 3,720, between 2.3× and 3.1×. The topology argument is the first
number; the per-port argument of §2.4 is the second.

Against the rebuilt SmartConnect's own slope of 1,862 LUTs per slave port
(§2.6), the six AXI ports a four-die tree spends on reaching itself come to
about 11,000 LUTs — and that slope is measured on a reconstruction that reads
2× low, so treat it as a floor.

### 2.8 In context: the KohakuAccel deployment

The SmartConnect tree and the station line, per die and in total. Line figures are
`FW=256`, `LUT_PER_BRAM=820` (no block RAM), `LINK_FULL=0`, four slaves per
station, three masters on SLR1. Station totals include that station's switch,
its NSU shims, and on SLR1 its three NMU shims.

Both sides at the same endpoint set. The station column is the per-instance
breakdown of one synthesis of the deployed configuration — `FW=256`, `AW=43`,
`BALANCED`, no block RAM, `LINK_FULL=0`, `LINK_CDC=1` — not a per-die
extrapolation. Its rows sum to 22,074 against the 22,106 reported for the whole
netlist, the 32 LUT difference being top-level glue, and its flip-flops sum to
48,167 exactly.

| | SMC LUTs | line LUTs | ratio | SMC FF | line FF |
|---|---|---|---|---|---|
| SLR0 | 8,516 | 3,718 | 2.29× | 16,575 | 6,963 |
| SLR1 | 41,788 | **8,756** | **4.77×** | 57,002 | 14,380 |
| SLR2 | 13,266 | 4,239 | 3.13× | 24,984 | 7,695 |
| SLR3 | 8,516 | 3,720 | 2.29× | 16,575 | 6,963 |
| inter-die | 9,795 | 1,641 | 5.97× | 14,988 | 12,166 |
| **total** | **81,881** | **22,106** | **3.70×** | **130,124** | **48,167** |
| BRAM | 0 | 0 | | | |

SLR1 breaks down as three NMUs at 1,017 + 2,119 + 1,005, four NSUs at 1,001 +
563 + 563 + 560, and a 1,928-LUT switch — the switch being larger than the other
stations' because it arbitrates three injecting masters rather than one. The
512-bit NMU costs about twice a 32-bit one at this flit width.

The saving concentrates on SLR1, which carries the root SmartConnect and XDMA.
Whether that is where the device needs it is a separate question, answered in
§7: in the placed v5 design SLR1 is the emptiest of the four dies, not the
fullest.

Provenance of the SmartConnect column, which is **not** uniformly v5:

| row | source |
|---|---|
| SLR1 | `multimesh_v5_root_smc_0` |
| SLR2 | `multimesh_v5_leaf_smc_2_0` |
| SLR0, SLR3 | `multimesh_`**`v4`**`_leaf_smc_0_0` — no v5 run exists for either |
| inter-die | 3 × `multimesh_`**`v2`**`_slr_cross_2_0` (3,265 LUT / 4,996 FF each) |

Those IPs were never re-synthesised for v5. `root_smc`'s split is 31,280 logic +
10,508 memory, confirming these are site counts and therefore comparable to the
station column.

#### A second deployment: one die, one station

KohakuAccel is the favourable case — four dies, five clock domains, mixed
widths. The unfavourable one is a **single-die peripheral fabric**: 3 masters,
9 slaves, one station, no links, no die crossing, and therefore none of the
credit-link or CDC-folding advantages. All that remains is the per-port
argument. Both sides measured at that shape, 512-bit, `AW=43`:

| | CLB LUTs | FF | BRAM |
|---|---|---|---|
| SmartConnect 3×9, rebuilt (§2.6) | 21,885 | 22,650 | 0 |
| station, `BALANCED`, no block RAM | 15,780 | 26,080 | 0 |
| station, `BALANCED`, block RAM | 13,848 | 22,708 | 24 |
| station, `MIN_AREA`, block RAM | 12,814 | 21,964 | 24 |

**0.72× on LUTs against the rebuild, 0.63× if block RAM is available** — and
1.15× on flip-flops in the no-BRAM row, which is the one place the station bus
loses outright. It buys its LUT saving partly with registers, and a design short
of flip-flops rather than LUTs should not expect a win here.

Against the *shipped* SmartConnect the ratio would be far larger, but that
instance carries address decode and protocol conversion this shape does not, so
the rebuild is the fair comparison for a single-die design and the shipped
figure is the fair one for §2.8's replacement.

### 2.9 The line as deployed

The same netlist as §2.8, so resources and timing describe one design rather
than two runs: `FW=256`, `AW=43`, `BALANCED`, no block RAM, `LINK_FULL=0`, one
fabric clock per station with the crossing inside the link, bus constrained to
200 MHz.

| | |
|---|---|
| CLB LUTs | 22,106 |
| — as logic | 14,492 |
| — as distributed RAM | 7,614 |
| CLB Registers | 48,167 |
| Block RAM | 0 |
| SRL | 0 |
| control sets | 975 |

All eleven clocks are constrained and all meet:

| clock | request | Fmax | slack |
|---|---|---|---|
| `bus_clk1` | 200 MHz | **357.9 MHz** | +2.206 ns |
| `bus_clk2` | 200 MHz | 392.6 MHz | +2.453 ns |
| `bus_clk0`, `bus_clk3` | 200 MHz | 428.1 MHz | +2.664 ns |
| `clk_ctrl` | 100 MHz | 395.9 MHz | +7.474 ns |
| `clk_xdma` | 250 MHz | 396.8 MHz | +1.480 ns |
| `clk_s0`, `clk_s2`, `clk_s3` | 178–237 MHz | 502.5 MHz | +2.2 to +3.6 ns |
| `clk_s1`, `clk_ddr` | 300 MHz | 520.3 MHz | +1.411 ns |

`bus_clk1` binds, at 1.79× the 200 MHz target. It is the station carrying the
three masters, so its switch arbitrates three injectors while the others
arbitrate one.

Everything above is out-of-context synthesis. OOC Fmax cannot show a
reset-fanout or congestion effect, so it bounds nothing about a routed design.

**Placed and routed.** The same line in a block design on `xcvu13p`, one pblock
per SLR with the links deliberately unpinned, driven by three JTAG masters into
sixteen block-RAM endpoints, `write_bitstream` reached:

| | |
|---|---|
| WNS | **+0.018 ns** |
| TNS | 0.000, 0 failing of 152,262 endpoints |
| WHS | +0.010 ns, 0 failing of 152,102 |
| pulse width | +1.125 ns, 0 failing |
| CLB LUTs (whole BD) | 24,554 |
| CLB Registers | 47,795 |

Per die, as placed, against §2.8's out-of-context per-station figures:

| | placed LUTs | OOC station |
|---|---|---|
| SLR0 | 3,838 | 3,718 |
| SLR1 | 11,731 | 8,756 |
| SLR2 | 4,613 | 4,239 |
| SLR3 | 4,372 | 3,720 |

The placed column is larger because each pblock also holds that die's block-RAM
controllers, and SLR1 additionally holds the three JTAG masters, the width
converter and the control clocking — which is most of the 3,000-LUT gap on that
row. The station part of the design places where it was told to.

*All user specified timing constraints are met*, with the bus at 200 MHz, ctrl
at 100, XDMA at 250, DDR at 300 and the four mesh clocks at 180–300. Ten clocks,
each with a real `create_clock` or MMCM-derived constraint — the report lists
them with their periods, which is the check that this run was timed at all.

The endpoints here are block RAM, not the mesh, so this measures the fabric and
its floorplan rather than the finished system.

Vivado's routed power estimate for the same build is 5.440 W total — 2.443 W
dynamic, 2.997 W device static, junction 27.9 °C — at **Medium** confidence and
with **no switching activity from real traffic**: it is a vectorless estimate at
default toggle rates. There is no equivalent figure for the SmartConnect tree,
so this is recorded rather than compared, and it should not be read as a power
result for either design.

### 2.10 Latency by hop

Masters on station 1, 32-bit single-beat read, control clock 100 MHz, at the
deployed configuration (`FW=256`, `LINK_CDC=1`, `LINK_FULL=0`).

| destination | hops | ctrl cycles |
|---|---|---|
| station 1 | 0 | 21 |
| station 0 | 1 | 27 |
| station 2 | 1 | 28 |
| station 3 | 2 | 31 |

Station 0 and station 2 are both one hop and differ by a cycle because each
station runs its own fabric clock; the crossing, not the hop, sets the cost.

### 2.11 Contention

A 32-bit access from one master against a *sustained* stream of 64-beat
512-bit bursts from another, to the same station. A single opposing burst
completes inside the store-and-forward delay and shows no penalty.

| | alone | contended |
|---|---|---|
| read behind writes | 27 | 48 |
| write completion behind reads | 28 | 49 |

### 2.12 Sustained write bandwidth

Eight consecutive maximum-length bursts (64 beats × 64 bytes = 32,768 bytes
total) from the 512-bit manager on station 1 to each station, counted in
`clk_xdma` cycles at 250 MHz. The manager issues one burst at a time and waits
for `B`, so each burst pays a full round trip.

| destination | hops | `FW=256` cycles | GB/s | `FW=512` cycles | GB/s |
|---|---|---|---|---|---|
| station 1 | 0 | 1,647 | 4.97 | 1,218 | 6.73 |
| station 0 | 1 | 2,018 | 4.06 | 1,481 | 5.53 |
| station 2 | 1 | 2,420 | 3.38 | 1,712 | 4.78 |
| station 3 | 2 | 2,325 | 3.52 | 1,714 | 4.78 |

This is a **floor, not a ceiling**: at one outstanding burst roughly half the
elapsed time is turnaround, and the figure is latency-bound rather than
width-bound. Doubling the flit width returns 1.35× here while costing 1.39× the
LUT (§2.5), because only the transfer half of each burst scales. A pipelined
master would move that ratio toward the widths themselves; nothing in this
document measures one.

Station 2 falls below station 3 despite being one hop closer because the
testbench runs each station on a different clock; hop count is not the only term.

### 2.13 Cross-SLR wires

23,040 SLLs per boundary, shared between both directions
(`docs/projects/kohakutpu/ship.md:59`). A boundary carries **two** streams —
one REQ, one RSP — because a master both reads and writes across it. That is
what `LINK_FULL=0` builds and it is the minimum, not an economy. `LINK_FULL=1`
builds four, which only a line with masters on both sides of a boundary needs.

Wire counts here are **derived**, not placed. Each direction carries `W`
payload bits plus one `valid`, and one credit bit returns, so a stream costs
`W+2`. From `sb_line4.v:121`:

```
RQW = 2*STNW + PORTW + SRCW + TAGW + 3 + AW + 8 + 3 + FW + FW/8
RSW =   STNW +         SRCW + TAGW + 4                + FW
```

At the deployed shape — `STNW=2`, `PORTW=2`, `SRCW=2`, `TAGW=4`, `AW=43`:

| | `RQW` | `RSW` | per boundary | % of budget |
|---|---|---|---|---|
| line, FW=256, `LINK_FULL=0` | 357 | 268 | 629 | 2.73% |
| line, FW=512, `LINK_FULL=0` | 645 | 524 | 1,173 | 5.09% |
| line, FW=512, `LINK_FULL=1` | 645 | 524 | 2,346 | 10.18% |
| `mag_link_cdc` interlink, for scale | | | 772 | 3.35% |

An earlier revision printed 642 and 626 here: those came from the sweep script's
hardcoded `AW=40`, the same defect as Appendix A item 13.

**The routed design confirms the derivation.** §2.9's placement reports actual
SLL usage per boundary, against 629 derived for this configuration:

| boundary | SLLs | % of 23,040 | outward | inward |
|---|---|---|---|---|
| SLR1 ↔ SLR0 | 639 | 2.77% | 365 | 274 |
| SLR2 ↔ SLR1 | 634 | 2.75% | 363 | 271 |
| SLR3 ↔ SLR2 | 644 | 2.80% | 364 | 280 |

Derived predicts 359 outward (REQ, `RQW+2`) and 270 inward (RSP, `RSW+2`);
measured is 363–365 and 271–282. The model is good to about 2%, and the
asymmetry is the expected one — requests leave the master station, responses
return.


Link logic, one direction, measured from the per-instance report:

| | LUT | FF |
|---|---|---|
| W=640 | 44 | 3,870 |
| W=528 | 44 | 3,172 |

Those two widths are the FW=512 streams at `AW=40`, which is what was built when
the report was taken; the logic cost is flat in `W` over this range, so the
three-bit difference does not move them.

The flops are `pipe_d`, the die-crossing pipeline itself. `srl_style =
"register"` keeps them as flops; without it the shift chain infers an SRL and
every stage lands in one site.

### 2.14 Primitive counts against LUT sites

Both accountings taken from the same netlist — an earlier `FW=256` line, before
the response-depth work. The ratios are a property of how primitives pack into
sites, not of that configuration, which is why this table is kept rather than
re-run; the absolute columns do not match §2.5 and are not meant to.

| | `ooc_count` primitives | Vivado sites | ratio |
|---|---|---|---|
| logic | 24,467 | 16,702 | 1.47 |
| distributed RAM | 8,806 | 4,418 | 1.99 |
| shift register | 0 | 0 | — |
| total | 33,273 | 21,120 | 1.58 |

Two `RAMD32` occupy one LUT6, and pairs of narrow logic LUTs share a site. Every
other figure in this document is the site count, so nothing here needs the
conversion — it exists because primitive counts are what a hand census produces,
and comparing one against a vendor site count overstates by about 1.6×.

## 3. Design

### Flit

| REQ field | bits | | RSP field | bits |
|---|---|---|---|---|
| `dst_stn`, `dst_port` | route | | `dst_stn`, `dst_port` | return route |
| `src_stn`, `src_port` | return route | | `tag` | ⌈log2 OST⌉ |
| `tag` | ⌈log2 OST⌉ | | `wr`, `last` | 2 |
| `wr`, `head`, `last` | 3 | | `resp` | 2 |
| `addr` | A, translated | | `data` | F |
| `len`, `size` | 11 | | | |
| `data`, `strb` | F + F/8 | | | |

Header rides **sideband alongside** the first payload beat. A separate header
flit costs 100% overhead on a single-beat transfer, which is what control
traffic is.

`{src_stn, src_port}` is packed as one opaque field that the slave shim
echoes back, so `sb_nsu` needs no knowledge of the line at all.

### Link and die crossing

**Credits, not `ready`.** `valid`/`ready` does not pipeline: n stages cost a
bubble each or a skid each, and `ready` becomes a long backwards chain — the
last signal you want crossing a die. With credits, pipeline depth affects only
the credit count, never correctness, never Fmax.

```
credits ≥ 2 · pipeline_depth_each_way + margin
```

### Clock crossing lives in the link

Each station runs its own fabric clock; a shared clock over four SLRs couples
every station to the worst one, and the SLL hop alone is 0.755 ns. So every
inter-station link is also a clock crossing, and `sb_link_cdc` contains it:

| | |
|---|---|
| data | `async_fifo`, wr = sender clock, rd = receiver clock |
| credit return | **gray-coded pop counter**, 2-FF synchronised back |
| block design sees | one clock per station. No CDC block, no clock converter |

The credit return must be a gray counter, not a pulse. Pops occur in the far
clock and can outpace a slower near clock; a pulse synchroniser drops them and
the link starves silently instead of failing.

**The link exposes no runtime status, because it has none to expose.** A flit
departs only against a credit (`i_ready = outstanding < CRED`) and the RX FIFO
is sized `RXD = max(16, CRED)`, so flits in the pipeline plus flits in the FIFO
can never exceed its depth — overflow is impossible by construction, at every
`CRED`, rather than by a check anyone has to run. `async_fifo` separately folds
XPM's reset-busy into `wr_full`, so a writer that respects the flag cannot lose
a beat during reset.

`CRED ≥ 2·PIPE` is a *throughput* condition, not a safety one: below it the
link stalls waiting for credit returns instead of overflowing. `sb_link_cdc`
carries a simulation-only `$display` for it, which is a diagnostic and not a
guard.

A credited link that ties `tready` high instead — as `mag_link_cdc` does —
*can* overflow, and then a sticky fault bit and a software poll are load
bearing. That is a property of that flow-control choice, not of clock crossing.

### Deadlock freedom

A response is reserved before its request is injected, and REQ and RSP never
share a buffer, so a full RSP path cannot block the REQ path that would drain
it. Reads are never split into multiple flits: a split read sets no body state
at the slave shim, so a phantom second flit sits at the queue head
forever.

## 4. Configuration

### Presets

| preset | outstanding | store-and-forward | queues |
|---|---|---|---|
| `PERF` / `BALANCED` | 4–16 tags | yes | must cover a full burst |
| `MIN_LUT` | 4–16 | yes | block RAM everywhere |
| `MIN_AREA` | **1** | **no** | minimum (16) |
| `NO_BRAM` | 4 | yes | distributed everywhere |

`LUT_PER_BRAM` overrides a preset's storage choice and costs each FIFO out
individually. At 820 it declines BRAM everywhere on its own.

### Independent knobs, all default-off or default-safe

| knob | on | measured cost |
|---|---|---|
| `LINK_CDC` | per-station fabric clocks, crossing inside the link | gray counter + 2 sync stages per link |
| `LINK_FULL` | topology declaration, not a saving: 0 = the two streams a boundary needs, 1 = four, for masters on both sides | 1 doubles cross-SLR wires and link flops |
| `TIMEOUT` | ages each slave slot, `SLVERR` on expiry | +1,511 LUT / +1,440 FF over 9 endpoints; Fmax unchanged |
| `STATS` | per-hub `flits`/`wait`, per-link `sent`/`nocred` | two 32-bit counters per hub and per link |
| `CRED` | link credits | 16 → 1 costs throughput, not correctness |
| `STORE_FWD` | packet-complete token | dropping it is `MIN_AREA`'s speed-for-size trade |
| `FW` | flit width | 512 → 256 takes the 3x9 station 15,780 → 12,398 LUTs, the line 30,785 → 22,106 |
| `MAX_BURST` | longest burst a port may issue | sets the response-FIFO floor; 1 on a Lite port |

### What a drop-in replacement fixes, and what is left to choose

Replacing an existing tree means the ports and the map are given. For KohakuAccel
that forces two parameters outright and constrains a third:

| forced | value | by what |
|---|---|---|
| `AW` | **43** | `mesh_{id}` MEM sits at `(id+1) << 40` |
| control windows | below 4 GB | `M_AXI_LITE` is 32-bit |
| `FW` | **≥ 256** | `sb_nsu` supports `SDW ≤ FW` only (`sb_nsu.v:99`), and the widest slave is `mesh_{id}/S_AXI_MEM` at 256 bits |

An earlier revision of this table read `FW ≥ 512`, on the belief that
`S_AXI_MEM` was a 512-bit slave. It is 256 (`ktpu_ship_*.v`, `DW=256`). Only
the XDMA master is 512, and a master wider than the flit is the *supported*
direction — it splits `SUB = MW/FW` ways in `sb_nmu`. So `FW=256` is a legal
drop-in, which §2.2 prices and the 606-check `SB_FW256` run demonstrates.

FW=256 halves the fabric and the cross-SLR wires, but it is only available if
the mesh memory port itself narrows — a mesh change, not a bus option. At a
200 MHz bus it would also carry 6.4 GB/s against XDMA's ~12.6 GB/s usable.

### Bandwidth

One flit per bus cycle per direction.

| `FW` | 150 MHz | 200 MHz | 250 MHz | 300 MHz |
|---|---|---|---|---|
| 128 | 2.4 GB/s | 3.2 | 4.0 | 4.8 |
| 256 | 4.8 GB/s | 6.4 | 8.0 | 9.6 |
| 512 | 9.6 GB/s | 12.8 | 16.0 | 19.2 |
| 1024 | 19.2 GB/s | 25.6 | 32.0 | 38.4 |

Two reference points for choosing among them. The SmartConnect tree being
replaced is 512-bit at 100 MHz — **6.4 GB/s**, which `FW=256` matches at
200 MHz. XDMA Gen3 x16 delivers ~12.6 GB/s usable, so anything above `FW=512`
at 200 MHz is buying bandwidth no master on this design can source. §2.12
measures what a single non-pipelined master actually achieves, which is well
under all of these.

### Width rules

`FW` is independent of the AXI port widths on either side, but the two
directions are not symmetric.

**A master wider than the flit is supported.** `sb_nmu` splits one port beat
into `SUB = MW/FW` flits. A read reserves its entire response before injecting,
so the response FIFO is sized in flits, not beats: `RSP_DEPTH` must cover
`MAXB * SUB`. The module clamps it there rather than trusting the parameter,
because an undersized FIFO does not overflow — `ar_ok` never asserts and the
port hangs with no error. A 512-bit master over a 256-bit fabric needs 128 and
the obvious value of 64 is silently fatal.

**`MAX_BURST` is what makes that clamp affordable.** The 4 KB rule permits 256
beats on a 32-bit port, so clamping every port to its theoretical maximum
provisions each control shim with a 256-deep response FIFO — measured at
+5,013 LUTs on the 3×9 station, for bursts no control master issues. `MAX_BURST`
declares what a port may actually send, and the clamp uses it. Set it to 1 on an
AXI4-Lite master: single-beat is a **protocol guarantee** there, not an
assumption about software. Leave it 0 elsewhere and the 4 KB bound applies. A
port that exceeds its declared bound stalls rather than corrupting, and the
simulation guard names it.

**A slave wider than the flit is not supported.** `sb_nsu` has a scatter path
(`NSLICE = FW/SDW`) but no gather path, so `SDW > FW` would drive a wide slave
from a single narrow flit and replicate read data back — it elaborates,
synthesises and simulates without a warning, and corrupts every wide beat. A
generate block instantiates an undefined module to make it an elaboration
error instead. Such a slave needs an `axi_dwidth_converter` in front of it, or
`FW` raised to its width.

For KohakuAccel neither rule binds at `FW=256`: the widest master is XDMA at
512 bits, and the widest slave is the mesh's `S_AXI_MEM` at 256.

**Two conversions the fabric does not do.** A crossbar converts protocol and ID
width on the way through; the station bus does not, and attaching real endpoints
rather than block RAM makes that visible:

| slave | mismatch | needs |
|---|---|---|
| a MIG's `C0_DDR4_S_AXI_CTRL` | AXI4-Lite, **no ID**, against a station port's AXI4 with a 4-bit ID | a 1×1 SmartConnect; `axi_protocol_converter` keeps the ID and fails BD 41-237 |
| a 64-bit control port | `gen_station_wrap` emits 32 or `FW` only | `axi_dwidth_converter`, or widening the generator's check |

The second is a generator limit rather than a fabric one — `sb_nsu` accepts any
`SDW <= FW`, so a 64-bit port is legal RTL and only the wrapper refuses to emit
it. The first is real: **a design with AXI4-Lite slaves still needs one small
SmartConnect per such port**, so "replaces the crossbar" means the crossbar's
routing and arbitration, not its protocol conversion.

### What may differ per station, and what may not

A flit is forwarded verbatim through intermediate stations — the switch does no
width or format conversion — so anything appearing in the flit is line-global.
Everything private to a shim is per-station.

| parameter | scope | why |
|---|---|---|
| `FW` | **line-global** | sets `RQW`/`RSW`; a neighbour would mis-parse |
| `AW`, `TAGW` | **line-global** | flit fields |
| `STNW` | **line-global** | flit field, sized for the station count |
| `PORTW` | **line-global** | flit field, but only needs to cover `max(⌈log2 NQ_i⌉)` |
| `SRCW` | **line-global** | flit field, sized for `max(⌈log2 NM_i⌉)` |
| `NQ`, `NM` | **per-station** | provided `PORTW`/`SRCW` cover the largest station |
| `OST` | **per-station** | private to that station's slave shims |
| `STORE_FWD` | **per-station** | private to that station's master shims |
| `TIMEOUT` | **per-station** | private to the slave shim |
| `LUT_PER_BRAM` | **per-station, per-FIFO** | only picks a storage primitive |
| `REQ_DEPTH`, `RSP_DEPTH` | **per-shim** | buffer sizing, clamped to a legal floor |
| `MAX_BURST` | **per-shim** | what that port may issue; sets the floor above |
| `CRED`, `PIPE` | **per-link** | one `sb_link_cdc` owns both ends |
| fabric clock | **per-station** | the link contains the crossing |

A die whose block RAM is contested can therefore run its station at
`LUT_PER_BRAM=820` while a die with spare block RAM runs `LUT_PER_BRAM=0` on
the same line; the storage choice never reaches the flit.

Verified, not asserted. `sb_line4` takes a per-station override for each
shim-private parameter, and `-d SB_MIXPRESET` drives all four differently at
once — outstanding 1/4/8/2, store-and-forward 0/1/1/0, block RAM 0/820/0/820,
and a 4000-cycle timeout on station 2 alone. That line passes 604 checks,
including the deadlock and ordering phases that cross every boundary between
unlike stations.

| | homogeneous | mixed |
|---|---|---|
| station 1 (local) | 21 | 22 |
| station 0 | 27 | 29 |
| station 2 | 28 | 33 |
| station 3 | 31 | 37 |

Control cycles to each destination. The mix changes latency because
store-and-forward differs per station; it does not change correctness.

### Presets

`NQ=4`, `AW=43`, `LINK_CDC=1`, `LINK_FULL=0`, masters on station 1, jtag
100 MHz, XDMA 250 MHz.

| preset | `FW` | `OST` | `STORE_FWD` | `CRED` | `TIMEOUT` | drop-in? |
|---|---|---|---|---|---|---|
| `MIN_AREA` | 512 | 1 | 0 | 16 | 0 | yes |
| `BALANCED` | 512 | 4 | 1 | 16 | 0 | yes |
| `PERF` | 512 | 8 | 1 | 32 | 0 | yes |
| `SAFE` | 512 | 4 | 1 | 16 | 4000 | yes |
| `NARROW` | 256 | 4 | 1 | 16 | 0 | yes |

`NARROW` is a drop-in: the mesh's `S_AXI_MEM` is 256 bits wide, so the fabric
meets it at full width, and XDMA's 512-bit master splits 2:1 through `sb_nmu`.
That split is what the `RSP_DEPTH` clamp under *Width rules* exists for; the
configuration passes the same 606 checks as the 512-bit presets. At 200 MHz it
carries 6.4 GB/s, matching the 512-bit-at-100 MHz SmartConnect it replaces.

`MIN_AREA` drops to one outstanding transaction and removes the
packet-complete token, so the hub holds its grant through a FIFO underrun —
the same trade `axi_interconnect` makes in minimum-area mode. `SAFE` adds a
per-slave timeout that answers `SLVERR` instead of blocking a REQ buffer.

Resources per preset are in §2.1, at a fixed 3-master 9-slave shape and both
storage settings. `NARROW` is the `FW=256` row of §2.2.

### The deployed shape

| | |
|---|---|
| topology | line, 4 stations, one per SLR, no root |
| masters | 3, all on station 1: jtag 32-bit @100 MHz, XDMA 512-bit @250 MHz, XDMA-Lite 32-bit @250 MHz |
| slaves | 4 per station: mesh `S_AXI_MEM` 256-bit, mesh CTRL 32-bit, DDR ctrl 32-bit, clk_wiz 32-bit |
| **no** GPIO endpoint | the interlink status port it existed for is not needed |
| address width | 43 — mesh MEM sits at `(id+1)<<40`, control windows below 4 GB |
| flit width | 256: it meets the widest slave exactly, and the 512-bit master splits 2:1 |
| protocol | AXI4 full throughout; 32-bit ports are AXI4-Lite-compatible single-beat |
| clocks | one fabric clock per station, plus ctrl 100 MHz, xdma 250 MHz, a mesh clock per SLR, one DDR clock |
| address map | station at bit `AW-4`, endpoint at bit 16 — one 64K window each |

The widest slave is 256 bits, not 512: `ktpu_ship_*.v` declares `S_AXI_MEM` at
`DW=256`. Only the XDMA master is 512, and a master wider than the flit is the
supported direction (see *Width rules*).

`sb_line4.v` is this design with `NQ` as a parameter; `NQ=4` is the deployed
shape.

## 5. Cost model

- **LUT cost tracks fan-in per output bit**, not logic volume — a LUT6 absorbs
  any 6-input function.
- **Never index a payload by a variable bit offset.** `i_pay[sel*PW +: PW]`
  builds a barrel shifter: measured 14,632 LUT for two hubs against ~1,700 for
  a constant-offset mux with an equality compare. FPGA-only divergence — on an
  ASIC both forms are the same one-hot pass-gate mux.
- **LUTRAM is 1 LUT per bit at 32 deep**; a RAMB36 SDP is 72×512, so its cost
  is `ceil(W/72)` tiles regardless of depth. One BRAM ≈ 2.25×D LUT.
- **Dual-port BRAM caps at 36 bits per port**, worse than SDP's 72 for a
  width-limited FIFO.
- **The 4 KB rule bounds every queue**: max legal burst is
  `min(256, 4096/(MW/8))` = 64 beats on a 512-bit port. The *response* queue is
  bounded in flits, not beats, so it needs `AXI_MAXB * SUB` — 128 for a 512-bit
  port on a 256-bit fabric. Under-size it and the port hangs rather than
  overflowing, which is why `sb_nmu` clamps instead of trusting the parameter.
- **Flit width is the biggest single lever**, and the numbers agree: on the
  four-station line `FW` spans 17,120 to 49,008 LUTs from 128 to 1024, while
  every outstanding/store-and-forward option together spans a few percent.

### Resets: control path only

The datapath takes no reset by construction — the skid payload, the link's
`pipe_d` (2,560 flops per crossing at W=640) and the slave id tables are
all reset-free; only valid bits, grant locks, pointers and counters reset.

Read the census from the **netlist**, never the RTL (`ooc_resets` in
`scripts/tcl/ooc_class.tcl`). On the 3x9 station 11,836 of 25,884 flops carry a
reset and essentially all of them are inside XPM's FIFO output stage, which
resets because `DOUT_RESET_VALUE` makes XPM guarantee `dout == 0` — not
something RTL can decline. The reverse also bites: each shim registers a local
`bus_rst` copy to break fanout and Vivado merged them all back into one net
because the flops were identical; `(* dont_touch *)` pins them.

Judge reset work by LUT and control sets — never by OOC Fmax, which cannot show
a reset-fanout gain.

## 6. Verification

1. **Protocol compliance at every shim boundary**, every test. Non-negotiable.
2. **Randomised all-to-all** — random widths, burst lengths, clock ratios,
   backpressure. Scoreboard by expected data.
3. **Deadlock stress** — every master to one slave, max outstanding,
   full backpressure, run until every buffer is full, then release.
4. **Ordering** — same-ID to different destinations with asymmetric latencies.
5. **Decode** — unmapped address returns `DECERR` and injects **zero flits**;
   assert the flit count, not just the response.
6. **Reset** — release fabric and ports in every order, mid-traffic.
7. **Credit exhaustion** — credits at 1; correctness must not depend on count.

3, 5 and 7 get skipped and are the ones that matter.

### Status

All seven items above are covered by `sb_line4_tb`, whose first twelve phases
map onto them one for one; phase 13 measures bandwidth and asserts nothing.
Every run carries 3 master-side and 16 slave-side protocol monitors.

**`SB_NQ4 SB_HALFLINK SB_LINKCDC` — exactly what the block design builds —
passes 606 checks**, including maximum legal bursts, all three masters hammering
one station, same-ID ordering across unequal hop counts, and randomised traffic
under random slave backpressure.

| config | checks |
|---|---|
| **deployed config, full stress** | **606** |
| `SB_FW256`, 512-bit master over a 256-bit fabric | **606** |
| `SB_MIXPRESET`, four unlike stations on one line | 604 |
| `SB_MIXPRESET SB_TIMEOUT` at `FW=512` | 604 |
| `SB_FW256 SB_CRED1`, one credit per link | 604 |
| deployed config, before stress phases were added | 139 |
| `SB_NQ4` / `+FW256` / `+HALFLINK` | 139 each |
| 2 endpoints per station: default, `LINKCDC`, `FW256`, `CRED1`, `AW32` | 141 each |
| `SB_WIDE512`, 512-bit slave under a 256-bit fabric | rejected at elaboration |

The `SB_FW256` row is the width rule above under test rather than asserted.
Before `RSP_DEPTH` was clamped to `AXI_MAXB * SUB` that configuration hung with
no error; it is the configuration §2 recommends, so the clamp is load-bearing
and not a defensive nicety. `SB_WIDE512` is the unsupported direction: it once
returned 512 wrong answers out of 606 while elaborating and synthesising
cleanly, and now fails to elaborate at all.

Other benches:

| bench | result |
|---|---|
| `sb_root9` — single station, 3x9 | 215 checks |
| `sb_chain2` — two stations over a link | 134 checks |
| `TIMEOUT` containment | proven: `SLVERR`, not a hang |
| resource measurement of the line | §2.4, §2.5, §2.8, §2.9 |
| placement with pblocks | running — see §2.9 |

## 7. Conclusion

### What to choose for KohakuAccel

Every value below is forced by a measurement or by the existing design, and the
reason is given rather than the preference.

| knob | choose | why, in numbers |
|---|---|---|
| `FW` | **256** | The widest slave, `S_AXI_MEM`, is 256 bits, so the fabric meets it exactly and the 512-bit XDMA master splits 2:1 (606 checks). 22,106 LUTs against 30,785 at 512 — 28% — and 629 cross-SLR wires against 1,173. At 200 MHz it carries 6.4 GB/s, which is exactly what the 512-bit-at-100 MHz SmartConnect provided. |
| `AW` | **43** | Forced: mesh MEM sits at `(id+1)<<40`. Costs 3.6% over 32 bits (21,345 → 22,106). |
| bus clock | **200 MHz** | Meets the bandwidth above. OOC binding clock is `bus_clk1` at 357.9 MHz, 1.79× the target; the routed design closes at +0.068 ns. Area is flat from 150 to 300 MHz (0.3%), so the constraint is free anywhere in that range and there is headroom to raise it later without paying for it. |
| `LINK_FULL` | **0** | Not a saving — a declaration. Every master sits on station 1, so each boundary needs one REQ stream and one RSP stream, which is what 0 builds. 1 builds four streams and is only correct if masters sit on both sides of a boundary. |
| `LINK_CDC` | **1** | Each die gets its own fabric clock, which is the point — a shared clock couples every station to the worst one, and the SLL hop alone is 0.755 ns. |
| `OST` / `STORE_FWD` | **`BALANCED`** | The whole outstanding range spans 4.3% (30,512 → 31,838). Not a lever; take the middle. |
| `LUT_PER_BRAM` | **820**, i.e. no block RAM | Block RAM saves 5,804 LUTs for 130.5 tiles — 44 LUTs per tile, against the ~820 a tile is worth on this device. Per station, so a die with spare block RAM may choose otherwise. |
| `MAX_BURST` | **1** on the 32-bit ports | Single-beat is a protocol guarantee on `M_AXI_LITE`. Declaring it keeps their response FIFOs at depth 16 instead of the 256 the 4 KB rule would demand. |

**What that buys.** 22,106 LUTs and 48,167 flip-flops against the tree's 81,881
and 130,124 — **3.70×**, and **4.77× on SLR1**, the die carrying both
`root_smc` and XDMA. The interconnect frees about 33,000 LUTs there; XDMA at one
channel per direction frees roughly 17,000 more.

**Where it does not help.** SLR1 is the *least* utilised die in the placed v5
design — 88.61% CLB against SLR0's 95.49%, and 45% DSP against 79.6% — because
it carries the interconnect but the smallest share of mesh. The saving therefore
lands where there was already the most room and does not relieve the binding
constraint, which is SLR0. Every die also sits at 88–95% CLB occupancy while
using 61–69% of its LUTs, so the design is packing-bound and a LUT saving
converts into placeable sites only as well as the placer packs what remains.
`docs/projects/kohakutpu/v6-plan.md` works through what the freed resource
actually buys.

**What it costs.** Latency rises with hop count — 21 control cycles to the local
station, 31 to the far end of the line — where a tree is flat. Sustained
single-outstanding write bandwidth measures 4.97 GB/s locally falling to
3.38 GB/s two hops away, well under the fabric's ceiling, because at one
outstanding burst the measurement is latency-bound. Nothing here measures a
pipelined master, and nothing measures the tree's latency for comparison.

### Position against the alternatives

**Against `axi_interconnect` minimum-area: it wins on area, and it should.**
Now measured (§2.6): SASD is 10,047 LUTs at 3×9 four-clock 512-bit against the
station's 15,780. It does strictly less — one outstanding transaction, one
serialised path, AXI3 internally — so this is the expected result and not a
defeat. Max-performance `axi_interconnect` is 16,532, just above the station.
Earlier revisions of this document said no valid number existed; the obstacle
was `CONFIG.XBAR_DATA_WIDTH` silently defaulting to 32, which setting it
explicitly and asserting the readback fixes.

**On performance: not measured head to head.** There is no SmartConnect or
`axi_interconnect` cycle measurement, so "faster" remains structural rather
than measured: SASD is one outstanding and serialises, so its ceiling sits
below anything with 4–16 outstanding.

**On clock domains: strongest claim, and structural.** A shim takes **no
parameter describing the clock relationship**. SmartConnect needs `NUM_CLKS`
plus per-port assignment and **fails silently** when domains do not propagate.
This is not a historical footnote: the §2.6 reconstruction drives every port
from its own `clk_wiz` with a declared `FREQ_HZ`, and *still* builds one domain
— `NUM_CLKS=1` and `NUM_CLKS=4` report the same 4,522 LUTRAM. A structure that
cannot be misconfigured this way is worth more than the LUT difference.

**Take vendor baselines from the vendor's own OOC report, never a
reconstruction.** How far a rebuild reads low depends on what is attached to
it: `axi_vip` slaves gave 12,481 against the real 41,788 (3.3×), and real
`axi_bram_ctrl` endpoints with declared widths and frequencies gave 21,885
(1.9×). Neither reaches the shipped instance, because neither reproduces its
address ranges, ID widths and protocol conversions. Use a reconstruction to
compare *shapes* against each other, and the vendor's own report to compare
against the thing being replaced.

### When not to build this

Two or three ports on one clock at one width; or `M·N·W` is genuinely small; or
you need runtime observability and remapping. The case *for* it is
**heterogeneity, not size** — many clocks, many widths, many dies.

The measurements say the same thing from the other end. Strip the heterogeneity
away and the margin collapses: the single-die 3×9 comparison in §2.8 wins 0.72×
on LUTs and **loses** on flip-flops, while the four-die replacement wins 3.70×
overall and 5.97× on the inter-die tissue alone. If a design has one clock, one
width and one die, the interesting number is 0.72× and it is not worth new RTL.

Also not worth it if flip-flops are the scarce resource rather than LUTs. The
station bus registers every station output by construction, and the die-crossing
pipelines are flops by design — six link instances carry 12,166 of the deployed
line's 48,167 flip-flops (§2.8), a quarter of the total for three boundaries.

### Open questions

- ~~**Head-of-line cost of packet-atomic arbitration**~~ — measured, bounded at
  one packet. A third VC is not warranted.
- ~~**Should the RSP VC carry write responses?**~~ — measured, identical bound.
  The delay is arbitration, not flit width, so a narrow completion path cannot
  shorten it. Keep them shared.
- ~~**Error containment**~~ — `TIMEOUT` knob, proven, priced.
- **Multicast writes** are nearly free in a broadcast-ejection station and the
  invariant break has a known fix: the segment knows the fan-out N at decode
  time, so the master shim decrements credit by N and merges N completions,
  taking the worst response code. **Not built — nothing issues one**, and
  adding a deadlock-relevant path with no test that exercises it is worse than
  not having the feature.
- **The config port's own path** — a config port reachable only through the
  fabric cannot debug a hung fabric. `STATS` exposes counters as plain wires;
  reading them still needs a port wired outside the fabric, local to that SLR.

## Prior art

| what | why |
|---|---|
| **Xilinx Versal NoC** | hardened NMU/NSU with this structure; the naming here follows it |
| credit flow control | standard in every die-to-die and chiplet link |
| packet-atomic arbitration | how AXI's no-interleaving rule is met without a reorder buffer |

## Appendix A. Open review findings

Known defects in this document. Struck entries are closed and the section that
closed them is named; the rest are open.

### A.1 Benchmarking the design itself

1. ~~**No option sweep.**~~ §2.1 sweeps every preset against LUT / FF / BRAM at
   a fixed port count. Performance across presets is still only Fmax, not
   throughput.
2. ~~**No `AW` or flit-width sweep.**~~ §2.2 and §2.3.
3. **Chaining is partly measured.** §2.5 gives the four-station line across
   width, address width and every option, and §2.9 times it; §2.4 separates
   per-port from per-station cost. What is still missing is S itself — every
   line measured here has four stations, so the per-station and per-link
   marginal cost is inferred from the port sweep rather than measured directly.
4. ~~**Per-station heterogeneity is never discussed.**~~ *What may differ per
   station* now scopes every parameter, and `-d SB_MIXPRESET` runs four
   unlike stations on one line through the full 604-check suite.

### A.2 Comparing against SmartConnect

5. ~~**No controlled baseline.**~~ §2.6 builds SmartConnect at matched port
   count and width, gives its per-slave-port slope, and states why a rebuild is
   ~2× low on the shipped instance. One limit survives and is now documented
   rather than assumed away: the rebuild will not build multiple clock domains
   however they are declared, so the clock axis has no vendor baseline at all.
6. ~~**The comparison is written as one product version against another.**~~ It
   is now SmartConnect against station bus at two fixed endpoint sets: the
   four-die KohakuAccel replacement (§2.8) and a single-die 3×9 peripheral
   fabric with no links and no clock crossings, which is the case where the
   station bus has none of its structural advantages and wins only 0.72× — and
   loses on flip-flops.
7. **Not every option is compared on every metric.** Resources now cover the
   whole option space at both levels (§2.1, §2.5). Timing covers the deployed
   configuration only (§2.9), and throughput covers two flit widths (§2.12).
   Carrying every option through timing would need a synthesis per option with
   its own constraint set, which is a sweep nobody has run.

### A.3 Conclusions

8. ~~**No number-driven recommendation.**~~ §7 gives one value per knob with the
   measurement that forces it, what the whole choice buys (3.70× overall,
   4.77× on the die that is full), and what it costs in latency and throughput.
9. ~~**BRAM is treated as one global choice.**~~ `LUT_PER_BRAM` is per-station
   per-FIFO in the scope table, §2.1 prices both settings, and the mixed-preset
   run drives 0/820/0/820 across the four stations at once.

### A.4 Method defects

10. ~~§2.1, §2.1b and §2.2 are three different storage configurations presented
    as one coherent set.~~ §2.1 states both storage settings side by side; §2.4,
    §2.5 and §2.8 are all `BALANCED`, no block RAM, `AW=43`, and say so. §2.9 is
    the one block-RAM table left and is labelled as such.
11. ~~§2.1 reports one arbitrary point and calls it "one station".~~ It sweeps
    all four presets against both storage settings. The point it used to report,
    9,254, was the cheapest corner of three independent knobs at once.
12. ~~**Every headline number is FW=256, which §4 rules out for a drop-in.**~~
    §4 was wrong to rule it out: the mesh slave port is 256 bits and the only
    512-bit master splits through `sb_nmu`. Headline numbers are now `FW=512`
    and `FW=256` is a measured, simulated option rather than an exclusion.
13. ~~§2.2 measures `AW=40`; §4 requires 43.~~ Worse than recorded: the line
    sweep parsed `aw` and never passed it to `synth_design`, so every line-level
    `AW` row would have been the same netlist under four different labels. Fixed
    in `ooc_line_sweep.tcl` and re-measured at 43.
14. ~~§2.8's SmartConnect column mixes provenance~~ — confirmed and now stated
    per row: SLR1 and SLR2 are v5 reports, SLR0 and SLR3 are a **v4** leaf
    report, and the inter-die row is 3× a **v2** `slr_cross`. Only two of five
    rows are v5, because the others were never re-synthesised for it.
15. ~~§2.2's Fmax and §2.1b's resources come from different runs of different
    configurations.~~ §2.8 and §2.9 are now the same synthesis: the per-die
    breakdown sums to the total, and the eleven-clock timing table is that
    netlist's.
16. ~~§2.5 quotes W=640/528 in a table under text stating RQW=642/RSW=524.~~
    §2.12 now derives both from `sb_line4.v:121` at the deployed shape, and says
    which of its two tables is derived and which is measured.
17. ~~Measured and derived numbers sit in adjacent tables, unmarked.~~ §2's
    method paragraph now names the three derived quantities; everything else is
    a netlist or simulation measurement.
18. ~~n=1 everywhere; no repeat, seed, or directive variation.~~ Partly: a
    configuration re-synthesised separately reproduced bit-identically (§2.4),
    so n=1 is sound for resources. Directive and seed variation is still
    unexplored, and would matter for timing rather than area.
19. ~~Every figure is post-synthesis OOC. No post-place-and-route number
    exists.~~ §2.9 places and routes the line with per-SLR pblocks: WNS
    +0.068 ns, all constraints met, bitstream written.

### A.5 Unsupported claims

20. ~~"Per-port cost is O(1)" — no port-count sweep exists.~~ §2.4 sweeps both
    sides: 818 LUTs per 32-bit slave port, 822–1,217 per 32-bit master and 4,212
    for the 512-bit one — each constant in the opposite side's count, with the
    slave line straight to 3.1% worst case over an eightfold range. §2.4 also
    states what the sweeps do not show: each holds the other side fixed, so
    independence between the two counts remains a structural argument rather
    than a measured one.
21. **"The `K×Q` term never appears" — measured, and it does not separate the
    two structures.** §2.6 now has SmartConnect at 3×5, 3×9, 3×16 and 6×9. Its
    marginal slave cost rises ~10% between intervals where the station's shows
    no trend, but an additive fit misses SmartConnect by 3.8% and the station by
    3.1%, which is not a distinction the totals can carry. The claim stands on
    the structure (broadcast ejection), not on this data, and proving it needs a
    slave sweep at a second master count on both sides.
22. ~~"2(S−1) AXI ports saved" — no resource number attached.~~ §2.7 attaches
    two: the measured inter-die tissue is 9,795 LUTs against 1,641, and the
    six ports themselves come to ~11,000 at SmartConnect's own per-port slope.
23. ~~§3's "no status port needed" generalises from a single comparison.~~ It
    now gives the structural reason (`RXD = max(16, CRED)` bounds everything in
    flight) instead of the elaboration check it wrongly claimed, and separates
    the `CRED ≥ 2·PIPE` throughput condition from safety.

### A.6 Missing measurements

24. ~~**No bandwidth or throughput measurement anywhere.**~~ §2.12 measures
    sustained write bandwidth per hop count. It is a single-outstanding floor;
    a pipelined-master ceiling is still unmeasured.
25. No latency or throughput measured for the design being replaced.
26. ~~No area/frequency curve — one 200 MHz point.~~ §2.5 sweeps 150–500 MHz:
    area flat to 300 MHz, +6.3% at 350, +11% beyond, and the structure saturates
    at about 390 MHz with 350 the highest target that closes.
27. No power — **and this stays open**. §2.9 records a routed 5.442 W estimate,
    but it is vectorless at Medium confidence with no traffic behind it, and no
    counterpart exists for the tree. A real answer needs SAIF-driven estimation
    on both, which nothing here does.
28. ~~No `axi_interconnect` comparison.~~ §2.6 measures both strategies at two
    widths and two clock counts. It also supplies the clock-domain baseline
    SmartConnect refused to give: 1→4 domains costs `axi_interconnect` 9,833
    LUTs, against −328 for the station bus.

### A.7 Structure

29. ~~§2.1b is deployment-specific and sits inside results promised as
    general.~~ §2 now runs standalone first (2.1–2.6) and in-context after
    (2.7–2.14), and says so in its opening.
30. ~~Numbering runs 2.1, 2.1a, 2.1b, 2.2 — insertion damage.~~
31. ~~§4 names five presets; §2 measures none of them by name.~~ §2.1 measures
    four by name; `NARROW` is §2.2's `FW=256` row.
32. Verification is disconnected from the results it qualifies.
