# multimesh v8t2 -- every knob. Nothing below this file decides a rate or a shape.
#
# The ship shape of the v8 memory path: four dies, each a 2x2 mesh of 2 clusters
# + 2 vector cores around a system node (MAG ilink ON), ONE sysnode clock for
# the four nodes and the partition-aware Kohaku Xache, ONE fixed 200 MHz system
# clock for JTAG and the four station-bus stations, per-die noc/mat2x/vec
# wizards, four MIGs. EVERY INDEX IS THE SLR: die = node = mesh = station =
# Xache partition/home = ddr4_<slr>. The board's own controller numbering
# appears in exactly one place, DDR_PORT_OF_SLR.

set part        xcvu13p-fhgb2104-2L-e
set design_name multimesh_v8t2
set root        C:/Users/apoll/Desktop/code/Project/KohakuTPU
# Board pin XDCs (ddr4_c*.xdc, pcie.xdc) live with the board project.
set BOARD_DIR   C:/Users/apoll/Desktop/vivado/JTAG-DMA-test
set proj_dir    C:/Users/apoll/Desktop/vivado/multimesh_v8t2

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
set SYS_CLK     clk_wiz_mesh$SYS_WIZ/clk_out4
set SYS_LOCK    clk_wiz_mesh$SYS_WIZ/locked
set CTRL_CLK    clk_wiz_ctrl/clk_out1
set BUS_CLK     clk_wiz_ctrl/clk_out2
# Die i's registered copy of the sysnode reset and of the bus reset
# (xcvu13p_rst_tree). One flop fanning a reset into four dies measured
# -6.053 ns at synthesis on v8; every load on die i takes its copy.
proc v8_rstn     {mid} { return rst_tree/rstn_o$mid }
proc v8_bus_rstn {mid} { return rst_tree_bus/rstn_o$mid }

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

# ---- meshes: die id == mesh id == MESH_ID == station == Xache partition ----
set MESHES {
    0 ktpu_ship_2x2_2c2v_1m_nol2_pump
    1 ktpu_ship_2x2_2c2v_1m_nol2_pump
    2 ktpu_ship_2x2_2c2v_1m_nol2_pump
    3 ktpu_ship_2x2_2c2v_1m_nol2_pump
}
# Staging: one 16384-entry bank, 4 URAM deep -- 64 URAM, 2 MB per node.
set L2_MAG_BANKS    1
set L2_MAG_ENTRIES  16384
# The node's DRAM master on the sysnode clock; the Xache's four DRAM edges are
# the only crossings on the memory path.
set DRAM_CDC        0
# The interlink surface between adjacent dies: kts_pipe_bd, one register each
# side of the SLL, both wires. Widths are the generated top's LKW / LKC.
set IL_W        288
set IL_VCW      1
set IL_CN_W     4

# ---- dies ----------------------------------------------------------------
# The board controller whose pins are in SLR s -- read off three builds'
# io_placed reports and the device model (c2 in SLR0, c3 in SLR1, c1 in SLR2,
# c0 in SLR3). The ONLY place the board's numbering appears: the cell in SLR s
# is ddr4_$s and connects to board port c<DDR_PORT_OF_SLR[s]>.
set DDR_PORT_OF_SLR {0 2  1 3  2 1  3 0}
set SLR_ROWS   {0 {Y0 Y3}  1 {Y4 Y7}  2 {Y8 Y11}  3 {Y12 Y15}}

# ---- Kohaku Xache (partition-aware) --------------------------------------
# The routed proof shape: 16384 sets x 2 ways, one bank, 15 URAM wide x 4 deep
# = 60 URAM (2 MB) per home, 240 in all; HCDC on the four DRAM edges only; the
# 16 KB rotation over the flat 16 GB (pairs (i, i+2), i = 14..31). HOME_LSB 32
# = 4 GB per MIG.
set KX_SETS      16384
set KX_SET_W     14
set KX_K         2
set KX_BANKS     1
set KX_RD_OUTQ   4
set KX_WR_OUTQ   4
set KX_HOP_DEPTH 16
set KX_HOP_RXREG 0
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
