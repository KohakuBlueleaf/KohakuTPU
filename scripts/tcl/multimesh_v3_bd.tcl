# multimesh v3: v2 plus a control plane that survives PCIe being down.
# Rationale: docs/projects/kohakutpu/multi-mesh.md.

# MULTIMESH_V3_XPR installs into that project; unset builds the standalone one.
# The BD is always multimesh_v3, so it never collides with an existing multimesh.
set design_name multimesh_v3
set part        xcvu13p-fhgb2104-2L-e
set root        C:/Users/apoll/Desktop/code/Project/KohakuTPU

# {id, module, SLR, ddr4 index, mag ports}. SLR is ENFORCED here: unpinned,
# mesh_2 and mesh_3 landed on each other's SLR and crossed for their own DRAM.
set MESHES {
    {0 ktpu_ship_2x2_6c2v_1m 0 2 2}
    {1 ktpu_ship_2x1_6c0v_1m 1 3 2}
    {2 ktpu_ship_2x2_6c2v_1m 3 0 2}
    {3 ktpu_ship_2x2_6c2v_1m 2 1 2}
}

# VU13P: four SLRs of four clock-region rows each.
set SLR_ROWS {0 {Y0 Y3} 1 {Y4 Y7} 2 {Y8 Y11} 3 {Y12 Y15}}

if {[info exists ::env(MULTIMESH_V3_XPR)]} {
    set proj_dir [file dirname $::env(MULTIMESH_V3_XPR)]
    open_project $::env(MULTIMESH_V3_XPR)
} else {
    set proj_dir C:/Users/apoll/Desktop/vivado/multimesh_v3
    create_project -force multimesh_v3 $proj_dir -part $part
}
set_property target_language Verilog [current_project]

# add_files on a path the project already has leaves a duplicate definition.
proc have {path} { expr {[llength [get_files -quiet $path]] > 0} }
foreach f {
    src/kohakuaccel/common/sync_fifo.v src/kohakuaccel/common/kohaku_sdpram.v src/kohakuaccel/common/async_fifo.v
    src/kohakuaccel/noc/router/noc_inport.v src/kohakuaccel/noc/router/noc_outport.v
    src/kohakuaccel/noc/router/noc_router.v src/kohakuaccel/noc/endpoint/noc_cu_base.v
    src/kohakuaccel/noc/ctrl/noc_orchestrator.v
    src/kohakutpu/matmul/mx_mac.v src/kohakutpu/matmul/mx_tcu.v
    src/kohakutpu/matmul/mx_fpacc.v src/kohakutpu/matmul/mx_acu_fp.v
    src/kohakutpu/matmul/mx_cluster_core.v src/kohakutpu/matmul/mx_cluster_mgr.v
    src/kohakutpu/matmul/mx_cluster_node.v src/kohakutpu/matmul/mx_cluster_cu.v
    src/kohakuaccel/sysnode/mover/mx_tdesc.v
    src/kohakutpu/vector/vec_dsp.v src/kohakutpu/vector/vec_delay.v
    src/kohakutpu/vector/vec_tables.v src/kohakutpu/vector/vec_alu.v
    src/kohakutpu/vector/vec_cvt.v src/kohakutpu/vector/vec_regfile.v
    src/kohakutpu/vector/vec_lanes.v src/kohakutpu/vector/vec_agu.v
    src/kohakutpu/vector/vec_core.v src/kohakutpu/vector/vec_cu.v
    src/kohakutpu/transform/mx_quant.v src/kohakuaccel/sysnode/core/mag_mem_port.v
    src/kohakuaccel/sysnode/mover/mm_prng.v src/kohakuaccel/sysnode/mover/mm_mover.v
    src/kohakuaccel/sysnode/interlink/il_pkt_arb.v src/kohakuaccel/sysnode/interlink/mag_link.v
    src/kohakuaccel/sysnode/interlink/mag_link_pipe.v src/kohakuaccel/sysnode/interlink/mag_switch.v
    src/kohakuaccel/sysnode/interlink/mag_ilink.v src/kohakuaccel/sysnode/core/mag.v
    src/kohakuaccel/sysnode/core/mag_dram_port.v src/kohakutpu/top/mag_1m.v
    src/kohakutpu/top/generated/ktpu_ship_2x2_6c2v_1m.v src/kohakutpu/top/generated/ktpu_ship_2x1_6c0v_1m.v
} { if {![have $root/$f]} { add_files -norecurse -fileset sources_1 $root/$f } }
update_compile_order -fileset sources_1

# Rebuilding replaces any earlier multimesh_v3. A design named anything else --
# multimesh, singlemesh, design_1 -- is never named here and never a candidate.
set old [get_files -quiet ${design_name}.bd]
if {[llength $old]} {
    catch { close_bd_design [get_bd_designs -quiet $design_name] }
    set olddir [file dirname $old]
    remove_files $old
    file delete -force $olddir
}

create_bd_design $design_name
current_bd_design $design_name

# ---- boundary ------------------------------------------------------------
set system [create_bd_intf_port -mode Slave -vlnv xilinx.com:interface:diff_clock_rtl:1.0 system]
set_property CONFIG.FREQ_HZ {100000000} $system
foreach i {0 1 2 3} {
    create_bd_intf_port -mode Master -vlnv xilinx.com:interface:ddr4_rtl:1.0 c${i}_ddr4
    set p [create_bd_intf_port -mode Slave -vlnv xilinx.com:interface:diff_clock_rtl:1.0 c${i}_sys]
    set_property CONFIG.FREQ_HZ {400160000} $p
}
create_bd_intf_port -mode Slave  -vlnv xilinx.com:interface:diff_clock_rtl:1.0 pcie_clk
create_bd_intf_port -mode Master -vlnv xilinx.com:interface:pcie_7x_mgt_rtl:1.0 pcie_lane
create_bd_intf_port -mode Master -vlnv xilinx.com:interface:gpio_rtl:1.0 led
create_bd_port -dir O user_lnk_up
set pr [create_bd_port -dir I -type rst pcie_reset]
set_property CONFIG.POLARITY {ACTIVE_LOW} $pr

# ---- clocks --------------------------------------------------------------
# TWO generators: the control plane must never stand on the clock it changes.
# One IBUFDS for the pin pair: two clk_wiz each instantiating their own would
# be two buffers on one differential input.
set udbs [create_bd_cell -type ip -vlnv xilinx.com:ip:util_ds_buf util_ds_buf_sys]
set_property CONFIG.C_BUF_TYPE {IBUFDS} $udbs

set cwc [create_bd_cell -type ip -vlnv xilinx.com:ip:clk_wiz clk_wiz_ctrl]
set_property -dict [list \
  CONFIG.PRIM_SOURCE {No_buffer} \
  CONFIG.PRIM_IN_FREQ {100.000} \
  CONFIG.CLKOUT1_REQUESTED_OUT_FREQ {100.000} \
  CONFIG.CLKOUT1_DRIVES {Buffer} CONFIG.PRIMITIVE {MMCM} \
  CONFIG.USE_RESET {false} CONFIG.FEEDBACK_SOURCE {FDBK_AUTO} \
] $cwc

# D=4 -> PFD 25 MHz, k=4 -> 200..400 MHz, one step of M = 6.25 MHz.
# 300 MHz is M=48. docs/clocking.md.
set cwm [create_bd_cell -type ip -vlnv xilinx.com:ip:clk_wiz clk_wiz_mesh]
set_property -dict [list \
  CONFIG.PRIM_SOURCE {No_buffer} \
  CONFIG.PRIM_IN_FREQ {100.000} \
  CONFIG.CLKOUT1_REQUESTED_OUT_FREQ {300.000} \
  CONFIG.CLKOUT1_DRIVES {Buffer} CONFIG.PRIMITIVE {MMCM} \
  CONFIG.USE_RESET {false} CONFIG.FEEDBACK_SOURCE {FDBK_AUTO} \
  CONFIG.USE_DYN_RECONFIG {true} CONFIG.INTERFACE_SELECTION {Enable_AXI} \
] $cwm

# OVERRIDE_MMCM first, then the dividers: without it the IP re-solves from the
# requested frequency and D comes back 1, a 25 MHz step instead of 6.25.
set_property CONFIG.OVERRIDE_MMCM {true} $cwm
set_property -dict [list CONFIG.MMCM_DIVCLK_DIVIDE {4} \
                         CONFIG.MMCM_CLKFBOUT_MULT_F {48.000} \
                         CONFIG.MMCM_CLKOUT0_DIVIDE_F {4.000}] $cwm
puts "@@@ mesh MMCM D=[get_property CONFIG.MMCM_DIVCLK_DIVIDE $cwm] M=[get_property CONFIG.MMCM_CLKFBOUT_MULT_F $cwm] k=[get_property CONFIG.MMCM_CLKOUT0_DIVIDE_F $cwm]"

create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset rst_ctrl_100M
create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset rst_mesh
set uvl [create_bd_cell -type ip -vlnv xilinx.com:ip:util_vector_logic util_vector_logic_0]
set_property -dict [list CONFIG.C_OPERATION {not} CONFIG.C_SIZE {1}] $uvl
set udb [create_bd_cell -type ip -vlnv xilinx.com:ip:util_ds_buf util_ds_buf_0]
set_property CONFIG.C_BUF_TYPE {IBUFDSGTE} $udb
set xlc [create_bd_cell -type ip -vlnv xilinx.com:ip:xlconstant xlconstant_0]
set_property -dict [list CONFIG.CONST_VAL {0} CONFIG.CONST_WIDTH {1}] $xlc
set gpio [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_gpio axi_gpio_0]
set_property -dict [list CONFIG.C_ALL_OUTPUTS {0} CONFIG.C_GPIO_WIDTH {8} \
                         CONFIG.C_IS_DUAL {0}] $gpio
set jtag [create_bd_cell -type ip -vlnv xilinx.com:ip:jtag_axi jtag_axi_0]
set_property -dict [list CONFIG.M_AXI_ADDR_WIDTH {64} \
                         CONFIG.M_AXI_DATA_WIDTH {64}] $jtag
set xdma [create_bd_cell -type ip -vlnv xilinx.com:ip:xdma xdma_0]
set_property -dict [list \
  CONFIG.axi_data_width {512_bit} CONFIG.axi_id_width {4} \
  CONFIG.axilite_master_en {true} CONFIG.axilite_master_scale {Megabytes} \
  CONFIG.axilite_master_size {16} CONFIG.axisten_freq {250} \
  CONFIG.functional_mode {DMA} CONFIG.mode_selection {Basic} \
  CONFIG.pcie_blk_locn {X0Y1} CONFIG.pf0_device_id {903F} \
  CONFIG.pf0_subsystem_id {0007} CONFIG.pf0_subsystem_vendor_id {10EE} \
  CONFIG.pl_link_cap_max_link_speed {8.0_GT/s} \
  CONFIG.pl_link_cap_max_link_width {X16} CONFIG.ref_clk_freq {100_MHz} \
  CONFIG.xdma_num_usr_irq {1} CONFIG.xdma_rnum_chnl {4} \
  CONFIG.xdma_wnum_chnl {4} \
] $xdma

foreach i {0 1 2 3} {
    set d [create_bd_cell -type ip -vlnv xilinx.com:ip:ddr4 ddr4_$i]
    set_property -dict [list \
      CONFIG.C0.DDR4_DataWidth {72} CONFIG.C0.DDR4_InputClockPeriod {2499} \
      CONFIG.C0.DDR4_MemoryPart {MT40A512M16HA-083E} \
      CONFIG.C0.DDR4_TimePeriod {833} CONFIG.C0.DDR4_isCustom {true} \
    ] $d
    create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset rst_ddr4_${i}_300M
    connect_bd_intf_net [get_bd_intf_ports c${i}_ddr4] [get_bd_intf_pins ddr4_$i/C0_DDR4]
    connect_bd_intf_net [get_bd_intf_ports c${i}_sys]  [get_bd_intf_pins ddr4_$i/C0_SYS_CLK]
}

foreach row $MESHES {
    lassign $row id mod slr ddr nmag
    set cell [create_bd_cell -type module -reference $mod mesh_$id]
    set_property -dict [list CONFIG.MESH_ID $id CONFIG.GA {512} \
                             CONFIG.GB {512} CONFIG.TILES {4096} \
                             CONFIG.TILE_PRIM {ultra} CONFIG.VEC_PRIM {block}] $cell
    connect_bd_intf_net [get_bd_intf_pins mesh_$id/M_AXI_DRAM] \
        [get_bd_intf_pins ddr4_$ddr/C0_DDR4_S_AXI]
}

# The SLR chain. SLR0..SLR3 hold mesh 0, 1, 3, 2 and an SLL joins only ADJACENT
# SLRs, so each hop leaves the lower mesh by LINK1 and arrives on the upper
# mesh's LINK0. mesh0's LINK0 and mesh2's LINK1 are the chain's ends and stay
# unconnected -- three links, not four, and none of them spans a die it may not.
foreach hop {{0 1} {1 3} {3 2}} {
    lassign $hop lo hi
    connect_bd_intf_net [get_bd_intf_pins mesh_$lo/M_AXIS_LINK1] \
                        [get_bd_intf_pins mesh_$hi/S_AXIS_LINK0]
    connect_bd_intf_net [get_bd_intf_pins mesh_$hi/M_AXIS_LINK0] \
                        [get_bd_intf_pins mesh_$lo/S_AXIS_LINK1]
}

# ---- the control plane, two levels ---------------------------------------
# One 3x13 crossbar becomes a 3x5 root and four ~1x3 leaves: 39 port pairs to
# 24. You cannot spread one SmartConnect; you place four small ones.
set rsmc [create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect root_smc]
set_property -dict [list CONFIG.NUM_SI {3} CONFIG.NUM_MI {5} \
                         CONFIG.NUM_CLKS {2}] $rsmc
connect_bd_intf_net [get_bd_intf_pins jtag_axi_0/M_AXI] [get_bd_intf_pins root_smc/S00_AXI]
connect_bd_intf_net [get_bd_intf_pins xdma_0/M_AXI]      [get_bd_intf_pins root_smc/S01_AXI]
connect_bd_intf_net [get_bd_intf_pins xdma_0/M_AXI_LITE] [get_bd_intf_pins root_smc/S02_AXI]

# leaf N serves the mesh whose SLR is N. mesh_2 is on SLR3 and mesh_3 on SLR2,
# so the leaf index is the SLR, not the mesh id.
array set MESH_ON_SLR {0 0 1 1 2 3 3 2}
array set DDR_ON_SLR  {0 2 1 3 2 1 3 0}

foreach slr {0 1 2 3} {
    set mid $MESH_ON_SLR($slr)
    set did $DDR_ON_SLR($slr)
    set nmi [expr {$slr == 1 ? 4 : 3}]
    set l [create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect leaf_smc_$slr]
    set_property -dict [list CONFIG.NUM_SI {1} CONFIG.NUM_MI $nmi \
                             CONFIG.NUM_CLKS {3}] $l

    # REG_* 15 is Multi SLR Crossing: Vivado sizes and places the pipeline
    # itself. The slice is NOT pblocked -- that would pin the crossing path.
    if {$slr == 1} {
        connect_bd_intf_net [get_bd_intf_pins root_smc/M0${slr}_AXI] \
                            [get_bd_intf_pins leaf_smc_$slr/S00_AXI]
    } else {
        # Boundaries between the root's SLR1 and this leaf, not always 1.
        set ncross [expr {abs($slr - 1)}]
        set rs [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_register_slice slr_cross_$slr]
        set_property -dict [list CONFIG.REG_AW {15} CONFIG.REG_AR {15} \
                                 CONFIG.REG_W {15} CONFIG.REG_R {15} \
                                 CONFIG.REG_B {15} CONFIG.NUM_SLR_CROSSINGS $ncross] $rs
        connect_bd_intf_net [get_bd_intf_pins root_smc/M0${slr}_AXI] \
                            [get_bd_intf_pins slr_cross_$slr/S_AXI]
        connect_bd_intf_net [get_bd_intf_pins slr_cross_$slr/M_AXI] \
                            [get_bd_intf_pins leaf_smc_$slr/S00_AXI]
    }

    connect_bd_intf_net [get_bd_intf_pins leaf_smc_$slr/M00_AXI] \
        [get_bd_intf_pins mesh_$mid/S_AXI_MEM]
    connect_bd_intf_net [get_bd_intf_pins leaf_smc_$slr/M01_AXI] \
        [get_bd_intf_pins mesh_$mid/S_AXI_CTRL]
    connect_bd_intf_net [get_bd_intf_pins leaf_smc_$slr/M02_AXI] \
        [get_bd_intf_pins ddr4_$did/C0_DDR4_S_AXI_CTRL]
    if {$slr == 1} {
        connect_bd_intf_net [get_bd_intf_pins leaf_smc_$slr/M03_AXI] \
            [get_bd_intf_pins axi_gpio_0/S_AXI]
    }
}

# The clock controller hangs off the ROOT, on the fixed domain: reaching it
# must not depend on the clock it is about to change.
connect_bd_intf_net [get_bd_intf_pins root_smc/M04_AXI] \
                    [get_bd_intf_pins clk_wiz_mesh/s_axi_lite]

connect_bd_intf_net [get_bd_intf_ports led]      [get_bd_intf_pins axi_gpio_0/GPIO]
connect_bd_intf_net [get_bd_intf_ports system]   [get_bd_intf_pins util_ds_buf_sys/CLK_IN_D]
connect_bd_net [get_bd_pins util_ds_buf_sys/IBUF_OUT] \
    [get_bd_pins clk_wiz_ctrl/clk_in1] [get_bd_pins clk_wiz_mesh/clk_in1]
connect_bd_intf_net [get_bd_intf_ports pcie_clk] [get_bd_intf_pins util_ds_buf_0/CLK_IN_D]
connect_bd_intf_net [get_bd_intf_ports pcie_lane] [get_bd_intf_pins xdma_0/pcie_mgt]

# ---- clock and reset wiring ----------------------------------------------
set ctrlclk [list [get_bd_pins jtag_axi_0/aclk] \
                  [get_bd_pins axi_gpio_0/s_axi_aclk] \
                  [get_bd_pins clk_wiz_mesh/s_axi_aclk] \
                  [get_bd_pins rst_ctrl_100M/slowest_sync_clk] \
                  [get_bd_pins root_smc/aclk]]
set ctrlrst [list [get_bd_pins jtag_axi_0/aresetn] \
                  [get_bd_pins axi_gpio_0/s_axi_aresetn] \
                  [get_bd_pins clk_wiz_mesh/s_axi_aresetn] \
                  [get_bd_pins root_smc/aresetn]]
foreach slr {0 1 2 3} {
    lappend ctrlclk [get_bd_pins leaf_smc_$slr/aclk]
    lappend ctrlrst [get_bd_pins leaf_smc_$slr/aresetn]
    if {$slr != 1} {
        lappend ctrlclk [get_bd_pins slr_cross_$slr/aclk]
        lappend ctrlrst [get_bd_pins slr_cross_$slr/aresetn]
    }
}
connect_bd_net -net clk_ctrl [get_bd_pins clk_wiz_ctrl/clk_out1] {*}$ctrlclk
connect_bd_net -net rst_ctrl_periph \
    [get_bd_pins rst_ctrl_100M/peripheral_aresetn] {*}$ctrlrst
connect_bd_net -net clk_wiz_ctrl_locked [get_bd_pins clk_wiz_ctrl/locked] \
    [get_bd_pins util_vector_logic_0/Op1] [get_bd_pins rst_ctrl_100M/dcm_locked]

# The mesh domain. rst_mesh gates on clk_wiz_mesh/locked, so a retune drops
# lock, holds every mesh in reset, and releases them when it relocks.
set meshclk [list [get_bd_pins rst_mesh/slowest_sync_clk]]
set meshrst {}
foreach row $MESHES {
    lassign $row id mod slr ddr nmag
    lappend meshclk [get_bd_pins mesh_$id/axi_aclk]
    lappend meshrst [get_bd_pins mesh_$id/axi_aresetn]
    lappend meshclk [get_bd_pins leaf_smc_$slr/aclk1]
}
connect_bd_net -net clk_mesh [get_bd_pins clk_wiz_mesh/clk_out1] {*}$meshclk
connect_bd_net -net rst_mesh_periph [get_bd_pins rst_mesh/peripheral_aresetn] {*}$meshrst
connect_bd_net -net clk_wiz_mesh_locked [get_bd_pins clk_wiz_mesh/locked] \
    [get_bd_pins rst_mesh/dcm_locked]

set sysrst {}
foreach i {0 1 2 3} { lappend sysrst [get_bd_pins ddr4_$i/sys_rst] }
connect_bd_net -net util_vector_logic_0_Res [get_bd_pins util_vector_logic_0/Res] {*}$sysrst

foreach row $MESHES {
    lassign $row id mod slr ddr nmag
    connect_bd_net -net ddr4_${ddr}_ui_clk [get_bd_pins ddr4_$ddr/c0_ddr4_ui_clk] \
        [get_bd_pins rst_ddr4_${ddr}_300M/slowest_sync_clk] \
        [get_bd_pins mesh_$id/dram_aclk] \
        [get_bd_pins leaf_smc_$slr/aclk2]
    connect_bd_net -net ddr4_${ddr}_ui_clk_sync_rst \
        [get_bd_pins ddr4_$ddr/c0_ddr4_ui_clk_sync_rst] \
        [get_bd_pins rst_ddr4_${ddr}_300M/ext_reset_in]
    connect_bd_net -net rst_ddr4_${ddr}_300M_peripheral_aresetn \
        [get_bd_pins rst_ddr4_${ddr}_300M/peripheral_aresetn] \
        [get_bd_pins ddr4_$ddr/c0_ddr4_aresetn] \
        [get_bd_pins mesh_$id/dram_aresetn]
}
connect_bd_net -net ddr4_0_ui_clk_sync_rst [get_bd_pins rst_ctrl_100M/ext_reset_in]
connect_bd_net -net ddr4_0_ui_clk_sync_rst [get_bd_pins rst_mesh/ext_reset_in]

connect_bd_net [get_bd_ports pcie_reset]  [get_bd_pins xdma_0/sys_rst_n]
connect_bd_net [get_bd_pins util_ds_buf_0/IBUF_DS_ODIV2] [get_bd_pins xdma_0/sys_clk]
connect_bd_net [get_bd_pins util_ds_buf_0/IBUF_OUT]      [get_bd_pins xdma_0/sys_clk_gt]
# aclk1, NEVER aclk: aclk is SmartConnect's primary and the whole crossbar runs
# on it, so binding it here freezes JTAG whenever PCIe is unplugged.
connect_bd_net [get_bd_pins xdma_0/axi_aclk]  [get_bd_pins root_smc/aclk1]
connect_bd_net [get_bd_pins xdma_0/user_lnk_up] [get_bd_ports user_lnk_up]
connect_bd_net [get_bd_pins xlconstant_0/dout]  [get_bd_pins xdma_0/usr_irq_req]

# ---- addresses -----------------------------------------------------------
# ONE map for every master. A leaf SmartConnect is shared, so an address
# arriving there must mean one thing -- per-master maps collide on it (BD 5-702).
foreach space {jtag_axi_0/Data xdma_0/M_AXI xdma_0/M_AXI_LITE} {
    set s [get_bd_addr_spaces $space]
    foreach id {0 1 2 3} {
        assign_bd_address -offset [format 0x%X [expr {$id * 0x100000}]] -range 0x100000 \
            -target_address_space $s \
            [get_bd_addr_segs ddr4_$id/C0_DDR4_MEMORY_MAP_CTRL/C0_REG] -force
        assign_bd_address -offset [format 0x%X [expr {0x800000 + $id * 0x10000}]] \
            -range 0x10000 -target_address_space $s \
            [get_bd_addr_segs mesh_$id/S_AXI_CTRL/reg0] -force
    }
    assign_bd_address -offset 0x400000 -range 0x10000 -target_address_space $s \
        [get_bd_addr_segs axi_gpio_0/S_AXI/Reg] -force
    assign_bd_address -offset 0x900000 -range 0x10000 -target_address_space $s \
        [get_bd_addr_segs clk_wiz_mesh/s_axi_lite/Reg] -force
}

# Memory starts at 4 GB, which keeps the whole control aperture inside the reach
# of xdma_0/M_AXI_LITE -- a 32-bit master, hence the exclusion below.
foreach space {jtag_axi_0/Data xdma_0/M_AXI} {
    foreach id {0 1 2 3} {
        assign_bd_address -offset [format 0x%llX [expr {($id + 1) * 0x100000000}]] \
            -range 0x100000000 -target_address_space [get_bd_addr_spaces $space] \
            [get_bd_addr_segs mesh_$id/S_AXI_MEM/reg0] -force
    }
}
set lite [get_bd_addr_spaces xdma_0/M_AXI_LITE]
foreach id {0 1 2 3} {
    exclude_bd_addr_seg -target_address_space $lite [get_bd_addr_segs mesh_$id/S_AXI_MEM/reg0]
}

foreach row $MESHES {
    lassign $row id mod slr ddr nmag
    set masters {M_AXI_UPLOAD M_AXI_MOVER M_AXI_ILINK}
    for {set k 0} {$k < $nmag} {incr k} { lappend masters M_AXI_MEM$k }
    foreach m $masters {
        assign_bd_address -offset 0x00000000 -range 0x100000000 \
            -target_address_space [get_bd_addr_spaces mesh_$id/$m] \
            [get_bd_addr_segs ddr4_$ddr/C0_DDR4_MEMORY_MAP/C0_DDR4_ADDRESS_BLOCK] -force
    }
}

regenerate_bd_layout
save_bd_design
if {[catch {validate_bd_design -force} e]} {
    puts "=== VALIDATE-FAIL ==="
    puts $e
} else {
    puts "=== VALIDATE-OK ==="
}

# -import rather than a path: the generated directory is named after the
# PROJECT, so a literal multimesh_v3.gen is wrong in any other one.
make_wrapper -files [get_files $design_name.bd] -top -import
set_property top ${design_name}_wrapper [current_fileset]

# ---- the floorplan -------------------------------------------------------
# Soft pblocks, placement only. Routing is NOT contained: the point is to keep
# each mesh with its own DRAM, not to fence the router.
set xdc $proj_dir/pblocks.xdc
set fh [open $xdc w]
puts $fh "# Generated by scripts/tcl/multimesh_v3_bd.tcl -- do not hand-edit."
# The BD instance is `multimesh_v<n>_i`, so pinning the version matched NOTHING
# under v4 and every pblock came out empty. Glob it.
puts $fh ""
puts $fh "# The BD instance is multimesh_v<n>_i; the version is globbed so one"
puts $fh "# floorplan serves every revision of the design."
array set SLRY $SLR_ROWS
foreach slr {0 1 2 3} {
    lassign $SLRY($slr) ylo yhi
    set mid $MESH_ON_SLR($slr)
    puts $fh ""
    puts $fh "create_pblock pb_slr$slr"
    puts $fh "resize_pblock \[get_pblocks pb_slr$slr\] -add \{CLOCKREGION_X0${ylo}:CLOCKREGION_X7${yhi}\}"
    puts $fh "add_cells_to_pblock \[get_pblocks pb_slr$slr\] \[get_cells -quiet {multimesh_v*_i/mesh_$mid}\]"
    puts $fh "add_cells_to_pblock \[get_pblocks pb_slr$slr\] \[get_cells -quiet {multimesh_v*_i/leaf_smc_$slr}\]"
    puts $fh "set_property CONTAIN_ROUTING false \[get_pblocks pb_slr$slr\]"
}
puts $fh ""
puts $fh "# The root sits with XDMA, whose PCIe block is fixed at PCIE40E4_X0Y1."
puts $fh "add_cells_to_pblock \[get_pblocks pb_slr1\] \[get_cells -quiet {multimesh_v*_i/root_smc}\]"

close $fh
add_files -fileset constrs_1 -norecurse $xdc
# Static, not generated: emitting Tcl braces through `puts` silently swallowed
# the whole block into an unterminated string the first time.
add_files -fileset constrs_1 -norecurse $root/scripts/xdc/multimesh_v3_clocks.xdc
# LATE, or the clk_wiz generated clocks do not exist yet and every group in it
# matches nothing -- 16 critical warnings and no async grouping, run of 2026-08-12.
set_property PROCESSING_ORDER LATE \
    [get_files -of_objects [get_filesets constrs_1] */multimesh_v3_clocks.xdc]

puts "=== ADDRESS MAP ==="
foreach s [get_bd_addr_segs -of_objects [get_bd_addr_spaces]] {
    puts [format "%-58s %s +%s" $s [get_property OFFSET $s] [get_property RANGE $s]]
}
write_bd_tcl -force -no_ip_version $proj_dir/multimesh_v3_generated.tcl
puts "=== [string toupper $design_name] BUILT: $proj_dir ==="
