---
title: v8 — the one-SLR probe
summary: One device image to answer one question — does a full node (system node, station, MIG, mesh) fit and close on each die, with ONE Kohaku Xache spanning all four? Four stations, four nodes on one sysnode clock, one 4×4 Xache pinned across the dies, meshes 2+2 / 6+2 / 2+2 / 2+2, no L2 adapters.
tags:
  - kohakutpu
  - device
  - roadmap
---

# v8 — the one-SLR probe

> **Kind: Yours throughout — a device-image decision.** Which clock the nodes
> share, whether the DRAM fabric is one module or four, and what each die
> carries are choices this project makes for its own image. The Xache and the
> station bus are separate components with their own pages
> ([component/xache](../kohakuaxi/xbar-cache.md),
> [component/station-bus](../kohakuaxi/station-bus.md)); the framework requires
> neither.

**Status: the flow is written and verified to the block-design level; no
image is built.** The flow is `scripts/tcl/v8/` behind
`scripts/tcl/multimesh_v8_bd.tcl`, every knob in `v8/00_config.tcl`, written
to be run **once**: nothing is placed until every check in §5 has passed on
the synthesised netlist.

This page is the roadmap for one image, not a specification of anything in it.

---

## 1. What changes against v7

| | v7 | **v8** |
|---|---|---|
| dies with a mesh | SLR1 only (6+2), the others node + station + MIG | **all four**: 2×2 meshes, 2+2 on SLR0/2/3, **6+2 on SLR1** |
| node (sysnode) clock | one MAG-rate output per die's wizard | **ONE clock for all four nodes**, `clk_wiz_mesh1/clk_out4`; the other wizards lose their fourth output |
| DRAM path | node → own MIG through the node's async DRAM port | node → **ONE DRAM fabric spanning the four dies** (one master and one home per die, 64 URAM per home) → the four MIGs; the node's DRAM port is synchronous (`DRAM_CDC = 0`) |
| clock crossings on the memory path | one per node (node → MIG) | **four, all at the fabric's DRAM edges** (`HCDC = 1111`); none between a node and the fabric; inside it only registered, credited hops on the one clock |
| DRAM address space | per node, mesh field in `[37:36]` | **one flat 16 GB seen identically by every node**, 16 KB rotation over the four channels, `[37:36]` zero |
| L2 | staging + CU/vector L2 adapters | **staging only** (64 URAM per node); the Xache is the L3 |
| interlink | `mag_link_cdc` per hop | `mag_link_pipe_bd`, a registered pipe of depth 2 per hop and direction, on the one clock |
| station bus, XDMA, JTAG | v7 | unchanged: four stations, ports 0/1 on the sysnode clock, port 2 on each die's MIG clock, port 3 on ctrl |

Everything the probe adds is on the sysnode clock, and the Xache is placed
across the dies rather than duplicated: one home's array, engines and DRAM edge
sit with its MIG, one master's edge and aggregation with its node, and every
(master, home) pair's five channels cross the die boundary through `SLRX`
register stages (§4).

---

## 2. Clocks and resets — one clock, one reset

Every clock has exactly one reset, clocked by that clock. Outside the tops it
is a `proc_sys_reset` released on its own generator's lock — ten of them:
ctrl, sys, four buses, four MIGs. Inside a top the NoC, mat and vec domains
own `kh_rst_sync` chains fed from `axi_aresetn`; that *is* the designed
crossing, and the analysis stage (§5) allows a foreign-domain load only
there. The NoC clock has no `proc_sys_reset` of its own: the top's
synchroniser is its reset.

**The sysnode reset is one reset delivered four times.** `rst_sys` lives in
SLR1 with its generator; its loads (5,703 registers) are on all four dies. A
die crossing is one TX register with exactly one load, its RX register — a
Laguna pair; `USER_SLL_REG` is ignored on a net with more loads (UG949) — so
a reset that spans dies is a tree of fanout-1 crossings: `xcvu13p_rst_tree`,
named for the part and kept beside the BD wrapper, takes `rst_sys` and gives
die `i` its own copy through a sending register (pinned with the source), a
landing register and a fan-out register (pinned to die `i`). Every load on
die `i` takes `rst_tree/rstn_o<i>`: node, station, converter, the pipes
leaving that die, and the fabric's per-partition reset `d_rstn<i>`. All four
copies release on the same edge. The analysis stage checks the fanout-1
property and that no load of copy `i` is pinned to another die. A single flop
fanning the reset into four dies measures −6.053 ns at synthesis; the tree is
why it does not.

| clock | source | rate | reset | what it clocks |
|---|---|---|---|---|
| ctrl | `clk_wiz_ctrl/clk_out1` | 100 | `rst_ctrl` | JTAG manager, the wizards' AXI-Lite, station port 3 |
| **sys** | `clk_wiz_mesh1/clk_out4` | 300 | **`rst_sys`**, as four per-die copies through `xcvu13p_rst_tree` | every node's `axi_aclk` **and `dram_aclk`**, the Xache, station ports 0/1, the interlink pipes, the ctrl-width converters |
| noc_i | `clk_wiz_mesh<i>/clk_out1` | 300 | the top's own `kh_rst_sync u_rs_noc` from `axi_aresetn` | die i's NoC fabric |
| mat2x_i | `clk_wiz_mesh<i>/clk_out2` | 600 | the top's own synchronisers (`mat_clk` = ÷2 via `ktpu_div2`, cleared off `locked`) | die i's matmul clusters |
| vec_i | `clk_wiz_mesh<i>/clk_out3` | 300 | the top's own synchronisers (units reach the NoC through `UNIT_CDC` FIFOs) | die i's vector cores |
| bus_i | `clk_wiz_bus<i>/clk_out1` | 200 | `rst_bus<i>` | station i's fabric |
| ui_i | `ddr4_<d>/c0_ddr4_ui_clk` | 300 | `rst_ddr4_<d>` | MIG d, the Xache's `h_clk<i>`, station i's port 2 |
| xdma | `xdma_0/axi_aclk` | 250 | XDMA's own | its masters |

`DDR_OF_SLR = {0→2, 1→3, 2→1, 3→0}` from v5's io_placed report: home `h` is
MIG `DDR_OF_SLR[h]`, the one whose pins are in SLR `h`. The analysis stage
re-derives this from the package pins and fails the build if it disagrees.

Clock groups (`60_constraints.tcl`): per die `{noc, vec}` and `{mat2x}`; the
sysnode clock alone; each bus; ctrl; each MIG; XDMA; the PCIe reference — the
same sets v7 closed with, plus the sysnode clock as its own group.

---

## 3. The address map, and how it is verified three times

**Station map** (`50_addr_lit.tcl`, decoded in the station RTL under
`SEG_OVERRIDE = 1`, 16 segments):

| window | host address | destination |
|---|---|---|
| node m's memory | `(m+1) << 40`, 1 TB | station m port 0 → that node's `S_AXI_MEM` |
| node m's control | `0x800000 + m·0x10000` | port 1 → width converter → `S_AXI_CTRL` |
| MIG m's controller | `m · 0x100000` | port 2 → `C0_DDR4_S_AXI_CTRL` |
| die m's clock wizard | `0x900000 + m·0x10000` | port 3 → `s_axi_lite` |

The memory window picks **which node the host enters through**, never a
channel: behind every node's DRAM master is the same flat 16 GB.

**Memory map** (`55_addr_fill.tcl`): `mesh_m/M_AXI_DRAM → xache/S0m_AXI` at 0,
16 GB; `xache/M0h_AXI → ddr4_{DDR_OF_SLR[h]}` at 0, 4 GB. The Xache's rotation
(`NSWAP = 18`, pairs `(i, i+2)` for `i = 14..31`) puts address bits `[15:14]`
in the home field: page `p` (16 KB) → home `p mod 4`, in-channel page `p div 4`.

Three checks, none of which trusts the other:

1. `v8/check_map.tcl` — the *intended* tables: prints every station segment and
   walks the rotation over the 16 GB, before anything is built;
2. `v8/75_verify_bd.tcl` — the *built* block design: every segment offset and
   range read back from the BD, every station port on its intended endpoint,
   the literals equal to the intended tables, every clock and reset pin driven
   by what §2 says;
3. `v8/70_analyze.tcl` — the *synthesised* netlist: clocks on every pin
   re-read, the reset audit, pblock coverage, and the address map read back
   again from the BD with the netlist open.

---

## 4. Floorplan

One pblock per die over its four clock-region rows. Pinned per die: its mesh,
wizard and resets, station (`g_stn[i]`, never `g_link`), MIG, divider,
converter — and the Xache's slices:

| in SLR i, home side | in SLR i, master side | crossings |
|---|---|---|
| `g_carray[i]`, `g_rd[i].g_pipe`, `g_rd[i].turn_q`, `g_wr[i].u_we`, `g_hedge[i]`, `g_wsel[i]` | `g_medge[i]`, `g_route[i]`, `g_agg[i]`, `g_wh[i]` | the **sending** half (`u_tx`) of every request crossing from master i and of every response crossing from home i; the **landing** half (`u_rx`) of every request crossing into home i and of every response crossing into master i |

`kx_slrx` is two modules for exactly this reason, and so is the reset tree:
`rst_tree`'s four sending registers sit in SLR1, its landing and fan-out
registers on their dies, with `USER_SLL_REG` on each crossing pair. What is
deliberately unpinned is what must span dies: the station links, the
interlink pipes, `xlconstant`.
SLR1 also holds XDMA, the JTAG manager, the ctrl wizard and the clock root
(`PCIE40E4_X0Y1` and the AY23 reference are both in SLR1), and `rst_sys` lives
with its generator.

The analysis stage checks that every leaf cell under the Xache is claimed by
**exactly one** die — one left out floats, one claimed twice is a placer error.

---

## 5. The ladder: what each rung verifies

| rung | what | state |
|---|---|---|
| the DRAM fabric | its own gate and OOC rows | its page |
| node DRAM port, `DRAM_CDC = 0` | `mag_dram_port_tb` and `_r1` with `m_aclk = s_aclk` and synchronous queues; the crossing mode | 64/64, 64/64; 64/64 |
| generated tops | 22 tops + 22 pumped tops with the NoC reset synchroniser and `DRAM_CDC`; `regen_tops.py --check` clean; both v8 tops lint clean | done |
| system e2e | `sb_mesh_e2e_sr` (host → station → node → DRAM and back, narrow endpoints), synchronous port and crossing port | 7/7, 7/7 |
| intended map | `check_map.tcl` | 16 segments; 16 GB walked at 16 KB |
| block design | `multimesh_v8_bd.tcl` (a fresh project; module-reference caches are dropped only on a rebuild, never before a plain reopen), then `75_verify_bd.tcl`: every clock and reset pin, every station port, the segment literals, the fabric's 4 + 4 interfaces, node → fabric at 0 → MIG at 0 (4 GB) | passes on the current flow |
| synthesis | OOC IP at 4 jobs, `synth_1`, `70_analyze.tcl`: clocks at rate, pins placed, DDR4 pins in their dies, reset audit (foreign loads 0; the reset tree's fanout-1 and per-die properties), every leaf of the fabric claimed by exactly one die (a leaf synthesis hoists out of its scope is pinned to the die of its neighbours), the address map read back from the BD | waits on the fabric |
| implementation | `v8_impl.tcl` to `write_bitstream`, then `80_report.tcl`: per-SLR resources, SLL per boundary, WNS per clock group, the worst path | waits on the fabric |

---

## 6. Settled parameter choices

| knob | value | why |
|---|---|---|
| sysnode clock | mesh 1's fourth wizard output, 300 | one node clock, one Xache clock, no crossing between them; retuning it retunes every node together |
| sysnode reset | `rst_sys` → `xcvu13p_rst_tree`, one registered copy per die | a die crossing is one flop driving one flop; −6.053 ns when one flop drove four dies |
| Xache | M 4, N 4, K 1, SAMD both, `RD_PIPE 1`, `RD_OUTQ 4`, `HCDC 1111`, `SLRX 1`, 64 URAM per home, `CDC_DEPTH 16` | the measured ship point, with the die crossings |
| interleave | 16 KB rotation over the flat 16 GB | a page walks the four channels; every set-index bit stays live |
| MIG AXI ID | 6 bits | the node's 4 bits plus the Xache's 2-bit master index |
| node | `DRAM_CDC 0`, `MAG_CDC 1`, `UNIT_CDC 1`, staging 4 × 16384 (64 URAM), no L2 adapters | the memory path is one clock; the units and the NoC keep their own rates |
| interlink | `mag_link_pipe_bd`, depth 2 | registered die crossing on one clock; no CDC FIFO |
| OOC synthesis | 4 jobs | four meshes at once took 141 GB of free memory to 2.8 GB |
| meshes | 2×2 2+2 / 2×2 6+2 / 2×2 2+2 / 2×2 2+2 | every die real; SLR1 carries the largest shape the ship targets |

What this image does **not** do: it is not programmed onto the card by this
flow, and no on-silicon acceptance exists for it yet; `boards/multimesh_v8.json`
names the bitstream and the addressing change so the driver is not surprised
when one is written.
