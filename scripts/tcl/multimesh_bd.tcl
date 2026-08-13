# multimesh.bd -- four meshes, four DRAMs, four SLRs. Placement, the measured
# bank->SLR map and the address split: docs/projects/kohakutpu/multi-mesh.md.

# One DDR4 per SLR, so no mesh crosses an SLR for its own DRAM. mesh0<->mesh2
# is the one link spanning SLR0..SLR3; pipeline THAT one and no other.

set design_name multimesh
set part xcvu13p-fhgb2104-2-e

# {id, module, SLR, ddr4 index, mag ports}. The master count is mags+3 with the
# interlink's landing channel, which is what sizes each smartconnect.
# 24 clusters, vec 2/2/2/0. SLR1 carries ~88.6k LUT more fixed load (XDMA and
# a MIG), so its mesh is the matmul-only one. Worst SLR ~72.2%.
# `_1m` packs MAG's masters INSIDE the mesh: one 512-bit M_AXI_DRAM, no SMC.
set MESHES {
    {0 ktpu_ship_2x2_6c2v_1m 0 2 2}
    {1 ktpu_ship_2x1_6c0v_1m 1 3 2}
    {2 ktpu_ship_2x2_6c2v_1m 3 0 2}
    {3 ktpu_ship_2x2_6c2v_1m 2 1 2}
}

proc mesh_field {row i} { return [lindex $row $i] }

# Set XPR to add multimesh to an existing project; unset builds a standalone
# one. Either way singlemesh.bd and design_1.bd are never opened or altered.
if {[info exists ::env(MULTIMESH_XPR)]} {
    set proj_dir [file dirname $::env(MULTIMESH_XPR)]
    open_project $::env(MULTIMESH_XPR)
} else {
    set proj_dir C:/Users/apoll/Desktop/vivado/multimesh_review
    create_project -force multimesh_review $proj_dir -part $part
}
set_property target_language Verilog [current_project]

# The generated tops and everything they instantiate. Skipped when the project
# already has the file, since add_files would leave a duplicate definition.
set root C:/Users/apoll/Desktop/code/Project/KohakuTPU
proc have {path} { expr {[llength [get_files -quiet $path]] > 0} }
foreach f {
    src/common/sync_fifo.v src/common/kohaku_sdpram.v src/common/async_fifo.v
    src/kohakunoc/noc_inport.v src/kohakunoc/noc_outport.v
    src/kohakunoc/noc_router.v src/kohakunoc/noc_cu_base.v
    src/kohakunoc/noc_orchestrator.v
    src/kohakutpu/matmul/mx_mac.v src/kohakutpu/matmul/mx_tcu.v
    src/kohakutpu/matmul/mx_fpacc.v src/kohakutpu/matmul/mx_acu_fp.v
    src/kohakutpu/matmul/mx_cluster_core.v src/kohakutpu/matmul/mx_cluster_mgr.v
    src/kohakutpu/matmul/mx_cluster_node.v src/kohakutpu/matmul/mx_cluster_cu.v
    src/kohakutpu/matmul/mx_tdesc.v
    src/kohakutpu/vector/vec_dsp.v src/kohakutpu/vector/vec_delay.v
    src/kohakutpu/vector/vec_tables.v src/kohakutpu/vector/vec_alu.v
    src/kohakutpu/vector/vec_cvt.v src/kohakutpu/vector/vec_regfile.v
    src/kohakutpu/vector/vec_lanes.v src/kohakutpu/vector/vec_agu.v
    src/kohakutpu/vector/vec_core.v src/kohakutpu/vector/vec_cu.v
    src/kohakumas/mx_quant.v src/kohakumas/mag_mem_port.v
    src/kohakumas/mm_prng.v src/kohakumas/mm_mover.v
    src/kohakumas/il_pkt_arb.v src/kohakumas/mag_link.v
    src/kohakumas/mag_link_pipe.v src/kohakumas/mag_switch.v
    src/kohakumas/mag_ilink.v src/kohakumas/mag.v
    src/kohakumas/mag_dram_port.v src/synth_top/mag_1m.v
    src/synth_top/ktpu_ship_2x2_6c2v_1m.v src/synth_top/ktpu_ship_2x1_6c0v_1m.v
} { if {![have $root/$f]} { add_files -norecurse -fileset sources_1 $root/$f } }

# Earlier mesh shapes left in the project are dead modules that still elaborate
# and still confuse a review. Drop every *_il top this design does not use.
set keep {}
foreach row $MESHES { lappend keep [lindex $row 1] }
foreach f [concat [get_files -quiet $root/src/synth_top/ktpu_ship_*_il.v] \
                  [get_files -quiet $root/src/synth_top/ktpu_ship_*_1m.v]] {
    if {[lsearch -exact $keep [file rootname [file tail $f]]] < 0} {
        puts "DROP stale source: $f"
        remove_files $f
    }
}
update_compile_order -fileset sources_1

# Rebuilding replaces any earlier multimesh; singlemesh and design_1 are never
# named here, so neither is ever a candidate for this.
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
    # As singlemesh.bd, the design on the card. design_1.bd's 100 MHz is wrong
    # and went unnoticed because that design was never implemented.
    set_property CONFIG.FREQ_HZ {400160000} $p
}
create_bd_intf_port -mode Slave  -vlnv xilinx.com:interface:diff_clock_rtl:1.0 pcie_clk
create_bd_intf_port -mode Master -vlnv xilinx.com:interface:pcie_7x_mgt_rtl:1.0 pcie_lane
create_bd_intf_port -mode Master -vlnv xilinx.com:interface:gpio_rtl:1.0 led
create_bd_port -dir O user_lnk_up
set pr [create_bd_port -dir I -type rst pcie_reset]
set_property CONFIG.POLARITY {ACTIVE_LOW} $pr

# ---- shared infrastructure, verbatim from singlemesh.bd -------------------
set clk_wiz_0 [create_bd_cell -type ip -vlnv xilinx.com:ip:clk_wiz clk_wiz_0]
set_property -dict [list \
  CONFIG.CLKOUT1_DRIVES {Buffer} CONFIG.CLKOUT1_REQUESTED_OUT_FREQ {300.000} \
  CONFIG.CLKOUT2_DRIVES {Buffer} CONFIG.CLKOUT3_DRIVES {Buffer} \
  CONFIG.CLKOUT4_DRIVES {Buffer} CONFIG.CLKOUT5_DRIVES {Buffer} \
  CONFIG.CLKOUT6_DRIVES {Buffer} CONFIG.CLKOUT7_DRIVES {Buffer} \
  CONFIG.ENABLE_CLOCK_MONITOR {false} CONFIG.FEEDBACK_SOURCE {FDBK_AUTO} \
  CONFIG.MMCM_BANDWIDTH {OPTIMIZED} CONFIG.MMCM_CLKFBOUT_MULT_F {12.000} \
  CONFIG.MMCM_CLKOUT0_DIVIDE_F {4.000} CONFIG.MMCM_COMPENSATION {AUTO} \
  CONFIG.OPTIMIZE_CLOCKING_STRUCTURE_EN {false} CONFIG.PRIMITIVE {MMCM} \
  CONFIG.PRIM_SOURCE {Differential_clock_capable_pin} CONFIG.USE_RESET {false} \
] $clk_wiz_0

create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset rst_clk_wiz_0_200M
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
  CONFIG.axilite_master_size {1} CONFIG.axisten_freq {250} \
  CONFIG.functional_mode {DMA} CONFIG.mode_selection {Basic} \
  CONFIG.pcie_blk_locn {X0Y1} CONFIG.pf0_device_id {903F} \
  CONFIG.pf0_subsystem_id {0007} CONFIG.pf0_subsystem_vendor_id {10EE} \
  CONFIG.pl_link_cap_max_link_speed {8.0_GT/s} \
  CONFIG.pl_link_cap_max_link_width {X16} CONFIG.ref_clk_freq {100_MHz} \
  CONFIG.xdma_num_usr_irq {1} CONFIG.xdma_rnum_chnl {4} \
  CONFIG.xdma_wnum_chnl {4} \
] $xdma

# ---- four DRAMs, design_1.bd's convention --------------------------------
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

# ---- the meshes ----------------------------------------------------------
foreach row $MESHES {
    lassign $row id mod slr ddr nmag
    set cell [create_bd_cell -type module -reference $mod mesh_$id]
    # GA/GB stay 512: L1 A/B is still block RAM, its offsets being 8-bit.
    # TILES 4096 is free -- the accumulator is width-bound at 5 URAM either way.
    set_property -dict [list CONFIG.MESH_ID $id CONFIG.GA {512} \
                             CONFIG.GB {512} CONFIG.TILES {4096} \
                             CONFIG.TILE_PRIM {ultra} CONFIG.VEC_PRIM {block}] $cell
    # STRAIGHT TO DRAM. mag_dram_port already arbitrated, packed 256->512 and
    # crossed to the memory clock, so there is nothing left for a fabric to do.
    connect_bd_intf_net [get_bd_intf_pins mesh_$id/M_AXI_DRAM] \
        [get_bd_intf_pins ddr4_$ddr/C0_DDR4_S_AXI]
}

# ---- MAG <-> MAG ---------------------------------------------------------
# The SLR chain: SLR0..SLR3 hold mesh 0, 1, 3, 2 and an SLL joins only ADJACENT
# SLRs, so each hop leaves the lower mesh by LINK1 and arrives on the upper
# mesh's LINK0. mesh0's LINK0 and mesh2's LINK1 are the chain's ends and stay
# unconnected -- three links, not four, and none spans a die it may not.
# Point to point, no FIFO and no register slice: TREADY MUST NOT CROSS AN SLR,
# and the far end's S_AXIS_LINK ties it high. Anything that backpressures here
# reintroduces the combinational crossing mag_link.v exists to avoid.
foreach hop {{0 1} {1 3} {3 2}} {
    lassign $hop lo hi
    connect_bd_intf_net [get_bd_intf_pins mesh_$lo/M_AXIS_LINK1] \
                        [get_bd_intf_pins mesh_$hi/S_AXIS_LINK0]
    connect_bd_intf_net [get_bd_intf_pins mesh_$hi/M_AXIS_LINK0] \
                        [get_bd_intf_pins mesh_$lo/S_AXIS_LINK1]
}

# ---- the control plane ---------------------------------------------------
# jtag + xdma M_AXI + xdma M_AXI_LITE reach every mesh's MEM and CTRL window,
# all four DDR control ports, and the GPIO.
set smc [create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect axi_smc]
set_property -dict [list CONFIG.NUM_CLKS {6} CONFIG.NUM_MI {13} \
                         CONFIG.NUM_SI {3}] $smc
connect_bd_intf_net [get_bd_intf_pins jtag_axi_0/M_AXI] [get_bd_intf_pins axi_smc/S00_AXI]
connect_bd_intf_net [get_bd_intf_pins xdma_0/M_AXI]      [get_bd_intf_pins axi_smc/S01_AXI]
connect_bd_intf_net [get_bd_intf_pins xdma_0/M_AXI_LITE] [get_bd_intf_pins axi_smc/S02_AXI]

set mi 0
foreach id {0 1 2 3} {
    connect_bd_intf_net [get_bd_intf_pins axi_smc/M[format %02d $mi]_AXI] \
        [get_bd_intf_pins mesh_$id/S_AXI_MEM] ; incr mi
    connect_bd_intf_net [get_bd_intf_pins axi_smc/M[format %02d $mi]_AXI] \
        [get_bd_intf_pins mesh_$id/S_AXI_CTRL] ; incr mi
}
foreach i {0 1 2 3} {
    connect_bd_intf_net [get_bd_intf_pins axi_smc/M[format %02d $mi]_AXI] \
        [get_bd_intf_pins ddr4_$i/C0_DDR4_S_AXI_CTRL] ; incr mi
}
connect_bd_intf_net [get_bd_intf_pins axi_smc/M[format %02d $mi]_AXI] \
    [get_bd_intf_pins axi_gpio_0/S_AXI]

connect_bd_intf_net [get_bd_intf_ports led]      [get_bd_intf_pins axi_gpio_0/GPIO]
connect_bd_intf_net [get_bd_intf_ports system]   [get_bd_intf_pins clk_wiz_0/CLK_IN1_D]
connect_bd_intf_net [get_bd_intf_ports pcie_clk] [get_bd_intf_pins util_ds_buf_0/CLK_IN_D]
connect_bd_intf_net [get_bd_intf_ports pcie_lane] [get_bd_intf_pins xdma_0/pcie_mgt]

# ---- clocks and resets ---------------------------------------------------
# One mesh clock for all four meshes and the whole control plane; each DDR
# keeps its own ui_clk, and its mesh's smartconnect is the crossing.
set meshclk [list [get_bd_pins jtag_axi_0/aclk] \
                  [get_bd_pins rst_clk_wiz_0_200M/slowest_sync_clk] \
                  [get_bd_pins axi_gpio_0/s_axi_aclk] \
                  [get_bd_pins axi_smc/aclk1]]
set meshrst [list [get_bd_pins jtag_axi_0/aresetn] \
                  [get_bd_pins axi_gpio_0/s_axi_aresetn] \
                  [get_bd_pins axi_smc/aresetn]]
foreach id {0 1 2 3} {
    lappend meshclk [get_bd_pins mesh_$id/axi_aclk]
    lappend meshrst [get_bd_pins mesh_$id/axi_aresetn]
}
connect_bd_net -net clk_wiz_0_clk_out1 [get_bd_pins clk_wiz_0/clk_out1] {*}$meshclk
connect_bd_net -net rst_peripheral_aresetn \
    [get_bd_pins rst_clk_wiz_0_200M/peripheral_aresetn] {*}$meshrst
connect_bd_net -net clk_wiz_0_locked [get_bd_pins clk_wiz_0/locked] \
    [get_bd_pins util_vector_logic_0/Op1] [get_bd_pins rst_clk_wiz_0_200M/dcm_locked]

# The NOT of `locked` resets every DDR, exactly as both references do.
set sysrst {}
foreach i {0 1 2 3} { lappend sysrst [get_bd_pins ddr4_$i/sys_rst] }
connect_bd_net -net util_vector_logic_0_Res [get_bd_pins util_vector_logic_0/Res] {*}$sysrst

# Each DDR's ui_clk drives its own reset block, its MESH's dram_aclk -- the
# crossing now lives in mag_dram_port -- and one axi_smc clock for the control.
set scaclk {aclk2 aclk3 aclk4 aclk5}
foreach row $MESHES {
    lassign $row id mod slr ddr nmag
    connect_bd_net -net ddr4_${ddr}_ui_clk [get_bd_pins ddr4_$ddr/c0_ddr4_ui_clk] \
        [get_bd_pins rst_ddr4_${ddr}_300M/slowest_sync_clk] \
        [get_bd_pins mesh_$id/dram_aclk] \
        [get_bd_pins axi_smc/[lindex $scaclk $ddr]]
    connect_bd_net -net ddr4_${ddr}_ui_clk_sync_rst \
        [get_bd_pins ddr4_$ddr/c0_ddr4_ui_clk_sync_rst] \
        [get_bd_pins rst_ddr4_${ddr}_300M/ext_reset_in]
    connect_bd_net -net rst_ddr4_${ddr}_300M_peripheral_aresetn \
        [get_bd_pins rst_ddr4_${ddr}_300M/peripheral_aresetn] \
        [get_bd_pins ddr4_$ddr/c0_ddr4_aresetn] \
        [get_bd_pins mesh_$id/dram_aresetn]
}
# ddr4_0's sync reset releases the mesh domain too, as singlemesh does. `-net`
# SELECTS the loop's existing net; a new name here is "net does not exist".
connect_bd_net -net ddr4_0_ui_clk_sync_rst \
    [get_bd_pins rst_clk_wiz_0_200M/ext_reset_in]

connect_bd_net [get_bd_ports pcie_reset]  [get_bd_pins xdma_0/sys_rst_n]
connect_bd_net [get_bd_pins util_ds_buf_0/IBUF_DS_ODIV2] [get_bd_pins xdma_0/sys_clk]
connect_bd_net [get_bd_pins util_ds_buf_0/IBUF_OUT]      [get_bd_pins xdma_0/sys_clk_gt]
connect_bd_net [get_bd_pins xdma_0/axi_aclk]  [get_bd_pins axi_smc/aclk]
connect_bd_net [get_bd_pins xdma_0/user_lnk_up] [get_bd_ports user_lnk_up]
connect_bd_net [get_bd_pins xlconstant_0/dout]  [get_bd_pins xdma_0/usr_irq_req]

# ---- addresses -----------------------------------------------------------
# ktpu.hw.interlink.global_addr() already defines the split: a 34-bit address
# is {mesh_id[1:0], local[31:0]}, so mesh N's 4 GB starts at N << 32 and the
# driver's remote addressing needs no translation here.
foreach space {jtag_axi_0/Data xdma_0/M_AXI} {
    foreach id {0 1 2 3} {
        assign_bd_address -offset [format 0x%llX [expr {$id * 0x100000000}]] \
            -range 0x100000000 -target_address_space [get_bd_addr_spaces $space] \
            [get_bd_addr_segs mesh_$id/S_AXI_MEM/reg0] -force
        assign_bd_address -offset [format 0x%llX [expr {0x400800000 + $id * 0x10000}]] \
            -range 0x10000 -target_address_space [get_bd_addr_spaces $space] \
            [get_bd_addr_segs mesh_$id/S_AXI_CTRL/reg0] -force
    }
    foreach i {0 1 2 3} {
        assign_bd_address -offset [format 0x%llX [expr {0x400000000 + $i * 0x100000}]] \
            -range 0x100000 -target_address_space [get_bd_addr_spaces $space] \
            [get_bd_addr_segs ddr4_$i/C0_DDR4_MEMORY_MAP_CTRL/C0_REG] -force
    }
    assign_bd_address -offset 0x400400000 -range 0x10000 \
        -target_address_space [get_bd_addr_spaces $space] \
        [get_bd_addr_segs axi_gpio_0/S_AXI/Reg] -force
}

# M_AXI_LITE is 32-bit and every control window is above 16 GB, so it can reach
# none of them. Excluded, not left unassigned: 13 CRITICAL WARNINGs otherwise.
set lite [get_bd_addr_spaces xdma_0/M_AXI_LITE]
foreach id {0 1 2 3} {
    exclude_bd_addr_seg -target_address_space $lite \
        [get_bd_addr_segs mesh_$id/S_AXI_MEM/reg0]
    exclude_bd_addr_seg -target_address_space $lite \
        [get_bd_addr_segs mesh_$id/S_AXI_CTRL/reg0]
    exclude_bd_addr_seg -target_address_space $lite \
        [get_bd_addr_segs ddr4_$id/C0_DDR4_MEMORY_MAP_CTRL/C0_REG]
}
exclude_bd_addr_seg -target_address_space $lite \
    [get_bd_addr_segs axi_gpio_0/S_AXI/Reg]

# Every master of mesh N sees only ddr4's 4 GB, at zero: the mesh id lives in
# the interlink header, not in the local AXI address.
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
# The reviewable artefact and the importable one.
write_bd_tcl -force -no_ip_version $proj_dir/multimesh_bd_generated.tcl
puts "=== ADDRESS MAP ==="
foreach s [get_bd_addr_segs -of_objects [get_bd_addr_spaces]] {
    puts [format "%-58s %s +%s" $s [get_property OFFSET $s] [get_property RANGE $s]]
}
puts "=== MULTIMESH BD BUILT: $proj_dir ==="
