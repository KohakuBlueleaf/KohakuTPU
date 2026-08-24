# multimesh v5: v3 plus L2 staging, the reworked mover, and spread clocks.
# The mover is invisible here -- it is entirely inside mag.

# MULTIMESH_V5_XPR installs into that project; unset builds the standalone one.
set design_name multimesh_v5
set part        xcvu13p-fhgb2104-2L-e
set root        C:/Users/apoll/Desktop/code/Project/KohakuTPU

# ---- v5 configuration ----------------------------------------------------
# Each knob takes its environment variable if set, so a build can sweep.
proc knob {name default} {
    if {[info exists ::env($name)]} { return $::env($name) }
    return $default
}

# 1 gives each mesh its own reconfigurable MMCM: 4 MMCM and 3 extra control
# ports, so mesh_0 can run at its ceiling while the crowded dice sit lower.
# DEFAULT 1: v5 ships per-mesh per-component clocks. At 0 the four meshes share
# one generator AND the build silently takes v3's clock XDC instead of v5's.
set V5_PER_MESH_CLK [knob V5_PER_MESH_CLK 1]

# One MMCM has ONE VCO, so a second output is VCO/k -- an integer ratio, never
# an independent rate. VCO = 100/4*48 = 1200: k=4 is 300, k=8 is 150.
# SHIP AT THE TARGET, not the rate the card runs: P&R only optimises what it is
# constrained to, and the driver retunes down afterwards. k=2 is 600 for the 2x.
set V5_UNIT_CLK   [knob V5_UNIT_CLK 1]
set V5_UNIT_DIV   [knob V5_UNIT_DIV 2]

# L2 depth in URAM, as CONFIG properties on the ship module. The names must
# match what `ktpu_ship_*.v` exposes once the L2 lands.
# 8 per CU adapter = 256 KB, 376 MHz. 64 per MAG = 2 MB in 4 banks, 387 MHz --
# banked AND pipelined, which measured 337 -> 357 -> 381 as each was added.
set V5_L2_CU_URAM   [knob V5_L2_CU_URAM   8]
set V5_L2_MAG_URAM  [knob V5_L2_MAG_URAM  64]
set V5_L2_MAG_BANKS [knob V5_L2_MAG_BANKS 4]

# `_pump`: only these carry mat_clk2x/mat_div_clr. With the plain tops clk_out2
# is generated and left dangling, and the matmul ships at 1x.
# SLR-BALANCED: mesh_1 shares SLR1 with root_smc/xdma/jtag, measured 99.27% CLB
# against 88.70% for the cleanest, so it carries 4 clusters and the others 7.
set psfx [expr {[knob V5_PUMP 1] ? "_pump" : ""}]
# mesh_3 is on SLR2, whose leaf carries two extra masters -- the SLR3 chain and
# the GPIO -- so it stays at 6 clusters while SLR0 and SLR3 take a 7th.
set MESHES [list \
    [list 0 ktpu_ship_2x2_7c2v_1m$psfx 0 2 2] \
    [list 1 ktpu_ship_1x2_4c0v_1m$psfx 1 3 2] \
    [list 2 ktpu_ship_2x2_7c2v_1m$psfx 3 0 2] \
    [list 3 ktpu_ship_2x2_6c2v_1m$psfx 2 1 2]]
set SLR_ROWS {0 {Y0 Y3} 1 {Y4 Y7} 2 {Y8 Y11} 3 {Y12 Y15}}

if {[info exists ::env(MULTIMESH_V5_XPR)]} {
    set proj_dir [file dirname $::env(MULTIMESH_V5_XPR)]
    open_project $::env(MULTIMESH_V5_XPR)
} else {
    set proj_dir C:/Users/apoll/Desktop/vivado/multimesh_v5
    create_project -force multimesh_v5 $proj_dir -part $part
}
set_property target_language Verilog [current_project]

proc have {path} { expr {[llength [get_files -quiet $path]] > 0} }

# v3's sources, plus the v5 additions at the end. A file the project already
# has would otherwise land as a duplicate definition.
set SOURCES {
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
    src/kohakuaccel/sysnode/interlink/mag_link_cdc.v
    src/kohakuaccel/sysnode/interlink/mag_link_pipe.v src/kohakuaccel/sysnode/interlink/mag_switch.v
    src/kohakuaccel/sysnode/interlink/mag_ilink.v src/kohakuaccel/sysnode/core/mag.v
    src/kohakuaccel/sysnode/core/mag_dram_port.v src/kohakutpu/top/mag_1m.v
    src/kohakutpu/top/generated/ktpu_ship_2x2_7c2v_1m.v src/kohakutpu/top/generated/ktpu_ship_1x2_4c0v_1m.v
    src/kohakutpu/top/generated/ktpu_ship_2x2_6c2v_1m.v
    src/kohakutpu/matmul/mx_acu_fp_pump.v src/kohakutpu/matmul/mx_cluster_mgr_pump.v
    src/kohakutpu/matmul/mx_cluster_node_pump.v src/kohakutpu/matmul/mx_cluster_cu_pump.v
    src/kohakuaccel/common/clk/ktpu_div2.v
    src/kohakutpu/top/generated/ktpu_ship_2x2_7c2v_1m_pump.v
    src/kohakutpu/top/generated/ktpu_ship_1x2_4c0v_1m_pump.v
    src/kohakutpu/top/generated/ktpu_ship_2x2_6c2v_1m_pump.v
}

# v5 additions. Each is added only if it exists, so this script runs before the
# RTL lands and says what is still missing rather than failing to open.
# The adapter still sits under poc/. If it ships it belongs in kohakunoc, so
# both paths are tried and whichever exists is taken.
set V5_SOURCES {
    src/kohakuaccel/noc/endpoint/noc_l2_adapter.v
    src/synth_top/poc/l2_adapter.v
    src/kohakuaccel/sysnode/core/mag_stage.v
    src/kohakuaccel/sysnode/core/mag_stage_port.v
}
set missing {}
foreach f $V5_SOURCES {
    if {[file exists $root/$f]} { lappend SOURCES $f } else { lappend missing $f }
}
if {[llength $missing]} {
    puts "@@@ v5: not yet in the tree, building without: $missing"
}

foreach f $SOURCES {
    if {![have $root/$f]} { add_files -norecurse -fileset sources_1 $root/$f }
}
update_compile_order -fileset sources_1

set old [get_files -quiet ${design_name}.bd]
if {[llength $old]} {
    # GUARDED: close_bd_design on an empty list logs BD 41-71 before catch eats
    # it, and an ERROR line in a clean run breaks every log check downstream.
    set opened [get_bd_designs -quiet $design_name]
    if {[llength $opened]} { close_bd_design $opened }
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
# NO led PORT: bank 64 is in SLR1, and an IO anchor there pins the GPIO to the
# tightest SLR. Channel 2 (link health) is readback and needs no pin at all.
create_bd_port -dir O user_lnk_up
set pr [create_bd_port -dir I -type rst pcie_reset]
set_property CONFIG.POLARITY {ACTIVE_LOW} $pr

# ---- clocks --------------------------------------------------------------
# The control plane must never stand on the clock it changes.
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

# One mesh generator, or one per mesh. `clocks` is the list of generator names,
# so everything downstream iterates it and neither case is special-cased.
proc mesh_wiz {name unit_clk unit_div} {
    set w [create_bd_cell -type ip -vlnv xilinx.com:ip:clk_wiz $name]
    set cfg [list \
      CONFIG.PRIM_SOURCE {No_buffer} \
      CONFIG.PRIM_IN_FREQ {100.000} \
      CONFIG.CLKOUT1_REQUESTED_OUT_FREQ {300.000} \
      CONFIG.CLKOUT1_DRIVES {Buffer} CONFIG.PRIMITIVE {MMCM} \
      CONFIG.USE_RESET {false} CONFIG.FEEDBACK_SOURCE {FDBK_AUTO} \
      CONFIG.USE_DYN_RECONFIG {true} CONFIG.INTERFACE_SELECTION {Enable_AXI}]
    # clk_out2 is the MATMUL's, clk_out3 the VECTOR's: per-type control means
    # they retune apart, and a 2x matmul needs k half the vector's.
    if {$unit_clk} {
        lappend cfg CONFIG.CLKOUT2_USED {true} \
                    CONFIG.CLKOUT2_REQUESTED_OUT_FREQ [expr {1200.0 / $unit_div}] \
                    CONFIG.CLKOUT2_DRIVES {Buffer} \
                    CONFIG.CLKOUT3_USED {true} \
                    CONFIG.CLKOUT3_REQUESTED_OUT_FREQ {300.000} \
                    CONFIG.CLKOUT3_DRIVES {Buffer}
    }
    set_property -dict $cfg $w

    # OVERRIDE_MMCM FIRST, then the dividers: without it the IP re-solves from
    # the requested frequency and D comes back 1, a 25 MHz step instead of 6.25.
    set_property CONFIG.OVERRIDE_MMCM {true} $w
    set div [list CONFIG.MMCM_DIVCLK_DIVIDE {4} \
                  CONFIG.MMCM_CLKFBOUT_MULT_F {48.000} \
                  CONFIG.MMCM_CLKOUT0_DIVIDE_F {4.000}]
    if {$unit_clk} {
        lappend div CONFIG.MMCM_CLKOUT1_DIVIDE $unit_div \
                    CONFIG.MMCM_CLKOUT2_DIVIDE {4}
    }
    set_property -dict $div $w
    puts "@@@ $name D=[get_property CONFIG.MMCM_DIVCLK_DIVIDE $w] M=[get_property CONFIG.MMCM_CLKFBOUT_MULT_F $w] k=[get_property CONFIG.MMCM_CLKOUT0_DIVIDE_F $w]"
    return $w
}

set MESHCLK {}
if {$V5_PER_MESH_CLK} {
    foreach row $MESHES {
        lassign $row id mod slr ddr nmag
        mesh_wiz clk_wiz_mesh_$id $V5_UNIT_CLK $V5_UNIT_DIV
        lappend MESHCLK clk_wiz_mesh_$id
    }
} else {
    mesh_wiz clk_wiz_mesh $V5_UNIT_CLK $V5_UNIT_DIV
    lappend MESHCLK clk_wiz_mesh
}

create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset rst_ctrl_100M
foreach w $MESHCLK {
    create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset rst_${w}
}
set uvl [create_bd_cell -type ip -vlnv xilinx.com:ip:util_vector_logic util_vector_logic_0]
set_property -dict [list CONFIG.C_OPERATION {not} CONFIG.C_SIZE {1}] $uvl
set udb [create_bd_cell -type ip -vlnv xilinx.com:ip:util_ds_buf util_ds_buf_0]
set_property CONFIG.C_BUF_TYPE {IBUFDSGTE} $udb
set xlc [create_bd_cell -type ip -vlnv xilinx.com:ip:xlconstant xlconstant_0]
set_property -dict [list CONFIG.CONST_VAL {0} CONFIG.CONST_WIDTH {1}] $xlc
set gpio [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_gpio axi_gpio_0]
# ONE CHANNEL, read-only link health: 6 `fault` then 6 `ready`. It WAS channel 2
# behind the LEDs, so software's data register moves from 0x08 to 0x00.
set gpio2 [expr {$V5_PER_MESH_CLK ? 1 : 0}]
set_property -dict [list CONFIG.C_ALL_INPUTS {1} CONFIG.C_GPIO_WIDTH {12} \
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

# ---- the meshes ----------------------------------------------------------
# L2 knobs apply only where the module declares them, so this runs before they land.
proc set_if_declared {cell name value} {
    if {[llength [list_property [get_bd_cells $cell] CONFIG.$name]]} {
        set_property CONFIG.$name $value [get_bd_cells $cell]
        return 1
    }
    return 0
}

foreach row $MESHES {
    lassign $row id mod slr ddr nmag
    set cell [create_bd_cell -type module -reference $mod mesh_$id]
    set_property -dict [list CONFIG.MESH_ID $id CONFIG.GA {512} \
                             CONFIG.GB {512} CONFIG.TILES {4096} \
                             CONFIG.TILE_PRIM {ultra} CONFIG.VEC_PRIM {block}] $cell
    set got 0
    incr got [set_if_declared mesh_$id L2_CU_URAM   $V5_L2_CU_URAM]
    incr got [set_if_declared mesh_$id L2_MAG_URAM  $V5_L2_MAG_URAM]
    incr got [set_if_declared mesh_$id L2_MAG_BANKS $V5_L2_MAG_BANKS]
    if {$got == 0} { puts "@@@ mesh_$id: no L2 knobs declared yet" }
    connect_bd_intf_net [get_bd_intf_pins mesh_$id/M_AXI_DRAM] \
        [get_bd_intf_pins ddr4_$ddr/C0_DDR4_S_AXI]
}

# SLR0..SLR3 hold mesh 0, 1, 3, 2 and an SLL joins only ADJACENT SLRs, so a hop
# leaves by LINK1 and arrives on LINK0. The chain's two ends stay unconnected.

# Per-mesh clocks make every hop ASYNCHRONOUS, and `mag_link` ties tready high,
# so a direct net would sample a foreign domain in silence.

# INTERCONNECT, NOT PERIPHERAL: it deasserts first, and `xpm_fifo_async` silently
# DISCARDS writes while `wr_rst_busy` holds -- a bench lost beats 0..13 per link.
set ::V5_CDC_READY {}
set ::V5_CDC_FAULT {}
# A link CDC must take the SAME clock as the mesh it faces -- the divider's 1x
# when pumped -- so the clock loop connects these, not link_cdc.
array set ::V5_CDC_CLK {}
proc link_cdc {name src dst src_wiz dst_wiz} {
    create_bd_cell -type module -reference mag_link_cdc $name
    connect_bd_intf_net [get_bd_intf_pins $src] [get_bd_intf_pins $name/S_AXIS]
    connect_bd_intf_net [get_bd_intf_pins $name/M_AXIS] [get_bd_intf_pins $dst]
    lappend ::V5_CDC_CLK($src_wiz) $name/s_axis_aclk
    lappend ::V5_CDC_CLK($dst_wiz) $name/m_axis_aclk
    connect_bd_net [get_bd_pins rst_${src_wiz}/interconnect_aresetn] \
                   [get_bd_pins $name/s_axis_aresetn]
    connect_bd_net [get_bd_pins rst_${dst_wiz}/interconnect_aresetn] \
                   [get_bd_pins $name/m_axis_aresetn]
    lappend ::V5_CDC_READY [get_bd_pins $name/ready]
    lappend ::V5_CDC_FAULT [get_bd_pins $name/fault]
}

foreach hop {{0 1} {1 3} {3 2}} {
    lassign $hop lo hi
    if {$V5_PER_MESH_CLK} {
        link_cdc cdc_${lo}_to_${hi} mesh_$lo/M_AXIS_LINK1 mesh_$hi/S_AXIS_LINK0 \
                 clk_wiz_mesh_$lo clk_wiz_mesh_$hi
        link_cdc cdc_${hi}_to_${lo} mesh_$hi/M_AXIS_LINK0 mesh_$lo/S_AXIS_LINK1 \
                 clk_wiz_mesh_$hi clk_wiz_mesh_$lo
    } else {
        connect_bd_intf_net [get_bd_intf_pins mesh_$lo/M_AXIS_LINK1] \
                            [get_bd_intf_pins mesh_$hi/S_AXIS_LINK0]
        connect_bd_intf_net [get_bd_intf_pins mesh_$hi/M_AXIS_LINK0] \
                            [get_bd_intf_pins mesh_$lo/S_AXIS_LINK1]
    }
}

# Every mesh waits for ALL crossings, not its own two: the chain routes through
# neighbours, so a mesh can send over a crossing it does not terminate.
if {[llength $::V5_CDC_READY]} {
    set n [llength $::V5_CDC_READY]
    create_bd_cell -type ip -vlnv xilinx.com:ip:xlconcat cdc_ready_cat
    set_property CONFIG.NUM_PORTS $n [get_bd_cells cdc_ready_cat]
    for {set i 0} {$i < $n} {incr i} {
        connect_bd_net [lindex $::V5_CDC_READY $i] \
                       [get_bd_pins cdc_ready_cat/In$i]
    }
    create_bd_cell -type ip -vlnv xilinx.com:ip:util_reduced_logic cdc_ready_all
    set_property -dict [list CONFIG.C_OPERATION {and} CONFIG.C_SIZE $n] \
                       [get_bd_cells cdc_ready_all]
    connect_bd_net [get_bd_pins cdc_ready_cat/dout] \
                   [get_bd_pins cdc_ready_all/Op1]

    create_bd_cell -type ip -vlnv xilinx.com:ip:xlconcat cdc_health_cat
    set_property CONFIG.NUM_PORTS [expr {2 * $n}] [get_bd_cells cdc_health_cat]
    for {set i 0} {$i < $n} {incr i} {
        connect_bd_net [lindex $::V5_CDC_FAULT $i] \
                       [get_bd_pins cdc_health_cat/In$i]
        connect_bd_net [lindex $::V5_CDC_READY $i] \
                       [get_bd_pins cdc_health_cat/In[expr {$n + $i}]]
    }
    connect_bd_net [get_bd_pins cdc_health_cat/dout] \
                   [get_bd_pins axi_gpio_0/gpio_io_i]
}

# ---- the control plane, two levels ---------------------------------------
# Four leaves plus one port per mesh generator, so the count follows the clocking.
set nclk [llength $MESHCLK]
set rsmc [create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect root_smc]
# 5: two leaf feeds (SLR0, SLR2) plus SLR1's own three slaves. SLR3 hangs off
# SLR2 and the GPIO moved there too, so both ports left the tightest SLR.
set_property -dict [list CONFIG.NUM_SI {3} CONFIG.NUM_MI [expr {5 + $nclk}] \
                         CONFIG.NUM_CLKS {4}] $rsmc
connect_bd_intf_net [get_bd_intf_pins jtag_axi_0/M_AXI] [get_bd_intf_pins root_smc/S00_AXI]
connect_bd_intf_net [get_bd_intf_pins xdma_0/M_AXI]      [get_bd_intf_pins root_smc/S01_AXI]
connect_bd_intf_net [get_bd_intf_pins xdma_0/M_AXI_LITE] [get_bd_intf_pins root_smc/S02_AXI]

array set MESH_ON_SLR {0 0 1 1 2 3 3 2}
array set DDR_ON_SLR  {0 2 1 3 2 1 3 0}

# CHAINED: SLR3 hangs off SLR2's leaf, not the root. Halves its SLL crossings
# AND drops root_smc a master port, which is LUT out of the tightest SLR.
array set FEED {0 root_smc/M00_AXI 2 root_smc/M02_AXI 3 leaf_smc_2/M03_AXI}

foreach slr {0 1 2 3} {
    set mid $MESH_ON_SLR($slr)
    set did $DDR_ON_SLR($slr)
    # NO LEAF ON SLR1: root_smc is pblocked there, so a leaf crosses nothing --
    # it was a pass-through hop and a few k LUTs.
    if {$slr == 1} {
        connect_bd_intf_net [get_bd_intf_pins root_smc/M01_AXI] \
            [get_bd_intf_pins mesh_$mid/S_AXI_MEM]
        connect_bd_intf_net [get_bd_intf_pins root_smc/M03_AXI] \
            [get_bd_intf_pins mesh_$mid/S_AXI_CTRL]
        connect_bd_intf_net [get_bd_intf_pins root_smc/M04_AXI] \
            [get_bd_intf_pins ddr4_$did/C0_DDR4_S_AXI_CTRL]
        continue
    }
    # SLR2's leaf carries two extra masters: the SLR3 chain and the GPIO. Both
    # are LUT and a crossbar port taken OUT of SLR1, which is the tight one.
    set l [create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect leaf_smc_$slr]
    set_property -dict [list CONFIG.NUM_SI {1} \
                             CONFIG.NUM_MI [expr {$slr == 2 ? 5 : 3}] \
                             CONFIG.NUM_CLKS {3}] $l

    set rs [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_register_slice slr_cross_$slr]
    set_property -dict [list CONFIG.REG_AW {15} CONFIG.REG_AR {15} \
                             CONFIG.REG_W {15} CONFIG.REG_R {15} \
                             CONFIG.REG_B {15} CONFIG.NUM_SLR_CROSSINGS {1}] $rs
    connect_bd_intf_net [get_bd_intf_pins $FEED($slr)] \
                        [get_bd_intf_pins slr_cross_$slr/S_AXI]
    connect_bd_intf_net [get_bd_intf_pins slr_cross_$slr/M_AXI] \
                        [get_bd_intf_pins leaf_smc_$slr/S00_AXI]

    connect_bd_intf_net [get_bd_intf_pins leaf_smc_$slr/M00_AXI] \
        [get_bd_intf_pins mesh_$mid/S_AXI_MEM]
    connect_bd_intf_net [get_bd_intf_pins leaf_smc_$slr/M01_AXI] \
        [get_bd_intf_pins mesh_$mid/S_AXI_CTRL]
    connect_bd_intf_net [get_bd_intf_pins leaf_smc_$slr/M02_AXI] \
        [get_bd_intf_pins ddr4_$did/C0_DDR4_S_AXI_CTRL]
    if {$slr == 2} {
        connect_bd_intf_net [get_bd_intf_pins leaf_smc_$slr/M04_AXI] \
            [get_bd_intf_pins axi_gpio_0/S_AXI]
    }
}

# Every clock controller hangs off the ROOT, on the fixed domain: reaching one
# must not depend on the clock it is about to change.
# M%02d, not M0$mi: past 9 the ports are M10.., and M010 does not exist.
set mi 5
foreach w $MESHCLK {
    connect_bd_intf_net [get_bd_intf_pins root_smc/[format M%02d $mi]_AXI] \
                        [get_bd_intf_pins $w/s_axi_lite]
    incr mi
}

connect_bd_intf_net [get_bd_intf_ports system]   [get_bd_intf_pins util_ds_buf_sys/CLK_IN_D]
# EXPLICIT BUFG: Vivado's own inserted one exists only from opt_design, so no
# xdc can name it and rule_bufg_mmcm_3loads fails (5 MMCMs across SLR0..SLR3).
set sysbuf [create_bd_cell -type ip -vlnv xilinx.com:ip:util_ds_buf util_ds_buf_bufg]
set_property CONFIG.C_BUF_TYPE {BUFG} $sysbuf
connect_bd_net [get_bd_pins util_ds_buf_sys/IBUF_OUT] \
               [get_bd_pins util_ds_buf_bufg/BUFG_I]

set clkin [list [get_bd_pins clk_wiz_ctrl/clk_in1]]
foreach w $MESHCLK { lappend clkin [get_bd_pins $w/clk_in1] }
connect_bd_net [get_bd_pins util_ds_buf_bufg/BUFG_O] {*}$clkin
connect_bd_intf_net [get_bd_intf_ports pcie_clk] [get_bd_intf_pins util_ds_buf_0/CLK_IN_D]
connect_bd_intf_net [get_bd_intf_ports pcie_lane] [get_bd_intf_pins xdma_0/pcie_mgt]

# ---- clock and reset wiring ----------------------------------------------
set ctrlclk [list [get_bd_pins jtag_axi_0/aclk] \
                  [get_bd_pins axi_gpio_0/s_axi_aclk] \
                  [get_bd_pins rst_ctrl_100M/slowest_sync_clk] \
                  [get_bd_pins root_smc/aclk]]
set ctrlrst [list [get_bd_pins jtag_axi_0/aresetn] \
                  [get_bd_pins axi_gpio_0/s_axi_aresetn] \
                  [get_bd_pins root_smc/aresetn]]
foreach w $MESHCLK {
    lappend ctrlclk [get_bd_pins $w/s_axi_aclk]
    lappend ctrlrst [get_bd_pins $w/s_axi_aresetn]
}
foreach slr {0 2 3} {
    lappend ctrlclk [get_bd_pins leaf_smc_$slr/aclk] \
                    [get_bd_pins slr_cross_$slr/aclk]
    lappend ctrlrst [get_bd_pins leaf_smc_$slr/aresetn] \
                    [get_bd_pins slr_cross_$slr/aresetn]
}
connect_bd_net -net clk_ctrl [get_bd_pins clk_wiz_ctrl/clk_out1] {*}$ctrlclk
connect_bd_net -net rst_ctrl_periph \
    [get_bd_pins rst_ctrl_100M/peripheral_aresetn] {*}$ctrlrst
connect_bd_net -net clk_wiz_ctrl_locked [get_bd_pins clk_wiz_ctrl/locked] \
    [get_bd_pins util_vector_logic_0/Op1] [get_bd_pins rst_ctrl_100M/dcm_locked]

# Each mesh domain gates on its own generator's lock, so a retune drops lock,
# holds the meshes it feeds in reset, and releases them when it relocks.
foreach w $MESHCLK {
    set fed {}
    if {$V5_PER_MESH_CLK} {
        regexp {clk_wiz_mesh_(\d+)} $w -> only
        lappend fed $only
    } else {
        foreach row $MESHES { lassign $row id m s d n; lappend fed $id }
    }
    set meshclk [list [get_bd_pins rst_${w}/slowest_sync_clk]]
    if {[info exists ::V5_CDC_CLK($w)]} {
        foreach p $::V5_CDC_CLK($w) { lappend meshclk [get_bd_pins $p] }
    }
    set meshrst {}
    foreach id $fed {
        lappend meshclk [get_bd_pins mesh_$id/axi_aclk]
        # axi_aclk is MAG's and noc_clk the fabric's; v5 drives both from
        # clk_out1, which is what its "one fabric clock" comment below means.
        lappend meshclk [get_bd_pins mesh_$id/noc_clk]
        lappend meshrst [get_bd_pins mesh_$id/axi_aresetn]
        foreach row $MESHES {
            lassign $row rid rmod rslr rddr rn
            if {$rid != $id} { continue }
            # SLR1 has no leaf: its mesh domain is root_smc's aclk2.
            lappend meshclk [get_bd_pins \
                [expr {$rslr == 1 ? "root_smc/aclk2" : "leaf_smc_$rslr/aclk1"}]]
        }
    }
    set pumped 0
    foreach id $fed {
        if {[llength [get_bd_pins -quiet mesh_$id/mat_clk2x]]} { set pumped 1 }
    }
    # clk_out1 IS MAG AND THE NoC: one fabric clock. Only mat_clk comes off the
    # divider, and UNIT_CDC crosses the matmul into the fabric.
    connect_bd_net [get_bd_pins $w/clk_out1] {*}$meshclk
    # NO `-net NAME` when the net does not exist yet: that flag SELECTS an
    # existing net, and naming a new one is "does not exist" (BD 41-722).
    if {$pumped} {
        create_bd_cell -type module -reference ktpu_div2 div2_${w}
        connect_bd_net [get_bd_pins $w/clk_out2] [get_bd_pins div2_${w}/clk2x]
        # NOT set here: ktpu_div2 declares FREQ_HZ in X_INTERFACE_PARAMETER,
        # which makes the pin read-only (BD 41-737).
    }
    if {[llength $::V5_CDC_READY]} {
        create_bd_cell -type ip -vlnv xilinx.com:ip:util_vector_logic hold_${w}
        set_property -dict [list CONFIG.C_OPERATION {and} CONFIG.C_SIZE {1}] \
                           [get_bd_cells hold_${w}]
        connect_bd_net [get_bd_pins rst_${w}/peripheral_aresetn] \
                       [get_bd_pins hold_${w}/Op1]
        connect_bd_net [get_bd_pins cdc_ready_all/Res] [get_bd_pins hold_${w}/Op2]
        connect_bd_net -net rst_${w}_periph [get_bd_pins hold_${w}/Res] {*}$meshrst
    } else {
        connect_bd_net -net rst_${w}_periph \
            [get_bd_pins rst_${w}/peripheral_aresetn] {*}$meshrst
    }
    connect_bd_net -net ${w}_locked [get_bd_pins $w/locked] \
        [get_bd_pins rst_${w}/dcm_locked]

    # The unit domain, when it exists: VCO/k off the SAME generator, so it
    # tracks a retune rather than drifting from the fabric it talks to.
    if {$V5_UNIT_CLK} {
        # clk_out2 is 2x and ONLY a pumped top may take it; an unpumped
        # `mat_clk` is a 1x unit clock and belongs on clk_out3 beside the vector.
        set matclk {}; set vecclk {}; set divclr {}; set mat1x {}
        foreach id $fed {
            if {[llength [get_bd_pins -quiet mesh_$id/mat_clk2x]]} {
                lappend matclk [get_bd_pins mesh_$id/mat_clk2x]
                # A pumped top keeps mat_clk TOO, as the 1x partner of mat_clk2x.
                # It has to come off the same divider or the pair is not aligned.
                lappend mat1x [get_bd_pins mesh_$id/mat_clk]
            } elseif {[llength [get_bd_pins -quiet mesh_$id/mat_clk]]} {
                lappend vecclk [get_bd_pins mesh_$id/mat_clk]
            }
            foreach p {vec_clk unit_aclk} {
                if {[llength [get_bd_pins -quiet mesh_$id/$p]]} {
                    lappend vecclk [get_bd_pins mesh_$id/$p]
                }
            }
            if {[llength [get_bd_pins -quiet mesh_$id/mat_div_clr]]} {
                lappend divclr [get_bd_pins mesh_$id/mat_div_clr]
            }
        }
        if {[llength $matclk]} {
            connect_bd_net [get_bd_pins $w/clk_out2] {*}$matclk
            connect_bd_net [get_bd_pins div2_${w}/clk1x] {*}$mat1x
        } else {
            puts "@@@ $w: clk_out2 [expr {1200.0/$V5_UNIT_DIV}] MHz is UNCONNECTED -- no\
                  mesh declares mat_clk2x, so the matmul runs 1x off clk_out3"
        }
        if {[llength $vecclk]} {
            connect_bd_net [get_bd_pins $w/clk_out3] {*}$vecclk
        }
        # NOT locked, not a mesh reset: that reset is clocked BY this divider,
        # so driving CLR from it is circular. Lock releases them all on one edge.
        if {[llength $divclr] || $pumped} {
            create_bd_cell -type ip -vlnv xilinx.com:ip:util_vector_logic dclr_${w}
            set_property -dict [list CONFIG.C_OPERATION {not} CONFIG.C_SIZE {1}] \
                               [get_bd_cells dclr_${w}]
            connect_bd_net [get_bd_pins $w/locked] [get_bd_pins dclr_${w}/Op1]
            if {$pumped} { lappend divclr [get_bd_pins div2_${w}/clr] }
            connect_bd_net [get_bd_pins dclr_${w}/Res] {*}$divclr
        }
    }
}

set sysrst {}
foreach i {0 1 2 3} { lappend sysrst [get_bd_pins ddr4_$i/sys_rst] }
connect_bd_net -net util_vector_logic_0_Res [get_bd_pins util_vector_logic_0/Res] {*}$sysrst

foreach row $MESHES {
    lassign $row id mod slr ddr nmag
    # SLR1's DDR ctrl port hangs off the root, so that domain is root_smc/aclk3.
    connect_bd_net -net ddr4_${ddr}_ui_clk [get_bd_pins ddr4_$ddr/c0_ddr4_ui_clk] \
        [get_bd_pins rst_ddr4_${ddr}_300M/slowest_sync_clk] \
        [get_bd_pins mesh_$id/dram_aclk] \
        [get_bd_pins [expr {$slr == 1 ? "root_smc/aclk3" : "leaf_smc_$slr/aclk2"}]]
    connect_bd_net -net ddr4_${ddr}_ui_clk_sync_rst \
        [get_bd_pins ddr4_$ddr/c0_ddr4_ui_clk_sync_rst] \
        [get_bd_pins rst_ddr4_${ddr}_300M/ext_reset_in]
    connect_bd_net -net rst_ddr4_${ddr}_300M_peripheral_aresetn \
        [get_bd_pins rst_ddr4_${ddr}_300M/peripheral_aresetn] \
        [get_bd_pins ddr4_$ddr/c0_ddr4_aresetn] \
        [get_bd_pins mesh_$id/dram_aresetn]
}
connect_bd_net -net ddr4_0_ui_clk_sync_rst [get_bd_pins rst_ctrl_100M/ext_reset_in]
foreach w $MESHCLK {
    connect_bd_net -net ddr4_0_ui_clk_sync_rst [get_bd_pins rst_${w}/ext_reset_in]
}

connect_bd_net [get_bd_ports pcie_reset]  [get_bd_pins xdma_0/sys_rst_n]
connect_bd_net [get_bd_pins util_ds_buf_0/IBUF_DS_ODIV2] [get_bd_pins xdma_0/sys_clk]
connect_bd_net [get_bd_pins util_ds_buf_0/IBUF_OUT]      [get_bd_pins xdma_0/sys_clk_gt]
# aclk1, NEVER aclk: aclk is SmartConnect's primary and the whole crossbar runs
# on it, so binding it here freezes JTAG whenever PCIe is unplugged.
connect_bd_net [get_bd_pins xdma_0/axi_aclk]  [get_bd_pins root_smc/aclk1]
connect_bd_net [get_bd_pins xdma_0/user_lnk_up] [get_bd_ports user_lnk_up]
connect_bd_net [get_bd_pins xlconstant_0/dout]  [get_bd_pins xdma_0/usr_irq_req]

# ---- addresses -----------------------------------------------------------
# ONE map for every master: a shared leaf collides otherwise (BD 5-702).
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
    # 0x900000 upward, one 64 KiB aperture per generator.
    set off 0x900000
    foreach w $MESHCLK {
        assign_bd_address -offset [format 0x%X $off] -range 0x10000 \
            -target_address_space $s [get_bd_addr_segs $w/s_axi_lite/Reg] -force
        set off [expr {$off + 0x10000}]
    }
}

# reg0 is ONE 1 TB segment (40-bit slave) and lands only on a 1 TB boundary:
# at 1<<36 Vivado put all four meshes at 0. Bits [39:0] pass through intact.
set MESH_WIN [expr {1 << 40}]
foreach space {jtag_axi_0/Data xdma_0/M_AXI} {
    foreach id {0 1 2 3} {
        assign_bd_address -offset [format 0x%llX [expr {($id + 1) * $MESH_WIN}]] \
            -range [format 0x%llX $MESH_WIN] \
            -target_address_space [get_bd_addr_spaces $space] \
            [get_bd_addr_segs mesh_$id/S_AXI_MEM/reg0] -force
    }
}
set lite [get_bd_addr_spaces xdma_0/M_AXI_LITE]
foreach id {0 1 2 3} {
    exclude_bd_addr_seg -target_address_space $lite [get_bd_addr_segs mesh_$id/S_AXI_MEM/reg0]
}

# Connecting M_AXI_DRAM does NOT address it: all four sat unassigned (BD 41-1356)
# and would have tied off. MAG strips the mesh/aperture bits, so the DDR is at 0.
foreach row $MESHES {
    lassign $row id mod slr ddr nmag
    assign_bd_address \
        -target_address_space [get_bd_addr_spaces mesh_$id/M_AXI_DRAM] \
        [get_bd_addr_segs ddr4_$ddr/C0_DDR4_MEMORY_MAP/C0_DDR4_ADDRESS_BLOCK] -force
}

# EVERY CONTROL REGISTER ENDS BELOW 4 GiB so AXI-Lite reaches it too. v4 served
# control from the 64-bit M_AXI instead; a Lite-unreachable window is silent.
set LITE_TOP [expr {1 << 32}]
set unreachable {}
foreach seg [get_bd_addr_segs -addressables -of_objects $lite] {
    set off [get_property OFFSET $seg]
    set rng [get_property RANGE $seg]
    if {$off eq "" || $rng eq ""} { continue }
    if {[expr {$off + $rng}] > $LITE_TOP} {
        lappend unreachable "[get_property NAME $seg] at [format 0x%llX $off]"
    }
}
if {[llength $unreachable]} {
    error "control window(s) above 4 GiB, unreachable by XDMA AXI-Lite: [join $unreachable {, }]"
}
puts "@@@ control space: every M_AXI_LITE segment ends below 4 GiB"

# ---- constraints ---------------------------------------------------------
# Unpinned, multimesh measured WNS -1.945; pinned per SLR, +0.179.
set xdc $root/build/${design_name}_pblocks.xdc
set fh [open $xdc w]
foreach row $MESHES {
    lassign $row id mod slr ddr nmag
    lassign [dict get $SLR_ROWS $slr] ylo yhi
    puts $fh "create_pblock pb_slr$slr"
    puts $fh "resize_pblock \[get_pblocks pb_slr$slr\] -add \{CLOCKREGION_X0${ylo}:CLOCKREGION_X7${yhi}\}"
    puts $fh "add_cells_to_pblock \[get_pblocks pb_slr$slr\] \[get_cells -quiet {multimesh_v*_i/mesh_$id}\]"
    # The controller serving this SLR's mesh, and its reset, belong with it.
    puts $fh "add_cells_to_pblock \[get_pblocks pb_slr$slr\] \[get_cells -quiet {multimesh_v*_i/ddr4_$ddr}\]"
    puts $fh "add_cells_to_pblock \[get_pblocks pb_slr$slr\] \[get_cells -quiet {multimesh_v*_i/rst_ddr4_${ddr}_300M}\]"
    # NOT SLR1 -- it has no leaf, and add_cells_to_pblock on an empty list is a
    # CRITICAL WARNING (12-1433), not the silent no-op `-quiet` suggests.
    if {$slr != 1} {
        puts $fh "add_cells_to_pblock \[get_pblocks pb_slr$slr\] \[get_cells -quiet {multimesh_v*_i/leaf_smc_$slr}\]"
    }
    # The generator and its reset BELONG TO THE MESH THEY FEED. Left floating,
    # an MMCM lands in another SLR and every clock this mesh uses crosses one.
    if {$V5_PER_MESH_CLK} {
        puts $fh "add_cells_to_pblock \[get_pblocks pb_slr$slr\] \[get_cells -quiet {multimesh_v*_i/clk_wiz_mesh_$id}\]"
        puts $fh "add_cells_to_pblock \[get_pblocks pb_slr$slr\] \[get_cells -quiet {multimesh_v*_i/rst_clk_wiz_mesh_$id}\]"
        puts $fh "add_cells_to_pblock \[get_pblocks pb_slr$slr\] \[get_cells -quiet {multimesh_v*_i/div2_clk_wiz_mesh_$id}\]"
        # dclr drives that divider's CLR and hold that mesh's reset. Unpinned,
        # they were free to land in any SLR while the divider they feed was not.
        puts $fh "add_cells_to_pblock \[get_pblocks pb_slr$slr\] \[get_cells -quiet {multimesh_v*_i/dclr_clk_wiz_mesh_$id}\]"
        puts $fh "add_cells_to_pblock \[get_pblocks pb_slr$slr\] \[get_cells -quiet {multimesh_v*_i/hold_clk_wiz_mesh_$id}\]"
    }
    puts $fh "set_property CONTAIN_ROUTING false \[get_pblocks pb_slr$slr\]"
}
puts $fh "add_cells_to_pblock \[get_pblocks pb_slr1\] \[get_cells -quiet {multimesh_v*_i/root_smc}\]"
# XDMA's transceivers are already anchored, but its fabric half was free to roam.
puts $fh "add_cells_to_pblock \[get_pblocks pb_slr1\] \[get_cells -quiet {multimesh_v*_i/xdma_0}\]"
puts $fh "add_cells_to_pblock \[get_pblocks pb_slr1\] \[get_cells -quiet {multimesh_v*_i/jtag_axi_0}\]"
puts $fh "add_cells_to_pblock \[get_pblocks pb_slr1\] \[get_cells -quiet {multimesh_v*_i/clk_wiz_ctrl}\]"
puts $fh "add_cells_to_pblock \[get_pblocks pb_slr1\] \[get_cells -quiet {multimesh_v*_i/rst_ctrl_100M}\]"
# The clock tree's root. Bank 65 is in SLR1, so it has to sit here anyway.
puts $fh "add_cells_to_pblock \[get_pblocks pb_slr1\] \[get_cells -quiet {multimesh_v*_i/util_ds_buf_bufg}\]"
# WRITE SIDE: each SLR then owns its mesh AND the FIFOs for links LEAVING it,
# and the SLL crossing starts at the FIFO's registered output.
set MESH_SLR [dict create]
foreach row $MESHES { lassign $row rid rmod rslr rddr rn; dict set MESH_SLR $rid $rslr }
foreach hop {{0 1} {1 3} {3 2}} {
    lassign $hop lo hi
    puts $fh "add_cells_to_pblock \[get_pblocks pb_slr[dict get $MESH_SLR $lo]\] \[get_cells -quiet {multimesh_v*_i/cdc_${lo}_to_${hi}}\]"
    puts $fh "add_cells_to_pblock \[get_pblocks pb_slr[dict get $MESH_SLR $hi]\] \[get_cells -quiet {multimesh_v*_i/cdc_${hi}_to_${lo}}\]"
}
close $fh
add_files -fileset constrs_1 -norecurse $xdc

# Vivado parses EVERY enabled xdc: v2+v3 clock groups were live alongside v5's
# (they add, not replace), and root pblocks.xdc defined pb_slr* first and won.
foreach pat {*multimesh_v2_clocks.xdc *multimesh_v3_clocks.xdc
             *multimesh_v4_clocks.xdc */JTAG-DMA-test/pblocks.xdc} {
    foreach f [get_files -quiet -of_objects [get_filesets constrs_1] $pat] {
        if {[get_property is_enabled $f]} {
            set_property is_enabled false $f
            puts "@@@ disabled stale constraint: [file tail $f]"
        }
    }
}

# REPLACES the v3 file, never joins it: two groupings of one clock conflict.
set clkxdc [expr {$V5_PER_MESH_CLK ? "multimesh_v5_clocks" : "multimesh_v3_clocks"}]
if {![file exists $root/scripts/xdc/${clkxdc}.xdc]} {
    error "clock constraints missing: scripts/xdc/${clkxdc}.xdc -- without them every mesh-to-mesh crossing is TIMED"
}
add_files -fileset constrs_1 -norecurse $root/scripts/xdc/${clkxdc}.xdc
# LATE, or the clk_wiz generated clocks do not exist yet and every group matches
# nothing -- 16 critical warnings and no async grouping, run of 2026-08-12.
set_property PROCESSING_ORDER LATE \
    [get_files -of_objects [get_filesets constrs_1] */${clkxdc}.xdc]

# NEVER a TCL.POST hook: it runs as PART of synth_design, so an error there fails
# the run and nothing can open the design. reset, not {} -- a blank value stays.
catch { reset_property STEPS.SYNTH_DESIGN.TCL.POST [get_runs synth_1] }

validate_bd_design
save_bd_design

# Without a wrapper AND a top, synth_design has nothing to elaborate.
set bdf [get_files [current_bd_design].bd]
generate_target all $bdf
make_wrapper -files $bdf -top -import -force
set_property top [current_bd_design]_wrapper [current_fileset]
update_compile_order -fileset sources_1
puts "@@@ top: [get_property top [current_fileset]] in [get_property DIRECTORY [current_project]]"
puts "@@@ v5 built: [llength $MESHCLK] mesh generator(s), unit clock $V5_UNIT_CLK, L2 CU $V5_L2_CU_URAM URAM, MAG $V5_L2_MAG_URAM URAM in $V5_L2_MAG_BANKS banks"
