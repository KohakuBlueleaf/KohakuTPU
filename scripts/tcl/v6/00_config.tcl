# multimesh v6 -- every knob. Nothing below this file decides a rate or a shape.

set part        xcvu13p-fhgb2104-2L-e
set design_name multimesh_v6
set root        C:/Users/apoll/Desktop/code/Project/KohakuTPU
# THE project: it already holds multimesh v2..v5, the board pin XDC and the
# sources, so v6 lands beside them. V6_STANDALONE builds a throwaway instead.
set BOARD_DIR   C:/Users/apoll/Desktop/vivado/JTAG-DMA-test
set MAIN_XPR    $BOARD_DIR/JTAG-DMA-test.xpr
set proj_dir    [expr {[info exists ::env(V6_STANDALONE)]
                       ? "C:/Users/apoll/Desktop/vivado/multimesh_v6" : $BOARD_DIR}]

# ---- clocks --------------------------------------------------------------
# 300 everywhere, matmul 2x at 600. A per-die ladder is a fallback needing
# evidence, never a default.
set MESH_MHZ    300.000
set MAT2X_MHZ   600.000
set VEC_MHZ     300.000
set BUS_MHZ     300.000
set CTRL_MHZ    100.000
set DDR_MHZ     300.000
# NOT generated here: XDMA's axi_aclk comes off the PCIe core, set by
# CONFIG.axisten_freq, and the station's xdma port takes it directly.
set XDMA_MHZ    250.000

# VCO = 100/D*M = 1200, so 300 is /4 and 600 is /2, DRP step 6.25 MHz.
# OVERRIDE_MMCM must be set BEFORE these or D returns 1 and the step is 25.
set VCO_D       4
set VCO_M       48.000
set DIV_MESH    4
set DIV_MAT2X   2
set DIV_VEC     4

# ---- meshes --------------------------------------------------------------
# Mesh id == SLR id. `_pump` tops are the ONLY ones carrying mat_clk2x; a plain
# top leaves clk_out2 dangling and ships the matmul at 1x.
set MESHES {
    0 ktpu_ship_2x2_7c2v_1m_pump
    1 ktpu_ship_2x2_6c2v_1m_pump
    2 ktpu_ship_2x2_7c2v_1m_pump
    3 ktpu_ship_2x2_7c2v_1m_pump
}

# 4 banks x 16384 entries IS 64 URAM; the ship top names these, not L2_MAG_URAM.
# 32 URAM measured WORSE: m72_u32 -0.617 against m72_n5 -0.561 on the same RTL.
set L2_MAG_BANKS    4
set L2_MAG_ENTRIES  16384
set L2_CU_DEPTH     8192
set L2_VEC_DEPTH    8192

# Measured from v5's io_placed report: which DDR4 controller is in which die.
set DDR_OF_SLR {0 2  1 3  2 1  3 0}
set SLR_ROWS   {0 {Y0 Y3}  1 {Y4 Y7}  2 {Y8 Y11}  3 {Y12 Y15}}

# ---- station bus ---------------------------------------------------------
# FW=256 meets the mesh's 256-bit S_AXI_MEM exactly; the 512-bit XDMA master
# splits 2:1 in sb_nmu. AW=43: station index at AW-4, mesh windows at [42:40].
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
# The driver opens only channel 0, so 1/1 costs nothing and returns ~17k LUT.
set XDMA_RCHNL   1
set XDMA_WCHNL   1
