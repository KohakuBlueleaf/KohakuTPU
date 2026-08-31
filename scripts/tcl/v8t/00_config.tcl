# multimesh v8t -- every knob. Nothing below this file decides a rate or a shape.
#
# The v8 memory path with NO mesh on any die: four stations, four dies each
# with a system node and a MIG, ONE sysnode clock for all four nodes and ONE
# partition-aware Kohaku Xache (kx_pxache: a partition per die, lanes of
# credited hops across every boundary) whose only clock crossings are its four
# DRAM edges. Every other clock stands alone, as in v7t.

set part        xcvu13p-fhgb2104-2L-e
set design_name multimesh_v8t
set root        C:/Users/apoll/Desktop/code/Project/KohakuTPU
# Board pin XDCs (ddr4_c*.xdc, pcie.xdc) live with the board project.
set BOARD_DIR   C:/Users/apoll/Desktop/vivado/JTAG-DMA-test
set proj_dir    C:/Users/apoll/Desktop/vivado/multimesh_v8t

# ---- clocks --------------------------------------------------------------
# ONE sysnode clock for every node and the Xache: the fourth output of the
# SLR1 wizard (the controllable one). A bus clock per die for its station.
# The control clock is fixed and never retuned. 200 is the only station-bus
# rate verified on hardware.
set SYS_MHZ     300.000
set BUS_MHZ     200.000
set CTRL_MHZ    100.000
# The per-die wizards keep a first output at the mesh rate so the board file's
# output indices and the driver's retune arithmetic stay those of v8; nothing
# loads it on any die.
set MESH_MHZ    300.000
# XDMA's axi_aclk comes off the PCIe core (CONFIG.axisten_freq), never an MMCM.
set XDMA_MHZ    250.000
set SYS_WIZ     1
set SYS_CLK     clk_wiz_mesh$SYS_WIZ/clk_out4
set SYS_LOCK    clk_wiz_mesh$SYS_WIZ/locked
# The sysnode reset as die i sees it: rst_sys -> xcvu13p_rst_tree -> one
# registered copy per die. One flop fanning into four dies measured -6.053 ns
# at synthesis on v8; every load on die i takes this copy.
proc v8_rstn {mid} { return rst_tree/rstn_o$mid }

# VCO = 100/D*M = 1200, so 300 is /4, DRP step 6.25 MHz. OVERRIDE_MMCM must be
# set BEFORE these or D returns 1 and the step is 25.
set VCO_D       4
set VCO_M       48.000
set DIV_MESH    4
set DIV_SYS     4
set DIV_BUS     6

# ---- nodes ---------------------------------------------------------------
# Die id == node id == station id == Xache partition. One module for all four;
# MESH_ID is the only parameter that differs.
set MESHES {
    0 ktpu_node_v8t
    1 ktpu_node_v8t
    2 ktpu_node_v8t
    3 ktpu_node_v8t
}
set NODE_PORTS      2
# 16384 entries of 1024 b in 4 banks of 4096 IS 64 single URAM of staging per node.
set L2_MAG_BANKS    4
set L2_MAG_ENTRIES  16384
# The node's DRAM master on the sysnode clock: its queues are synchronous and
# dram_aclk is wired to SYS_CLK. The only crossings on the memory path are
# the Xache's four DRAM edges.
set DRAM_CDC        0

# Measured from v5's io_placed report: which DDR4 controller is in which die.
set DDR_OF_SLR {0 2  1 3  2 1  3 0}
set SLR_ROWS   {0 {Y0 Y3}  1 {Y4 Y7}  2 {Y8 Y11}  3 {Y12 Y15}}

# ---- Kohaku Xache (partition-aware) --------------------------------------
# One kx_pxache: P = 4 partitions, master m = node m and home h = the MIG in
# SLR h, both in partition h; 64 URAM per home (2 MB, 32768 sets); the
# streaming engine with RD_OUTQ bursts in flight per master; HCDC on the four
# DRAM edges only; and the 16 KB rotation over the flat 16 GB: pairs (i, i+2)
# for i = 14..31 rotate [33:14] down by two so consecutive 16 KB pages walk
# the four channels. HOME_LSB 32 = 4 GB per MIG.
set KX_SETS      32768
set KX_SET_W     15
set KX_RD_OUTQ   4
set KX_WR_OUTQ   4
set KX_HOP_DEPTH 16
set KX_HOP_RXREG 0
# kx_carray banks per home: 1 = one 64-URAM array (v8t as measured), 8 = eight
# single-URAM-deep banks (v8t2). Read from here by 35_xache and 70_analyze.
set KX_BANKS     1
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
# The whole DRAM the probe addresses: 4 x 4 GB, flat, mesh bits [37:36] zero.
set DRAM_GB     16

# ---- station bus ---------------------------------------------------------
set FW           256
set AW           43
set NQ           4
set OST          4
set STORE_FWD    1
set LUT_PER_BRAM 0
set TIMEOUT      0
set CRED         16
set PIPE         4
set LINK_CDC     1
# Managers are all on station 1, so REQ flows outward and RSP inward only.
set LINK_FULL    0

# ---- XDMA ----------------------------------------------------------------
set XDMA_RCHNL   1
set XDMA_WCHNL   1

# ---- OOC IP runs ---------------------------------------------------------
set OOC_JOBS     4
