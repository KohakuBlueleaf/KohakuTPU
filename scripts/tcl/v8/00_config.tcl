# multimesh v8 -- every knob. Nothing below this file decides a rate or a shape.
#
# The one-SLR probe on the v8 memory path: four stations, four dies each with a
# system node, a MIG and a mesh (6+2 on SLR1, 2+2 elsewhere), ONE sysnode clock
# for all four nodes and ONE Kohaku Xache (4 masters, 4 homes, 64 URAM each)
# whose only clock crossings are its four DRAM edges.

set part        xcvu13p-fhgb2104-2L-e
set design_name multimesh_v8
set root        C:/Users/apoll/Desktop/code/Project/KohakuTPU
# Board pin XDCs (ddr4_c*.xdc, pcie.xdc) live with the board project.
set BOARD_DIR   C:/Users/apoll/Desktop/vivado/JTAG-DMA-test
set proj_dir    C:/Users/apoll/Desktop/vivado/multimesh_v8

# ---- clocks --------------------------------------------------------------
# Per die: NoC 300, matmul 2x 600, vector 300 off that die's wizard. ONE
# sysnode clock for every node, the Xache and every station port: the fourth
# output of the SLR1 wizard. 200 is the only station-bus rate verified on
# hardware; the control clock is fixed and never retuned.
set MESH_MHZ    300.000
set MAT2X_MHZ   600.000
set VEC_MHZ     300.000
set SYS_MHZ     300.000
set BUS_MHZ     200.000
set CTRL_MHZ    100.000
# XDMA's axi_aclk comes off the PCIe core (CONFIG.axisten_freq), never an MMCM.
set XDMA_MHZ    250.000
# The wizard whose clk_out4 is the sysnode clock, and the pin every stage --
# build, verify, analyze -- names it by.
set SYS_WIZ     1
set SYS_CLK     clk_wiz_mesh$SYS_WIZ/clk_out4
set SYS_LOCK    clk_wiz_mesh$SYS_WIZ/locked
# The sysnode reset as die i sees it: rst_sys -> xcvu13p_rst_tree -> one
# registered copy per die. One flop fanning into four dies (5,703 loads)
# measured -6.053 ns at synthesis; every load on die i takes this copy.
proc v8_rstn {mid} { return rst_tree/rstn_o$mid }

# VCO = 100/D*M = 1200, so 300 is /4 and 600 is /2, DRP step 6.25 MHz.
# OVERRIDE_MMCM must be set BEFORE these or D returns 1 and the step is 25.
set VCO_D       4
set VCO_M       48.000
set DIV_MESH    4
set DIV_MAT2X   2
set DIV_VEC     4
set DIV_SYS     4
set DIV_BUS     6

# ---- meshes --------------------------------------------------------------
# Mesh id == SLR id. `_pump` tops carry mat_clk2x. `_nol2`: staging kept, no
# CU/vector L2 adapters -- the Xache is the L3.
set MESHES {
    0 ktpu_ship_2x2_2c2v_1m_nol2_pump
    1 ktpu_ship_2x2_6c2v_1m_nol2_pump
    2 ktpu_ship_2x2_2c2v_1m_nol2_pump
    3 ktpu_ship_2x2_2c2v_1m_nol2_pump
}
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
# Every die has its node, its MIG and its mesh in v8.
proc v8_has_mesh {mid} { return 1 }
proc v8_has_ddr  {i}   { return 1 }

# ---- Kohaku Xache --------------------------------------------------------
# One kx_xache: M = the four nodes' DRAM masters, N = the four MIGs, 64 URAM
# per home (2 MB, 32768 sets), the streaming engine with 4 bursts in flight
# per master, a kx_slrx per (master, home) pair, and the 16 KB rotation over
# the flat 16 GB: pairs (i, i+2) for i = 14..31 rotate [33:14] down by two so
# consecutive 16 KB pages walk the four channels. HOME_LSB 32 = 4 GB per MIG.
set KX_SETS     32768
set KX_SET_W    15
set KX_RD_OUTQ  4
set KX_SLRX     1
set KX_CDC_DEPTH 16
set KX_HOME_LSB 32
set KX_ILV_LG   14
set KX_NSWAP    [expr {$KX_HOME_LSB - $KX_ILV_LG}]
set KX_SWAP_A   {}
set KX_SWAP_B   {}
for {set i $KX_ILV_LG} {$i < $KX_HOME_LSB} {incr i} {
    lappend KX_SWAP_A $i
    lappend KX_SWAP_B [expr {$i + 2}]
}
# The MIG's AXI ID carries the master index: 4 bits of ID + 2 of master.
set KX_DRAM_IDW 6
# The whole DRAM the probe addresses: 4 x 4 GB, flat, mesh bits [37:36] zero.
set DRAM_GB     16

# ---- interlink -----------------------------------------------------------
# One clock on both ends now: a registered pipe per hop, no CDC FIFO.
set IL_PIPE     2

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
# 4 meshes in OOC synthesis at once took 141 GB of free memory to 2.8 GB.
set OOC_JOBS     4
