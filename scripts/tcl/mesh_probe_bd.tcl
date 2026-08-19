# One mesh on one SLR, real station-bus line, other dies on block RAM.
#   -tclargs <tag> <mesh_module> <target_slr> <l2_depth> <use_xdma> [impl]

set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e

set tag   [lindex $argv 0]
set mod   [lindex $argv 1]
set tslr  [lindex $argv 2]
set l2d   [lindex $argv 3]
set xdma_on [lindex $argv 4]
# MAG L2 in URAM. A bank is 4096 rows x 4 words = 16 URAM and a shallower one
# wastes depth, so capacity moves by BANKS, never by entries alone.
set maguram [lindex $argv 5]
set do_impl [expr {[lsearch $argv impl] >= 0}]
if {$tag eq ""}  { set tag probe }
if {$mod eq ""}  { set mod ktpu_ship_2x2_7c2v_1m_pump }
if {$tslr eq ""} { set tslr 3 }
if {$l2d eq ""}  { set l2d 4096 }
if {$xdma_on eq ""} { set xdma_on 0 }
# Implementation strategy. Empty leaves Vivado Implementation Defaults, which
# applies NO congestion directive at all.
set strat [lindex $argv 6]
if {$strat eq "impl" || $strat eq "build" || $strat eq "none"} { set strat "" }
if {$maguram eq "" || ![string is integer -strict $maguram]} { set maguram 64 }
if {$maguram % 16 != 0} { error "MAG URAM must be a multiple of 16, got $maguram" }
set magbanks [expr {$maguram / 16}]
set magent   [expr {$magbanks * 4096}]


set design_name mp_$tag
set proj_dir C:/Users/apoll/Desktop/vivado/probe/$tag
set board_dir C:/Users/apoll/Desktop/vivado/JTAG-DMA-test

# Measured from the v5 io_placed report: the controller physically in each die.
set DDR_OF_SLR [dict create 0 2 1 3 2 1 3 0]
set ddr [dict get $DDR_OF_SLR $tslr]

set BUS_MHZ 200.0 ; set CTRL_MHZ 100.0 ; set XDMA_MHZ 250.0 ; set NQ 4 ; set AW 43
# 300 on every die, so clk_out2 is 600: the stated stop criterion. The old
# per-die ladder asked SLR2 for 180 and SLR3 for 210 and flattered both.
set SLR_MHZ  {0 300.000 1 300.000 2 300.000 3 300.000}
set SLR_ROWS {0 {Y0 Y3} 1 {Y4 Y7} 2 {Y8 Y11} 3 {Y12 Y15}}

# The board constraints are required, never optional: a silent skip leaves the
# DDR4 and PCIe pins unconstrained and the run fails much later, elsewhere.
proc board_xdc {name} {
    global board_dir
    if {![file exists $board_dir/$name]} { error "missing board XDC: $board_dir/$name" }
    return $board_dir/$name
}

set_param general.maxThreads 16
create_project -force $design_name $proj_dir -part $part
set_property target_language Verilog [current_project]

set SRC {
    src/kohakuaccel/common/sync_fifo.v src/kohakuaccel/common/kohaku_sdpram.v src/kohakuaccel/common/async_fifo.v
    src/kohakuaccel/noc/router/noc_inport.v src/kohakuaccel/noc/router/noc_outport.v
    src/kohakuaccel/noc/router/noc_router.v src/kohakuaccel/noc/endpoint/noc_cu_base.v
    src/kohakuaccel/noc/ctrl/noc_orchestrator.v src/kohakuaccel/noc/endpoint/noc_l2_adapter.v
    src/kohakuaccel/noc/endpoint/noc_local_cdc.v
    src/kohakutpu/matmul/mx_mac.v src/kohakutpu/matmul/mx_tcu.v
    src/kohakutpu/matmul/mx_fpacc.v src/kohakutpu/matmul/mx_acu_fp.v
    src/kohakutpu/matmul/mx_cluster_core.v src/kohakutpu/matmul/mx_cluster_mgr.v
    src/kohakutpu/matmul/mx_cluster_node.v src/kohakutpu/matmul/mx_cluster_cu.v
    src/kohakuaccel/mas/mover/mx_tdesc.v
    src/kohakutpu/matmul/mx_acu_fp_pump.v src/kohakutpu/matmul/mx_cluster_mgr_pump.v
    src/kohakutpu/matmul/mx_cluster_node_pump.v src/kohakutpu/matmul/mx_cluster_cu_pump.v
    src/kohakutpu/vector/vec_dsp.v src/kohakutpu/vector/vec_delay.v
    src/kohakutpu/vector/vec_tables.v src/kohakutpu/vector/vec_alu.v
    src/kohakutpu/vector/vec_cvt.v src/kohakutpu/vector/vec_regfile.v
    src/kohakutpu/vector/vec_lanes.v src/kohakutpu/vector/vec_agu.v
    src/kohakutpu/vector/vec_core.v src/kohakutpu/vector/vec_cu.v
    src/kohakutpu/transform/mx_quant.v src/kohakuaccel/mas/core/mag_mem_port.v
    src/kohakuaccel/mas/mover/mm_prng.v src/kohakuaccel/mas/mover/mm_mover.v
    src/kohakuaccel/mas/interlink/il_pkt_arb.v src/kohakuaccel/mas/interlink/mag_link.v
    src/kohakuaccel/mas/interlink/mag_link_cdc.v src/kohakuaccel/mas/interlink/mag_link_pipe.v
    src/kohakuaccel/mas/interlink/mag_switch.v src/kohakuaccel/mas/interlink/mag_ilink.v src/kohakuaccel/mas/core/mag.v
    src/kohakuaccel/mas/core/mag_dram_port.v src/kohakuaccel/mas/core/mag_stage.v
    src/kohakuaccel/mas/core/mag_stage_port.v src/kohakutpu/top/mag_1m.v
    src/kohakuaccel/common/clk/ktpu_div2.v src/kohakuaccel/common/clk/ktpu_pumpclk.v
    src/kohakuaccel/common/sb_skid.v src/kohakuaccel/axi/station/sb_hub.v
    src/kohakuaccel/axi/station/sb_nmu.v src/kohakuaccel/axi/station/sb_nsu.v
    src/kohakuaccel/axi/link/sb_link.v src/kohakuaccel/axi/link/sb_link_cdc.v
    src/kohakuaccel/axi/topo/sb_stn_line.v src/kohakuaccel/axi/topo/sb_line4.v
    src/kohakuaccel/axi/bd/sb_v6_bus.v
}
# Imported, not referenced: each probe then owns a frozen copy of the RTL, so
# concurrent runs share no source and an edit mid-run cannot change a result.
set srcs {}
foreach f $SRC { lappend srcs $root/$f }
if {[file exists $root/src/kohakutpu/top/generated/$mod.v]} {
    lappend srcs $root/src/kohakutpu/top/generated/$mod.v
} else {
    lappend srcs $root/src/kohakutpu/top/$mod.v
}
import_files -norecurse -fileset sources_1 $srcs
update_compile_order -fileset sources_1

create_bd_design $design_name
current_bd_design $design_name

# ---- clocking ------------------------------------------------------------
set sysp [create_bd_intf_port -mode Slave -vlnv xilinx.com:interface:diff_clock_rtl:1.0 system_clk]
set_property CONFIG.FREQ_HZ {100000000} $sysp
set udbs [create_bd_cell -type ip -vlnv xilinx.com:ip:util_ds_buf util_ds_buf_sys]
set_property CONFIG.C_BUF_TYPE {IBUFDS} $udbs
connect_bd_intf_net $sysp [get_bd_intf_pins util_ds_buf_sys/CLK_IN_D]
set udbg [create_bd_cell -type ip -vlnv xilinx.com:ip:util_ds_buf util_ds_buf_bufg]
set_property CONFIG.C_BUF_TYPE {BUFG} $udbg
connect_bd_net [get_bd_pins util_ds_buf_sys/IBUF_OUT] [get_bd_pins util_ds_buf_bufg/BUFG_I]

proc wiz {name mhz {mhz2 0}} {
    set w [create_bd_cell -type ip -vlnv xilinx.com:ip:clk_wiz $name]
    set cfg [list CONFIG.PRIM_SOURCE {No_buffer} CONFIG.PRIM_IN_FREQ {100.000} \
        CONFIG.CLKOUT1_REQUESTED_OUT_FREQ [format %.3f $mhz] \
        CONFIG.CLKOUT1_DRIVES {Buffer} CONFIG.PRIMITIVE {MMCM} \
        CONFIG.USE_RESET {false} CONFIG.FEEDBACK_SOURCE {FDBK_AUTO}]
    if {$mhz2 > 0} {
        lappend cfg CONFIG.CLKOUT2_USED {true} \
                    CONFIG.CLKOUT2_REQUESTED_OUT_FREQ [format %.3f $mhz2] \
                    CONFIG.CLKOUT2_DRIVES {Buffer}
    }
    set_property -dict $cfg $w
    connect_bd_net [get_bd_pins util_ds_buf_bufg/BUFG_O] [get_bd_pins $name/clk_in1]
}
proc psr {name clkpin} {
    create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset $name
    connect_bd_net [get_bd_pins $clkpin] [get_bd_pins $name/slowest_sync_clk]
}

wiz clk_wiz_ctrl $CTRL_MHZ $XDMA_MHZ
foreach slr {0 1 2 3} {
    wiz clk_wiz_bus$slr $BUS_MHZ
    # The die under test gets clk_out2 at 2x for the pumped matmul domain.
    set mhz [dict get $SLR_MHZ $slr]
    wiz clk_wiz_s$slr $mhz [expr {$slr == $tslr ? 2.0 * $mhz : 0}]
    psr rst_bus$slr clk_wiz_bus$slr/clk_out1
    psr rst_s$slr   clk_wiz_s$slr/clk_out1
}
psr rst_ctrl clk_wiz_ctrl/clk_out1
psr rst_xdma clk_wiz_ctrl/clk_out2
foreach w {ctrl xdma} {
    connect_bd_net [get_bd_pins clk_wiz_ctrl/locked] [get_bd_pins rst_$w/ext_reset_in]
}
foreach slr {0 1 2 3} {
    connect_bd_net [get_bd_pins clk_wiz_bus$slr/locked] [get_bd_pins rst_bus$slr/ext_reset_in]
    connect_bd_net [get_bd_pins clk_wiz_s$slr/locked]   [get_bd_pins rst_s$slr/ext_reset_in]
}

# rst_s$tslr STAYS on clk_out1: it drives the pair's CLR, and a reset clocked by
# the clock its own CLR stops can never release.
set pumped [string match {*_pump} $mod]
if {$pumped} {
    create_bd_cell -type module -reference ktpu_pumpclk pump_mesh
    connect_bd_net [get_bd_pins clk_wiz_s$tslr/clk_out2] [get_bd_pins pump_mesh/clk_in]
    set inv [create_bd_cell -type ip -vlnv xilinx.com:ip:util_vector_logic dclr_mesh]
    set_property -dict [list CONFIG.C_OPERATION {not} CONFIG.C_SIZE {1}] $inv
    connect_bd_net [get_bd_pins rst_s$tslr/peripheral_aresetn] [get_bd_pins dclr_mesh/Op1]
    connect_bd_net [get_bd_pins dclr_mesh/Res] [get_bd_pins pump_mesh/clr]
}
# The NoC and the L2 adapters. The matmul's own 1x comes off pump_mesh, never
# off this -- collapsing them costs clock c its independence.
proc dclk {slr} {
    return "clk_wiz_s${slr}/clk_out1"
}

# The MIG comes first: its ui_clk IS the DDR domain, for the mesh's DRAM port,
# the station's port 2 and the controller's own AXI-Lite.
set d [create_bd_cell -type ip -vlnv xilinx.com:ip:ddr4 ddr4_$ddr]
set_property -dict [list CONFIG.C0.DDR4_DataWidth {72} \
    CONFIG.C0.DDR4_InputClockPeriod {2499} \
    CONFIG.C0.DDR4_MemoryPart {MT40A512M16HA-083E} \
    CONFIG.C0.DDR4_TimePeriod {833} CONFIG.C0.DDR4_isCustom {true}] $d
create_bd_intf_port -mode Master -vlnv xilinx.com:interface:ddr4_rtl:1.0 c${ddr}_ddr4
set csys [create_bd_intf_port -mode Slave \
              -vlnv xilinx.com:interface:diff_clock_rtl:1.0 c${ddr}_sys]
# DDR4_InputClockPeriod 2499 ps: the IP rejects anything outside 399.4-401.0 MHz.
set_property CONFIG.FREQ_HZ {400160000} $csys
connect_bd_intf_net [get_bd_intf_ports c${ddr}_ddr4] [get_bd_intf_pins ddr4_$ddr/C0_DDR4]
connect_bd_intf_net [get_bd_intf_ports c${ddr}_sys]  [get_bd_intf_pins ddr4_$ddr/C0_SYS_CLK]
create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset rst_ddr
connect_bd_net [get_bd_pins ddr4_$ddr/c0_ddr4_ui_clk] [get_bd_pins rst_ddr/slowest_sync_clk]
connect_bd_net [get_bd_pins ddr4_$ddr/c0_ddr4_ui_clk_sync_rst] [get_bd_pins rst_ddr/ext_reset_in]
connect_bd_net [get_bd_pins rst_ddr/peripheral_aresetn] [get_bd_pins ddr4_$ddr/c0_ddr4_aresetn]
set sysrstp [create_bd_port -dir I -type rst sys_rst]
set_property CONFIG.POLARITY ACTIVE_HIGH $sysrstp
connect_bd_net $sysrstp [get_bd_pins ddr4_$ddr/sys_rst]

# ---- the station-bus line ------------------------------------------------
set bus [create_bd_cell -type module -reference sb_bd_line4 station_bus]
set_property -dict [list CONFIG.FW 256 CONFIG.OST 4 CONFIG.STORE_FWD 1 \
    CONFIG.LUT_PER_BRAM 0 CONFIG.TIMEOUT 0 CONFIG.LINK_CDC 1 \
    CONFIG.LINK_FULL 0 CONFIG.CRED 16 CONFIG.PIPE 4] $bus
foreach slr {0 1 2 3} {
    connect_bd_net [get_bd_pins clk_wiz_bus$slr/clk_out1] [get_bd_pins station_bus/bus_clk$slr]
    connect_bd_net [get_bd_pins rst_bus$slr/peripheral_reset] [get_bd_pins station_bus/bus_rst$slr]
    connect_bd_net [get_bd_pins [dclk $slr]] [get_bd_pins station_bus/clk_s$slr]
    connect_bd_net [get_bd_pins rst_s$slr/peripheral_aresetn] [get_bd_pins station_bus/aresetn_s$slr]
}
connect_bd_net [get_bd_pins clk_wiz_ctrl/clk_out1] [get_bd_pins station_bus/clk_ctrl]
connect_bd_net [get_bd_pins rst_ctrl/peripheral_aresetn] [get_bd_pins station_bus/aresetn_ctrl]
# clk_xdma is wired in the master section: with XDMA present its M_AXI runs on
# xdma_0/axi_aclk, and using clk_wiz_ctrl there fails BD 41-237 on CLK_DOMAIN.
# Per-station clk_ddr0..3 (the scalar clk_ddr is retired). Only the target die
# has a MIG here; the others take their own mesh clock as the port-2 domain.
foreach slr {0 1 2 3} {
    if {$slr == $tslr} {
        connect_bd_net [get_bd_pins ddr4_$ddr/c0_ddr4_ui_clk] \
                       [get_bd_pins station_bus/clk_ddr$slr]
        connect_bd_net [get_bd_pins rst_ddr/peripheral_aresetn] \
                       [get_bd_pins station_bus/aresetn_ddr$slr]
    } else {
        connect_bd_net [get_bd_pins [dclk $slr]] \
                       [get_bd_pins station_bus/clk_ddr$slr]
        connect_bd_net [get_bd_pins rst_s$slr/peripheral_aresetn] \
                       [get_bd_pins station_bus/aresetn_ddr$slr]
    }
}

# ---- masters -------------------------------------------------------------
# proto 0 = AXI4, 2 = AXI4LITE (S02 is Lite since the B6 pin work).
proc jtag_master {name clkpin rstpin {proto 0} {aw 64}} {
    set j [create_bd_cell -type ip -vlnv xilinx.com:ip:jtag_axi $name]
    set_property -dict [list CONFIG.PROTOCOL $proto CONFIG.M_AXI_ID_WIDTH {4} \
                             CONFIG.M_AXI_ADDR_WIDTH $aw] $j
    connect_bd_net [get_bd_pins $clkpin] [get_bd_pins $name/aclk]
    connect_bd_net [get_bd_pins $rstpin] [get_bd_pins $name/aresetn]
}
jtag_master jtag_ctrl clk_wiz_ctrl/clk_out1 rst_ctrl/peripheral_aresetn
connect_bd_intf_net [get_bd_intf_pins jtag_ctrl/M_AXI] [get_bd_intf_pins station_bus/S00_AXI]
# sb_line4 puts every manager but i==0 on clk_xdma, so S02 shares S01's domain.

if {$xdma_on} {
    create_bd_intf_port -mode Slave  -vlnv xilinx.com:interface:diff_clock_rtl:1.0 pcie_clk
    create_bd_intf_port -mode Master -vlnv xilinx.com:interface:pcie_7x_mgt_rtl:1.0 pcie_lane
    set pr [create_bd_port -dir I -type rst pcie_reset]
    set_property CONFIG.POLARITY ACTIVE_LOW $pr
    set ub [create_bd_cell -type ip -vlnv xilinx.com:ip:util_ds_buf util_ds_buf_pcie]
    set_property CONFIG.C_BUF_TYPE {IBUFDSGTE} $ub
    connect_bd_intf_net [get_bd_intf_ports pcie_clk] [get_bd_intf_pins util_ds_buf_pcie/CLK_IN_D]
    set x [create_bd_cell -type ip -vlnv xilinx.com:ip:xdma xdma_0]
    set_property -dict [list CONFIG.mode_selection {Advanced} \
      CONFIG.pl_link_cap_max_link_speed {8.0_GT/s} \
      CONFIG.pl_link_cap_max_link_width {X16} CONFIG.ref_clk_freq {100_MHz} \
      CONFIG.pcie_blk_locn {X0Y1} CONFIG.pf0_device_id {903F} \
      CONFIG.axisten_freq {250} CONFIG.axi_data_width {512_bit} \
      CONFIG.axi_id_width {4} CONFIG.xdma_num_usr_irq {1} \
      CONFIG.xdma_rnum_chnl {1} CONFIG.xdma_wnum_chnl {1}] $x
    connect_bd_intf_net [get_bd_intf_ports pcie_lane] [get_bd_intf_pins xdma_0/pcie_mgt]
    connect_bd_net [get_bd_ports pcie_reset] [get_bd_pins xdma_0/sys_rst_n]
    connect_bd_net [get_bd_pins util_ds_buf_pcie/IBUF_DS_ODIV2] [get_bd_pins xdma_0/sys_clk]
    connect_bd_net [get_bd_pins util_ds_buf_pcie/IBUF_OUT]      [get_bd_pins xdma_0/sys_clk_gt]
    connect_bd_intf_net [get_bd_intf_pins xdma_0/M_AXI] [get_bd_intf_pins station_bus/S01_AXI]
    set xclk xdma_0/axi_aclk
    set xrst xdma_0/axi_aresetn
} else {
    set xclk clk_wiz_ctrl/clk_out2
    set xrst rst_xdma/peripheral_aresetn
    jtag_master jtag_wide clk_wiz_ctrl/clk_out2 rst_xdma/peripheral_aresetn
    set dwc [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_dwidth_converter dwc_wide]
    set_property -dict [list CONFIG.SI_DATA_WIDTH {32} CONFIG.MI_DATA_WIDTH {512}] $dwc
    connect_bd_net [get_bd_pins clk_wiz_ctrl/clk_out2] [get_bd_pins dwc_wide/s_axi_aclk]
    connect_bd_net [get_bd_pins rst_xdma/peripheral_aresetn] [get_bd_pins dwc_wide/s_axi_aresetn]
    connect_bd_intf_net [get_bd_intf_pins jtag_wide/M_AXI] [get_bd_intf_pins dwc_wide/S_AXI]
    connect_bd_intf_net [get_bd_intf_pins dwc_wide/M_AXI]  [get_bd_intf_pins station_bus/S01_AXI]
}
connect_bd_net [get_bd_pins $xclk] [get_bd_pins station_bus/clk_xdma]
connect_bd_net [get_bd_pins $xrst] [get_bd_pins station_bus/aresetn_xdma]
jtag_master jtag_lite $xclk $xrst 2 32
connect_bd_intf_net [get_bd_intf_pins jtag_lite/M_AXI] [get_bd_intf_pins station_bus/S02_AXI]

# ---- the mesh under test and its DDR -------------------------------------
set m [create_bd_cell -type module -reference $mod mesh_u]
set_property -dict [list CONFIG.MESH_ID 0 CONFIG.GA {512} CONFIG.GB {512} \
    CONFIG.TILES {4096} CONFIG.TILE_PRIM {ultra} CONFIG.VEC_PRIM {block} \
    CONFIG.L2_CU_DEPTH $l2d CONFIG.L2_VEC_DEPTH $l2d \
    CONFIG.L2_MAG_ENTRIES $magent CONFIG.L2_MAG_BANKS $magbanks] $m
connect_bd_net [get_bd_pins [dclk $tslr]] [get_bd_pins mesh_u/axi_aclk]
connect_bd_net [get_bd_pins rst_s$tslr/peripheral_aresetn] [get_bd_pins mesh_u/axi_aresetn]
connect_bd_net [get_bd_pins ddr4_$ddr/c0_ddr4_ui_clk] [get_bd_pins mesh_u/dram_aclk]
connect_bd_net [get_bd_pins rst_ddr/peripheral_aresetn] [get_bd_pins mesh_u/dram_aresetn]
connect_bd_net [get_bd_pins [dclk $tslr]] [get_bd_pins mesh_u/vec_clk]
# Same net as MAG for now. MAG_CDC's value is the registered FIFO flag, which
# breaks enc_in_busy -> router flow control whether or not the rates differ.
connect_bd_net [get_bd_pins [dclk $tslr]] [get_bd_pins mesh_u/noc_clk]
if {[llength [get_bd_pins -quiet mesh_u/mat_clk2x]]} {
    if {!$pumped} { error "mesh_u has mat_clk2x but $mod is not a _pump top" }
    connect_bd_net [get_bd_pins pump_mesh/clk2x] [get_bd_pins mesh_u/mat_clk2x]
    connect_bd_net [get_bd_pins pump_mesh/clk1x] [get_bd_pins mesh_u/mat_clk]
} else {
    connect_bd_net [get_bd_pins [dclk $tslr]] [get_bd_pins mesh_u/mat_clk]
}

connect_bd_intf_net [get_bd_intf_pins mesh_u/M_AXI_DRAM] \
                    [get_bd_intf_pins ddr4_$ddr/C0_DDR4_S_AXI]

# The interlink stays UNCONNECTED, as the chain's two ends do in the full
# design. Looping it back would make the mesh receive its own packets.
foreach lk {M_AXIS_LINK0 M_AXIS_LINK1 S_AXIS_LINK0 S_AXIS_LINK1} {
    puts "@@@ interlink $lk left unconnected (no neighbour mesh)"
}

# ---- endpoints -----------------------------------------------------------
# Mesh takes ports 0/1 of its station, DDR control port 2; rest is block RAM.
proc bram_ep {name dw clkpin rstpin {lite 0}} {
    set c [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_bram_ctrl $name]
    set_property -dict [list CONFIG.DATA_WIDTH $dw CONFIG.SINGLE_PORT_BRAM {1} \
                             CONFIG.ECC_TYPE {0}] $c
    # Station ports 2/3 are AXI4LITE since the B6 pin work.
    if {$lite} { set_property CONFIG.PROTOCOL {AXI4LITE} $c }
    set mm [create_bd_cell -type ip -vlnv xilinx.com:ip:blk_mem_gen ${name}_mem]
    set_property -dict [list CONFIG.Memory_Type {Single_Port_RAM} \
                             CONFIG.use_bram_block {BRAM_Controller}] $mm
    connect_bd_intf_net [get_bd_intf_pins $name/BRAM_PORTA] \
                        [get_bd_intf_pins ${name}_mem/BRAM_PORTA]
    connect_bd_net [get_bd_pins $clkpin] [get_bd_pins $name/s_axi_aclk]
    connect_bd_net [get_bd_pins $rstpin] [get_bd_pins $name/s_axi_aresetn]
}
for {set slr 0} {$slr < 4} {incr slr} {
    for {set p 0} {$p < $NQ} {incr p} {
        set k [expr {$slr * $NQ + $p}]
        set mp [format M%02d_AXI $k]
        # NO register slice here: m62_c3 added one and routed -1.631 against
        # m62_c2's -0.858, despite gating better (-0.179 vs -0.354).
        if {$slr == $tslr && $p == 0} {
            connect_bd_intf_net [get_bd_intf_pins station_bus/$mp] \
                                [get_bd_intf_pins mesh_u/S_AXI_MEM]
            continue
        }
        if {$slr == $tslr && $p == 1} {
            # S_AXI_CTRL is 64-bit; gen_station_wrap only emits 32 or FW, so the
            # station cannot meet it natively and v6 needs this converter too.
            set dc [create_bd_cell -type ip \
                        -vlnv xilinx.com:ip:axi_dwidth_converter dwc_ctrl]
            set_property -dict [list CONFIG.SI_DATA_WIDTH {32} \
                                     CONFIG.MI_DATA_WIDTH {64}] $dc
            connect_bd_net [get_bd_pins [dclk $slr]] \
                           [get_bd_pins dwc_ctrl/s_axi_aclk]
            connect_bd_net [get_bd_pins rst_s$slr/peripheral_aresetn] \
                           [get_bd_pins dwc_ctrl/s_axi_aresetn]
            connect_bd_intf_net [get_bd_intf_pins station_bus/$mp] \
                                [get_bd_intf_pins dwc_ctrl/S_AXI]
            connect_bd_intf_net [get_bd_intf_pins dwc_ctrl/M_AXI] \
                                [get_bd_intf_pins mesh_u/S_AXI_CTRL]
            continue
        }
        if {$slr == $tslr && $p == 2} {
            # AXI4-LITE with no ID against the station's AXI4 with 4 bits of it.
            # axi_protocol_converter keeps the ID and fails BD 41-237.
            set pc [create_bd_cell -type ip \
                        -vlnv xilinx.com:ip:smartconnect pc_ddrctl]
            set_property -dict [list CONFIG.NUM_SI {1} CONFIG.NUM_MI {1} \
                                     CONFIG.NUM_CLKS {1}] $pc
            connect_bd_net [get_bd_pins ddr4_$ddr/c0_ddr4_ui_clk] \
                           [get_bd_pins pc_ddrctl/aclk]
            connect_bd_net [get_bd_pins rst_ddr/peripheral_aresetn] \
                           [get_bd_pins pc_ddrctl/aresetn]
            connect_bd_intf_net [get_bd_intf_pins station_bus/$mp] \
                                [get_bd_intf_pins pc_ddrctl/S00_AXI]
            connect_bd_intf_net [get_bd_intf_pins pc_ddrctl/M00_AXI] \
                                [get_bd_intf_pins ddr4_$ddr/C0_DDR4_S_AXI_CTRL]
            continue
        }
        if {$p < 2} {
            set ck [dclk $slr] ; set rk rst_s$slr/peripheral_aresetn
        } elseif {$p == 2} {
            # Must match that station's clk_ddr$slr wiring above.
            if {$slr == $tslr} {
                set ck ddr4_$ddr/c0_ddr4_ui_clk ; set rk rst_ddr/peripheral_aresetn
            } else {
                set ck [dclk $slr] ; set rk rst_s$slr/peripheral_aresetn
            }
        } else {
            set ck clk_wiz_ctrl/clk_out1  ; set rk rst_ctrl/peripheral_aresetn
        }
        bram_ep ep$k [expr {$p == 0 ? 256 : 32}] $ck $rk [expr {$p >= 2}]
        connect_bd_intf_net [get_bd_intf_pins station_bus/$mp] \
                            [get_bd_intf_pins ep$k/S_AXI]
    }
}

# ---- address map ---------------------------------------------------------
assign_bd_address -force
validate_bd_design
save_bd_design
make_wrapper -files [get_files ${design_name}.bd] -top -import -force

# ---- floorplan -----------------------------------------------------------
set xdc $proj_dir/pblocks.xdc
set fh [open $xdc w]
foreach slr {0 1 2 3} {
    lassign [dict get $SLR_ROWS $slr] ylo yhi
    puts $fh "create_pblock pb_slr$slr"
    puts $fh "resize_pblock \[get_pblocks pb_slr$slr\] -add \{CLOCKREGION_X0${ylo}:CLOCKREGION_X7${yhi}\}"
    puts $fh "add_cells_to_pblock \[get_pblocks pb_slr$slr\] \[get_cells -quiet \{${design_name}_i/station_bus/inst/u_line/g_stn\[$slr\].*\}\]"
    puts $fh "add_cells_to_pblock \[get_pblocks pb_slr$slr\] \[get_cells -quiet \{${design_name}_i/clk_wiz_s$slr\}\]"
    puts $fh "add_cells_to_pblock \[get_pblocks pb_slr$slr\] \[get_cells -quiet \{${design_name}_i/clk_wiz_bus$slr\}\]"
    for {set p 0} {$p < $NQ} {incr p} {
        set k [expr {$slr * $NQ + $p}]
        puts $fh "add_cells_to_pblock \[get_pblocks pb_slr$slr\] \[get_cells -quiet \{${design_name}_i/ep$k\}\]"
        puts $fh "add_cells_to_pblock \[get_pblocks pb_slr$slr\] \[get_cells -quiet \{${design_name}_i/ep${k}_mem\}\]"
    }
    puts $fh "set_property CONTAIN_ROUTING false \[get_pblocks pb_slr$slr\]"
}
puts $fh "add_cells_to_pblock \[get_pblocks pb_slr$tslr\] \[get_cells -quiet \{${design_name}_i/mesh_u\}\]"
puts $fh "add_cells_to_pblock \[get_pblocks pb_slr$tslr\] \[get_cells -quiet \{${design_name}_i/ddr4_$ddr\}\]"
puts $fh "add_cells_to_pblock \[get_pblocks pb_slr$tslr\] \[get_cells -quiet \{${design_name}_i/rst_ddr\}\]"
puts $fh "add_cells_to_pblock \[get_pblocks pb_slr$tslr\] \[get_cells -quiet \{${design_name}_i/pump_mesh\}\]"
puts $fh "add_cells_to_pblock \[get_pblocks pb_slr$tslr\] \[get_cells -quiet \{${design_name}_i/dclr_mesh\}\]"
puts $fh "add_cells_to_pblock \[get_pblocks pb_slr$tslr\] \[get_cells -quiet \{${design_name}_i/pc_ddrctl\}\]"
puts $fh "add_cells_to_pblock \[get_pblocks pb_slr$tslr\] \[get_cells -quiet \{${design_name}_i/dwc_ctrl\}\]"
foreach c {jtag_ctrl jtag_lite jtag_wide dwc_wide xdma_0 clk_wiz_ctrl rst_ctrl \
           rst_xdma util_ds_buf_bufg util_ds_buf_sys util_ds_buf_pcie} {
    puts $fh "add_cells_to_pblock \[get_pblocks pb_slr1\] \[get_cells -quiet \{${design_name}_i/$c\}\]"
}
close $fh
add_files -fileset constrs_1 -norecurse $xdc

# A GT reference clock has no legal auto-placement, and every XDMA clock is
# generated from it -- so this has to precede the clock groups below.
if {$xdma_on} {
    import_files -fileset constrs_1 -norecurse [board_xdc pcie.xdc]
}

set clk $proj_dir/clocks.xdc
set fh [open $clk w]
puts $fh "create_clock -name sys_clk -period 10.000 \[get_ports system_clk_clk_p\]"
puts $fh "set_property CLOCK_DEDICATED_ROUTE ANY_CMT_COLUMN \[get_nets -of \[get_pins ${design_name}_i/clk_wiz_ctrl/clk_in1\]\]"
# The matmul pair is its OWN group: every endpoint now crosses into the NoC
# through an async FIFO, and one group with clk_out1 times what the CDC breaks.
set groups {}
lappend groups [list clk_wiz_ctrl {clk_out1 clk_out2}]
foreach w {clk_wiz_bus0 clk_wiz_bus1 clk_wiz_bus2 clk_wiz_bus3 \
           clk_wiz_s0 clk_wiz_s1 clk_wiz_s2 clk_wiz_s3} {
    lappend groups [list $w {clk_out1}]
}
lappend groups [list clk_wiz_s$tslr {clk_out2}]
set grp {}
foreach g $groups {
    lassign $g w outs
    set pins {}
    foreach o $outs { lappend pins "${design_name}_i/$w/$o" }
    # No `if` guard: XDC rejects it, so the ten emitted here never ran.
    # synth_only.tcl asserts in real Tcl instead.
    append grp " \\\n    -group \[get_clocks -include_generated_clocks -of_objects \[get_pins \{$pins\}\]\]"
}
if {$xdma_on} {
    append grp " \\\n    -group \[get_clocks -include_generated_clocks pcie_refclk\]"
}
# mag_dram_port crosses to the MIG through five async_fifos. By PIN, not name:
# `get_clocks c3_sys_clk_p` matched nothing and an empty -group is a no-op.
append grp " \\\n    -group \[get_clocks -include_generated_clocks -of_objects\
 \[get_pins ${design_name}_i/ddr4_${ddr}/c0_ddr4_ui_clk\]\]"
puts $fh "set_clock_groups -asynchronous$grp"
close $fh
add_files -fileset constrs_1 -norecurse $clk

import_files -fileset constrs_1 -norecurse [board_xdc ddr4_c${ddr}.xdc]

update_compile_order -fileset sources_1
# Auto-top re-runs here and picks the bare mesh: place_design then dies on
# 3,610 I/O, one full synthesis later.
set_property top ${design_name}_wrapper [current_fileset]
set_property top_auto_set 0 [current_fileset]
if {[get_property top [current_fileset]] ne "${design_name}_wrapper"} {
    error "top is [get_property top [current_fileset]], not the BD wrapper"
}
puts "@@@ probe $tag: mesh $mod slr $tslr ddr $ddr l2 $l2d mag ${maguram}U\
 (${magbanks}x4096) xdma $xdma_on at $proj_dir"
if {!$do_impl} { puts "@@@ built only" ; exit 0 }

if {$strat ne ""} {
    set_property strategy $strat [get_runs impl_1]
    puts "@@@ probe $tag impl strategy $strat"
}

launch_runs synth_1 -jobs 4
wait_on_run synth_1
if {[get_property PROGRESS [get_runs synth_1]] != "100%"} {
    error "synth failed: $proj_dir/${design_name}.runs/synth_1/runme.log"
}
launch_runs impl_1 -to_step route_design -jobs 4
wait_on_run impl_1
puts "@@@ probe $tag impl [get_property PROGRESS [get_runs impl_1]]"
puts "@@@ probe $tag done"
