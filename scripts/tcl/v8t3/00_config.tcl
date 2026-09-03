# multimesh v8t3 -- every knob. Nothing below this file decides a rate or a shape.
#
# The ship shape of the v8 memory path: four dies, each a 2x2 mesh of 2 clusters
# + 2 vector cores around a system node (MAG ilink ON), ONE sysnode clock for
# the four nodes and the partition-aware Kohaku Xache, ONE fixed 200 MHz system
# clock for JTAG and the four station-bus stations, per-die noc/mat2x/vec
# wizards, four MIGs. EVERY INDEX IS THE SLR: die = node = mesh = station =
# Xache partition/home = ddr4_<slr>. The board's own controller numbering
# appears in exactly one place, DDR_PORT_OF_SLR.

set part        xcvu13p-fhgb2104-2L-e
set design_name multimesh_v8t3
set root        C:/Users/apoll/Desktop/code/Project/KohakuTPU
# Board pin XDCs (ddr4_c*.xdc, pcie.xdc) live with the board project.
set BOARD_DIR   C:/Users/apoll/Desktop/vivado/JTAG-DMA-test
set proj_dir    C:/Users/apoll/Desktop/vivado/multimesh_v8t3

# ---- clocks --------------------------------------------------------------
set MESH_MHZ    300.000
set MAT2X_MHZ   600.000
set VEC_MHZ     300.000
set SYS_MHZ     300.000
# The fixed control wizard: out1 is the 100 MHz control plane (wizard Lite
# ports, every reset's boot lock, station port 3), out2 the 200 MHz system
# clock (JTAG and all four stations; 200 is the only station-bus rate verified
# on hardware). Neither is ever retuned.
set CTRL_MHZ    100.000
set BUS_MHZ     200.000
# XDMA's axi_aclk comes off the PCIe core (CONFIG.axisten_freq), never an MMCM.
set XDMA_MHZ    250.000
set SYS_WIZ     1
set CTRL_CLK    clk_wiz_ctrl/clk_out1
# 0: ONE sysnode clock (mesh$SYS_WIZ) and ONE bus clock for the four dies, each
# reset fanned out by an xcvu13p_rst_tree. 1 (v8t4): die i drives its own
# sysnode clock off its own wizard and its own bus clock off a ctrl output of
# its own, each with its own reset -- so no clock net spans the four dies and
# every boundary becomes a crossing (KX_PCLK, IL_ASYNC, LINK_CDC follow).
set PER_DIE_CLK 0
proc v8_sys_clk  {mid} {
    global PER_DIE_CLK SYS_WIZ
    return clk_wiz_mesh[expr {$PER_DIE_CLK ? $mid : $SYS_WIZ}]/clk_out4
}
proc v8_sys_lock {mid} {
    global PER_DIE_CLK SYS_WIZ
    return clk_wiz_mesh[expr {$PER_DIE_CLK ? $mid : $SYS_WIZ}]/locked
}
proc v8_bus_clk  {mid} {
    global PER_DIE_CLK
    return clk_wiz_ctrl/clk_out[expr {$PER_DIE_CLK ? $mid + 2 : 2}]
}
# Die i's copy of the sysnode and bus resets: a registered copy off the one
# tree at PER_DIE_CLK 0 (one flop fanning into four dies measured -6.053 ns at
# synthesis on v8), its own proc_sys_reset on its own clock at 1.
proc v8_rstn     {mid} {
    global PER_DIE_CLK
    return [expr {$PER_DIE_CLK ? "rst_sys$mid/peripheral_aresetn" : "rst_tree/rstn_o$mid"}]
}
proc v8_bus_rstn {mid} {
    global PER_DIE_CLK
    return [expr {$PER_DIE_CLK ? "rst_bus$mid/peripheral_aresetn" : "rst_tree_bus/rstn_o$mid"}]
}

# VCO = 100/D*M = 1200: 300 is /4, 600 /2, 200 /6, 100 /12; DRP step 6.25 MHz.
# OVERRIDE_MMCM must be set BEFORE these or D returns 1 and the step is 25.
set VCO_D       4
set VCO_M       48.000
set DIV_MESH    4
set DIV_MAT2X   2
set DIV_VEC     4
set DIV_SYS     4
set DIV_BUS     6
set DIV_CTRL    12

# ---- dies: die id == MESH_ID == station == Xache partition -----------------
# v8t3 carries NO mesh: each die is one system node, interlinked 0-1-2-3 as the
# meshes were. The name MESHES stays because every stage iterates it.
set MESHES {
    0 ktpu_node_v8t
    1 ktpu_node_v8t
    2 ktpu_node_v8t
    3 ktpu_node_v8t
}
set NODE_ILINK      1
set NODE_PORTS      2
# Staging: 4 banks of 4096 entries -- 64 SINGLE URAM, 2 MB per node. Never a
# chain: a cascade is combinational from the first block's clock (~0.27 ns a
# hop) and UG573 p.116 pins it bottom-up in one column.
set L2_MAG_BANKS    4
set L2_MAG_ENTRIES  16384
# The node's DRAM master on the sysnode clock; the Xache's four DRAM edges are
# the only crossings on the memory path.
set DRAM_CDC        0
# The interlink surface between adjacent dies: kts_pipe_bd, one register each
# side of the SLL, both wires. Widths are the generated top's LKW / LKC.
set IL_W        288
set IL_VCW      1
set IL_CN_W     4
# At IL_ASYNC 1 each hop carries a kts_cdc, whose forward buffer must cover every
# flit the credits allow in flight. IL_CRED is mag_link's RX_BEATS.
set IL_ASYNC    0
set IL_CRED     64
# Register stages in each pipe half, free to walk from the node's port to the
# die boundary; 1 is the v8t3..v8t6 image, each stage a cycle the credits absorb.
set IL_STAGES   1

# ---- dies ----------------------------------------------------------------
# The board controller whose pins are in SLR s -- read off three builds'
# io_placed reports and the device model (c2 in SLR0, c3 in SLR1, c1 in SLR2,
# c0 in SLR3). The ONLY place the board's numbering appears: the cell in SLR s
# is ddr4_$s and connects to board port c<DDR_PORT_OF_SLR[s]>.
set DDR_PORT_OF_SLR {0 2  1 3  2 1  3 0}
set SLR_ROWS   {0 {Y0 Y3}  1 {Y4 Y7}  2 {Y8 Y11}  3 {Y12 Y15}}
# Optional per-die column range for the compute half -- node, its Xache
# partition, its station. Every MIG's fabric lands in clock-region column X4 and
# splits its die; on the v8t3 image die 2 alone put its node at X5 and its Xache
# partition at X3, on opposite sides of that block, and every congestion window
# in the design is in the gap. Empty leaves the die-wide pblock as the only
# constraint, which is what v8t3 and v8t4 were built with. An entry is
# {xlo xhi} over the die's rows or {xlo xhi ylo yhi}; CMP_STN lists the dies
# whose station follows the node into the box (the others stay die-wide).
set CMP_COLS   {}
set CMP_STN    {0 1 2 3}
set CMP_HOME   {0 1 2 3}

# ---- Kohaku Xache (partition-aware) --------------------------------------
# The routed proof shape (impl_pxache d4_k2b4_f4): 16384 sets x 2 ways in FOUR
# banks, flat rows so one row is one word and a bank is 8 URAM wide x 2 deep =
# 64 URAM (2 MB) per home, 256 in all; 16,357 LUT / WNS +0.006 / 184 BRAM /
# 1,062 wires a boundary. HCDC on the four DRAM edges only; the 16 KB rotation
# over the flat 16 GB (pairs (i, i+2), i = 14..31). HOME_LSB 32 = 4 GB per MIG.
set KX_SETS      16384
set KX_SET_W     14
set KX_K         2
set KX_BANKS     4
set KX_RD_OUTQ   4
set KX_WR_OUTQ   4
# Beats a read slot holds (0 = a 4 KB page, 64 beats); the reorder ring is
# RD_OUTQ x this deep, and every master must keep its read bursts within it.
set KX_RB_BEATS  0
set KX_HOP_DEPTH 16
set KX_HOP_RXREG 0
# ONE shared crossing per boundary-direction instead of a lane per source:
# 1,062 wires a boundary against the per-lane 4,696, which is the SLL usage
# that walled v8t2's die.
set KX_BND_TRUNK 1
# Partition p on its own clock: every boundary trunk becomes the crossing, and
# so does each master edge (master p and partition p share die p's clock).
set KX_PCLK      0
# Which primitive holds each FIFO class. A 16-deep class costs ~width/2 LUT
# in LUTRAM (28 bits a LUT); the reorder ring costs width x depth / 56 plus a
# read mux past 64 deep, so it is only cheap with KX_RB_BEATS 16 (592 LUT a
# master against 2,887 at a page).
set KX_MEM_TRUNK block
set KX_MEM_RB    block
set KX_MEM_HRD   block
set KX_MEM_HWR   block
set KX_CDC_DEPTH 16
set KX_HOME_LSB  32
set KX_ILV_LG    14
set KX_NSWAP [expr {$KX_HOME_LSB - $KX_ILV_LG}]
set KX_SWAP_A {}
set KX_SWAP_B {}
for {set i $KX_ILV_LG} {$i < $KX_HOME_LSB} {incr i} {
    lappend KX_SWAP_A $i
    lappend KX_SWAP_B [expr {$i + 2}]
}
# The MIG's AXI ID carries the master index: 4 bits of ID + 2 of master.
set KX_DRAM_IDW 6
set DRAM_GB     16

# ---- station bus ---------------------------------------------------------
# One clock for the four stations (LINK_CDC 0: sb_link register pipes across
# the SLLs, each end on its own die's reset copy) and JTAG on that same clock
# through the jbus wrapper (MGR0_DOM 1: synchronous NMU queues, no crossing).
set SB_WRAP      sb_bd_line4_jbus
set FW           256
set AW           43
set NQ           4
set OST          4
set STORE_FWD    1
set LUT_PER_BRAM 0
# Station 1's threshold alone, and its three managers' queue depths and burst
# bound (jtag, xdma, xdma-lite): sb_nmu floors are one MAX_BURST packet (x2
# flits for RSP on the 512-bit xdma), 64 / 128 there; 256 fills block-RAM rows.
set SB_LPB1      $LUT_PER_BRAM
set SB_MREQ0 256
set SB_MREQ1 256
set SB_MREQ2 16
set SB_MRSP0 256
set SB_MRSP1 256
set SB_MRSP2 16
set SB_MMAXB0 0
set SB_MMAXB1 0
set SB_MMAXB2 1
set TIMEOUT      0
set CRED         16
set PIPE         4
set LINK_CDC     0
# Managers are all on station 1, so REQ flows outward and RSP inward only.
set LINK_FULL    0
set LINK_KTS     0
set MGR0_DOM     1

# ---- XDMA ----------------------------------------------------------------
set XDMA_RCHNL   1
set XDMA_WCHNL   1

# ---- OOC IP runs ---------------------------------------------------------
set OOC_JOBS     4
